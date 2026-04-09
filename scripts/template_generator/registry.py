from __future__ import annotations

import importlib
import pkgutil
import time
from typing import Any, Dict, List, Optional, Tuple

from utility import (
    apply_site_scope_to_target,
    normalize_service_name,
    parse_tags,
    resolve_service_region,
    utc_ts,
)

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


def _build_service_reference_map(services: List[ManifestService]) -> Dict[str, Tuple[int, str]]:
    totals: Dict[str, int] = {}
    for svc in services:
        key = f"{svc.name}:{svc.action}"
        totals[key] = totals.get(key, 0) + 1

    refs: Dict[str, Tuple[int, str]] = {}
    occurrences: Dict[str, int] = {}
    for index, svc in enumerate(services, start=1):
        key = f"{svc.name}:{svc.action}"
        occurrences[key] = occurrences.get(key, 0) + 1
        ordinal = occurrences[key]
        action_name = f"a_{svc.name}_{svc.action}_{index}"

        refs[f"{key}#{ordinal}"] = (index, action_name)
        if totals[key] == 1:
            refs[key] = (index, action_name)

    return refs


def _resolve_start_after(
    services: List[ManifestService],
) -> Dict[int, List[str]]:
    ref_map = _build_service_reference_map(services)
    resolved: Dict[int, List[str]] = {}

    for index, svc in enumerate(services, start=1):
        dependencies: List[str] = []
        seen = set()
        for ref in svc.start_after:
            target = ref_map.get(ref)
            if target is None:
                raise ValueError(
                    f"services[{index - 1}].start_after references unknown action '{ref}'. "
                    "Use '<service>:<action>' for unique actions or '<service>:<action>#<n>' when duplicates exist."
                )

            target_index, target_action_name = target
            if target_index >= index:
                raise ValueError(
                    f"services[{index - 1}].start_after reference '{ref}' must point to an earlier service action."
                )

            if target_action_name not in seen:
                dependencies.append(target_action_name)
                seen.add(target_action_name)

        resolved[index] = dependencies

    return resolved


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
        start_after = _parse_start_after_refs(s.get("start_after"), f"services[{i}].start_after")

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
                start_after=start_after,
                config=dict(s),
            )
        )

    return services


def generate_template_payload(
    manifest: Dict[str, Any],
    fis_role_arn: str,
    selection_mode: str = "ALL",
) -> Dict[str, Any]:
    services = _parse_manifest_services(manifest)
    regions = {
        str(resolve_service_region(manifest, svc.config) or "").strip()
        for svc in services
    }
    regions.discard("")
    if not regions:
        raise ValueError("FIS actions require region at the top level or service level (e.g. eu-west-1).")
    if len(regions) > 1:
        raise ValueError(
            "All FIS actions in one manifest must resolve to the same AWS Region. "
            "Use top-level region for a shared default or keep per-service region values identical."
        )

    start_after_map = _resolve_start_after(services)

    targets: Dict[str, Any] = {}
    actions: Dict[str, Any] = {}

    for idx, svc in enumerate(services, start=1):
        generator = _find_service_generator(svc.name, svc.action)
        action_id = generator.get_action_id(svc.action)
        spec = generator.get_target_spec(svc.action)
        target_key: Optional[str] = None
        target_name: Optional[str] = None

        if spec is not None:
            resource_type = spec["resourceType"]
            target_key = spec["target_key"]

            resource_arns = generator.get_resource_arns(manifest=manifest, svc=svc)
            resource_parameters = generator.get_target_parameters(manifest=manifest, svc=svc)
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
                resource_parameters=resource_parameters,
            )

            generator.apply_site_scope(
                target=targets[target_name],
                manifest=manifest,
                svc=svc,
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
            start_after=start_after_map.get(idx) or None,
        )

    template_name = f"resilience-fis-{utc_ts()}"
    return {
        "clientToken": f"{template_name}-{int(time.time())}",
        "description": template_name,
        "roleArn": fis_role_arn,
        "stopConditions": [{"source": "none"}],
        "targets": targets,
        "actions": actions,
        "experimentOptions": {"emptyTargetResolutionMode": "skip"},
        "tags": {"managed-by": "main.py"},
    }


def create_template(fis_client, payload: Dict[str, Any]) -> str:
    resp = fis_client.create_experiment_template(**payload)
    return resp["experimentTemplate"]["id"]
