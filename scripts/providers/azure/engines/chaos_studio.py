from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from providers.azure.resource import (
    chaos_target_id,
    parse_resource_id,
    resource_label,
    resolve_location,
    resolve_resource_group,
    selection_summary,
    target_resource_ids,
)
from providers.azure.runtime import AzureRuntimeContext, create_runtime_context
from utility import log_message, normalize_service_name, pretty, sanitize_filename, utc_ts

import requests


AZURE_ENGINES = {"chaos_studio", "custom"}
CHAOS_STUDIO_API_VERSION = "2025-01-01"
AZURE_MANAGEMENT_SCOPE = "https://management.azure.com/.default"
VM_SHUTDOWN_URN = "urn:csci:microsoft:virtualMachine:shutdown/1.0"
VM_SHUTDOWN_TARGET_TYPE = "Microsoft-VirtualMachine"
VM_SHUTDOWN_RESOURCE_TYPE = ("microsoft.compute", "virtualmachines")
TERMINAL_EXECUTION_STATUSES = {"success", "succeeded", "completed", "failed", "canceled", "cancelled", "stopped"}


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


def _bool_text(value: Any, default: bool = False) -> str:
    if value is None:
        return "true" if default else "false"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return "true"
    if text in {"0", "false", "no", "n", "off"}:
        return "false"
    return "true" if default else "false"


def _is_vm_shutdown_item(item: Dict[str, Any]) -> bool:
    service = str(item.get("service") or item.get("actionRef") or "").strip().lower()
    if ":" not in service:
        return False
    service_name, action_name = service.split(":", 1)
    return service_name in {"vm", "virtual-machine", "virtual_machine"} and action_name in {"shutdown", "stop"}


def _validate_vm_shutdown_target(resource_id: str) -> None:
    parsed = parse_resource_id(resource_id)
    if not parsed:
        raise ValueError("Azure VM shutdown requires a full Azure virtual machine resource ID.")
    actual = (parsed.provider_namespace.lower(), parsed.resource_type.lower())
    if actual != VM_SHUTDOWN_RESOURCE_TYPE:
        raise ValueError(
            "Azure VM shutdown requires Microsoft.Compute/virtualMachines targets. "
            f"Received {parsed.provider_namespace}/{parsed.resource_type} for resource '{parsed.name}'."
        )


def _experiment_name(execution_plan: Dict[str, Any]) -> str:
    value = str(execution_plan.get("experimentName") or execution_plan.get("name") or "").strip()
    return sanitize_filename(value, max_len=64)


def _experiment_resource_group(execution_plan: Dict[str, Any], runtime_context: AzureRuntimeContext) -> str:
    value = str(execution_plan.get("resourceGroup") or "").strip()
    if value:
        return value
    for item in execution_plan.get("items") or []:
        value = str(item.get("resourceGroup") or "").strip()
        if value:
            return value
    return runtime_context.resource_group


def _experiment_location(execution_plan: Dict[str, Any], runtime_context: AzureRuntimeContext) -> str:
    value = str(execution_plan.get("location") or "").strip()
    if value:
        return value
    for item in execution_plan.get("items") or []:
        value = str(item.get("location") or "").strip()
        if value:
            return value
    return runtime_context.location


def build_chaos_studio_experiment_payload(
    execution_plan: Dict[str, Any],
    *,
    runtime_context: AzureRuntimeContext,
) -> Dict[str, Any]:
    if str(execution_plan.get("engineFamily") or "").lower() != "chaos_studio":
        raise ValueError("Azure Chaos Studio execution only supports engineFamily=chaos_studio.")

    location = _experiment_location(execution_plan, runtime_context)
    if not location:
        raise ValueError("Azure Chaos Studio experiments require location in the manifest or service block.")

    selectors: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    items = list(execution_plan.get("items") or [])
    if not items:
        raise ValueError("Azure Chaos Studio execution plan has no actions.")
    if len(items) != 1:
        raise ValueError("Azure Chaos Studio live execution currently supports exactly one VM shutdown action per manifest.")

    for index, item in enumerate(items, start=1):
        if str(item.get("engine") or "").lower() != "chaos_studio":
            raise ValueError("Azure Chaos Studio execution cannot run non-chaos_studio Azure actions.")
        if not _is_vm_shutdown_item(item):
            raise ValueError(
                "Azure Chaos Studio currently supports only Virtual Machine Shutdown "
                "using service/action vm:stop or vm:shutdown."
            )

        target_ids = target_resource_ids(item.get("target") or {})
        if not target_ids:
            raise ValueError("Azure VM shutdown requires service.target.resource_id or service.target.resource_ids.")
        for resource_id in target_ids:
            _validate_vm_shutdown_target(resource_id)

        selector_id = f"selector{index}"
        selectors.append(
            {
                "type": "List",
                "id": selector_id,
                "targets": [
                    {
                        "type": "ChaosTarget",
                        "id": chaos_target_id(resource_id, VM_SHUTDOWN_TARGET_TYPE),
                    }
                    for resource_id in target_ids
                ],
            }
        )

        parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
        duration = str(parameters.get("duration") or item.get("duration") or "PT10M").strip()
        abrupt_shutdown = _bool_text(parameters.get("abruptShutdown", parameters.get("abrupt_shutdown")), default=False)
        steps.append(
            {
                "name": f"step{index}",
                "branches": [
                    {
                        "name": f"branch{index}",
                        "actions": [
                            {
                                "name": VM_SHUTDOWN_URN,
                                "type": "continuous",
                                "duration": duration,
                                "parameters": [
                                    {
                                        "key": "abruptShutdown",
                                        "value": abrupt_shutdown,
                                    }
                                ],
                                "selectorId": selector_id,
                            }
                        ],
                    }
                ],
            }
        )

    return {
        "identity": {"type": "SystemAssigned"},
        "location": location,
        "tags": {
            "createdBy": "resilience-automation",
            "engine": "chaos_studio",
        },
        "properties": {
            "selectors": selectors,
            "steps": steps,
        },
    }


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
        "resourceGroup": context.resource_group,
        "location": context.location,
        "description": "Azure resilience execution plan",
        "items": items,
    }


