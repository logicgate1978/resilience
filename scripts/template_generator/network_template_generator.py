from __future__ import annotations

from typing import Any, Dict

from .base import ManifestService, ServiceTemplateGenerator


class NetworkTemplateGenerator(ServiceTemplateGenerator):
    service_name = "network"
    action_map = {
        "disrupt-connectivity": "aws:network:disrupt-connectivity",
    }
    target_spec_map = {
        "disrupt-connectivity": {"resourceType": "aws:ec2:subnet", "target_key": "Subnets"},
    }

    def build_action_parameters(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        action_id: str,
    ) -> Dict[str, str]:
        if action_id != "aws:network:disrupt-connectivity":
            return {}

        if not svc.duration:
            raise ValueError("network:disrupt-connectivity requires services[].duration (e.g. PT30M).")
        rtype = (manifest.get("resilience_test_type") or "").strip().lower()
        return {
            "duration": svc.duration,
            "scope": "availability-zone" if rtype == "site" else "all",
        }
