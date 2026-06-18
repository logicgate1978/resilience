from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from providers.azure.auth import create_default_credential, credential_summary
from providers.azure.resource import normalize_location, validate_resource_ids_for_subscription


@dataclass(frozen=True)
class AzureRuntimeContext:
    subscription_id: str
    resource_group: str
    location: str
    credential: Any = None
    credential_type: str = ""


def resolve_subscription_id(manifest: Dict[str, Any], subscription_id: Optional[str]) -> str:
    value = str(subscription_id or manifest.get("subscription_id") or "").strip()
    if not value:
        raise ValueError("Azure manifests require subscription_id at the top level or --subscription-id.")
    return value


def validate_service_subscription_ids(manifest: Dict[str, Any], subscription_id: str) -> None:
    expected = str(subscription_id or "").strip().lower()
    if not expected:
        return

    services = manifest.get("services") or []
    for service_index, svc in enumerate(services):
        if not isinstance(svc, dict):
            continue
        service_subscription_id = str(svc.get("subscription_id") or "").strip()
        if service_subscription_id and service_subscription_id.lower() != expected:
            raise ValueError(
                "Azure subscription validation failed: "
                f"services[{service_index}].subscription_id is '{service_subscription_id}', "
                f"but the requested subscription is '{subscription_id}'."
            )


def create_runtime_context(
    manifest: Dict[str, Any],
    *,
    subscription_id: Optional[str],
    require_credential: bool = False,
) -> AzureRuntimeContext:
    resolved_subscription_id = resolve_subscription_id(manifest, subscription_id)
    validate_service_subscription_ids(manifest, resolved_subscription_id)
    validate_resource_ids_for_subscription(manifest, resolved_subscription_id)

    credential = create_default_credential() if require_credential else None
    return AzureRuntimeContext(
        subscription_id=resolved_subscription_id,
        resource_group=str(manifest.get("resource_group") or "").strip(),
        location=normalize_location(manifest.get("location") or manifest.get("region")),
        credential=credential,
        credential_type=credential_summary() if require_credential else "",
    )
