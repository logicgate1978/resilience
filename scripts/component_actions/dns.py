from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from component_actions.base import CustomComponentAction
from dns_utils import (
    clone_rrset,
    list_matching_record_sets,
    normalize_record_name,
    parse_weight_assignments,
    resolve_hosted_zone_id,
    wait_for_change_insync,
)
from utility import normalize_service_name


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_target_cfg(svc: Dict[str, Any]) -> Dict[str, Any]:
    target = svc.get("target")
    if not isinstance(target, dict):
        raise ValueError("dns actions require services[].target to be an object.")
    return target


def _require_target_field(target: Dict[str, Any], key: str) -> str:
    value = str(target.get(key) or "").strip()
    if not value:
        raise ValueError(f"dns actions require services[].target.{key}.")
    return value


def _record_label(hosted_zone: str, record_name: str, record_type: str, set_identifier: str = "") -> str:
    suffix = f"#{set_identifier}" if set_identifier else ""
    return f"route53://{hosted_zone}/{record_name}/{record_type}{suffix}"


class DNSAction(CustomComponentAction):
    service_name = "dns"
    action_names = ["set-value", "set-weight"]

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
        _ = region
        route53 = session.client("route53")
        target = _get_target_cfg(svc)
        action = str(svc.get("action") or "").strip().lower()
        hosted_zone_name = _require_target_field(target, "hosted_zone")
        record_name = _require_target_field(target, "record_name")
        record_type = _require_target_field(target, "record_type").upper()
        hosted_zone_id = resolve_hosted_zone_id(route53, hosted_zone_name)
        matching_rrsets = list_matching_record_sets(
            route53,
            hosted_zone_id=hosted_zone_id,
            record_name=record_name,
            record_type=record_type,
        )
        if not matching_rrsets:
            raise ValueError(
                f"dns:{action} did not find any Route 53 record sets for "
                f"{hosted_zone_name} {record_name} {record_type}."
            )

        if action == "set-value":
            if len(matching_rrsets) != 1:
                raise ValueError(
                    "dns:set-value requires exactly one matching record set. "
                    "If multiple policy records exist, use a policy-specific action instead."
                )
            rrset = matching_rrsets[0]
            if rrset.get("SetIdentifier"):
                raise ValueError("dns:set-value currently supports simple routing records only, not policy records with SetIdentifier.")
            if rrset.get("AliasTarget"):
                raise ValueError("dns:set-value currently supports non-alias records only.")

            value = str(svc.get("value") or "").strip()
            if not value:
                raise ValueError("dns:set-value requires services[].value.")

            updated_rrset = clone_rrset(rrset)
            updated_rrset["ResourceRecords"] = [{"Value": value}]

            return {
                "name": f"a_dns_set_value_{index}",
                "engine": "custom",
                "service": f"{normalize_service_name(svc.get('name'))}:{action}",
                "action": action,
                "description": f"Set Route 53 record value for {record_name} to {value}",
                "target": {
                    "hostedZone": hosted_zone_name,
                    "hostedZoneId": hosted_zone_id,
                    "recordName": normalize_record_name(record_name),
                    "recordType": record_type,
                },
                "parameters": {
                    "value": value,
                    "timeoutSeconds": int(default_timeout_seconds),
                },
                "recordSetBefore": rrset,
                "recordSetAfter": updated_rrset,
                "impacted_resource": {
                    "service": f"dns:{action}",
                    "arn": _record_label(hosted_zone_name, normalize_record_name(record_name), record_type),
                    "selection_mode": "CUSTOM",
                },
            }

        assignments = parse_weight_assignments(svc.get("value"))
        rrsets_by_identifier = {str(rrset.get("SetIdentifier") or ""): rrset for rrset in matching_rrsets}
        missing = [identifier for identifier in assignments if identifier not in rrsets_by_identifier]
        if missing:
            raise ValueError(
                "dns:set-weight could not find matching weighted records for set identifier(s): "
                + ", ".join(sorted(missing))
            )

        changed_rrsets: List[Dict[str, Any]] = []
        impacted_resources: List[Dict[str, str]] = []
        for set_identifier, weight in assignments.items():
            rrset = rrsets_by_identifier[set_identifier]
            if not rrset.get("SetIdentifier"):
                raise ValueError("dns:set-weight requires weighted records with SetIdentifier.")
            if "Weight" not in rrset:
                raise ValueError(
                    f"dns:set-weight requires weighted Route 53 records. "
                    f"Record with set identifier '{set_identifier}' does not have a Weight field."
                )
            updated_rrset = clone_rrset(rrset)
            updated_rrset["Weight"] = weight
            changed_rrsets.append(
                {
                    "setIdentifier": set_identifier,
                    "before": rrset,
                    "after": updated_rrset,
                }
            )
            impacted_resources.append(
                {
                    "service": f"dns:{action}",
                    "arn": _record_label(hosted_zone_name, normalize_record_name(record_name), record_type, set_identifier),
                    "selection_mode": "CUSTOM",
                }
            )

        return {
            "name": f"a_dns_set_weight_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:{action}",
            "action": action,
            "description": f"Set Route 53 weights for {record_name}",
            "target": {
                "hostedZone": hosted_zone_name,
                "hostedZoneId": hosted_zone_id,
                "recordName": normalize_record_name(record_name),
                "recordType": record_type,
            },
            "parameters": {
                "assignments": assignments,
                "timeoutSeconds": int(default_timeout_seconds),
            },
            "recordSets": changed_rrsets,
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
        route53 = session.client("route53")
        started_at = _utc_now_iso()
        target = item["target"]
        action = str(item.get("action") or "").strip().lower()
        effective_timeout_seconds = int(item.get("parameters", {}).get("timeoutSeconds") or timeout_seconds)

        try:
            if action == "set-value":
                change_resp = route53.change_resource_record_sets(
                    HostedZoneId=target["hostedZoneId"],
                    ChangeBatch={
                        "Comment": f"resilience-framework dns:set-value for {target['recordName']}",
                        "Changes": [
                            {
                                "Action": "UPSERT",
                                "ResourceRecordSet": item["recordSetAfter"],
                            }
                        ],
                    },
                )
            else:
                changes = []
                for entry in item.get("recordSets") or []:
                    changes.append(
                        {
                            "Action": "UPSERT",
                            "ResourceRecordSet": entry["after"],
                        }
                    )
                change_resp = route53.change_resource_record_sets(
                    HostedZoneId=target["hostedZoneId"],
                    ChangeBatch={
                        "Comment": f"resilience-framework dns:set-weight for {target['recordName']}",
                        "Changes": changes,
                    },
                )
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"Route 53 change_resource_record_sets error: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": target,
                    "parameters": item.get("parameters"),
                },
            }

        change_info = change_resp.get("ChangeInfo", {})
        change_id = str(change_info.get("Id") or "")

        try:
            final_change = wait_for_change_insync(
                route53,
                change_id,
                poll_seconds=max(1, poll_seconds),
                timeout_seconds=effective_timeout_seconds,
            )
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"Route 53 change did not reach INSYNC: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": target,
                    "parameters": item.get("parameters"),
                    "changeInfo": change_info,
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
                "changeInfo": final_change,
            },
        }
