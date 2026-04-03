import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from component_actions.base import CustomComponentAction
from resource import collect_service_resource_arns, discover_rds_global_clusters
from utility import (
    normalize_service_name,
    parse_tags,
    resolve_service_primary_region,
    resolve_service_region,
    resolve_service_secondary_region,
    resolve_service_zone,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_instance_identifier_from_arn(arn: str) -> str:
    marker = ":db:"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


def _db_cluster_identifier_from_arn(arn: str) -> str:
    marker = ":cluster:"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


def _cluster_writer_identifier(cluster: Dict[str, Any]) -> str:
    for member in cluster.get("DBClusterMembers") or []:
        if bool(member.get("IsClusterWriter")):
            return str(member.get("DBInstanceIdentifier") or "")
    return ""


def _describe_db_cluster(rds, cluster_identifier: str) -> Dict[str, Any]:
    resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_identifier)
    clusters = resp.get("DBClusters") or []
    if not clusters:
        raise ValueError(f"DB cluster not found: {cluster_identifier}")
    return clusters[0]


def _describe_db_instance(rds, instance_identifier: str) -> Dict[str, Any]:
    resp = rds.describe_db_instances(DBInstanceIdentifier=instance_identifier)
    instances = resp.get("DBInstances") or []
    if not instances:
        raise ValueError(f"DB instance not found: {instance_identifier}")
    return instances[0]


def _resolve_global_db_from_side(from_value: Any, primary_region: str, secondary_region: str) -> str:
    value = str(from_value or "").strip().lower()
    if value == "primary" or value == str(primary_region or "").strip().lower():
        return "primary"
    if value == "secondary" or value == str(secondary_region or "").strip().lower():
        return "secondary"
    raise ValueError(
        "services[].from must be 'primary', 'secondary', primary_region, or secondary_region."
    )


def _region_from_cluster_arn(cluster_arn: str) -> str:
    parts = (cluster_arn or "").split(":")
    if len(parts) < 4:
        return ""
    return str(parts[3] or "")


def _resource_has_matching_tags(rds_client, resource_arn: str, tags: Dict[str, str]) -> bool:
    if not tags:
        return True
    try:
        response = rds_client.list_tags_for_resource(ResourceName=resource_arn)
    except Exception:
        return False
    actual = {t["Key"]: t.get("Value", "") for t in response.get("TagList", []) if "Key" in t}
    return all(actual.get(k) == v for k, v in tags.items())


def _discover_global_db_target_generic(
    *,
    session,
    lookup_region: str,
    svc: Dict[str, Any],
    action: str,
) -> Dict[str, Any]:
    identifier = str(svc.get("identifier") or "").strip()
    target_region = str(svc.get("target_region") or "").strip()
    tags = parse_tags(svc.get("tags"))

    if not identifier and not tags:
        raise ValueError(f"rds:{action} requires identifier or tags for Aurora Global Database discovery.")
    if not target_region:
        raise ValueError(f"rds:{action} with use_arc=false requires service.target_region.")

    rds = session.client("rds", region_name=lookup_region)
    paginator = rds.get_paginator("describe_global_clusters")
    matches: List[Dict[str, Any]] = []

    for page in paginator.paginate():
        for global_cluster in page.get("GlobalClusters", []):
            global_cluster_identifier = str(global_cluster.get("GlobalClusterIdentifier") or "")
            global_cluster_arn = str(global_cluster.get("GlobalClusterArn") or "")
            if identifier and global_cluster_identifier != identifier:
                continue

            member_cluster_arns: Dict[str, str] = {}
            for member in global_cluster.get("GlobalClusterMembers") or []:
                cluster_arn = str(member.get("DBClusterArn") or "")
                if not cluster_arn:
                    continue
                member_region = _region_from_cluster_arn(cluster_arn)
                if not member_region:
                    continue
                member_cluster_arns[member_region] = cluster_arn

            if target_region not in member_cluster_arns:
                continue

            if tags:
                matches_global_tags = bool(global_cluster_arn) and _resource_has_matching_tags(rds, global_cluster_arn, tags)
                matches_member_tags = any(
                    _resource_has_matching_tags(session.client("rds", region_name=member_region), cluster_arn, tags)
                    for member_region, cluster_arn in member_cluster_arns.items()
                )
                if not (matches_global_tags or matches_member_tags):
                    continue

            matches.append(
                {
                    "service": f"rds:{action}",
                    "action": action,
                    "use_arc": False,
                    "selection_mode": "ALL",
                    "global_cluster_identifier": global_cluster_identifier,
                    "global_cluster_arn": global_cluster_arn,
                    "member_cluster_arns": member_cluster_arns,
                    "target_region": target_region,
                    "tags": tags,
                }
            )

    if len(matches) == 0:
        raise ValueError(
            f"No Aurora global database matched the requested selector for rds:{action} "
            f"(identifier={identifier or '<none>'}, target_region={target_region})."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple Aurora global databases matched the requested selector for rds:{action}. "
            "Please refine identifier/tags so exactly one global database is selected."
        )

    return matches[0]


