from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from providers.azure.resource import (
    resource_label,
    resolve_location,
    resolve_resource_group,
    selection_summary,
    target_resource_ids,
)
from providers.azure.runtime import AzureRuntimeContext, create_runtime_context
from utility import normalize_service_name, utc_ts


AZURE_ENGINES = {"chaos_studio", "custom"}


def _manifest_services(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    services = manifest.get("services") or []
    if not isinstance(services, list) or not services:
        raise ValueError("Top-level 'services' must be a non-empty list.")
    out = [svc for svc in services if isinstance(svc, dict)]
    if len(out) != len(services):
        raise ValueError("Every item in top-level 'services' must be an object.")
    return out


def _service_engine(manifest: Dict[str, Any], svc: Dict[str, Any], index: int) -> str:
    raw = svc.get("engine", manifest.get("engine", "chaos_studio"))
    engine = str(raw or "").strip().lower().replace("-", "_")
    if engine not in AZURE_ENGINES:
        raise ValueError(
            f"services[{index}].engine must be one of: chaos_studio, custom."
        )
    return engine


def _parse_start_after_refs(value: Any, field_name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        refs = [value]
    elif isinstance(value, list):
        refs = value
    else:
        raise ValueError(f"{field_name} must be a string or list of strings if provided.")
    return [str(ref or "").strip().lower() for ref in refs if str(ref or "").strip()]


def _build_service_reference_map(services: List[Dict[str, Any]], item_names: List[str]) -> Dict[str, Tuple[int, str]]:
    totals: Dict[str, int] = {}
    normalized: List[Tuple[str, str]] = []
    for svc in services:
        service_name = normalize_service_name(svc.get("name"))
        action_name = str(svc.get("action") or "").strip().lower()
        key = f"{service_name}:{action_name}"
        normalized.append((service_name, action_name))
        totals[key] = totals.get(key, 0) + 1

    refs: Dict[str, Tuple[int, str]] = {}
    occurrences: Dict[str, int] = {}
    for index, ((service_name, action_name), item_name) in enumerate(zip(normalized, item_names), start=1):
        key = f"{service_name}:{action_name}"
        occurrences[key] = occurrences.get(key, 0) + 1
        ordinal = occurrences[key]
        refs[f"{key}#{ordinal}"] = (index, item_name)
        if totals[key] == 1:
            refs[key] = (index, item_name)
    return refs


def _resolve_start_after(services: List[Dict[str, Any]], item_names: List[str]) -> Dict[int, List[str]]:
    ref_map = _build_service_reference_map(services, item_names)
    resolved: Dict[int, List[str]] = {}
    for index, svc in enumerate(services, start=1):
        dependencies: List[str] = []
        seen = set()
        refs = _parse_start_after_refs(svc.get("start_after"), f"services[{index - 1}].start_after")
        for ref in refs:
            target = ref_map.get(ref)
            if target is None:
                raise ValueError(
                    f"services[{index - 1}].start_after references unknown action '{ref}'. "
                    "Use '<service>:<action>' for unique actions or '<service>:<action>#<n>' when duplicates exist."
                )
            target_index, target_item_name = target
            if target_index >= index:
                raise ValueError(
                    f"services[{index - 1}].start_after reference '{ref}' must point to an earlier service action."
                )
            if target_item_name not in seen:
                dependencies.append(target_item_name)
                seen.add(target_item_name)
        resolved[index] = dependencies
    return resolved


def _stringify_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "-"
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if isinstance(value, dict):
        return ",".join(f"{key}={_stringify_value(val)}" for key, val in value.items())
    return str(value)


def _format_key_parameters(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict) or not data:
        return "-"
    parts = []
    for key, value in data.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        parts.append(f"{key}={_stringify_value(value)}")
    return ", ".join(parts) if parts else "-"


def _detail_entry(*, index: int, item: Dict[str, Any], resources: List[Dict[str, Any]]) -> Dict[str, Any]:
    detailed_resources = []
    for resource in resources:
        arn = str(resource.get("arn") or "").strip()
        if not arn:
            continue
        detailed_resources.append(
            {
                "label": str(resource.get("label") or resource_label(arn)),
                "arn": arn,
            }
        )
    return {
        "index": index,
        "action": item.get("actionRef") or item.get("service") or "-",
        "engine": item.get("engine") or "-",
        "region": item.get("location") or item.get("resourceGroup") or "-",
        "zone": "-",
        "key_parameters": _format_key_parameters(item.get("parameters") or {}),
        "resources": detailed_resources,
    }


def build_azure_execution_plan(
    manifest: Dict[str, Any],
    *,
    subscription_id: Optional[str],
    default_timeout_seconds: int,
    runtime_context: Optional[AzureRuntimeContext] = None,
) -> Dict[str, Any]:
    services = _manifest_services(manifest)
    context = runtime_context or create_runtime_context(
        manifest,
        subscription_id=subscription_id,
        require_credential=False,
    )
    resolved_subscription_id = context.subscription_id
    engine_families = {_service_engine(manifest, svc, index) for index, svc in enumerate(services)}
    if len(engine_families) > 1:
        raise ValueError(
            "Mixing Azure chaos_studio and custom implementations in one manifest is not supported yet. "
            "Please split them into separate manifests."
        )

    plan_name = f"resilience-azure-{next(iter(engine_families))}-{utc_ts()}"
    items: List[Dict[str, Any]] = []
    for index, svc in enumerate(services, start=1):
        service_name = normalize_service_name(svc.get("name"))
        action_name = str(svc.get("action") or "").strip().lower()
        if not service_name or not action_name:
            raise ValueError(f"services[{index - 1}] must include 'name' and 'action'.")

        engine = _service_engine(manifest, svc, index - 1)
        target = svc.get("target") if isinstance(svc.get("target"), dict) else {}
        parameters = svc.get("parameters") if isinstance(svc.get("parameters"), dict) else {}
        item_subscription_id = str(svc.get("subscription_id") or resolved_subscription_id).strip()
        item_name = f"a_{service_name}_{action_name}_{index}"
        item = {
            "name": item_name,
            "actionRef": f"{service_name}:{action_name}",
            "service": f"{service_name}:{action_name}",
            "provider": "azure",
            "engine": engine,
            "subscriptionId": item_subscription_id,
            "resourceGroup": resolve_resource_group(manifest, svc, target),
            "location": resolve_location(manifest, svc, target),
            "target": target,
            "parameters": {
                **parameters,
                "timeout_seconds": parameters.get("timeout_seconds", svc.get("timeout_seconds", default_timeout_seconds)),
            },
            "description": str(svc.get("description") or f"Azure {engine} action {service_name}:{action_name}"),
        }
        items.append(item)

    start_after_map = _resolve_start_after(services, [str(item["name"]) for item in items])
    for index, item in enumerate(items, start=1):
        item["startAfter"] = start_after_map.get(index) or []

    return {
        "name": plan_name,
        "provider": "azure",
        "engineFamily": next(iter(engine_families)),
        "subscriptionId": resolved_subscription_id,
        "description": "Azure resilience execution plan",
        "items": items,
    }


def collect_azure_impacted_resources(execution_plan: Dict[str, Any]) -> List[Dict[str, str]]:
    resources: List[Dict[str, str]] = []
    for item in execution_plan.get("items") or []:
        target = item.get("target") or {}
        for resource_id in target_resource_ids(target):
            resources.append(
                {
                    "service": str(item.get("service") or ""),
                    "arn": resource_id,
                    "selection_mode": "AZURE_RESOURCE_ID",
                    "label": resource_label(resource_id),
                }
            )
        if not target_resource_ids(target):
            resources.append(
                {
                    "service": str(item.get("service") or ""),
                    "arn": selection_summary(target),
                    "selection_mode": "AZURE_SELECTOR",
                    "label": selection_summary(target),
                }
            )
    return resources


def build_azure_dry_run_rows(execution_plan: Dict[str, Any]) -> Tuple[List[List[str]], List[Dict[str, Any]]]:
    rows: List[List[str]] = []
    details: List[Dict[str, Any]] = []
    items = list(execution_plan.get("items") or [])
    for index, item in enumerate(items, start=1):
        resources = []
        target = item.get("target") or {}
        for resource_id in target_resource_ids(target):
            resources.append({"arn": resource_id, "label": resource_label(resource_id)})
        if not resources:
            summary = selection_summary(target)
            resources.append({"arn": summary, "label": summary})

        key_parameters = _format_key_parameters(item.get("parameters") or {})
        rows.append(
            [
                str(index),
                str(item.get("actionRef") or "-"),
                "Azure",
                str(item.get("engine") or "-"),
                str(item.get("location") or item.get("resourceGroup") or "-"),
                "-",
                ", ".join(item.get("startAfter") or []) or "-",
                ", ".join(str(resource.get("label") or "-") for resource in resources),
                key_parameters,
            ]
        )
        details.append(_detail_entry(index=index, item=item, resources=resources))
    return rows, details
