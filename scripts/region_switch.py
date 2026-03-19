import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from utility import utc_ts

from resource import discover_rds_global_clusters


REGION_ACTION_CONFIG: Dict[str, Dict[str, Any]] = {
    "failover-global-db": {
        "arc": {
            "behavior": "failover",
            "mode": "ungraceful",
            "description": "Fail over Aurora global database with ARC Region switch",
        },
        "non_arc": {
            "engine": "sdk",
            "sdk_api": "failover_global_cluster",
            "description": "Fail over Aurora global database with the RDS SDK",
        },
    },
    "switchover-global-db": {
        "arc": {
            "behavior": "switchoverOnly",
            "mode": "graceful",
            "description": "Switchover Aurora global database with ARC Region switch",
        },
        "non_arc": {
            "engine": "sdk",
            "sdk_api": "switchover_global_cluster",
            "description": "Switchover Aurora global database with the RDS SDK",
        },
    },
}


def validate_region_manifest(manifest: Dict[str, Any]) -> None:
    rtype = (manifest.get("resilience_test_type") or "").strip().lower()
    if rtype != "region":
        raise ValueError("Region switch flow requires resilience_test_type to be 'region'.")

    primary_region = manifest.get("primary_region")
    secondary_region = manifest.get("secondary_region")
    if not primary_region or not isinstance(primary_region, str):
        raise ValueError("region resilience tests require top-level primary_region.")
    if not secondary_region or not isinstance(secondary_region, str):
        raise ValueError("region resilience tests require top-level secondary_region.")
    if primary_region == secondary_region:
        raise ValueError("primary_region and secondary_region must be different.")

    services = manifest.get("services")
    if not isinstance(services, list) or not services:
        raise ValueError("Top-level 'services' must be a non-empty list.")

    for i, svc in enumerate(services):
        if not isinstance(svc, dict):
            raise ValueError(f"services[{i}] must be an object.")
        name = (svc.get("name") or "").strip().lower()
        action = (svc.get("action") or "").strip().lower()
        from_side = (svc.get("from") or "").strip().lower()
        use_arc = svc.get("use_arc", True)

        if name != "rds" or action not in REGION_ACTION_CONFIG:
            raise ValueError(f"Unsupported region resilience service action: {name}:{action}")
        if from_side not in ("primary", "secondary"):
            raise ValueError(f"services[{i}].from must be 'primary' or 'secondary'.")
        if not isinstance(svc.get("tags"), str) or not str(svc.get("tags") or "").strip():
            raise ValueError(f"services[{i}].tags is required for Aurora global database discovery.")
        if not isinstance(use_arc, bool):
            raise ValueError(f"services[{i}].use_arc must be true or false when provided.")


def resolve_region_targets(manifest: Dict[str, Any], session) -> List[Dict[str, Any]]:
    validate_region_manifest(manifest)
    return discover_rds_global_clusters(manifest=manifest, session=session)


