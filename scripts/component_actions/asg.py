from datetime import datetime, timezone
from typing import Any, Dict, List

from component_actions.base import CustomComponentAction
from resource import collect_service_resource_arns
from utility import normalize_service_name


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_parameters_cfg(svc: Dict[str, Any]) -> Dict[str, Any]:
    params = svc.get("parameters")
    if not isinstance(params, dict):
        raise ValueError("asg:scale requires services[].parameters to be an object.")
    return params


def _require_int(source: Dict[str, Any], key: str, field_name: str) -> int:
    value = source.get(key)
    try:
        parsed = int(value)
    except Exception:
        raise ValueError(f"asg:scale requires integer {field_name}.")
    if parsed < 0:
        raise ValueError(f"asg:scale requires non-negative {field_name}.")
    return parsed


def _optional_int(source: Dict[str, Any], key: str, default: int) -> int:
    value = source.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _optional_bool(source: Dict[str, Any], key: str, default: bool) -> bool:
    value = source.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def _asg_name_from_arn(arn: str) -> str:
    marker = "autoScalingGroupName/"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


def _count_active_instances(instances: List[Dict[str, Any]]) -> int:
    active_states = {"Pending", "Pending:Wait", "Pending:Proceed", "InService", "Standby", "Warmed:Pending", "Warmed:Running"}
    return sum(1 for inst in instances if str(inst.get("LifecycleState") or "") in active_states)


def _count_in_service_instances(instances: List[Dict[str, Any]]) -> int:
    return sum(1 for inst in instances if str(inst.get("LifecycleState") or "") == "InService")


