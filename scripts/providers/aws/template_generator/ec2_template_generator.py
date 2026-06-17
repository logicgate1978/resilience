from __future__ import annotations

from typing import Any, Dict, List, Optional

from utility import resolve_iam_role_arns_from_names, resolve_service_zone

from .base import ManifestService, ServiceTemplateGenerator


class EC2TemplateGenerator(ServiceTemplateGenerator):
    service_name = "ec2"
    action_map = {
        "pause-launch": "aws:ec2:api-insufficient-instance-capacity-error",
        "stop": "aws:ec2:stop-instances",
        "reboot": "aws:ec2:reboot-instances",
        "terminate": "aws:ec2:terminate-instances",
    }
    target_spec_map = {
        "pause-launch": {"resourceType": "aws:iam:role", "target_key": "Roles"},
        "stop": {"resourceType": "aws:ec2:instance", "target_key": "Instances"},
        "reboot": {"resourceType": "aws:ec2:instance", "target_key": "Instances"},
        "terminate": {"resourceType": "aws:ec2:instance", "target_key": "Instances"},
    }

    def get_selection_mode(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        default_selection_mode: str,
    ) -> str:
        _ = manifest
        if svc.instance_count is not None:
            return f"COUNT({svc.instance_count})"
        return "ALL" if default_selection_mode == "ALL" else default_selection_mode

    def get_resource_arns(self, *, manifest: Dict[str, Any], svc: ManifestService) -> Optional[List[str]]:
        _ = manifest
        if svc.action != "pause-launch":
            return None

        if svc.iam_role_arns:
            return svc.iam_role_arns

        role_names = svc.iam_roles or "BAU,Admin,scb-user-instance-role"
        resolved = resolve_iam_role_arns_from_names(role_names)
        if not resolved:
            raise ValueError(
                "ec2:pause-launch requires IAM role ARNs. Provide services[].iam_roles (role names) "
                "or services[].iam_role_arns (list of role ARNs)."
            )
        return resolved

    def build_action_parameters(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        action_id: str,
    ) -> Dict[str, str]:
        params: Dict[str, str] = {}

        if action_id == "aws:ec2:stop-instances":
            if svc.duration and str(svc.duration).strip():
                params["startInstancesAfterDuration"] = str(svc.duration).strip()
                params["completeIfInstancesTerminated"] = "true"

        if action_id == "aws:ec2:api-insufficient-instance-capacity-error":
            if not svc.duration:
                raise ValueError(f"{svc.name}:{svc.action} requires services[].duration (e.g. PT30M).")
            zone = resolve_service_zone(manifest, svc.config)
            if not zone or not isinstance(zone, str):
                raise ValueError(f"{svc.name}:{svc.action} requires zone at the top level or service level (e.g. eu-west-1a).")
            params["duration"] = svc.duration
            params["availabilityZoneIdentifiers"] = zone
            params["percentage"] = "100"

        return params
