from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Type


@dataclass
class ManifestService:
    name: str
    action: str
    duration: Optional[str] = None
    tags: Optional[str] = None
    iam_role_arns: Optional[List[str]] = None
    iam_roles: Optional[str] = None
    instance_count: Optional[int] = None


def build_target(
    *,
    name: str,
    resource_type: str,
    selection_mode: str,
    resource_tags: Optional[Dict[str, str]] = None,
    resource_arns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    t: Dict[str, Any] = {"resourceType": resource_type, "selectionMode": selection_mode}

    if resource_arns is not None:
        t["resourceArns"] = resource_arns
        return t

    if resource_tags:
        t["resourceTags"] = resource_tags
        return t

    raise ValueError(f"Target '{name}' is unconstrained (no tags and no resourceArns).")


class ServiceTemplateGenerator(ABC):
    registry: ClassVar[List[Type["ServiceTemplateGenerator"]]] = []
    service_name: ClassVar[Optional[str]] = None
    action_map: ClassVar[Dict[str, str]] = {}
    target_spec_map: ClassVar[Dict[str, Dict[str, str]]] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.service_name:
            ServiceTemplateGenerator.registry.append(cls)

    def supports(self, service_name: str, action: str) -> bool:
        return service_name == self.service_name and action in self.action_map

    def get_action_id(self, action: str) -> str:
        return self.action_map[action]

    def get_target_spec(self, action: str) -> Dict[str, str]:
        return self.target_spec_map[action]

    def get_selection_mode(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        default_selection_mode: str,
    ) -> str:
        _ = manifest
        _ = svc
        return default_selection_mode

    def get_resource_arns(self, *, manifest: Dict[str, Any], svc: ManifestService) -> Optional[List[str]]:
        _ = manifest
        _ = svc
        return None

    def apply_site_scope(
        self,
        *,
        target: Dict[str, Any],
        manifest: Dict[str, Any],
        resource_type: str,
        resource_arns: Optional[List[str]],
        apply_site_scope_to_target_fn,
    ) -> None:
        rtype = (manifest.get("resilience_test_type") or "").strip().lower()
        zone = manifest.get("zone")
        if rtype == "site" and resource_arns is None and isinstance(zone, str):
            apply_site_scope_to_target_fn(target, resource_type, zone)

    def build_action_parameters(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        action_id: str,
    ) -> Dict[str, str]:
        _ = manifest
        _ = svc
        _ = action_id
        return {}

    def build_action(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        action_id: str,
        target_key: str,
        target_ref_name: str,
        start_after: Optional[str],
    ) -> Dict[str, Any]:
        _ = start_after
        action_obj: Dict[str, Any] = {
            "actionId": action_id,
            "description": f"{svc.name}:{svc.action}",
            "targets": {target_key: target_ref_name},
        }

        params = self.build_action_parameters(manifest=manifest, svc=svc, action_id=action_id)
        if params:
            action_obj["parameters"] = params

        return action_obj