class ASGScaleAction(CustomComponentAction):
    service_name = "asg"
    action_names = ["scale"]

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
        params = _get_parameters_cfg(svc)
        max_size = _require_int(params, "max", "services[].parameters.max")
        min_size = _optional_int(params, "min", 0)
        desired_capacity = _optional_int(params, "desired", max_size)
        wait_for_ready = _optional_bool(params, "wait_for_ready", True)
        item_timeout_seconds = _optional_int(params, "timeout_seconds", default_timeout_seconds)

        if min_size > max_size:
            raise ValueError("asg:scale requires services[].parameters.min to be <= max.")
        if desired_capacity < min_size or desired_capacity > max_size:
            raise ValueError("asg:scale requires desired to be between min and max.")

        arns = collect_service_resource_arns(
            svc,
            session=session,
            region=region,
            zone=None,
        )
        if not arns:
            raise ValueError("asg:scale did not resolve any Auto Scaling Groups from the manifest selection.")

        group_names = []
        impacted_resources = []
        for arn in arns:
            name = _asg_name_from_arn(arn)
            if not name:
                continue
            group_names.append(name)
            impacted_resources.append(
                {
                    "service": "asg:scale",
                    "arn": arn,
                    "selection_mode": "CUSTOM",
                }
            )

        if not group_names:
            raise ValueError("asg:scale could not extract Auto Scaling Group names from the resolved ARNs.")

        return {
            "name": f"a_asg_scale_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:{str(svc.get('action') or '').strip().lower()}",
            "action": "scale",
            "description": f"Scale {len(group_names)} Auto Scaling Group(s) to min={min_size}, max={max_size}, desired={desired_capacity}",
            "target": {
                "groupNames": group_names,
            },
            "parameters": {
                "min": min_size,
                "max": max_size,
                "desired": desired_capacity,
                "waitForReady": wait_for_ready,
                "timeoutSeconds": item_timeout_seconds,
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
        import time

        started_at = _utc_now_iso()
        params = item["parameters"]
        group_names = list(item["target"]["groupNames"])
        target_min = int(params["min"])
        target_max = int(params["max"])
        target_desired = int(params["desired"])
        wait_for_ready = bool(params["waitForReady"])
        effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)
        autoscaling = session.client("autoscaling", region_name=item["region"])

        try:
            before_resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=group_names)
            before_groups = {g["AutoScalingGroupName"]: g for g in before_resp.get("AutoScalingGroups", [])}
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"Auto Scaling API error while reading groups: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {"target": item["target"], "parameters": params},
            }

        original_sizes = {
            name: {
                "min": int(group.get("MinSize") or 0),
                "max": int(group.get("MaxSize") or 0),
                "desired": int(group.get("DesiredCapacity") or 0),
            }
            for name, group in before_groups.items()
        }

        try:
            for group_name in group_names:
                autoscaling.update_auto_scaling_group(
                    AutoScalingGroupName=group_name,
                    MinSize=target_min,
                    MaxSize=target_max,
                    DesiredCapacity=target_desired,
                )
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"Auto Scaling API error while updating groups: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": item["target"],
                    "parameters": params,
                    "originalSizes": original_sizes,
                },
            }

        last_observed: Dict[str, Any] = {}
        if wait_for_ready:
            deadline = time.time() + effective_timeout_seconds
            while True:
                try:
                    resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=group_names)
                except Exception as e:
                    ended_at = _utc_now_iso()
                    return {
                        "name": item["name"],
                        "status": "failed",
                        "reason": f"Auto Scaling API error while waiting for scaling readiness: {e}",
                        "startTime": started_at,
                        "endTime": ended_at,
                        "details": {
                            "target": item["target"],
                            "parameters": params,
                            "originalSizes": original_sizes,
                        },
                    }

                ready = True
                last_observed = {}
                groups_by_name = {g["AutoScalingGroupName"]: g for g in resp.get("AutoScalingGroups", [])}
                for group_name in group_names:
                    group = groups_by_name.get(group_name)
                    if not group:
                        ready = False
                        last_observed[group_name] = {"error": "Group not found during readiness polling."}
                        continue

                    instances = group.get("Instances") or []
                    active_count = _count_active_instances(instances)
                    in_service_count = _count_in_service_instances(instances)
                    snapshot = {
                        "min": int(group.get("MinSize") or 0),
                        "max": int(group.get("MaxSize") or 0),
                        "desired": int(group.get("DesiredCapacity") or 0),
                        "activeInstances": active_count,
                        "inServiceInstances": in_service_count,
                    }
                    last_observed[group_name] = snapshot

                    if (
                        snapshot["min"] != target_min
                        or snapshot["max"] != target_max
                        or snapshot["desired"] != target_desired
                        or active_count != target_desired
                        or in_service_count != target_desired
                    ):
                        ready = False

                if ready:
                    break

                if time.time() > deadline:
                    ended_at = _utc_now_iso()
                    return {
                        "name": item["name"],
                        "status": "failed",
                        "reason": (
                            f"Timed out waiting for Auto Scaling Groups to reach "
                            f"min={target_min}, max={target_max}, desired={target_desired}."
                        ),
                        "startTime": started_at,
                        "endTime": ended_at,
                        "details": {
                            "target": item["target"],
                            "parameters": params,
                            "originalSizes": original_sizes,
                            "lastObservedStatus": last_observed,
                        },
                    }

                time.sleep(max(1, poll_seconds))

        try:
            after_resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=group_names)
            after_groups = {g["AutoScalingGroupName"]: g for g in after_resp.get("AutoScalingGroups", [])}
            final_status = {
                name: {
                    "min": int(group.get("MinSize") or 0),
                    "max": int(group.get("MaxSize") or 0),
                    "desired": int(group.get("DesiredCapacity") or 0),
                    "activeInstances": _count_active_instances(group.get("Instances") or []),
                    "inServiceInstances": _count_in_service_instances(group.get("Instances") or []),
                }
                for name, group in after_groups.items()
            }
        except Exception as e:
            final_status = {
                "error": f"Unable to read final Auto Scaling Group state: {e}",
            }

        ended_at = _utc_now_iso()
        return {
            "name": item["name"],
            "status": "completed",
            "reason": None,
            "startTime": started_at,
            "endTime": ended_at,
            "details": {
                "target": item["target"],
                "parameters": params,
                "originalSizes": original_sizes,
                "finalStatus": final_status,
            },
        }
