from __future__ import annotations

from typing import Any, Dict

from utility import resolve_service_zone

from .base import ManifestService, ServiceTemplateGenerator


class ASGTemplateGenerator(ServiceTemplateGenerator):
    service_name = "asg"
    action_map = {
        "pause-launch": "aws:ec2:asg-insufficient-instance-capacity-error",
    }
    target_spec_map = {
        "pause-launch": {"resourceType": "aws:ec2:autoscaling-group", "target_key": "AutoScalingGroups"},
    }

    def apply_site_scope(
        self,
        *,
        target: Dict[str, Any],
        manifest: Dict[str, Any],
        svc: ManifestService,
        resource_type: str,
        resource_arns,
        apply_site_scope_to_target_fn,
    ) -> None:
        _ = target
        _ = manifest
        _ = svc
        _ = resource_type
        _ = resource_arns
        _ = apply_site_scope_to_target_fn
        # aws:ec2:asg-insufficient-instance-capacity-error already scopes the blast
        # radius via the action parameter "availabilityZoneIdentifiers". Adding a
        # second target-level AvailabilityZones filter can cause FIS target
        # resolution to return no matching ASGs.
        return

    def build_action_parameters(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        action_id: str,
    ) -> Dict[str, str]:
        if action_id != "aws:ec2:asg-insufficient-instance-capacity-error":
            return {}

        if not svc.duration:
            raise ValueError(f"{svc.name}:{svc.action} requires services[].duration (e.g. PT30M).")
        zone = resolve_service_zone(manifest, svc.config)
        if not zone or not isinstance(zone, str):
            raise ValueError(f"{svc.name}:{svc.action} requires zone at the top level or service level (e.g. eu-west-1a).")

        return {
            "duration": svc.duration,
            "availabilityZoneIdentifiers": zone,
            "percentage": "100",
        }
