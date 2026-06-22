from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Dict, List

import yaml

from providers.azure.runtime import AzureRuntimeContext
from providers.azure.validations.base import ValidationContext, ValidationError
from providers.azure.validations.vm import VMValidator
from utility import coerce_bool, log_message, normalize_service_name


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIONS_PATH = os.path.join(SCRIPT_DIR, "actions.yml")

_VALIDATORS = {
    "vm": VMValidator(),
}


def normalize_azure_service_name(service_name: Any) -> str:
    normalized = normalize_service_name(service_name)
    if normalized in {"virtual-machine", "virtual_machine"}:
        return "vm"
    return normalized


@lru_cache(maxsize=1)
def load_action_validations() -> Dict[str, List[str]]:
    if not os.path.exists(ACTIONS_PATH):
        return {}

    with open(ACTIONS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    actions = data.get("actions") or []
    if not isinstance(actions, list):
        raise ValidationError("scripts/providers/azure/validations/actions.yml must contain an 'actions' list.")

    out: Dict[str, List[str]] = {}
    for item in actions:
        if not isinstance(item, dict):
            continue
        action_key = str(item.get("action") or "").strip().lower()
        if not action_key:
            continue
        validations = item.get("validations") or []
        if not isinstance(validations, list):
            raise ValidationError(f"Validation config for '{action_key}' must use a list under 'validations'.")
        out[action_key] = [str(name).strip() for name in validations if str(name).strip()]
    return out


def get_service_validator(service_name: str):
    return _VALIDATORS.get(normalize_azure_service_name(service_name))


def manifest_skip_validation_enabled(manifest: Dict[str, Any]) -> bool:
    return coerce_bool((manifest or {}).get("skip_validation"), False)


def service_skip_validation_enabled(svc: Dict[str, Any]) -> bool:
    return coerce_bool((svc or {}).get("skip_validation"), False)


def validate_manifest_services(
    manifest: Dict[str, Any],
    *,
    runtime_context: AzureRuntimeContext,
) -> None:
    if manifest_skip_validation_enabled(manifest):
        log_message("WARN", "manifest.skip_validation enabled: skipping all Azure pre-execution action validations.")
        return

    services = manifest.get("services") or []
    if not isinstance(services, list):
        return

    action_validations = load_action_validations()
    for svc in services:
        if not isinstance(svc, dict):
            continue

        service_name = normalize_azure_service_name(svc.get("name"))
        action = str(svc.get("action") or "").strip().lower()
        action_key = f"{service_name}:{action}"
        validation_names = action_validations.get(action_key) or []
        if not validation_names:
            continue
        if service_skip_validation_enabled(svc):
            log_message("WARN", f"services[].skip_validation enabled: skipping Azure validation for {action_key}.")
            continue

        validator = get_service_validator(service_name)
        if validator is None:
            raise ValidationError(
                f"Validation config exists for '{action_key}', but no Azure validator is registered for service '{service_name}'."
            )

        context = ValidationContext(
            manifest=manifest,
            service=svc,
            runtime_context=runtime_context,
        )
        for validation_name in validation_names:
            log_message("INFO", f"Running Azure validation: {action_key} -> {validation_name}")
            validator.run(validation_name, context)
            log_message("OK", f"Azure validation passed: {action_key} -> {validation_name}")
