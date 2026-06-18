from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AzureResourceId:
    raw: str
    subscription_id: str
    resource_group: str
    provider_namespace: str
    resource_type: str
    name: str


def normalize_resource_id(resource_id: Any) -> str:
    return str(resource_id or "").strip()


def parse_resource_id(resource_id: Any) -> Optional[AzureResourceId]:
    raw = normalize_resource_id(resource_id)
    if not raw:
        return None

    parts = [part for part in raw.split("/") if part]
    lower_parts = [part.lower() for part in parts]
    if len(parts) < 8 or lower_parts[0] != "subscriptions":
        raise ValueError(f"Invalid Azure resource ID '{raw}'. Expected '/subscriptions/<id>/resourceGroups/<name>/providers/...'.")

    try:
        rg_index = lower_parts.index("resourcegroups")
        provider_index = lower_parts.index("providers")
    except ValueError as exc:
        raise ValueError(
            f"Invalid Azure resource ID '{raw}'. Expected subscription, resourceGroups, and providers segments."
        ) from exc

    if rg_index + 1 >= len(parts) or provider_index + 1 >= len(parts):
        raise ValueError(f"Invalid Azure resource ID '{raw}'. Missing resource group or provider namespace.")

    provider_namespace = parts[provider_index + 1]
    provider_tail = parts[provider_index + 2 :]
    resource_type = "/".join(provider_tail[:-1]) if len(provider_tail) > 1 else ""
    name = provider_tail[-1] if provider_tail else ""
    if not resource_type or not name:
        raise ValueError(f"Invalid Azure resource ID '{raw}'. Missing resource type or resource name.")

    return AzureResourceId(
        raw=raw,
        subscription_id=parts[1],
        resource_group=parts[rg_index + 1],
        provider_namespace=provider_namespace,
        resource_type=resource_type,
        name=name,
    )


def target_resource_ids(target: Dict[str, Any]) -> List[str]:
    resource_ids = target.get("resource_ids")
    if isinstance(resource_ids, list):
        return [normalize_resource_id(value) for value in resource_ids if normalize_resource_id(value)]
    resource_id = normalize_resource_id(target.get("resource_id"))
    return [resource_id] if resource_id else []


def resource_label(resource_id: Any) -> str:
    text = normalize_resource_id(resource_id)
    if not text:
        return "-"
    if not text.lower().startswith("/subscriptions/"):
        return text
    parsed = parse_resource_id(text)
    return parsed.name if parsed else text


def selection_summary(target: Dict[str, Any]) -> str:
    resource_ids = target_resource_ids(target)
    if resource_ids:
        return ", ".join(resource_label(resource_id) for resource_id in resource_ids)
    tags = target.get("tags")
    if isinstance(tags, str) and tags.strip():
        return f"tags: {tags.strip()}"
    if isinstance(tags, dict) and tags:
        return "tags: " + ",".join(f"{key}={value}" for key, value in sorted(tags.items()))
    resource_group = str(target.get("resource_group") or "").strip()
    return f"resource_group: {resource_group}" if resource_group else "-"


def normalize_location(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")


def resolve_resource_group(manifest: Dict[str, Any], svc: Dict[str, Any], target: Dict[str, Any]) -> str:
    explicit = str(target.get("resource_group") or svc.get("resource_group") or manifest.get("resource_group") or "").strip()
    if explicit:
        return explicit
    for resource_id in target_resource_ids(target):
        parsed = parse_resource_id(resource_id)
        if parsed and parsed.resource_group:
            return parsed.resource_group
    return ""


def resolve_location(manifest: Dict[str, Any], svc: Dict[str, Any], target: Dict[str, Any]) -> str:
    return normalize_location(target.get("location") or svc.get("location") or manifest.get("location") or manifest.get("region"))


def validate_resource_ids_for_subscription(manifest: Dict[str, Any], subscription_id: str) -> None:
    expected = str(subscription_id or "").strip().lower()
    if not expected:
        return

    services = manifest.get("services") or []
    for service_index, svc in enumerate(services):
        if not isinstance(svc, dict):
            continue
        target = svc.get("target") if isinstance(svc.get("target"), dict) else {}
        for resource_id in target_resource_ids(target):
            parsed = parse_resource_id(resource_id)
            if parsed and parsed.subscription_id.lower() != expected:
                raise ValueError(
                    "Azure subscription validation failed: "
                    f"services[{service_index}].target resource '{parsed.name}' belongs to subscription "
                    f"'{parsed.subscription_id}', but the requested subscription is '{subscription_id}'."
                )
