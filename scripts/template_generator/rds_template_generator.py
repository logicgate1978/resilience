from __future__ import annotations

import boto3
from typing import Any, Dict, List, Optional

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

    def get_resource_arns(self, *, manifest: Dict[str, Any], svc: ManifestService) -> Optional[List[str]]:
        identifier = str(svc.config.get("identifier") or "").strip()
        if not identifier:
            return None

        from resource import collect_service_resource_arns
        from utility import resolve_service_region, resolve_service_zone

        region = resolve_service_region(manifest, svc.config)
        if not region:
            raise ValueError(f"{svc.name}:{svc.action} requires region at the top level or service level.")

        arns = collect_service_resource_arns(
            svc.config,
            session=boto3.Session(region_name=region),
            region=region,
            zone=resolve_service_zone(manifest, svc.config),
        )
        if not arns:
            raise ValueError(
                f"{svc.name}:{svc.action} did not resolve any RDS resources for identifier '{identifier}'."
            )
        return arns

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
