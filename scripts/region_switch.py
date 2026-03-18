import time
from typing import Any, Dict, List, Tuple

from utility import utc_ts

from resource import discover_rds_global_clusters


REGION_ACTION_CONFIG: Dict[str, Dict[str, str]] = {
    "failover-global-db": {
        "behavior": "failover",
        "mode": "ungraceful",
        "description": "Fail over Aurora global database with ARC Region switch",
    },
    "switchover-global-db": {
        "behavior": "switchoverOnly",
        "mode": "graceful",
        "description": "Switchover Aurora global database with ARC Region switch",
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

        if name != "rds" or action not in REGION_ACTION_CONFIG:
            raise ValueError(f"Unsupported region resilience service action: {name}:{action}")
        if from_side not in ("primary", "secondary"):
            raise ValueError(f"services[{i}].from must be 'primary' or 'secondary'.")
        if not isinstance(svc.get("tags"), str) or not str(svc.get("tags") or "").strip():
            raise ValueError(f"services[{i}].tags is required for Aurora global database discovery.")


def resolve_region_targets(manifest: Dict[str, Any], session) -> List[Dict[str, Any]]:
    validate_region_manifest(manifest)
    return discover_rds_global_clusters(manifest=manifest, session=session)


def build_plan_payload(
    manifest: Dict[str, Any],
    execution_role_arn: str,
    resolved_targets: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    validate_region_manifest(manifest)

    if not execution_role_arn:
        raise ValueError("ARC region switch requires an execution role ARN.")
    if len(resolved_targets) != 1:
        raise ValueError("Current region switch implementation expects exactly one resolved target.")

    primary_region = manifest["primary_region"]
    secondary_region = manifest["secondary_region"]
    target = resolved_targets[0]

    action = str(target.get("action") or "").strip().lower()
    action_cfg = REGION_ACTION_CONFIG[action]
    from_side = str(target.get("from") or "").strip().lower()
    target_region = secondary_region if from_side == "primary" else primary_region

    short_ts = utc_ts()
    plan_name = f"rs-rds-{short_ts}".lower()
    plan_description = (
        f"{action} from {from_side} using Aurora global database "
        f"{target.get('global_cluster_identifier')}"
    )

    step_name = f"rds-{action}".replace("_", "-")
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

    payload: Dict[str, Any] = {
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
    }

    execution_request = {
        "targetRegion": target_region,
        "action": "activate",
        "mode": action_cfg["mode"],
        "comment": plan_description,
        "latestVersion": "true",
    }
    return payload, execution_request


def create_plan(region_switch_client, payload: Dict[str, Any]) -> Dict[str, Any]:
    return region_switch_client.create_plan(**payload)["plan"]


def start_plan_execution(region_switch_client, plan_arn: str, execution_request: Dict[str, Any]) -> Dict[str, Any]:
    return region_switch_client.start_plan_execution(planArn=plan_arn, **execution_request)


def wait_for_plan_execution(
    region_switch_client,
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
        execution = region_switch_client.get_plan_execution(
            planArn=plan_arn,
            executionId=execution_id,
        )
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


def summarize_plan_execution(execution: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "experimentId": execution.get("executionId"),
        "experimentTemplateId": execution.get("planArn"),
        "status": execution.get("executionState"),
        "reason": execution.get("comment"),
        "startTime": execution.get("startTime"),
        "endTime": execution.get("endTime"),
        "actions": {},
    }

    for step in execution.get("stepStates") or []:
        name = step.get("name") or "step"
        out["actions"][name] = {
            "status": step.get("status"),
            "reason": step.get("stepMode"),
            "startTime": step.get("startTime"),
            "endTime": step.get("endTime"),
        }

    return out
