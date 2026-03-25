#!/usr/bin/env python3
import boto3
from typing import Any, Dict, List, Optional

from utility import get_account_id, normalize_service_name, parse_tags, resolve_iam_role_arns_from_names


def _tags_match(expected: Dict[str, str], actual: Dict[str, str]) -> bool:
    if not expected:
        return True
    return all(actual.get(k) == v for k, v in expected.items())


def _selection_mode_label(instance_count: Optional[Any]) -> str:
    if instance_count is not None:
        try:
            n = int(instance_count)
            return f"Count ({n})"
        except Exception:
            pass
    return "ALL"


def _service_label(name: str, action: str) -> str:
    return f"{name}:{action}"


def _apply_count_selection(arns: List[str], instance_count: Optional[Any]) -> List[str]:
    if instance_count is None:
        return arns
    try:
        n = int(instance_count)
    except Exception:
        return arns
    if n <= 0:
        return []
    # Deterministic "final hit" approximation: sort then take first N
    arns_sorted = sorted(arns)
    return arns_sorted[:n]


def _collect_ec2_instances(session, region: str, zone: Optional[str], tags: Dict[str, str]) -> List[str]:
    ec2 = session.client("ec2", region_name=region)

    filters = []
    for k, v in tags.items():
        filters.append({"Name": f"tag:{k}", "Values": [v]})
    if zone:
        filters.append({"Name": "availability-zone", "Values": [zone]})

    arns: List[str] = []
    sts = session.client("sts")
    account_id = get_account_id(sts)

    paginator = ec2.get_paginator("describe_instances")
    kwargs = {"Filters": filters} if filters else {}
    for page in paginator.paginate(**kwargs):
        for reservation in page.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                instance_id = inst.get("InstanceId")
                if instance_id:
                    arns.append(f"arn:aws:ec2:{region}:{account_id}:instance/{instance_id}")
    return arns


def _collect_subnets(session, region: str, zone: Optional[str], tags: Dict[str, str]) -> List[str]:
    ec2 = session.client("ec2", region_name=region)

    filters = []
    for k, v in tags.items():
        filters.append({"Name": f"tag:{k}", "Values": [v]})
    if zone:
        filters.append({"Name": "availability-zone", "Values": [zone]})

    arns: List[str] = []
    sts = session.client("sts")
    account_id = get_account_id(sts)

    paginator = ec2.get_paginator("describe_subnets")
    kwargs = {"Filters": filters} if filters else {}
    for page in paginator.paginate(**kwargs):
        for subnet in page.get("Subnets", []):
            subnet_id = subnet.get("SubnetId")
            if subnet_id:
                arns.append(f"arn:aws:ec2:{region}:{account_id}:subnet/{subnet_id}")
    return arns


def _collect_rds_instances(session, region: str, zone: Optional[str], tags: Dict[str, str]) -> List[str]:
    rds = session.client("rds", region_name=region)
    arns: List[str] = []

    paginator = rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for db in page.get("DBInstances", []):
            db_arn = db.get("DBInstanceArn")
            if not db_arn:
                continue

            if zone:
                az = db.get("AvailabilityZone")
                if az != zone:
                    continue

            actual_tags: Dict[str, str] = {}
            if tags:
                tag_resp = rds.list_tags_for_resource(ResourceName=db_arn)
                actual_tags = {t["Key"]: t.get("Value", "") for t in tag_resp.get("TagList", []) if "Key" in t}
                if not _tags_match(tags, actual_tags):
                    continue

            arns.append(db_arn)

    return arns


def _cluster_writer_az(session, region: str, cluster: Dict[str, Any]) -> Optional[str]:
    """
    Find the writer instance AZ for a DBCluster (best-effort, strict for site test).
    """
    try:
        members = cluster.get("DBClusterMembers") or []
        writer_id = None
        for m in members:
            if m.get("IsClusterWriter"):
                writer_id = m.get("DBInstanceIdentifier")
                break
        if not writer_id:
            return None

        rds = session.client("rds", region_name=region)
        resp = rds.describe_db_instances(DBInstanceIdentifier=writer_id)
        insts = resp.get("DBInstances") or []
        if not insts:
            return None
        return insts[0].get("AvailabilityZone")
    except Exception:
        return None


def _collect_rds_clusters(session, region: str, zone: Optional[str], tags: Dict[str, str]) -> List[str]:
    rds = session.client("rds", region_name=region)
    arns: List[str] = []

    paginator = rds.get_paginator("describe_db_clusters")
    for page in paginator.paginate():
        for db in page.get("DBClusters", []):
            db_arn = db.get("DBClusterArn")
            if not db_arn:
                continue

            # Strict for site test:
            # FIS scopes rds:cluster by writerAvailabilityZoneIdentifiers, so match writer AZ exactly.
            if zone:
                writer_az = _cluster_writer_az(session, region, db)
                if writer_az != zone:
                    continue

            actual_tags: Dict[str, str] = {}
            if tags:
                tag_resp = rds.list_tags_for_resource(ResourceName=db_arn)
                actual_tags = {t["Key"]: t.get("Value", "") for t in tag_resp.get("TagList", []) if "Key" in t}
                if not _tags_match(tags, actual_tags):
                    continue

            arns.append(db_arn)

    return arns


