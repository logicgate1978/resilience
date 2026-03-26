from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Dict, List, Optional, Tuple

from utility import normalize_service_name, utc_ts

from .base import CustomComponentAction


def _load_action_modules() -> None:
    package_name = __package__
    package = importlib.import_module(package_name)

    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name in ("base", "registry", "__init__", "k8s_auth"):
            continue
        importlib.import_module(f"{package_name}.{module_info.name}")


def _get_action_handlers() -> List[CustomComponentAction]:
    _load_action_modules()
    return [cls() for cls in CustomComponentAction.registry]


def _find_action_handler(service_name: str, action: str) -> Optional[CustomComponentAction]:
    for handler in _get_action_handlers():
        if handler.supports(service_name, action):
            return handler
    return None


def _split_manifest_services(manifest: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    services = manifest.get("services") or []
    if not isinstance(services, list):
        return [], []

    fis_services: List[Dict[str, Any]] = []
    custom_services: List[Dict[str, Any]] = []

    for svc in services:
        if not isinstance(svc, dict):
            continue
        service_name = normalize_service_name(svc.get("name"))
        action = str(svc.get("action") or "").strip().lower()
        handler = _find_action_handler(service_name, action)
        if service_name == "common" and action == "wait":
            use_fis_value = svc.get("use_fis", True)
            if isinstance(use_fis_value, bool):
                use_fis = use_fis_value
            else:
                use_fis = str(use_fis_value).strip().lower() not in ("0", "false", "no", "n", "off")
            if use_fis:
                fis_services.append(svc)
            elif handler is None:
                raise ValueError("common:wait with use_fis=false requires a custom wait handler, but none is registered.")
            else:
                custom_services.append(svc)
            continue
        if handler is None:
            fis_services.append(svc)
        else:
            custom_services.append(svc)

    return fis_services, custom_services


def manifest_has_custom_actions(manifest: Dict[str, Any]) -> bool:
    _, custom_services = _split_manifest_services(manifest)
    return len(custom_services) > 0


def validate_component_action_mix(manifest: Dict[str, Any]) -> None:
    fis_services, custom_services = _split_manifest_services(manifest)
    if fis_services and custom_services:
        raise ValueError(
            "Mixing native FIS actions and custom component actions in one manifest is not supported yet. "
            "Please split them into separate manifests."
        )


def build_custom_execution_plan(
    manifest: Dict[str, Any],
    *,
    session,
    region: str,
    default_timeout_seconds: int,
) -> Dict[str, Any]:
    validate_component_action_mix(manifest)
    _, custom_services = _split_manifest_services(manifest)
    if not custom_services:
        raise ValueError("No custom component actions were found in the manifest.")

    rtype = (manifest.get("resilience_test_type") or "").strip().lower()
    plan_name = f"resilience-{rtype}-{utc_ts()}"
    items: List[Dict[str, Any]] = []

    for index, svc in enumerate(custom_services, start=1):
        service_name = normalize_service_name(svc.get("name"))
        action = str(svc.get("action") or "").strip().lower()
        handler = _find_action_handler(service_name, action)
        if handler is None:
            raise ValueError(f"Unsupported custom component action: {service_name}:{action}")

        item = handler.build_plan_item(
            manifest=manifest,
            svc=svc,
            session=session,
            region=region,
            index=index,
            default_timeout_seconds=default_timeout_seconds,
        )
        item["region"] = region
        items.append(item)

    return {
        "name": plan_name,
        "description": "Custom component resilience execution plan",
        "items": items,
    }


def collect_custom_impacted_resources(execution_plan: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in execution_plan.get("items") or []:
        impacted_many = item.get("impacted_resources")
        if isinstance(impacted_many, list):
            for impacted in impacted_many:
                if not isinstance(impacted, dict):
                    continue
                out.append(
                    {
                        "service": str(impacted.get("service") or ""),
                        "arn": str(impacted.get("arn") or ""),
                        "selection_mode": str(impacted.get("selection_mode") or "CUSTOM"),
                    }
                )
        impacted = item.get("impacted_resource")
        if isinstance(impacted, dict):
            out.append(
                {
                    "service": str(impacted.get("service") or ""),
                    "arn": str(impacted.get("arn") or ""),
                    "selection_mode": str(impacted.get("selection_mode") or "CUSTOM"),
                }
            )
    return out


def execute_custom_plan(
    *,
    session,
    execution_plan: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    item_summaries: List[Dict[str, Any]] = []
    for item in execution_plan.get("items") or []:
        service_name, action = str(item.get("service") or ":").split(":", 1)
        handler = _find_action_handler(service_name, action)
        if handler is None:
            raise ValueError(f"Unsupported custom component action during execution: {item.get('service')}")

        item_summary = handler.execute_item(
            session=session,
            item=item,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
        item_summaries.append(item_summary)

    overall_status = "completed"
    if any(s["status"] != "completed" for s in item_summaries):
        overall_status = "failed"

    start_times = [s.get("startTime") for s in item_summaries if s.get("startTime")]
    end_times = [s.get("endTime") for s in item_summaries if s.get("endTime")]

    summary: Dict[str, Any] = {
        "experimentId": None,
        "experimentTemplateId": None,
        "status": overall_status,
        "reason": None if overall_status == "completed" else "One or more custom component actions failed.",
        "startTime": min(start_times) if start_times else None,
        "endTime": max(end_times) if end_times else None,
        "actions": {},
        "customExecution": {
            "name": execution_plan.get("name"),
            "items": item_summaries,
        },
    }

    for item_summary in item_summaries:
        summary["actions"][item_summary["name"]] = {
            "status": item_summary["status"],
            "reason": item_summary.get("reason"),
            "startTime": item_summary.get("startTime"),
            "endTime": item_summary.get("endTime"),
        }

    return summary
