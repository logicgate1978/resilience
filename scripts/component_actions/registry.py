from __future__ import annotations

import concurrent.futures
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


def _parse_start_after_refs(value: Any, field_name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        refs = [value]
    elif isinstance(value, list):
        refs = value
    else:
        raise ValueError(f"{field_name} must be a string or list of strings if provided.")

    out: List[str] = []
    for raw in refs:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        out.append(text)
    return out


def _build_service_reference_map(services: List[Dict[str, Any]], item_names: List[str]) -> Dict[str, Tuple[int, str]]:
    totals: Dict[str, int] = {}
    normalized: List[Tuple[str, str]] = []
    for svc in services:
        service_name = normalize_service_name(svc.get("name"))
        action = str(svc.get("action") or "").strip().lower()
        key = f"{service_name}:{action}"
        normalized.append((service_name, action))
        totals[key] = totals.get(key, 0) + 1

    refs: Dict[str, Tuple[int, str]] = {}
    occurrences: Dict[str, int] = {}
    for index, ((service_name, action), item_name) in enumerate(zip(normalized, item_names), start=1):
        key = f"{service_name}:{action}"
        occurrences[key] = occurrences.get(key, 0) + 1
        ordinal = occurrences[key]
        refs[f"{key}#{ordinal}"] = (index, item_name)
        if totals[key] == 1:
            refs[key] = (index, item_name)

    return refs


def _resolve_custom_start_after(services: List[Dict[str, Any]], item_names: List[str]) -> Dict[int, List[str]]:
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

    start_after_map = _resolve_custom_start_after(custom_services, [str(item["name"]) for item in items])
    for index, item in enumerate(items, start=1):
        item["startAfter"] = start_after_map.get(index) or []

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
    items = list(execution_plan.get("items") or [])
    items_by_name = {str(item["name"]): item for item in items}
    pending = {str(item["name"]) for item in items}
    running: Dict[concurrent.futures.Future, str] = {}
    results: Dict[str, Dict[str, Any]] = {}

    def _execute(item: Dict[str, Any]) -> Dict[str, Any]:
        service_name, action = str(item.get("service") or ":").split(":", 1)
        handler = _find_action_handler(service_name, action)
        if handler is None:
            raise ValueError(f"Unsupported custom component action during execution: {item.get('service')}")
        return handler.execute_item(
            session=session,
            item=item,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(items))) as executor:
        while pending or running:
            made_progress = False

            for item_name in list(pending):
                item = items_by_name[item_name]
                deps = list(item.get("startAfter") or [])
                if not all(dep in results for dep in deps):
                    continue

                failed_deps = [dep for dep in deps if results[dep].get("status") != "completed"]
                if failed_deps:
                    print(
                        f"[INFO] Skipping custom action {item.get('service')} "
                        f"because dependency action(s) did not complete successfully: {', '.join(failed_deps)}"
                    )
                    results[item_name] = {
                        "name": item_name,
                        "status": "skipped",
                        "reason": f"Skipped because dependency action(s) did not complete successfully: {', '.join(failed_deps)}",
                        "startTime": None,
                        "endTime": None,
                        "details": {
                            "target": item.get("target"),
                            "parameters": item.get("parameters"),
                            "startAfter": deps,
                        },
                    }
                    pending.remove(item_name)
                    made_progress = True
                    continue

                print(f"[INFO] Starting custom action: {item.get('service')} ({item_name})")
                running[executor.submit(_execute, item)] = item_name
                pending.remove(item_name)
                made_progress = True

            if running:
                done, _ = concurrent.futures.wait(
                    running.keys(),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    item_name = running.pop(future)
                    try:
                        results[item_name] = future.result()
                    except Exception as e:
                        results[item_name] = {
                            "name": item_name,
                            "status": "failed",
                            "reason": f"Unhandled custom action error: {e}",
                            "startTime": None,
                            "endTime": None,
                            "details": {
                                "target": items_by_name[item_name].get("target"),
                                "parameters": items_by_name[item_name].get("parameters"),
                                "startAfter": items_by_name[item_name].get("startAfter") or [],
                            },
                        }
                    result = results[item_name]
                    print(
                        f"[INFO] Finished custom action: {items_by_name[item_name].get('service')} "
                        f"({item_name}) status={result.get('status')}"
                    )
                    made_progress = True

            if not made_progress and pending:
                blocked = sorted(pending)
                raise ValueError(
                    "Custom execution plan contains unresolved or cyclic start_after dependencies: "
                    + ", ".join(blocked)
                )

    item_summaries: List[Dict[str, Any]] = [results[str(item["name"])] for item in items]

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