def _credential_headers(runtime_context: AzureRuntimeContext) -> Dict[str, str]:
    if runtime_context.credential is None:
        raise ValueError("Azure execution requires an authenticated Azure credential.")
    token = runtime_context.credential.get_token(AZURE_MANAGEMENT_SCOPE)
    return {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json",
    }


def _management_url(path: str, query: str = "") -> str:
    base = f"https://management.azure.com{path}"
    separator = "&" if query else "?"
    return f"{base}{separator}api-version={CHAOS_STUDIO_API_VERSION}" if query else f"{base}?api-version={CHAOS_STUDIO_API_VERSION}"


def _experiment_path(subscription_id: str, resource_group: str, experiment_name: str) -> str:
    return (
        f"/subscriptions/{quote(subscription_id, safe='')}"
        f"/resourceGroups/{quote(resource_group, safe='')}"
        f"/providers/Microsoft.Chaos/experiments/{quote(experiment_name, safe='')}"
    )


def _request_json(method: str, url: str, *, headers: Dict[str, str], json_body: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str], int]:
    response = requests.request(method, url, headers=headers, json=json_body, timeout=60)
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"Azure Chaos Studio API call failed: {method} {url} status={response.status_code} detail={detail}")
    payload: Dict[str, Any] = {}
    if response.text.strip():
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text}
    return payload, dict(response.headers), response.status_code


def _poll_async_operation(url: str, *, headers: Dict[str, str], poll_seconds: int, timeout_seconds: int) -> Dict[str, Any]:
    if not url:
        return {}
    start = time.time()
    while True:
        payload, _, _ = _request_json("GET", url, headers=headers)
        status = str(payload.get("status") or payload.get("properties", {}).get("provisioningState") or "").strip()
        if status.lower() in {"succeeded", "failed", "canceled", "cancelled"}:
            if status.lower() != "succeeded":
                raise RuntimeError(f"Azure Chaos Studio async operation ended with status={status}: {payload}")
            return payload
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Azure Chaos Studio async operation timed out after {timeout_seconds}s: {url}")
        log_message("INFO", f"Azure async operation status={status or 'unknown'} elapsed={int(time.time() - start)}s")
        time.sleep(max(1, poll_seconds))


def _list_executions(
    *,
    headers: Dict[str, str],
    subscription_id: str,
    resource_group: str,
    experiment_name: str,
) -> List[Dict[str, Any]]:
    url = _management_url(f"{_experiment_path(subscription_id, resource_group, experiment_name)}/executions")
    try:
        payload, _, _ = _request_json("GET", url, headers=headers)
    except RuntimeError as exc:
        if "status=404" in str(exc):
            return []
        raise
    return list(payload.get("value") or [])


def _get_execution(
    *,
    headers: Dict[str, str],
    subscription_id: str,
    resource_group: str,
    experiment_name: str,
    execution_id: str,
) -> Dict[str, Any]:
    url = _management_url(f"{_experiment_path(subscription_id, resource_group, experiment_name)}/executions/{quote(execution_id, safe='')}")
    payload, _, _ = _request_json("GET", url, headers=headers)
    return payload


def _get_execution_details(
    *,
    headers: Dict[str, str],
    subscription_id: str,
    resource_group: str,
    experiment_name: str,
    execution_id: str,
) -> Dict[str, Any]:
    url = _management_url(
        f"{_experiment_path(subscription_id, resource_group, experiment_name)}/executions/{quote(execution_id, safe='')}/getExecutionDetails"
    )
    payload, _, _ = _request_json("POST", url, headers=headers)
    return payload


