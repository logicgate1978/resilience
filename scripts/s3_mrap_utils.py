from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from utility import get_account_id, resolve_service_region


MRAP_MANAGEMENT_REGION = "us-west-2"
MRAP_FAILOVER_CONTROL_REGIONS = (
    "us-east-1",
    "us-west-2",
    "ap-southeast-2",
    "ap-northeast-1",
    "eu-west-1",
)
MRAP_DEFAULT_CONTROL_REGION = "eu-west-1"


def resolve_mrap_control_region(manifest: Dict[str, Any], svc: Dict[str, Any], fallback_region: Optional[str] = None) -> str:
    region = resolve_service_region(manifest, svc, default=None)
    text = str(region or "").strip()
    if text:
        return text
    _ = fallback_region
    return MRAP_DEFAULT_CONTROL_REGION


def validate_mrap_control_region(region: str) -> None:
    if region not in MRAP_FAILOVER_CONTROL_REGIONS:
        allowed = ", ".join(MRAP_FAILOVER_CONTROL_REGIONS)
        raise ValueError(
            "s3:failover requires region to be one of the S3 MRAP failover-control regions: "
            f"{allowed}. Received: {region}"
        )


def get_mrap_target(svc: Dict[str, Any]) -> Dict[str, Any]:
    target = svc.get("target")
    if not isinstance(target, dict):
        raise ValueError("s3:failover requires services[].target to be an object.")
    return target


def get_mrap_target_region(svc: Dict[str, Any]) -> str:
    target = get_mrap_target(svc)
    value = str(target.get("target_region") or "").strip()
    if not value:
        raise ValueError("s3:failover requires services[].target.target_region.")
    return value


def get_mrap_selector(target: Dict[str, Any]) -> Tuple[str, str]:
    selectors = {
        "mrap_name": str(target.get("mrap_name") or "").strip(),
        "mrap_alias": str(target.get("mrap_alias") or "").strip(),
        "mrap_arn": str(target.get("mrap_arn") or "").strip(),
    }
    present = [(key, value) for key, value in selectors.items() if value]
    if len(present) != 1:
        raise ValueError(
            "s3:failover requires exactly one of services[].target.mrap_name, "
            "services[].target.mrap_alias, or services[].target.mrap_arn."
        )
    return present[0]


def _mrap_arn_from_alias(account_id: str, alias: str) -> str:
    return f"arn:aws:s3::{account_id}:accesspoint/{alias}"


def _mrap_alias_from_arn(mrap_arn: str) -> str:
    text = str(mrap_arn or "").strip()
    marker = "accesspoint/"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1]


