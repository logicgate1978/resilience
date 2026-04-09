from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from component_actions.base import CustomComponentAction
from s3_mrap_utils import (
    analyze_failover_routes,
    get_mrap_selector,
    get_mrap_target,
    get_mrap_target_region,
    get_mrap_routes,
    resolve_mrap,
    resolve_mrap_control_region,
    validate_mrap_control_region,
    wait_for_mrap_routes,
)
from utility import normalize_service_name


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _optional_int(source: Dict[str, Any], key: str, default: int) -> int:
    value = source.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


class S3FailoverAction(CustomComponentAction):
    service_name = "s3"
    action_names = ["failover"]

    def default_region(self, *, manifest: Dict[str, Any], svc: Dict[str, Any], fallback_region: str | None) -> str | None:
        _ = fallback_region
        return resolve_mrap_control_region(manifest, svc, fallback_region=None)

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
        target = get_mrap_target(svc)
        control_region = resolve_mrap_control_region(manifest, svc, fallback_region=region)
        validate_mrap_control_region(control_region)

        selector_key, selector_value = get_mrap_selector(target)
        target_region = get_mrap_target_region(svc)
        wait_for_ready = _optional_bool(svc, "wait_for_ready", True)
        item_timeout_seconds = _optional_int(svc, "timeout_seconds", default_timeout_seconds)

        mrap = resolve_mrap(session, selector_key=selector_key, selector_value=selector_value)
        if str(mrap.get("status") or "").strip().upper() != "READY":
            raise ValueError(
                f"s3:failover requires the Multi-Region Access Point to be READY. Current status: {mrap.get('status') or 'unknown'}."
            )

        routes = get_mrap_routes(
            session,
            control_region=control_region,
            account_id=mrap["account_id"],
            mrap_arn=mrap["arn"],
        )
        analysis = analyze_failover_routes(routes, target_region)

        return {
            "name": f"a_s3_failover_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:failover",
            "action": "failover",
            "description": f"Fail over S3 MRAP {mrap['name'] or mrap['alias']} to {target_region}",
            "target": {
                "mrapName": mrap["name"],
                "mrapAlias": mrap["alias"],
                "mrapArn": mrap["arn"],
                "targetRegion": target_region,
                "controlRegion": control_region,
            },
            "parameters": {
                "waitForReady": wait_for_ready,
                "timeoutSeconds": item_timeout_seconds,
            },
            "routesBefore": analysis["routesBefore"],
            "routeUpdates": analysis["routeUpdates"],
            "activeRegionBefore": analysis["activeRegion"],
            "impacted_resource": {
                "service": "s3:failover",
                "arn": mrap["arn"],
                "selection_mode": "CUSTOM",
            },
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
        target = item["target"]
        params = item["parameters"]
        effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)
        control_region = target["controlRegion"]
        account_id = target["mrapArn"].split(":")[4]

        s3control = session.client("s3control", region_name=control_region)

        try:
            s3control.submit_multi_region_access_point_routes(
                AccountId=account_id,
                Mrap=target["mrapArn"],
                RouteUpdates=item["routeUpdates"],
            )
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"S3 Control submit_multi_region_access_point_routes error: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": target,
                    "parameters": params,
                    "routesBefore": item.get("routesBefore"),
                    "routeUpdates": item.get("routeUpdates"),
                },
            }

        routes_after = None
        if params.get("waitForReady"):
            try:
                routes_after = wait_for_mrap_routes(
                    session,
                    control_region=control_region,
                    account_id=account_id,
                    mrap_arn=target["mrapArn"],
                    target_region=target["targetRegion"],
                    poll_seconds=max(1, poll_seconds),
                    timeout_seconds=effective_timeout_seconds,
                )
            except Exception as e:
                ended_at = _utc_now_iso()
                return {
                    "name": item["name"],
                    "status": "failed",
                    "reason": f"S3 MRAP routes did not converge after failover: {e}",
                    "startTime": started_at,
                    "endTime": ended_at,
                    "details": {
                        "target": target,
                        "parameters": params,
                        "routesBefore": item.get("routesBefore"),
                        "routeUpdates": item.get("routeUpdates"),
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
                "parameters": params,
                "activeRegionBefore": item.get("activeRegionBefore"),
                "routesBefore": item.get("routesBefore"),
                "routeUpdates": item.get("routeUpdates"),
                "routesAfter": routes_after,
            },
        }