def _execution_status(execution: Dict[str, Any]) -> str:
    return str((execution.get("properties") or {}).get("status") or execution.get("status") or "").strip()


def _find_new_execution_id(previous_ids: set[str], executions: List[Dict[str, Any]]) -> Optional[str]:
    for execution in executions:
        execution_id = str(execution.get("name") or "").strip()
        if execution_id and execution_id not in previous_ids:
            return execution_id
    return None


def execute_chaos_studio_plan(
    *,
    execution_plan: Dict[str, Any],
    runtime_context: AzureRuntimeContext,
    outdir: str,
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    resource_group = _experiment_resource_group(execution_plan, runtime_context)
    if not resource_group:
        raise ValueError("Azure Chaos Studio experiments require resource_group in the manifest or target resource ID.")

    experiment_name = _experiment_name(execution_plan)
    payload = build_chaos_studio_experiment_payload(
        execution_plan,
        runtime_context=runtime_context,
    )
    headers = _credential_headers(runtime_context)
    subscription_id = runtime_context.subscription_id
    experiment_base_path = _experiment_path(subscription_id, resource_group, experiment_name)

    previous_execution_ids = {
        str(execution.get("name") or "").strip()
        for execution in _list_executions(
            headers=headers,
            subscription_id=subscription_id,
            resource_group=resource_group,
            experiment_name=experiment_name,
        )
        if str(execution.get("name") or "").strip()
    }

    create_url = _management_url(experiment_base_path)
    create_payload, create_headers, create_status = _request_json("PUT", create_url, headers=headers, json_body=payload)
    _poll_async_operation(
        create_headers.get("Azure-AsyncOperation") or create_headers.get("azure-asyncoperation") or "",
        headers=headers,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )
    log_message("OK", f"Created/updated Azure Chaos Studio experiment: {experiment_name}")

    start_url = _management_url(f"{experiment_base_path}/start")
    start_payload, start_headers, start_status = _request_json("POST", start_url, headers=headers)
    _poll_async_operation(
        start_headers.get("Azure-AsyncOperation") or start_headers.get("azure-asyncoperation") or "",
        headers=headers,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )
    log_message("OK", f"Started Azure Chaos Studio experiment: {experiment_name}")

    started = time.time()
    execution_id = None
    latest_execution: Dict[str, Any] = {}
    while execution_id is None:
        executions = _list_executions(
            headers=headers,
            subscription_id=subscription_id,
            resource_group=resource_group,
            experiment_name=experiment_name,
        )
        execution_id = _find_new_execution_id(previous_execution_ids, executions)
        if execution_id:
            latest_execution = next((execution for execution in executions if execution.get("name") == execution_id), {})
            break
        if time.time() - started > timeout_seconds:
            raise TimeoutError(f"Timed out waiting for Azure Chaos Studio execution ID for experiment {experiment_name}.")
        time.sleep(max(1, poll_seconds))

    while True:
        latest_execution = _get_execution(
            headers=headers,
            subscription_id=subscription_id,
            resource_group=resource_group,
            experiment_name=experiment_name,
            execution_id=execution_id,
        )
        status = _execution_status(latest_execution).lower()
        if status in TERMINAL_EXECUTION_STATUSES:
            break
        if time.time() - started > timeout_seconds:
            raise TimeoutError(f"Azure Chaos Studio execution {execution_id} timed out after {timeout_seconds}s.")
        log_message("INFO", f"Azure Chaos Studio execution running: executionId={execution_id} status={status or 'unknown'} elapsed={int(time.time() - started)}s")
        time.sleep(max(1, poll_seconds))

    execution_details: Dict[str, Any] = {}
    try:
        execution_details = _get_execution_details(
            headers=headers,
            subscription_id=subscription_id,
            resource_group=resource_group,
            experiment_name=experiment_name,
            execution_id=execution_id,
        )
    except Exception as exc:
        execution_details = {"warning": f"Unable to fetch execution details: {exc}"}

    summary = {
        "provider": "azure",
        "engine": "chaos_studio",
        "experimentName": experiment_name,
        "experimentResourceGroup": resource_group,
        "experimentId": create_payload.get("id"),
        "executionId": execution_id,
        "status": _execution_status(latest_execution),
        "createStatusCode": create_status,
        "startStatusCode": start_status,
        "experiment": create_payload,
        "startResponse": start_payload,
        "execution": latest_execution,
        "executionDetails": execution_details,
    }

    result_path = os.path.join(outdir, f"azure_chaos_result_{experiment_name}.json")
    with open(result_path, "w", encoding="utf-8") as f:
        f.write(pretty(summary))
    log_message("OK", f"Wrote Azure Chaos Studio result JSON: {result_path}")
    summary["resultPath"] = result_path
    return summary


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
