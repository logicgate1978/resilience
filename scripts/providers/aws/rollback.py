from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from component_actions.asg import ASGScaleAction
from component_actions.dns import DNSAction
from component_actions.eks import EKSAction
from component_actions.s3 import S3FailoverAction
from utility import log_message, utc_ts


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_label(hosted_zone_id: str, record_name: str, record_type: str, set_identifier: str = "") -> str:
    suffix = f"#{set_identifier}" if set_identifier else ""
    return f"route53://{hosted_zone_id}/{record_name}/{record_type}{suffix}"


def _normalize_route53_record_name(record_set: Dict[str, Any]) -> str:
    name = str(record_set.get("Name") or "").strip()
    if name.endswith("."):
        name = name[:-1]
    return name


def _resource_record_value(record_set: Dict[str, Any]) -> str:
    alias_target = record_set.get("AliasTarget") or {}
    alias_name = str(alias_target.get("DNSName") or "").strip()
    if alias_name:
        return alias_name.rstrip(".")
    values = [
        str(entry.get("Value") or "").strip()
        for entry in (record_set.get("ResourceRecords") or [])
        if str(entry.get("Value") or "").strip()
    ]
    return ",".join(values) if values else "-"


def _weight_assignments_value(record_sets: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for entry in record_sets:
        if not isinstance(entry, dict):
            continue
        record_set = entry.get("recordSet") or {}
        set_identifier = str(entry.get("setIdentifier") or record_set.get("SetIdentifier") or "").strip()
        weight = record_set.get("Weight")
        if set_identifier:
            parts.append(f"{set_identifier}={weight}")
    return ",".join(parts) if parts else "-"


def _route_target_region(route_updates: List[Dict[str, Any]]) -> Optional[str]:
    for entry in route_updates or []:
        if entry.get("TrafficDialPercentage") == 100:
            region = str(entry.get("Region") or "").strip()
            if region:
                return region
    return None


def _summarize_impacted_resources(resources: List[Dict[str, Any]]) -> str:
    labels: List[str] = []
    for resource in resources or []:
        arn = str(resource.get("arn") or "").strip()
        if not arn:
            continue
        if arn.startswith("arn:"):
            if "/" in arn:
                labels.append(arn.rsplit("/", 1)[-1])
            else:
                labels.append(arn.rsplit(":", 1)[-1])
        elif arn.startswith("route53://"):
            labels.append(arn.replace("route53://", "", 1))
        elif arn.startswith("eks://"):
            labels.append(arn.replace("eks://", "", 1))
        else:
            labels.append(arn)
    if not labels:
        return "-"
    if len(labels) <= 3:
        return ", ".join(labels)
    return ", ".join(labels[:3]) + f" (+{len(labels) - 3} more)"


def _format_key_parameters(item: Dict[str, Any]) -> str:
    action_key = str(item.get("service") or "").strip().lower()
    if action_key == "dns:set-value":
        record_set = item.get("recordSetAfter") or {}
        value = _resource_record_value(record_set)
        return f"value={value}" if value != "-" else "-"
    if action_key == "dns:set-weight":
        value = _weight_assignments_value(item.get("recordSets") or [])
        return f"value={value}" if value != "-" else "-"

    parameters = item.get("parameters") or {}
    out: List[str] = []
    for key in ("min", "max", "desired", "replicas"):
        if key in parameters and parameters.get(key) is not None:
            out.append(f"{key}={parameters.get(key)}")
    if action_key == "s3:failover":
        target_region = (((item.get("target") or {}).get("targetRegion")) or "")
        if target_region:
            out.append(f"target_region={target_region}")
    return ", ".join(out) if out else "-"


def _rollback_exec_type(action_key: str) -> str:
    mapping = {
        "asg:scale": "restore ASG capacity",
        "eks:scale-deployment": "restore deployment replicas",
        "eks:scale-nodegroup": "restore nodegroup scaling",
        "dns:set-value": "restore Route53 record",
        "dns:set-weight": "restore Route53 weights",
        "s3:failover": "restore MRAP routing",
    }
    return mapping.get(action_key, "rollback")


def _build_asg_rollback_item(row: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    plan = row.get("rollback_plan_json") or {}
    groups = list(plan.get("groups") or [])
    group_names = [str(group.get("groupName") or "").strip() for group in groups if str(group.get("groupName") or "").strip()]
    parameters = {"waitForReady": True, "timeoutSeconds": timeout_seconds}
    if groups:
        first = groups[0]
        parameters.update(
            {
                "min": first.get("min"),
                "max": first.get("max"),
                "desired": first.get("desired"),
            }
        )
    return {
        "rollbackActionId": str(row.get("action_id") or ""),
        "name": f"rollback_asg_scale_{row.get('sequence_no')}",
        "engine": "rollback",
        "service": "asg:scale",
        "action": "scale",
        "region": plan.get("region") or row.get("requested_region"),
        "target": {"groupNames": group_names},
        "parameters": parameters,
        "impacted_resources": [
            {
                "service": "asg:scale",
                "arn": group.get("groupName"),
                "selection_mode": "ROLLBACK",
            }
            for group in groups
            if str(group.get("groupName") or "").strip()
        ],
        "originalGroups": groups,
    }


def _build_eks_scale_deployment_rollback_item(row: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    plan = row.get("rollback_plan_json") or {}
    target = plan.get("target") or {}
    params = plan.get("parameters") or {}
    cluster_identifier = str(target.get("cluster_identifier") or "").strip()
    namespace = str(target.get("namespace") or "").strip()
    deployment_name = str(target.get("deployment_name") or "").strip()
    return {
        "rollbackActionId": str(row.get("action_id") or ""),
        "name": f"rollback_eks_scale_deployment_{row.get('sequence_no')}",
        "engine": "rollback",
        "service": "eks:scale-deployment",
        "action": "scale-deployment",
        "region": plan.get("region") or row.get("requested_region"),
        "target": {
            "clusterIdentifier": cluster_identifier,
            "namespace": namespace,
            "deploymentName": deployment_name,
        },
        "parameters": {
            "replicas": params.get("replicas"),
            "waitForReady": True,
            "timeoutSeconds": timeout_seconds,
        },
        "impacted_resource": {
            "service": "eks:scale-deployment",
            "arn": f"eks://{plan.get('region') or row.get('requested_region')}/{cluster_identifier}/{namespace}/deployment/{deployment_name}",
            "selection_mode": "ROLLBACK",
        },
    }


def _build_eks_scale_nodegroup_rollback_item(row: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    plan = row.get("rollback_plan_json") or {}
    target = plan.get("target") or {}
    params = plan.get("parameters") or {}
    cluster_identifier = str(target.get("cluster_identifier") or "").strip()
    nodegroup_name = str(target.get("nodegroup_name") or "").strip()
    region = plan.get("region") or row.get("requested_region")
    return {
        "rollbackActionId": str(row.get("action_id") or ""),
        "name": f"rollback_eks_scale_nodegroup_{row.get('sequence_no')}",
        "engine": "rollback",
        "service": "eks:scale-nodegroup",
        "action": "scale-nodegroup",
        "region": region,
        "target": {
            "clusterIdentifier": cluster_identifier,
            "nodegroupName": nodegroup_name,
        },
        "parameters": {
            "min": params.get("min"),
            "max": params.get("max"),
            "desired": params.get("desired"),
            "waitForReady": True,
            "timeoutSeconds": timeout_seconds,
        },
        "impacted_resource": {
            "service": "eks:scale-nodegroup",
            "arn": f"eks://{region}/{cluster_identifier}/nodegroup/{nodegroup_name}",
            "selection_mode": "ROLLBACK",
        },
    }


def _build_dns_set_value_rollback_item(row: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    plan = row.get("rollback_plan_json") or {}
    record_set = dict(plan.get("recordSet") or {})
    record_name = _normalize_route53_record_name(record_set)
    record_type = str(record_set.get("Type") or "").strip().upper()
    return {
        "rollbackActionId": str(row.get("action_id") or ""),
        "name": f"rollback_dns_set_value_{row.get('sequence_no')}",
        "engine": "rollback",
        "service": "dns:set-value",
        "action": "set-value",
        "region": None,
        "target": {
            "hostedZoneId": plan.get("hostedZoneId"),
            "recordName": record_name,
            "recordType": record_type,
        },
        "parameters": {
            "value": _resource_record_value(record_set),
            "timeoutSeconds": timeout_seconds,
        },
        "recordSetAfter": record_set,
        "impacted_resource": {
            "service": "dns:set-value",
            "arn": _record_label(str(plan.get("hostedZoneId") or ""), record_name, record_type),
            "selection_mode": "ROLLBACK",
        },
    }


def _build_dns_set_weight_rollback_item(row: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    plan = row.get("rollback_plan_json") or {}
    record_sets = []
    impacted_resources = []
    for entry in plan.get("recordSets") or []:
        if not isinstance(entry, dict):
            continue
        rrset = dict(entry)
        set_identifier = str(rrset.get("SetIdentifier") or "").strip()
        record_name = _normalize_route53_record_name(rrset)
        record_type = str(rrset.get("Type") or "").strip().upper()
        record_sets.append(
            {
                "setIdentifier": set_identifier,
                "after": rrset,
            }
        )
        impacted_resources.append(
            {
                "service": "dns:set-weight",
                "arn": _record_label(str(plan.get("hostedZoneId") or ""), record_name, record_type, set_identifier),
                "selection_mode": "ROLLBACK",
            }
        )
    return {
        "rollbackActionId": str(row.get("action_id") or ""),
        "name": f"rollback_dns_set_weight_{row.get('sequence_no')}",
        "engine": "rollback",
        "service": "dns:set-weight",
        "action": "set-weight",
        "region": None,
        "target": {
            "hostedZoneId": plan.get("hostedZoneId"),
        },
        "parameters": {
            "value": _weight_assignments_value(
                [
                    {"setIdentifier": entry.get("setIdentifier"), "recordSet": entry.get("after")}
                    for entry in record_sets
                ]
            ),
            "timeoutSeconds": timeout_seconds,
        },
        "recordSets": record_sets,
        "impacted_resources": impacted_resources,
    }


def _build_s3_failover_rollback_item(row: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    plan = row.get("rollback_plan_json") or {}
    route_updates = list(plan.get("routeUpdates") or [])
    target_region = _route_target_region(route_updates)
    return {
        "rollbackActionId": str(row.get("action_id") or ""),
        "name": f"rollback_s3_failover_{row.get('sequence_no')}",
        "engine": "rollback",
        "service": "s3:failover",
        "action": "failover",
        "region": plan.get("region") or row.get("requested_region"),
        "target": {
            "mrapArn": plan.get("mrapArn"),
            "targetRegion": target_region,
            "controlRegion": plan.get("region") or row.get("requested_region"),
        },
        "parameters": {
            "waitForReady": True,
            "timeoutSeconds": timeout_seconds,
        },
        "routesBefore": row.get("after_state_json", {}).get("routes") if isinstance(row.get("after_state_json"), dict) else None,
        "routeUpdates": route_updates,
        "activeRegionBefore": (row.get("after_state_json") or {}).get("activeRegion") if isinstance(row.get("after_state_json"), dict) else None,
        "impacted_resource": {
            "service": "s3:failover",
            "arn": str(plan.get("mrapArn") or ""),
            "selection_mode": "ROLLBACK",
        },
    }


def build_rollback_execution_plan(
    rollback_rows: List[Dict[str, Any]],
    *,
    default_timeout_seconds: int,
) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for row in rollback_rows:
        action_key = f"{row.get('service_name')}:{row.get('action_name')}"
        if action_key == "asg:scale":
            item = _build_asg_rollback_item(row, default_timeout_seconds)
        elif action_key == "eks:scale-deployment":
            item = _build_eks_scale_deployment_rollback_item(row, default_timeout_seconds)
        elif action_key == "eks:scale-nodegroup":
            item = _build_eks_scale_nodegroup_rollback_item(row, default_timeout_seconds)
        elif action_key == "dns:set-value":
            item = _build_dns_set_value_rollback_item(row, default_timeout_seconds)
        elif action_key == "dns:set-weight":
            item = _build_dns_set_weight_rollback_item(row, default_timeout_seconds)
        elif action_key == "s3:failover":
            item = _build_s3_failover_rollback_item(row, default_timeout_seconds)
        else:
            continue
        item["actionRef"] = str(row.get("action_ref") or action_key)
        item["requestedZone"] = row.get("requested_zone")
        items.append(item)

    return {
        "name": f"resilience-rollback-{utc_ts()}",
        "sourceRunId": str(rollback_rows[0].get("run_id") or "") if rollback_rows else None,
        "items": items,
    }


def build_rollback_dry_run_rows(execution_plan: Dict[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    items = list(execution_plan.get("items") or [])
    for index, item in enumerate(items, start=1):
        impacted_many = list(item.get("impacted_resources") or [])
        if not impacted_many and isinstance(item.get("impacted_resource"), dict):
            impacted_many = [item.get("impacted_resource")]
        rows.append(
            [
                str(index),
                str(item.get("actionRef") or item.get("service") or "-"),
                "Rollback",
                _rollback_exec_type(str(item.get("service") or "")),
                str(item.get("region") or "-"),
                str(item.get("requestedZone") or "-"),
                "-",
                _summarize_impacted_resources(impacted_many),
                _format_key_parameters(item),
            ]
        )
    return rows


def _execute_one_rollback_item(
    *,
    session,
    item: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    action_key = str(item.get("service") or "").strip().lower()
    if action_key == "asg:scale":
        return ASGScaleAction().execute_item(
            session=session,
            item=item,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
    if action_key in {"eks:scale-deployment", "eks:scale-nodegroup"}:
        return EKSAction().execute_item(
            session=session,
            item=item,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
    if action_key in {"dns:set-value", "dns:set-weight"}:
        return DNSAction().execute_item(
            session=session,
            item=item,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
    if action_key == "s3:failover":
        return S3FailoverAction().execute_item(
            session=session,
            item=item,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"Unsupported rollback action: {action_key}")


def execute_rollback_plan(
    *,
    session,
    execution_plan: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
    status_updater: Optional[Callable[[str, str, Optional[str], bool], None]] = None,
) -> Dict[str, Any]:
    items = list(execution_plan.get("items") or [])
    item_summaries: List[Dict[str, Any]] = []
    overall_status = "completed"

    for index, item in enumerate(items):
        action_ref = str(item.get("actionRef") or item.get("service") or item.get("name") or "-")
        rollback_action_id = str(item.get("rollbackActionId") or "")
        log_message("INFO", f"Starting rollback action: {action_ref}")
        if status_updater and rollback_action_id:
            status_updater(rollback_action_id, "running", None, False)

        try:
            result = _execute_one_rollback_item(
                session=session,
                item=item,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            result = {
                "name": item.get("name"),
                "status": "failed",
                "reason": f"Unhandled rollback action error: {e}",
                "startTime": None,
                "endTime": _utc_now_iso(),
                "details": {
                    "target": item.get("target"),
                    "parameters": item.get("parameters"),
                },
            }

        item_summaries.append(result)
        status = str(result.get("status") or "failed")
        reason = result.get("reason")
        if status_updater and rollback_action_id:
            status_updater(rollback_action_id, status, reason, status == "completed")
        log_message("INFO", f"Finished rollback action: {action_ref} status={status}")

        if status != "completed":
            overall_status = "failed"
            for skipped_item in items[index + 1 :]:
                skipped_ref = str(skipped_item.get("actionRef") or skipped_item.get("service") or skipped_item.get("name") or "-")
                skipped_summary = {
                    "name": skipped_item.get("name"),
                    "status": "skipped",
                    "reason": f"Skipped because an earlier rollback action failed: {action_ref}",
                    "startTime": None,
                    "endTime": None,
                    "details": {
                        "target": skipped_item.get("target"),
                        "parameters": skipped_item.get("parameters"),
                    },
                }
                item_summaries.append(skipped_summary)
                skipped_action_id = str(skipped_item.get("rollbackActionId") or "")
                if status_updater and skipped_action_id:
                    status_updater(skipped_action_id, "skipped", skipped_summary["reason"], False)
                log_message("WARN", f"Skipping rollback action: {skipped_ref} because a prior rollback action failed.")
            break

    start_times = [entry.get("startTime") for entry in item_summaries if entry.get("startTime")]
    end_times = [entry.get("endTime") for entry in item_summaries if entry.get("endTime")]
    return {
        "rollbackExecutionId": execution_plan.get("name"),
        "sourceRunId": execution_plan.get("sourceRunId"),
        "status": overall_status,
        "reason": None if overall_status == "completed" else "One or more rollback actions failed.",
        "startTime": min(start_times) if start_times else None,
        "endTime": max(end_times) if end_times else None,
        "actions": {
            str(entry.get("name") or ""): {
                "status": entry.get("status"),
                "reason": entry.get("reason"),
                "startTime": entry.get("startTime"),
                "endTime": entry.get("endTime"),
            }
            for entry in item_summaries
        },
        "rollbackExecution": {
            "name": execution_plan.get("name"),
            "sourceRunId": execution_plan.get("sourceRunId"),
            "items": item_summaries,
        },
    }
