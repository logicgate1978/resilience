import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from component_actions.base import CustomComponentAction
from resource import collect_service_resource_arns
from utility import normalize_service_name, resolve_service_zone


_ISO_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
    r")?$",
    re.IGNORECASE,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_duration_seconds(value: Any) -> int:
    text = str(value or "").strip().upper()
    match = _ISO_DURATION_RE.match(text)
    if not match:
        raise ValueError("ec2:stop requires services[].duration in ISO-8601 format, for example PT2M or PT30S.")

    parts = match.groupdict(default="0")
    total_seconds = (
        int(parts["days"]) * 86400
        + int(parts["hours"]) * 3600
        + int(parts["minutes"]) * 60
        + int(parts["seconds"])
    )
    if total_seconds <= 0:
        raise ValueError("ec2:stop requires services[].duration to be greater than zero.")
    return total_seconds


def _instance_id_from_arn(arn: str) -> str:
    marker = "instance/"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


class EC2Action(CustomComponentAction):
    service_name = "ec2"
    action_names = ["stop", "reboot", "terminate"]

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
        arns = collect_service_resource_arns(
            svc,
            session=session,
            region=region,
            zone=resolve_service_zone(manifest, svc),
        )
        if not arns:
            raise ValueError(f"ec2:{action} did not resolve any EC2 instances from the manifest selection.")

        instance_ids = []
        impacted_resources = []
        for arn in arns:
            instance_id = _instance_id_from_arn(arn)
            if not instance_id:
                continue
            instance_ids.append(instance_id)
            impacted_resources.append(
                {
                    "service": f"ec2:{action}",
                    "arn": arn,
                    "selection_mode": "CUSTOM",
                }
            )

        if not instance_ids:
            raise ValueError(f"ec2:{action} could not extract EC2 instance IDs from the resolved ARNs.")

        parameters: Dict[str, Any] = {
            "timeoutSeconds": int(default_timeout_seconds),
            "useFis": False,
        }
        description = f"{action.title()} {len(instance_ids)} EC2 instance(s)"
        if action == "stop":
            duration = svc.get("duration")
            if duration is None or str(duration).strip() == "":
                raise ValueError("ec2:stop requires services[].duration (for example PT2M).")
            duration_seconds = _parse_iso_duration_seconds(duration)
            parameters["duration"] = str(duration).strip()
            parameters["durationSeconds"] = duration_seconds
            parameters["timeoutSeconds"] = max(duration_seconds, int(default_timeout_seconds))
            description = (
                f"Stop {len(instance_ids)} EC2 instance(s), wait {parameters['duration']}, "
                "then start them again"
            )

        return {
            "name": f"a_ec2_{action}_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:{action}",
            "action": action,
            "description": description,
            "target": {
                "instanceIds": instance_ids,
            },
            "parameters": parameters,
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
        _ = poll_seconds
        started_at = _utc_now_iso()
        action = str(item.get("action") or "").strip().lower()
        params = dict(item.get("parameters") or {})
        instance_ids = list((item.get("target") or {}).get("instanceIds") or [])
        region = item["region"]
        ec2 = session.client("ec2", region_name=region)
        effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)

        try:
            describe_before = ec2.describe_instances(InstanceIds=instance_ids)
            original_states = {
                inst["InstanceId"]: str((inst.get("State") or {}).get("Name") or "")
                for reservation in describe_before.get("Reservations", [])
                for inst in reservation.get("Instances", [])
                if inst.get("InstanceId")
            }

            if action == "stop":
                ec2.stop_instances(InstanceIds=instance_ids)
                stopped_waiter = ec2.get_waiter("instance_stopped")
                stopped_waiter.wait(
                    InstanceIds=instance_ids,
                    WaiterConfig={"Delay": 15, "MaxAttempts": max(1, effective_timeout_seconds // 15)},
                )
                time.sleep(int(params["durationSeconds"]))
                ec2.start_instances(InstanceIds=instance_ids)
                running_waiter = ec2.get_waiter("instance_running")
                running_waiter.wait(
                    InstanceIds=instance_ids,
                    WaiterConfig={"Delay": 15, "MaxAttempts": max(1, effective_timeout_seconds // 15)},
                )
            elif action == "reboot":
                ec2.reboot_instances(InstanceIds=instance_ids)
                status_waiter = ec2.get_waiter("instance_status_ok")
                status_waiter.wait(
                    InstanceIds=instance_ids,
                    WaiterConfig={"Delay": 15, "MaxAttempts": max(1, effective_timeout_seconds // 15)},
                )
            elif action == "terminate":
                ec2.terminate_instances(InstanceIds=instance_ids)
                terminated_waiter = ec2.get_waiter("instance_terminated")
                terminated_waiter.wait(
                    InstanceIds=instance_ids,
                    WaiterConfig={"Delay": 15, "MaxAttempts": max(1, effective_timeout_seconds // 15)},
                )
            else:
                raise ValueError(f"Unsupported custom EC2 action: {action}")
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"EC2 API error during ec2:{action}: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": item.get("target"),
                    "parameters": params,
                },
            }

        try:
            if action == "terminate":
                final_status = {instance_id: "terminated" for instance_id in instance_ids}
            else:
                describe_after = ec2.describe_instances(InstanceIds=instance_ids)
                final_status = {
                    inst["InstanceId"]: str((inst.get("State") or {}).get("Name") or "")
                    for reservation in describe_after.get("Reservations", [])
                    for inst in reservation.get("Instances", [])
                    if inst.get("InstanceId")
                }
        except Exception as e:
            final_status = {"error": f"Unable to read final instance state: {e}"}

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
                "originalStates": original_states,
                "finalStatus": final_status,
            },
        }
