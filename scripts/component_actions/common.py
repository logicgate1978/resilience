import re
import time
from datetime import datetime, timezone
from typing import Any, Dict

from component_actions.base import CustomComponentAction
from utility import normalize_service_name


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
        raise ValueError("common:wait requires services[].duration in ISO-8601 format, for example PT2M or PT30S.")

    parts = match.groupdict(default="0")
    total_seconds = (
        int(parts["days"]) * 86400
        + int(parts["hours"]) * 3600
        + int(parts["minutes"]) * 60
        + int(parts["seconds"])
    )
    if total_seconds <= 0:
        raise ValueError("common:wait requires services[].duration to be greater than zero.")
    return total_seconds


class CommonWaitAction(CustomComponentAction):
    service_name = "common"
    action_names = ["wait"]

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
        _ = session
        duration = svc.get("duration")
        if duration is None or str(duration).strip() == "":
            raise ValueError("common:wait requires services[].duration (for example PT2M).")

        duration_seconds = _parse_iso_duration_seconds(duration)
        item_timeout_seconds = max(duration_seconds, int(default_timeout_seconds))

        return {
            "name": f"a_common_wait_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:{str(svc.get('action') or '').strip().lower()}",
            "action": "wait",
            "description": f"Wait for {duration}",
            "target": {},
            "parameters": {
                "duration": str(duration).strip(),
                "durationSeconds": duration_seconds,
                "timeoutSeconds": item_timeout_seconds,
                "useFis": False,
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
        _ = session
        _ = poll_seconds
        _ = timeout_seconds
        started_at = _utc_now_iso()
        params = item["parameters"]
        duration_seconds = int(params["durationSeconds"])

        time.sleep(duration_seconds)

        ended_at = _utc_now_iso()
        return {
            "name": item["name"],
            "status": "completed",
            "reason": None,
            "startTime": started_at,
            "endTime": ended_at,
            "details": {
                "parameters": params,
            },
        }
