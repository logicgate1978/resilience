from __future__ import annotations

from typing import Any, Dict

from .base import ManifestService, ServiceTemplateGenerator


class ASGTemplateGenerator(ServiceTemplateGenerator):
    service_name = "asg"
    action_map = {
        "pause-launch": "aws:ec2:asg-insufficient-instance-capacity-error",
    }
    target_spec_map = {
        "pause-launch": {"resourceType": "aws:ec2:autoscaling-group", "target_key": "AutoScalingGroups"},
    }

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
        zone = manifest.get("zone")
        if not zone or not isinstance(zone, str):
            raise ValueError(f"{svc.name}:{svc.action} requires top-level 'zone' (e.g. eu-west-1a).")

        return {
            "duration": svc.duration,
            "availabilityZoneIdentifiers": zone,
            "percentage": "100",
        }