class RDSAction(CustomComponentAction):
    service_name = "rds"
    action_names = ["reboot", "failover", "failover-global-db", "switchover-global-db"]

    def build_plan_item(
        self,
        *,
        manifest: Dict[str, Any],
        svc: Dict[str, Any],
        session,
        region: str,
        index: int,
        default_timeout_seconds: int,
    ) -> Dict[str, Any]:
        _ = manifest
        action = str(svc.get("action") or "").strip().lower()
        if action in ("failover-global-db", "switchover-global-db"):
            return self._build_global_db_plan_item(
                manifest=manifest,
                svc=svc,
                session=session,
                region=region,
                index=index,
                default_timeout_seconds=default_timeout_seconds,
            )

        arns = collect_service_resource_arns(
            svc,
            session=session,
            region=region,
            zone=resolve_service_zone(manifest, svc),
        )
        if not arns:
            raise ValueError(f"rds:{action} did not resolve any RDS resources from the manifest selection.")

        identifiers: List[str] = []
        impacted_resources: List[Dict[str, str]] = []
        if action == "reboot":
            for arn in arns:
                identifier = _db_instance_identifier_from_arn(arn)
                if not identifier:
                    continue
                identifiers.append(identifier)
                impacted_resources.append(
                    {
                        "service": "rds:reboot",
                        "arn": arn,
                        "selection_mode": "CUSTOM",
                    }
                )
        elif action == "failover":
            for arn in arns:
                identifier = _db_cluster_identifier_from_arn(arn)
                if not identifier:
                    continue
                identifiers.append(identifier)
                impacted_resources.append(
                    {
                        "service": "rds:failover",
                        "arn": arn,
                        "selection_mode": "CUSTOM",
                    }
                )
        else:
            raise ValueError(f"Unsupported custom RDS action: {action}")

        if not identifiers:
            raise ValueError(f"rds:{action} could not extract RDS identifiers from the resolved ARNs.")

        return {
            "name": f"a_rds_{action}_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:{action}",
            "action": action,
            "description": f"{action.title()} {len(identifiers)} RDS resource(s)",
            "target": {
                "identifiers": identifiers,
            },
            "parameters": {
                "timeoutSeconds": int(default_timeout_seconds),
                "useFis": False,
            },
            "impacted_resources": impacted_resources,
        }

    def _build_global_db_plan_item(
        self,
        *,
        manifest: Dict[str, Any],
        svc: Dict[str, Any],
        session,
        region: str,
        index: int,
        default_timeout_seconds: int,
    ) -> Dict[str, Any]:
        action = str(svc.get("action") or "").strip().lower()
        target_region = str(svc.get("target_region") or "").strip()

        if target_region:
            lookup_region = resolve_service_region(manifest, svc, default=target_region or region) or target_region or region
            target = _discover_global_db_target_generic(
                session=session,
                lookup_region=lookup_region,
                svc=svc,
                action=action,
            )
        else:
            primary_region = resolve_service_primary_region(manifest, svc)
            secondary_region = resolve_service_secondary_region(manifest, svc)
            _resolve_global_db_from_side(svc.get("from"), primary_region, secondary_region)

            if not primary_region or not secondary_region:
                raise ValueError(
                    f"rds:{action} requires primary_region and secondary_region at the top level or service level."
                )
            if primary_region == secondary_region:
                raise ValueError(f"rds:{action} requires primary_region and secondary_region to be different.")

            resolved = discover_rds_global_clusters(
                manifest={
                    "services": [svc],
                    "primary_region": primary_region,
                    "secondary_region": secondary_region,
                },
                session=session,
            )
            if len(resolved) != 1:
                raise ValueError(f"rds:{action} expected exactly one Aurora global database match.")
            target = dict(resolved[0])

        impacted_resources = []
        if target.get("global_cluster_arn"):
            impacted_resources.append(
                {
                    "service": f"rds:{action}",
                    "arn": str(target.get("global_cluster_arn") or ""),
                    "selection_mode": "CUSTOM",
                }
            )
        for cluster_arn in (target.get("member_cluster_arns") or {}).values():
            impacted_resources.append(
                {
                    "service": f"rds:{action}",
                    "arn": str(cluster_arn or ""),
                    "selection_mode": "CUSTOM",
                }
            )

        return {
            "name": f"a_rds_{action}_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:{action}",
            "action": action,
            "description": f"{action.replace('-', ' ').title()} Aurora global database",
            "target": target,
            "parameters": {
                "timeoutSeconds": int(default_timeout_seconds),
                "useArc": False,
            },
            "impacted_resources": impacted_resources,
        }

    def execute_item(
        self,
        *,
        session,
        item: Dict[str, Any],
        poll_seconds: int,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        started_at = _utc_now_iso()
        action = str(item.get("action") or "").strip().lower()
        identifiers = list((item.get("target") or {}).get("identifiers") or [])
        params = dict(item.get("parameters") or {})
        region = item["region"]
        effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)

        original_state: Dict[str, Any] = {}

        if action in ("failover-global-db", "switchover-global-db"):
            return self._execute_global_db_item(
                session=session,
                item=item,
                poll_seconds=poll_seconds,
                timeout_seconds=effective_timeout_seconds,
            )

        rds = session.client("rds", region_name=region)

        try:
            if action == "reboot":
                for identifier in identifiers:
                    original_state[identifier] = _describe_db_instance(rds, identifier)
                    rds.reboot_db_instance(DBInstanceIdentifier=identifier)

                deadline = time.time() + effective_timeout_seconds
                while True:
                    ready = True
                    latest_state: Dict[str, Any] = {}
                    for identifier in identifiers:
                        instance = _describe_db_instance(rds, identifier)
                        latest_state[identifier] = instance
                        status = str(instance.get("DBInstanceStatus") or "").strip().lower()
                        if status != "available":
                            ready = False

                    if ready:
                        original_state["__latest__"] = latest_state
                        break

                    if time.time() > deadline:
                        raise TimeoutError(
                            "Timed out waiting for RDS DB instance reboot to complete and the instance to return to available state."
                        )
                    time.sleep(max(1, poll_seconds))
            elif action == "failover":
                original_writers: Dict[str, str] = {}
                for identifier in identifiers:
                    cluster = _describe_db_cluster(rds, identifier)
                    original_state[identifier] = cluster
                    original_writers[identifier] = _cluster_writer_identifier(cluster)
                    rds.failover_db_cluster(DBClusterIdentifier=identifier)

                deadline = time.time() + effective_timeout_seconds
                while True:
                    ready = True
                    latest_state: Dict[str, Any] = {}
                    for identifier in identifiers:
                        cluster = _describe_db_cluster(rds, identifier)
                        latest_state[identifier] = cluster
                        status = str(cluster.get("Status") or "").strip().lower()
                        current_writer = _cluster_writer_identifier(cluster)
                        original_writer = original_writers.get(identifier) or ""
                        writer_changed = bool(current_writer) and current_writer != original_writer
                        if status != "available":
                            ready = False
                            continue
                        if original_writer and not writer_changed:
                            ready = False

                    if ready:
                        original_state["__latest__"] = latest_state
                        break

                    if time.time() > deadline:
                        raise TimeoutError(
                            "Timed out waiting for RDS DB cluster failover to complete and the cluster to return to available state."
                        )
                    time.sleep(max(1, poll_seconds))
            else:
                raise ValueError(f"Unsupported custom RDS action: {action}")
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"RDS API error during rds:{action}: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": item.get("target"),
                    "parameters": params,
                    "originalState": original_state,
                },
            }

        try:
            if action == "reboot":
                final_status = {identifier: _describe_db_instance(rds, identifier) for identifier in identifiers}
            else:
                final_status = {
                    identifier: _describe_db_cluster(rds, identifier)
                    for identifier in identifiers
                }
        except Exception as e:
            final_status = {"error": f"Unable to read final RDS state: {e}"}

        ended_at = _utc_now_iso()
        return {
            "name": item["name"],
            "status": "completed",
            "reason": None,
            "startTime": started_at,
            "endTime": ended_at,
            "details": {
                "target": item.get("target"),
                "parameters": params,
                "originalState": original_state,
                "finalStatus": final_status,
            },
        }

    def _execute_global_db_item(
        self,
        *,
        session,
        item: Dict[str, Any],
        poll_seconds: int,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        started_at = _utc_now_iso()
        action = str(item.get("action") or "").strip().lower()
        target = dict(item.get("target") or {})
        target_region = str(target.get("target_region") or "").strip()
        if not target_region:
            primary_region = str(target.get("primary_region") or "").strip()
            secondary_region = str(target.get("secondary_region") or "").strip()
            from_side = _resolve_global_db_from_side(target.get("from"), primary_region, secondary_region)
            target_region = secondary_region if from_side == "primary" else primary_region
        client = session.client("rds", region_name=target_region)

        request: Dict[str, Any] = {
            "GlobalClusterIdentifier": target["global_cluster_identifier"],
            "TargetDbClusterIdentifier": target["member_cluster_arns"][target_region],
        }
        if action == "failover-global-db":
            request["AllowDataLoss"] = True

        try:
            if action == "failover-global-db":
                response = client.failover_global_cluster(**request)
            else:
                response = client.switchover_global_cluster(**request)
            final_state = _wait_for_global_db_ready(
                session=session,
                target=target,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"RDS API error during rds:{action}: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": target,
                    "parameters": item.get("parameters"),
                },
            }

        ended_at = _utc_now_iso()
        return {
            "name": item["name"],
            "status": "completed",
            "reason": None,
            "startTime": started_at,
            "endTime": ended_at,
            "details": {
                "target": target,
                "parameters": item.get("parameters"),
                "request": request,
                "initialResponse": response,
                "finalGlobalDbState": final_state,
            },
        }


