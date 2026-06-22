from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import requests

from providers.azure.resource import resource_label, target_resource_ids
from providers.azure.runtime import AzureRuntimeContext
from utility import normalize_service_name


AZURE_MANAGEMENT_SCOPE = "https://management.azure.com/.default"


class ValidationError(ValueError):
    pass


@dataclass
class ValidationContext:
    manifest: Dict[str, Any]
    service: Dict[str, Any]
    runtime_context: AzureRuntimeContext

    @property
    def service_name(self) -> str:
        return normalize_service_name(self.service.get("name"))

    @property
    def action(self) -> str:
        return str(self.service.get("action") or "").strip().lower()

    @property
    def action_key(self) -> str:
        return f"{self.service_name}:{self.action}"

    def target_resource_ids(self) -> List[str]:
        target = self.service.get("target") if isinstance(self.service.get("target"), dict) else {}
        return target_resource_ids(target)

    def selection_summary(self) -> str:
        resource_ids = self.target_resource_ids()
        if resource_ids:
            return ", ".join(resource_label(resource_id) for resource_id in resource_ids)
        return "no explicit Azure resource ID selector provided"

    def auth_headers(self) -> Dict[str, str]:
        if self.runtime_context.credential is None:
            raise ValidationError("Azure validation requires an authenticated Azure credential.")
        token = self.runtime_context.credential.get_token(AZURE_MANAGEMENT_SCOPE)
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    def get_resource(self, resource_id: str, api_version: str) -> Dict[str, Any]:
        url = f"https://management.azure.com{resource_id}?api-version={api_version}"
        response = requests.get(url, headers=self.auth_headers(), timeout=60)
        if response.status_code == 404:
            raise ValidationError(f"Azure resource was not found: {resource_id}")
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise ValidationError(
                f"Azure resource lookup failed: {resource_id} status={response.status_code} detail={detail}"
            )
        try:
            return response.json()
        except Exception as exc:
            raise ValidationError(f"Azure resource lookup returned invalid JSON for {resource_id}: {exc}") from exc


class BaseServiceValidator:
    service_name = ""

    def run(self, validation_name: str, context: ValidationContext) -> None:
        fn = getattr(self, validation_name, None)
        if not callable(fn):
            raise ValidationError(
                f"Validation '{validation_name}' is not implemented for service '{context.service_name}'."
            )
        fn(context)

    def fail(self, context: ValidationContext, message: str) -> None:
        raise ValidationError(f"Validation failed for {context.action_key}: {message}")
