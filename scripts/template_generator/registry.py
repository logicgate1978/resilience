from __future__ import annotations

import importlib
import pkgutil
import time
from typing import Any, Dict, List, Optional

from utility import apply_site_scope_to_target, normalize_service_name, parse_tags, utc_ts

from .base import ManifestService, ServiceTemplateGenerator, build_target


def _load_service_modules() -> None:
    package_name = __package__
    package = importlib.import_module(package_name)

    for module_info in pkgutil.iter_modules(package.__path__):
        if not module_info.name.endswith("_template_generator"):
            continue
        importlib.import_module(f"{package_name}.{module_info.name}")


def _get_service_generators() -> List[ServiceTemplateGenerator]:
    _load_service_modules()
    return [cls() for cls in ServiceTemplateGenerator.registry]


def _find_service_generator(service_name: str, action: str) -> ServiceTemplateGenerator:
    for generator in _get_service_generators():
        if generator.supports(service_name, action):
            return generator
    raise ValueError(f"Unsupported service action: {service_name}:{action}")


def _parse_manifest_services(manifest: Dict[str, Any]) -> List[ManifestService]:
    services_raw = manifest.get("services")
    if not isinstance(services_raw, list) or not services_raw:
        raise ValueError("Top-level 'services' must be a non-empty list.")

    services: List[ManifestService] = []
    for i, s in enumerate(services_raw):
        if not isinstance(s, dict):
            raise ValueError(f"services[{i}] must be an object.")
        name = normalize_service_name(s.get("name"))
        action = (s.get("action") or "").strip().lower()
        duration = s.get("duration")
        tags = s.get("tags")
        iam_role_arns = s.get("iam_role_arns")
        iam_roles = s.get("iam_roles")
        instance_count = s.get("instance_count")

        if not name or not action:
            raise ValueError(f"services[{i}] must include 'name' and 'action'.")

        if iam_role_arns is not None and not isinstance(iam_role_arns, list):
            raise ValueError(f"services[{i}].iam_role_arns must be a list if provided.")

        if iam_roles is not None and not isinstance(iam_roles, str):
            raise ValueError(f"services[{i}].iam_roles must be a comma-separated string if provided.")

        if instance_count is not None:
            try:
                instance_count = int(instance_count)
            except Exception:
                raise ValueError(f"services[{i}].instance_count must be an integer if provided.")
            if instance_count <= 0:
                raise ValueError(f"services[{i}].instance_count must be > 0.")

        _find_service_generator(name, action)

        services.append(
            ManifestService(
                name=name,
                action=action,
                duration=duration,
                tags=tags,
                iam_role_arns=iam_role_arns,
                iam_roles=iam_roles,
                instance_count=instance_count,
            )
        )

    return services


def generate_template_payload(
    manifest: Dict[str, Any],
    fis_role_arn: str,
    selection_mode: str = "ALL",
) -> Dict[str, Any]:
    region = manifest.get("region")
    if not region or not isinstance(region, str):
        raise ValueError("Top-level 'region' is required (e.g. eu-west-1).")

    rtype = (manifest.get("resilience_test_type") or "").strip().lower()
    if rtype not in ("component", "site"):
        raise ValueError("Top-level 'resilience_test_type' must be 'component' or 'site'.")

    zone = manifest.get("zone")
    if rtype == "site":
        if not zone or not isinstance(zone, str):
            raise ValueError("For resilience_test_type: site, top-level 'zone' is required (e.g. eu-west-1a).")

    services = _parse_manifest_services(manifest)

    targets: Dict[str, Any] = {}
    actions: Dict[str, Any] = {}
    prev_action_name: Optional[str] = None

    for idx, svc in enumerate(services, start=1):
        generator = _find_service_generator(svc.name, svc.action)
        action_id = generator.get_action_id(svc.action)
        spec = generator.get_target_spec(svc.action)
        resource_type = spec["resourceType"]
        target_key = spec["target_key"]

        resource_arns = generator.get_resource_arns(manifest=manifest, svc=svc)
        this_selection_mode = generator.get_selection_mode(
            manifest=manifest,
            svc=svc,
            default_selection_mode=selection_mode,
        )

        target_name = f"t_{svc.name}_{svc.action}_{idx}"
        targets[target_name] = build_target(
            name=target_name,
            resource_type=resource_type,
            selection_mode=this_selection_mode,
            resource_tags=parse_tags(svc.tags) if (resource_arns is None) else None,
            resource_arns=resource_arns,
        )

        generator.apply_site_scope(
            target=targets[target_name],
            manifest=manifest,
            resource_type=resource_type,
            resource_arns=resource_arns,
            apply_site_scope_to_target_fn=apply_site_scope_to_target,
        )

        action_name = f"a_{svc.name}_{svc.action}_{idx}"
        actions[action_name] = generator.build_action(
            manifest=manifest,
            svc=svc,
            action_id=action_id,
            target_key=target_key,
            target_ref_name=target_name,
            start_after=prev_action_name,
        )
        prev_action_name = action_name

    template_name = f"resilience-{rtype}-{utc_ts()}"
    return {
        "clientToken": f"{template_name}-{int(time.time())}",
        "description": template_name,
        "roleArn": fis_role_arn,
        "stopConditions": [{"source": "none"}],
        "targets": targets,
        "actions": actions,
        "experimentOptions": {"emptyTargetResolutionMode": "skip"},
        "tags": {"managed-by": "fis.py"},
    }


def create_template(fis_client, payload: Dict[str, Any]) -> str:
    resp = fis_client.create_experiment_template(**payload)
    return resp["experimentTemplate"]["id"]