def _collect_asgs(session, region: str, zone: Optional[str], tags: Dict[str, str]) -> List[str]:
    asg = session.client("autoscaling", region_name=region)
    arns: List[str] = []

    paginator = asg.get_paginator("describe_auto_scaling_groups")
    for page in paginator.paginate():
        for g in page.get("AutoScalingGroups", []):
            if zone:
                azs = g.get("AvailabilityZones") or []
                if zone not in azs:
                    continue

            actual_tags = {t["Key"]: t.get("Value", "") for t in g.get("Tags", []) if "Key" in t}
            if not _tags_match(tags, actual_tags):
                continue

            arn = g.get("AutoScalingGroupARN")
            if arn:
                arns.append(arn)

    return arns


def _collect_iam_roles_from_service(svc: Dict[str, Any]) -> List[str]:
    iam_role_arns = svc.get("iam_role_arns")
    if isinstance(iam_role_arns, list) and iam_role_arns:
        return [str(x) for x in iam_role_arns if x]

    iam_roles = svc.get("iam_roles")
    if isinstance(iam_roles, str) and iam_roles.strip():
        return resolve_iam_role_arns_from_names(iam_roles)

    return resolve_iam_role_arns_from_names("BAU,Admin,scb-user-instance-role")


def _bucket_region_matches(actual_region: Optional[str], desired_region: Optional[str]) -> bool:
    if not desired_region:
        return True
    normalized = actual_region or "us-east-1"
    return normalized == desired_region


def _collect_s3_buckets(session, region: str, tags: Dict[str, str]) -> List[str]:
    s3 = session.client("s3", region_name=region)
    arns: List[str] = []

    resp = s3.list_buckets()
    for bucket in resp.get("Buckets", []):
        bucket_name = bucket.get("Name")
        if not bucket_name:
            continue

        try:
            loc = s3.get_bucket_location(Bucket=bucket_name)
            bucket_region = loc.get("LocationConstraint") or "us-east-1"
        except Exception:
            continue

        if not _bucket_region_matches(bucket_region, region):
            continue

        actual_tags: Dict[str, str] = {}
        if tags:
            try:
                tag_resp = s3.get_bucket_tagging(Bucket=bucket_name)
                actual_tags = {t["Key"]: t.get("Value", "") for t in tag_resp.get("TagSet", []) if "Key" in t}
            except Exception:
                continue
            if not _tags_match(tags, actual_tags):
                continue

        arns.append(f"arn:aws:s3:::{bucket_name}")

    return arns


def collect_service_resource_arns(
    svc: Dict[str, Any],
    *,
    session,
    region: str,
    zone: Optional[str] = None,
) -> List[str]:
    if not isinstance(svc, dict):
        return []

    name = normalize_service_name(svc.get("name"))
    action = (svc.get("action") or "").strip().lower()
    tags = parse_tags(svc.get("tags"))

    if name == "ec2" and action in ("stop", "reboot", "terminate"):
        arns = _collect_ec2_instances(session, region, zone, tags)
        return _apply_count_selection(arns, svc.get("instance_count"))

    if name == "network" and action == "disrupt-connectivity":
        return _collect_subnets(session, region, zone, tags)

    if name == "rds" and action == "reboot":
        return _collect_rds_instances(session, region, zone, tags)

    if name == "rds" and action == "failover":
        return _collect_rds_clusters(session, region, zone, tags)

    if name == "asg" and action in ("pause-launch", "scale"):
        return _collect_asgs(session, region, zone, tags)

    if name == "ec2" and action == "pause-launch":
        return _collect_iam_roles_from_service(svc)

    if name == "s3" and action in ("pause-replication", "pause-relication"):
        return _collect_s3_buckets(session, region, tags)

    return []


def _region_from_arn(arn: str) -> Optional[str]:
    parts = (arn or "").split(":")
    if len(parts) < 4:
        return None
    return parts[3] or None


def _resource_has_matching_tags(rds_client, resource_arn: str, tags: Dict[str, str]) -> bool:
    if not tags:
        return True
    try:
        tag_resp = rds_client.list_tags_for_resource(ResourceName=resource_arn)
    except Exception:
        return False
    actual_tags = {t["Key"]: t.get("Value", "") for t in tag_resp.get("TagList", []) if "Key" in t}
    return _tags_match(tags, actual_tags)


