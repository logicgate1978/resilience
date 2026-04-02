from __future__ import annotations

import copy
import time
from typing import Any, Dict, List


def normalize_record_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    return text if text.endswith(".") else f"{text}."


def strip_hosted_zone_id(hosted_zone_id: str) -> str:
    text = str(hosted_zone_id or "").strip()
    if text.startswith("/hostedzone/"):
        return text.split("/", 2)[-1]
    return text


def resolve_hosted_zone_id(route53, hosted_zone_name: str) -> str:
    dns_name = normalize_record_name(hosted_zone_name)
    if not dns_name:
        raise ValueError("dns actions require target.hosted_zone.")

    resp = route53.list_hosted_zones_by_name(DNSName=dns_name)
    for zone in resp.get("HostedZones", []):
        if normalize_record_name(zone.get("Name", "")) == dns_name:
            return strip_hosted_zone_id(zone.get("Id", ""))

    raise ValueError(f"Hosted zone '{hosted_zone_name}' was not found.")


def list_matching_record_sets(
    route53,
    *,
    hosted_zone_id: str,
    record_name: str,
    record_type: str,
) -> List[Dict[str, Any]]:
    normalized_name = normalize_record_name(record_name)
    normalized_type = str(record_type or "").strip().upper()
    if not normalized_name or not normalized_type:
        raise ValueError("dns actions require target.record_name and target.record_type.")

    paginator = route53.get_paginator("list_resource_record_sets")
    matches: List[Dict[str, Any]] = []
    for page in paginator.paginate(
        HostedZoneId=strip_hosted_zone_id(hosted_zone_id),
        StartRecordName=normalized_name,
        StartRecordType=normalized_type,
    ):
        for rrset in page.get("ResourceRecordSets", []):
            rr_name = normalize_record_name(rrset.get("Name", ""))
            rr_type = str(rrset.get("Type", "")).strip().upper()
            if rr_name != normalized_name or rr_type != normalized_type:
                if matches:
                    return matches
                continue
            matches.append(rrset)
    return matches


def parse_weight_assignments(value: Any) -> Dict[str, int]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("dns:set-weight requires a non-empty value like 'primary=0,secondary=100'.")

    assignments: Dict[str, int] = {}
    for item in text.split(","):
        part = item.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                "dns:set-weight requires value entries in 'set_identifier=weight' format, separated by commas."
            )
        identifier, raw_weight = part.split("=", 1)
        set_identifier = identifier.strip()
        if not set_identifier:
            raise ValueError("dns:set-weight requires a non-empty set identifier before '='.")
        try:
            weight = int(str(raw_weight).strip())
        except Exception:
            raise ValueError(f"dns:set-weight requires an integer weight for set identifier '{set_identifier}'.")
        if weight < 0 or weight > 255:
            raise ValueError(f"dns:set-weight requires weights between 0 and 255. Invalid value for '{set_identifier}'.")
        assignments[set_identifier] = weight

    if not assignments:
        raise ValueError("dns:set-weight requires at least one set_identifier=weight assignment.")

    return assignments


def wait_for_change_insync(route53, change_id: str, *, poll_seconds: int = 5, timeout_seconds: int = 300) -> Dict[str, Any]:
    started = time.time()
    normalized_change_id = str(change_id or "").strip()
    if normalized_change_id.startswith("/change/"):
        normalized_change_id = normalized_change_id.split("/", 2)[-1]

    while True:
        resp = route53.get_change(Id=normalized_change_id)
        info = resp.get("ChangeInfo", {})
        if info.get("Status") == "INSYNC":
            return info
        if time.time() - started > timeout_seconds:
            raise TimeoutError(f"Route 53 change {normalized_change_id} did not reach INSYNC within {timeout_seconds} seconds.")
        time.sleep(max(1, poll_seconds))


def clone_rrset(rrset: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(rrset)