def _describe_db_cluster_by_arn(rds_client, cluster_arn: str) -> Dict[str, Any]:
    cluster_identifier = _db_cluster_identifier_from_arn(cluster_arn)
    return _describe_db_cluster(rds_client, cluster_identifier)


def _wait_for_global_cluster_role_once(
    client,
    global_cluster_identifier: str,
    target_cluster_arn: str,
) -> Dict[str, Any]:
    response = client.describe_global_clusters(GlobalClusterIdentifier=global_cluster_identifier)
    global_clusters = response.get("GlobalClusters") or []
    if not global_clusters:
        return {}

    global_cluster = global_clusters[0]
    status = str(global_cluster.get("Status") or "").strip().lower()
    failover_state = global_cluster.get("FailoverState") or {}
    members = global_cluster.get("GlobalClusterMembers") or []
    target_is_writer = any(
        m.get("DBClusterArn") == target_cluster_arn and bool(m.get("IsWriter"))
        for m in members
    )
    in_progress = bool(failover_state.get("Status"))
    if status == "available" and target_is_writer and not in_progress:
        return global_cluster
    return {}


def _wait_for_global_db_ready(
    *,
    session,
    target: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    member_cluster_arns = target["member_cluster_arns"]
    target_region = str(target.get("target_region") or "").strip()
    if not target_region:
        primary_region = target["primary_region"]
        secondary_region = target["secondary_region"]
        from_side = _resolve_global_db_from_side(target.get("from"), primary_region, secondary_region)
        target_region = secondary_region if from_side == "primary" else primary_region
    target_cluster_arn = member_cluster_arns[target_region]
    control_client = session.client("rds", region_name=target_region)

    start = time.time()
    while True:
        global_cluster = _wait_for_global_cluster_role_once(
            client=control_client,
            global_cluster_identifier=target["global_cluster_identifier"],
            target_cluster_arn=target_cluster_arn,
        )

        cluster_states: Dict[str, Dict[str, Any]] = {}
        member_states: Dict[str, Dict[str, Any]] = {}
        all_available = True
        all_synchronized = True

        if global_cluster:
            for member in global_cluster.get("GlobalClusterMembers") or []:
                cluster_arn = str(member.get("DBClusterArn") or "")
                if cluster_arn in member_cluster_arns.values():
                    member_states[cluster_arn] = member

        for member_region, cluster_arn in member_cluster_arns.items():
            rds_client = session.client("rds", region_name=member_region)
            cluster = _describe_db_cluster_by_arn(rds_client, cluster_arn)
            cluster_states[member_region] = cluster

            cluster_status = str(cluster.get("Status") or "").strip().lower()
            if cluster_status != "available":
                all_available = False

            member = member_states.get(cluster_arn) or {}
            is_writer = bool(member.get("IsWriter"))
            sync_status = str(member.get("SynchronizationStatus") or "").strip().lower()

            if not is_writer and sync_status != "connected":
                all_synchronized = False
            elif is_writer and sync_status not in ("", "connected"):
                all_synchronized = False

        if global_cluster and all_available and all_synchronized:
            return {
                "globalCluster": global_cluster,
                "clusters": cluster_states,
                "members": member_states,
            }

        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                "Aurora global database did not reach a fully synchronized and available state "
                f"in both Regions within {timeout_seconds}s."
            )

        time.sleep(max(1, poll_seconds))