def discover_rds_global_clusters(
    manifest: Dict[str, Any],
    session=None,
) -> List[Dict[str, Any]]:
    if session is None:
        session = boto3.Session()

    primary_region = manifest.get("primary_region")
    secondary_region = manifest.get("secondary_region")
    if not primary_region or not secondary_region:
        raise ValueError("region resilience tests require top-level primary_region and secondary_region.")

    services = manifest.get("services") or []
    if not isinstance(services, list):
        return []

    primary_rds = session.client("rds", region_name=primary_region)
    region_clients = {
        primary_region: primary_rds,
        secondary_region: session.client("rds", region_name=secondary_region),
    }

    global_clusters_by_identifier: Dict[str, Dict[str, Any]] = {}
    paginator = primary_rds.get_paginator("describe_global_clusters")
    for page in paginator.paginate():
        for global_cluster in page.get("GlobalClusters", []):
            identifier = global_cluster.get("GlobalClusterIdentifier")
            if identifier:
                global_clusters_by_identifier[identifier] = global_cluster

    resolved: List[Dict[str, Any]] = []

    for svc in services:
        if not isinstance(svc, dict):
            continue

        name = normalize_service_name(svc.get("name"))
        action = (svc.get("action") or "").strip().lower()
        if name != "rds" or action not in ("failover-global-db", "switchover-global-db"):
            continue

        tags = parse_tags(svc.get("tags"))
        matches: List[Dict[str, Any]] = []

        for global_cluster in global_clusters_by_identifier.values():
            members = global_cluster.get("GlobalClusterMembers") or []
            member_arns_by_region: Dict[str, str] = {}
            extra_member_counts: Dict[str, int] = {}

            for member in members:
                cluster_arn = member.get("DBClusterArn")
                if not cluster_arn:
                    continue
                member_region = _region_from_arn(cluster_arn)
                if member_region not in (primary_region, secondary_region):
                    continue
                extra_member_counts[member_region] = extra_member_counts.get(member_region, 0) + 1
                if member_region in member_arns_by_region:
                    continue
                member_arns_by_region[member_region] = cluster_arn

            if primary_region not in member_arns_by_region or secondary_region not in member_arns_by_region:
                continue

            if extra_member_counts.get(primary_region, 0) > 1 or extra_member_counts.get(secondary_region, 0) > 1:
                raise ValueError(
                    "Aurora global database discovery found more than one member cluster in a configured Region. "
                    "Please narrow tags so exactly one cluster is selected per Region."
                )

            global_cluster_arn = global_cluster.get("GlobalClusterArn") or ""

            matches_global_tags = bool(global_cluster_arn) and _resource_has_matching_tags(primary_rds, global_cluster_arn, tags)
            matches_member_tags = all(
                _resource_has_matching_tags(region_clients[member_region], cluster_arn, tags)
                for member_region, cluster_arn in member_arns_by_region.items()
            )

            if tags and not (matches_global_tags or matches_member_tags):
                continue

            matches.append(
                {
                    "service": _service_label(name, action),
                    "action": action,
                    "from": str(svc.get("from") or "").strip().lower(),
                    "use_arc": bool(svc.get("use_arc", True)),
                    "selection_mode": "ALL",
                    "primary_region": primary_region,
                    "secondary_region": secondary_region,
                    "global_cluster_identifier": str(global_cluster.get("GlobalClusterIdentifier") or ""),
                    "global_cluster_arn": global_cluster_arn,
                    "member_cluster_arns": {
                        primary_region: member_arns_by_region[primary_region],
                        secondary_region: member_arns_by_region[secondary_region],
                    },
                    "tags": tags,
                }
            )

        if len(matches) == 0:
            raise ValueError(f"No Aurora global database matched tags for service action {name}:{action}.")
        if len(matches) > 1:
            raise ValueError(
                f"Multiple Aurora global databases matched tags for service action {name}:{action}. "
                "Please refine tags so exactly one global database is selected."
            )

        resolved.extend(matches)

    return resolved


def collect_impacted_resources(
    manifest: Dict[str, Any],
    session=None,
    region: Optional[str] = None,
) -> List[Dict[str, str]]:
    if session is None:
        session = boto3.Session(region_name=region)

    rtype = (manifest.get("resilience_test_type") or "").strip().lower()

    if rtype == "region":
        out: List[Dict[str, str]] = []
        for item in discover_rds_global_clusters(manifest=manifest, session=session):
            if item.get("global_cluster_arn"):
                out.append(
                    {
                        "service": str(item.get("service") or ""),
                        "arn": str(item.get("global_cluster_arn") or ""),
                        "selection_mode": str(item.get("selection_mode") or "ALL"),
                    }
                )
            for cluster_arn in (item.get("member_cluster_arns") or {}).values():
                out.append(
                    {
                        "service": str(item.get("service") or ""),
                        "arn": str(cluster_arn or ""),
                        "selection_mode": str(item.get("selection_mode") or "ALL"),
                    }
                )
        return out

    manifest_region = region or manifest.get("region")
    if not manifest_region:
        raise ValueError("manifest.yml must include top-level region")

    zone = manifest.get("zone") if rtype == "site" else None

    services = manifest.get("services") or []
    if not isinstance(services, list):
        return []

    out: List[Dict[str, str]] = []

    for svc in services:
        if not isinstance(svc, dict):
            continue

        name = normalize_service_name(svc.get("name"))
        action = (svc.get("action") or "").strip().lower()
        selection_mode = _selection_mode_label(svc.get("instance_count"))
        service_label = _service_label(name, action)
        arns = collect_service_resource_arns(
            svc,
            session=session,
            region=manifest_region,
            zone=zone,
        )

        for arn in arns:
            out.append(
                {
                    "service": service_label,
                    "arn": arn,
                    "selection_mode": selection_mode,
                }
            )

    return out