def _normalize_routes(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(resp.get("Routes") or [])


def _management_client(session):
    return session.client("s3control", region_name=MRAP_MANAGEMENT_REGION)


def _routes_client(session, control_region: str):
    return session.client("s3control", region_name=control_region)


def resolve_mrap(session, *, selector_key: str, selector_value: str) -> Dict[str, Any]:
    s3control = _management_client(session)
    sts = session.client("sts")
    account_id = get_account_id(sts)

    if selector_key == "mrap_name":
        resp = s3control.get_multi_region_access_point(
            AccountId=account_id,
            Name=selector_value,
        )
        access_point = resp.get("AccessPoint") or {}
        alias = str(access_point.get("Alias") or "").strip()
        if not alias:
            raise ValueError(f"Could not resolve alias for Multi-Region Access Point '{selector_value}'.")
        return {
            "account_id": account_id,
            "name": str(access_point.get("Name") or selector_value).strip(),
            "alias": alias,
            "arn": _mrap_arn_from_alias(account_id, alias),
            "status": str(access_point.get("Status") or "").strip(),
            "regions": [str(region.get("BucketRegion") or "").strip() for region in (access_point.get("Regions") or [])],
        }

    paginator = s3control.get_paginator("list_multi_region_access_points")
    for page in paginator.paginate(AccountId=account_id):
        for access_point in page.get("AccessPoints") or []:
            alias = str(access_point.get("Alias") or "").strip()
            name = str(access_point.get("Name") or "").strip()
            arn = _mrap_arn_from_alias(account_id, alias) if alias else ""
            if selector_key == "mrap_alias" and alias == selector_value:
                return {
                    "account_id": account_id,
                    "name": name,
                    "alias": alias,
                    "arn": arn,
                    "status": str(access_point.get("Status") or "").strip(),
                    "regions": [str(region.get("BucketRegion") or "").strip() for region in (access_point.get("Regions") or [])],
                }
            if selector_key == "mrap_arn" and arn == selector_value:
                return {
                    "account_id": account_id,
                    "name": name,
                    "alias": alias,
                    "arn": arn,
                    "status": str(access_point.get("Status") or "").strip(),
                    "regions": [str(region.get("BucketRegion") or "").strip() for region in (access_point.get("Regions") or [])],
                }

    raise ValueError(f"Multi-Region Access Point not found for {selector_key}='{selector_value}'.")


def get_mrap_routes(session, *, control_region: str, account_id: str, mrap_arn: str) -> List[Dict[str, Any]]:
    validate_mrap_control_region(control_region)
    resp = _routes_client(session, control_region).get_multi_region_access_point_routes(
        AccountId=account_id,
        Mrap=mrap_arn,
    )
    return _normalize_routes(resp)


def analyze_failover_routes(routes: List[Dict[str, Any]], target_region: str) -> Dict[str, Any]:
    if not routes:
        raise ValueError("The selected Multi-Region Access Point does not have any routes configured.")

    regions = [str(route.get("Region") or "").strip() for route in routes if str(route.get("Region") or "").strip()]
    if target_region not in regions:
        raise ValueError(
            f"Target region '{target_region}' does not exist in the Multi-Region Access Point route configuration."
        )

    if len(regions) < 2:
        raise ValueError("The selected Multi-Region Access Point has fewer than two regions, so failover is not meaningful.")

    active_regions: List[str] = []
    route_updates: List[Dict[str, Any]] = []
    before: List[Dict[str, Any]] = []

    for route in routes:
        region = str(route.get("Region") or "").strip()
        bucket = str(route.get("Bucket") or "").strip()
        dial = route.get("TrafficDialPercentage")
        if region == "":
            continue
        if dial not in (0, 100):
            raise ValueError(
                "s3:failover only supports active/passive MRAP routes with traffic dials of 0 or 100. "
                f"Found {dial!r} for region {region}."
            )
        if dial == 100:
            active_regions.append(region)

        before.append(
            {
                "Bucket": bucket,
                "Region": region,
                "TrafficDialPercentage": dial,
            }
        )
        route_updates.append(
            {
                "Bucket": bucket,
                "Region": region,
                "TrafficDialPercentage": 100 if region == target_region else 0,
            }
        )

    if len(active_regions) != 1:
        raise ValueError(
            "s3:failover requires the MRAP to be in active/passive mode with exactly one active region. "
            f"Current active regions: {', '.join(active_regions) if active_regions else 'none'}."
        )

    active_region = active_regions[0]
    if active_region == target_region:
        raise ValueError(f"Target region '{target_region}' is already the active region for the Multi-Region Access Point.")

    return {
        "activeRegion": active_region,
        "routeUpdates": route_updates,
        "routesBefore": before,
    }


def wait_for_mrap_routes(
    session,
    *,
    control_region: str,
    account_id: str,
    mrap_arn: str,
    target_region: str,
    poll_seconds: int = 10,
    timeout_seconds: int = 300,
) -> List[Dict[str, Any]]:
    deadline = time.time() + timeout_seconds
    while True:
        routes = get_mrap_routes(
            session,
            control_region=control_region,
            account_id=account_id,
            mrap_arn=mrap_arn,
        )
        matched_target = False
        other_active = False
        for route in routes:
            region = str(route.get("Region") or "").strip()
            dial = route.get("TrafficDialPercentage")
            if region == target_region and dial == 100:
                matched_target = True
            elif region != target_region and dial != 0:
                other_active = True
        if matched_target and not other_active:
            return routes
        if time.time() > deadline:
            raise TimeoutError(
                f"Multi-Region Access Point routes did not converge to active region '{target_region}' "
                f"within {timeout_seconds} seconds."
            )
        time.sleep(max(1, poll_seconds))
