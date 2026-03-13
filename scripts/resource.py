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


def collect_impacted_resources(
    manifest: Dict[str, Any],
    session=None,
    region: Optional[str] = None,
) -> List[Dict[str, str]]:
    if session is None:
        session = boto3.Session(region_name=region)

    manifest_region = region or manifest.get("region")
    if not manifest_region:
        raise ValueError("manifest.yml must include top-level region")

    rtype = (manifest.get("resilience_test_type") or "").strip().lower()
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
        tags = parse_tags(svc.get("tags"))
        selection_mode = _selection_mode_label(svc.get("instance_count"))
        service_label = _service_label(name, action)

        arns: List[str] = []

        if name == "ec2" and action in ("stop", "reboot", "terminate"):
            arns = _collect_ec2_instances(session, manifest_region, zone, tags)
            arns = _apply_count_selection(arns, svc.get("instance_count"))

        elif name == "network" and action == "disrupt-connectivity":
            arns = _collect_subnets(session, manifest_region, zone, tags)

        elif name == "rds" and action == "reboot":
            arns = _collect_rds_instances(session, manifest_region, zone, tags)

        elif name == "rds" and action == "failover":
            arns = _collect_rds_clusters(session, manifest_region, zone, tags)

        elif name == "asg" and action == "pause-launch":
            arns = _collect_asgs(session, manifest_region, zone, tags)

        elif name == "ec2" and action == "pause-launch":
            arns = _collect_iam_roles_from_service(svc)

        for arn in arns:
            out.append(
                {
                    "service": service_label,
                    "arn": arn,
                    "selection_mode": selection_mode,
                }
            )

    return out