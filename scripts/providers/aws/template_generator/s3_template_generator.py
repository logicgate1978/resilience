from __future__ import annotations

from typing import Any, Dict

from .base import ServiceTemplateGenerator


class S3TemplateGenerator(ServiceTemplateGenerator):
    service_name = "s3"
    action_map = {
        "pause-replication": "aws:s3:bucket-pause-replication",
        "pause-relication": "aws:s3:bucket-pause-replication",
    }
    target_spec_map = {
        "pause-replication": {"resourceType": "aws:s3:bucket", "target_key": "Buckets"},
        "pause-relication": {"resourceType": "aws:s3:bucket", "target_key": "Buckets"},
    }

    def build_action_parameters(
        self,
        *,
        manifest: Dict[str, Any],
        svc,
        action_id: str,
    ) -> Dict[str, str]:
        _ = manifest
        if action_id != "aws:s3:bucket-pause-replication":
            return {}

        cfg = svc.config
        if not svc.duration or not str(svc.duration).strip():
            raise ValueError("s3:pause-replication requires services[].duration (e.g. PT5M).")

        destination_region = cfg.get("destination_region") or cfg.get("destinationRegion")
        if destination_region is None or str(destination_region).strip() == "":
            raise ValueError(
                "s3:pause-replication requires services[].destination_region "
                "(the Region where destination buckets are located)."
            )

        params: Dict[str, str] = {
            "duration": str(svc.duration).strip(),
            "region": str(destination_region).strip(),
        }

        destination_buckets = cfg.get("destination_buckets") or cfg.get("destinationBuckets")
        if destination_buckets is not None:
            if isinstance(destination_buckets, list):
                params["destinationBuckets"] = ",".join(str(x).strip() for x in destination_buckets if str(x).strip())
            else:
                params["destinationBuckets"] = str(destination_buckets).strip()

        prefixes = cfg.get("prefixes")
        if prefixes is not None:
            if isinstance(prefixes, list):
                params["prefixes"] = ",".join(str(x).strip() for x in prefixes if str(x).strip())
            else:
                params["prefixes"] = str(prefixes).strip()

        return params
