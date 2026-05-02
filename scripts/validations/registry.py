import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

import yaml

from utility import log_message, normalize_service_name, resolve_service_region, resolve_service_zone
from validations.asg import ASGValidator
from validations.base import ValidationContext, ValidationError
from validations.dns import DNSValidator
from validations.ec2 import EC2Validator
from validations.efs import EFSValidator
from validations.eks import EKSValidator
from validations.network import NetworkValidator
from validations.rds import RDSValidator
from validations.s3 import S3Validator


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIONS_PATH = os.path.join(SCRIPT_DIR, "actions.yml")

_VALIDATORS = {
    "asg": ASGValidator(),
    "dns": DNSValidator(),
    "ec2": EC2Validator(),
    "efs": EFSValidator(),
    "eks": EKSValidator(),
    "network": NetworkValidator(),
    "rds": RDSValidator(),
    "s3": S3Validator(),
}


@lru_cache(maxsize=1)
def load_action_validations() -> Dict[str, List[str]]:
    if not os.path.exists(ACTIONS_PATH):
        return {}

    with open(ACTIONS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    actions = data.get("actions") or []
    if not isinstance(actions, list):
        raise ValidationError("scripts/validations/actions.yml must contain an 'actions' list.")

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
    return _VALIDATORS.get(normalize_service_name(service_name))


def validate_manifest_services(
    manifest: Dict[str, Any],
    *,
    session,
    region: Optional[str] = None,
    zone: Optional[str] = None,
) -> None:
    services = manifest.get("services") or []
    if not isinstance(services, list):
        return

    action_validations = load_action_validations()
    for svc in services:
        if not isinstance(svc, dict):
            continue

        service_name = normalize_service_name(svc.get("name"))
        action = str(svc.get("action") or "").strip().lower()
        action_key = f"{service_name}:{action}"
        validation_names = action_validations.get(action_key) or []
        if not validation_names:
            continue

        validator = get_service_validator(service_name)
        if validator is None:
            raise ValidationError(
                f"Validation config exists for '{action_key}', but no validator is registered for service '{service_name}'."
            )

        service_region = resolve_service_region(manifest, svc, default=region)
        service_zone = resolve_service_zone(manifest, svc, default=zone)

        context = ValidationContext(
            manifest=manifest,
            service=svc,
            session=session,
            region=service_region,
            zone=service_zone,
        )
        for validation_name in validation_names:
            log_message("INFO", f"Running validation: {action_key} -> {validation_name}")
            validator.run(validation_name, context)
            log_message("OK", f"Validation passed: {action_key} -> {validation_name}")
