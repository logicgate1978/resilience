from __future__ import annotations

from typing import Any, Dict

from .base import ManifestService, ServiceTemplateGenerator


class CommonTemplateGenerator(ServiceTemplateGenerator):
    service_name = "common"
    action_map = {
        "wait": "aws:fis:wait",
    }
    target_spec_map = {}

    def build_action_parameters(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        action_id: str,
    ) -> Dict[str, str]:
        _ = manifest
        if action_id != "aws:fis:wait":
            return {}
        if not svc.duration:
            raise ValueError("common:wait requires services[].duration (for example PT2M).")
        return {"duration": svc.duration}