def build_execution_plan(
    manifest: Dict[str, Any],
    execution_role_arn: str,
    resolved_targets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    validate_region_manifest(manifest)

    plan_name = f"region-run-{utc_ts()}".lower()
    items: List[Dict[str, Any]] = []
    for idx, target in enumerate(resolved_targets, start=1):
        use_arc = bool(target.get("use_arc", True))
        if use_arc:
            if not execution_role_arn:
                raise ValueError("ARC region switch requires an execution role ARN when use_arc=true.")
            item = _build_arc_execution_item(manifest, target, execution_role_arn, idx)
        else:
            item = _build_non_arc_execution_item(manifest, target, idx)
        items.append(item)

    if not items:
        raise ValueError("No region execution items were resolved from the manifest.")

    return {
        "name": plan_name,
        "description": "Region resilience execution plan",
        "resolvedTargets": resolved_targets,
        "items": items,
    }


def execute_region_plan(
    session,
    execution_plan: Dict[str, Any],
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    item_summaries: List[Dict[str, Any]] = []

    for item in execution_plan["items"]:
        if item["engine"] == "arc":
            item_summary = _execute_arc_item(
                session=session,
                item=item,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
        elif item["engine"] == "sdk":
            item_summary = _execute_sdk_item(
                session=session,
                item=item,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
        else:
            raise ValueError(f"Unsupported region execution engine: {item['engine']}")

        item_summaries.append(item_summary)

    overall_status = "completed"
    if any(s["status"] not in ("completed", "completedWithExceptions") for s in item_summaries):
        overall_status = "failed"
    elif any(s["status"] == "completedWithExceptions" for s in item_summaries):
        overall_status = "completedWithExceptions"

    start_times = [s["startTime"] for s in item_summaries if s.get("startTime")]
    end_times = [s["endTime"] for s in item_summaries if s.get("endTime")]

    summary = {
        "experimentId": execution_plan["name"],
        "experimentTemplateId": execution_plan["name"],
        "status": overall_status,
        "reason": "region resilience execution",
        "startTime": min(start_times) if start_times else None,
        "endTime": max(end_times) if end_times else None,
        "actions": {},
        "regionSwitch": {
            "executionPlan": execution_plan,
            "items": item_summaries,
        },
    }

    for item_summary in item_summaries:
        summary["actions"][item_summary["name"]] = {
            "status": item_summary["status"],
            "reason": item_summary["reason"],
            "startTime": item_summary["startTime"],
            "endTime": item_summary["endTime"],
        }

    return summary


def _build_arc_execution_item(
    manifest: Dict[str, Any],
    target: Dict[str, Any],
    execution_role_arn: str,
    index: int,
) -> Dict[str, Any]:
    primary_region = manifest["primary_region"]
    secondary_region = manifest["secondary_region"]
    action = str(target["action"])
    action_cfg = REGION_ACTION_CONFIG[action]["arc"]
    from_side = str(target["from"])
    target_region = _target_region(manifest, from_side)

    plan_name = f"rs-rds-{index}-{utc_ts()}".lower()
    plan_description = (
        f"{action} from {from_side} using ARC for Aurora global database "
        f"{target['global_cluster_identifier']}"
    )
    step_name = f"rds-{action}-{index}".replace("_", "-")

    global_aurora_config: Dict[str, Any] = {
        "behavior": action_cfg["behavior"],
        "globalClusterIdentifier": target["global_cluster_identifier"],
        "databaseClusterArns": [
            target["member_cluster_arns"][primary_region],
            target["member_cluster_arns"][secondary_region],
        ],
    }
    if action_cfg["mode"] == "ungraceful":
        global_aurora_config["ungraceful"] = {"ungraceful": "failover"}

    return {
        "name": step_name,
        "engine": "arc",
        "service": target["service"],
        "action": action,
        "use_arc": True,
        "planControlRegion": primary_region,
        "target": target,
        "payload": {
            "name": plan_name,
            "description": plan_description,
            "executionRole": execution_role_arn,
            "regions": [primary_region, secondary_region],
            "recoveryApproach": "activePassive",
            "primaryRegion": primary_region,
            "workflows": [
                {
                    "workflowTargetAction": "activate",
                    "workflowTargetRegion": target_region,
                    "workflowDescription": plan_description,
                    "steps": [
                        {
                            "name": step_name,
                            "description": action_cfg["description"],
                            "executionBlockType": "AuroraGlobalDatabase",
                            "executionBlockConfiguration": {
                                "globalAuroraConfig": global_aurora_config,
                            },
                        }
                    ],
                }
            ],
            "tags": {
                "managed-by": "fis.py",
                "resilience-test-type": "region",
                "service": "rds",
                "action": action,
            },
        },
        "request": {
            "targetRegion": target_region,
            "action": "activate",
            "mode": action_cfg["mode"],
            "comment": plan_description,
            "latestVersion": "true",
        },
    }


def _build_non_arc_execution_item(
    manifest: Dict[str, Any],
    target: Dict[str, Any],
    index: int,
) -> Dict[str, Any]:
    action = str(target["action"])
    action_cfg = REGION_ACTION_CONFIG[action]["non_arc"]
    from_side = str(target["from"])
    source_region = _source_region(manifest, from_side)
    target_region = _target_region(manifest, from_side)
    target_cluster_arn = target["member_cluster_arns"][target_region]

    if action == "failover-global-db":
        client_region = target_region
        params = {
            "GlobalClusterIdentifier": target["global_cluster_identifier"],
            "TargetDbClusterIdentifier": target_cluster_arn,
            "AllowDataLoss": True,
        }
    elif action == "switchover-global-db":
        client_region = source_region
        params = {
            "GlobalClusterIdentifier": target["global_cluster_identifier"],
            "TargetDbClusterIdentifier": target_cluster_arn,
        }
    else:
        raise ValueError(f"Unsupported non-ARC region action: {action}")

    return {
        "name": f"rds-{action}-{index}".replace("_", "-"),
        "engine": action_cfg["engine"],
        "service": target["service"],
        "action": action,
        "use_arc": False,
        "clientRegion": client_region,
        "targetRegion": target_region,
        "sourceRegion": source_region,
        "request": {
            "sdkApi": action_cfg["sdk_api"],
            "params": params,
            "description": action_cfg["description"],
        },
        "target": target,
    }


def _execute_arc_item(
    session,
    item: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    client = session.client("arc-region-switch", region_name=item["planControlRegion"])
    start_time = datetime.now(timezone.utc).isoformat()

    plan = client.create_plan(**item["payload"])["plan"]
    plan_arn = plan["arn"]
    print(f"[OK] Created ARC Region switch plan: {plan_arn}")

    execution = client.start_plan_execution(planArn=plan_arn, **item["request"])
    execution_id = execution["executionId"]
    print(f"[OK] Started ARC Region switch executionId: {execution_id}")

    final_execution = _wait_for_arc_execution(
        client=client,
        plan_arn=plan_arn,
        execution_id=execution_id,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )
    final_state = _wait_for_global_db_ready(
        session=session,
        target=item["target"],
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )

    return {
        "name": item["name"],
        "engine": "arc",
        "status": final_execution.get("executionState"),
        "reason": final_execution.get("comment"),
        "startTime": final_execution.get("startTime") or start_time,
        "endTime": datetime.now(timezone.utc).isoformat(),
        "details": {
            "planArn": plan_arn,
            "executionId": execution_id,
            "payload": item["payload"],
            "request": item["request"],
            "rawExecution": final_execution,
            "finalGlobalDbState": final_state,
        },
    }


def _execute_sdk_item(
    session,
    item: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    client = session.client("rds", region_name=item["clientRegion"])
    start_time = datetime.now(timezone.utc).isoformat()
    request = item["request"]

    print(
        f"[INFO] Starting non-ARC region action {item['action']} via {request['sdkApi']} "
        f"in region {item['clientRegion']}"
    )

    sdk_api = getattr(client, request["sdkApi"])
    response = sdk_api(**request["params"])
    final_state = _wait_for_global_db_ready(
        session=session,
        target=item["target"],
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )

    return {
        "name": item["name"],
        "engine": "sdk",
        "status": "completed",
        "reason": f"{request['sdkApi']} in {item['clientRegion']}",
        "startTime": start_time,
        "endTime": datetime.now(timezone.utc).isoformat(),
        "details": {
            "request": request,
            "initialResponse": response,
            "finalGlobalClusterState": final_state,
        },
    }


def _wait_for_arc_execution(
    client,
    plan_arn: str,
    execution_id: str,
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    terminal = {
        "completed",
        "completedWithExceptions",
        "canceled",
        "planExecutionTimedOut",
        "failed",
    }
    start = time.time()
    while True:
        execution = client.get_plan_execution(planArn=plan_arn, executionId=execution_id)
        status = execution.get("executionState", "unknown")
        if status in terminal:
            return execution
        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                f"Region switch execution {execution_id} timed out after {timeout_seconds}s (last={status})."
            )
        print(
            f"[INFO] Region switch execution {execution_id} status={status} "
            f"elapsed={int(time.time() - start)}s"
        )
        time.sleep(poll_seconds)


def _wait_for_global_cluster_role(
    client,
    global_cluster_identifier: str,
    target_cluster_arn: str,
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    start = time.time()
    while True:
        response = client.describe_global_clusters(GlobalClusterIdentifier=global_cluster_identifier)
        global_clusters = response.get("GlobalClusters") or []
        if global_clusters:
            global_cluster = global_clusters[0]
            status = str(global_cluster.get("Status") or "").strip().lower()
            members = global_cluster.get("GlobalClusterMembers") or []
            target_is_writer = any(
                m.get("DBClusterArn") == target_cluster_arn and bool(m.get("IsWriter"))
                for m in members
            )
            if status == "available" and target_is_writer:
                return global_cluster

        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                f"Global cluster {global_cluster_identifier} did not promote target {target_cluster_arn} "
                f"within {timeout_seconds}s."
            )
        time.sleep(poll_seconds)


def _wait_for_global_db_ready(
    session,
    target: Dict[str, Any],
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    member_cluster_arns = target["member_cluster_arns"]
    primary_region = target["primary_region"]
    secondary_region = target["secondary_region"]
    target_region = secondary_region if str(target["from"]) == "primary" else primary_region
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
        all_available = True
        for region, cluster_arn in member_cluster_arns.items():
            rds_client = session.client("rds", region_name=region)
            cluster = _describe_db_cluster_by_arn(rds_client, cluster_arn)
            cluster_states[region] = cluster
            status = str(cluster.get("Status") or "").strip().lower()
            if status != "available":
                all_available = False

        if global_cluster is not None and all_available:
            return {
                "globalCluster": global_cluster,
                "clusters": cluster_states,
            }

        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                "Aurora global database did not reach a fully available state in both Regions "
                f"within {timeout_seconds}s."
            )

        time.sleep(poll_seconds)


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
    members = global_cluster.get("GlobalClusterMembers") or []
    target_is_writer = any(
        m.get("DBClusterArn") == target_cluster_arn and bool(m.get("IsWriter"))
        for m in members
    )
    if status == "available" and target_is_writer:
        return global_cluster
    return {}


def _describe_db_cluster_by_arn(rds_client, cluster_arn: str) -> Dict[str, Any]:
    cluster_identifier = _cluster_identifier_from_arn(cluster_arn)
    response = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_identifier)
    clusters = response.get("DBClusters") or []
    if not clusters:
        raise ValueError(f"DB cluster not found for ARN: {cluster_arn}")
    return clusters[0]


def _cluster_identifier_from_arn(cluster_arn: str) -> str:
    marker = ":cluster:"
    if marker not in cluster_arn:
        raise ValueError(f"Invalid DB cluster ARN: {cluster_arn}")
    return cluster_arn.split(marker, 1)[1]




def _source_region(manifest: Dict[str, Any], from_side: str) -> str:
    return manifest["primary_region"] if from_side == "primary" else manifest["secondary_region"]


def _target_region(manifest: Dict[str, Any], from_side: str) -> str:
    return manifest["secondary_region"] if from_side == "primary" else manifest["primary_region"]
