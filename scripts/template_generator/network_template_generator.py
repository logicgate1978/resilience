from __future__ import annotations

from typing import Any, Dict

import boto3

from utility import resolve_service_zone

from .base import ManifestService, ServiceTemplateGenerator


class NetworkTemplateGenerator(ServiceTemplateGenerator):
    service_name = "network"
    action_map = {
        "disrupt-connectivity": "aws:network:disrupt-connectivity",
        "disrupt-vpc-endpoint": "aws:network:disrupt-vpc-endpoint",
    }
    target_spec_map = {
        "disrupt-connectivity": {"resourceType": "aws:ec2:subnet", "target_key": "Subnets"},
        "disrupt-vpc-endpoint": {"resourceType": "aws:ec2:vpc-endpoint", "target_key": "VPCEndpoints"},
    }

    def get_resource_arns(self, *, manifest: Dict[str, Any], svc: ManifestService):
        if svc.action != "disrupt-vpc-endpoint":
            return None

        from resource import collect_service_resource_arns

        region = svc.config.get("region") or manifest.get("region")
        if not region or not str(region).strip():
            raise ValueError("network:disrupt-vpc-endpoint requires region at the top level or service level.")

        arns = collect_service_resource_arns(
            svc.config,
            session=boto3.Session(),
            region=str(region).strip(),
            zone=None,
        )
        if not arns:
            raise ValueError("network:disrupt-vpc-endpoint did not resolve any VPC endpoints from the provided selectors.")
        return arns

    def build_action_parameters(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        action_id: str,
    ) -> Dict[str, str]:
        if action_id not in ("aws:network:disrupt-connectivity", "aws:network:disrupt-vpc-endpoint"):
            return {}

        if not svc.duration:
            raise ValueError(f"network:{svc.action} requires services[].duration (e.g. PT30M).")

        params = {"duration": svc.duration}
        if action_id == "aws:network:disrupt-connectivity":
            params["scope"] = "availability-zone" if resolve_service_zone(manifest, svc.config) else "all"
        return params
