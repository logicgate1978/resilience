from __future__ import annotations

from typing import Any, Dict

from .base import ManifestService, ServiceTemplateGenerator


class RDSTemplateGenerator(ServiceTemplateGenerator):
    service_name = "rds"
    action_map = {
        "reboot": "aws:rds:reboot-db-instances",
        "failover": "aws:rds:failover-db-cluster",
    }
    target_spec_map = {
        "reboot": {"resourceType": "aws:rds:db", "target_key": "DBInstances"},
        "failover": {"resourceType": "aws:rds:cluster", "target_key": "Clusters"},
    }

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
