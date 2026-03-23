from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .base import ManifestService, ServiceTemplateGenerator


class EKSTemplateGenerator(ServiceTemplateGenerator):
    service_name = "eks"
    action_map = {
        "delete-pod": "aws:eks:pod-delete",
        "pod-delete": "aws:eks:pod-delete",
        "cpu-stress": "aws:eks:pod-cpu-stress",
        "pod-cpu-stress": "aws:eks:pod-cpu-stress",
        "io-stress": "aws:eks:pod-io-stress",
        "pod-io-stress": "aws:eks:pod-io-stress",
        "memory-stress": "aws:eks:pod-memory-stress",
        "pod-memory-stress": "aws:eks:pod-memory-stress",
    }
    target_spec_map = {
        "delete-pod": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
        "pod-delete": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
        "cpu-stress": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
        "pod-cpu-stress": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
        "io-stress": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
        "pod-io-stress": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
        "memory-stress": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
        "pod-memory-stress": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
    }

    def get_selection_mode(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        default_selection_mode: str,
    ) -> str:
        _ = manifest
        target_cfg = self._get_target_cfg(svc)
        selection_mode = target_cfg.get("selection_mode") or target_cfg.get("selectionMode")
        if selection_mode:
            return str(selection_mode)

        count = target_cfg.get("count")
        if count is not None:
            return f"COUNT({int(count)})"

        return default_selection_mode

    def get_target_parameters(self, *, manifest: Dict[str, Any], svc: ManifestService) -> Optional[Dict[str, str]]:
        _ = manifest
        target_cfg = self._get_target_cfg(svc)
        cluster_identifier = self._require_str(
            target_cfg,
            ["cluster_identifier", "clusterIdentifier"],
            "services[].target.cluster_identifier",
        )
        namespace = self._require_str(
            target_cfg,
            ["namespace"],
            "services[].target.namespace",
        )
        selector_type = self._require_str(
            target_cfg,
            ["selector_type", "selectorType"],
            "services[].target.selector_type",
        )
        selector_value = self._require_str(
            target_cfg,
            ["selector_value", "selectorValue"],
            "services[].target.selector_value",
        )
        return {
            "clusterIdentifier": cluster_identifier,
            "namespace": namespace,
            "selectorType": selector_type,
            "selectorValue": selector_value,
        }

    def build_action_parameters(
        self,
        *,
        manifest: Dict[str, Any],
        svc: ManifestService,
        action_id: str,
    ) -> Dict[str, str]:
        _ = manifest
        supported_action_ids = {
            "aws:eks:pod-delete",
            "aws:eks:pod-cpu-stress",
            "aws:eks:pod-io-stress",
            "aws:eks:pod-memory-stress",
        }
        if action_id not in supported_action_ids:
            return {}

        action_cfg = self._get_action_cfg(svc)
        params = {
            "kubernetesServiceAccount": self._require_service_str(
                svc,
                action_cfg,
                ["kubernetes_service_account", "kubernetesServiceAccount"],
                "services[].kubernetes_service_account or services[].parameters.kubernetes_service_account",
            )
        }

        if action_id == "aws:eks:pod-delete":
            grace_period_seconds = self._optional_value(
                action_cfg,
                ["grace_period_seconds", "gracePeriodSeconds"],
            )
            if grace_period_seconds is not None:
                params["gracePeriodSeconds"] = str(grace_period_seconds)

        if action_id in (
            "aws:eks:pod-cpu-stress",
            "aws:eks:pod-io-stress",
            "aws:eks:pod-memory-stress",
        ):
            if not svc.duration or not str(svc.duration).strip():
                raise ValueError(f"eks:{svc.action} requires services[].duration (e.g. PT2M).")
            params["duration"] = str(svc.duration).strip()

            workers = self._optional_value(action_cfg, ["workers"])
            if workers is not None:
                params["workers"] = str(workers)

            percent = self._optional_value(action_cfg, ["percent"])
            if percent is not None:
                params["percent"] = str(percent)

        fis_pod_container_image = self._optional_value(
            action_cfg,
            ["fis_pod_container_image", "fisPodContainerImage"],
        )
        if fis_pod_container_image is not None:
            params["fisPodContainerImage"] = str(fis_pod_container_image)

        max_errors_percent = self._optional_value(
            action_cfg,
            ["max_errors_percent", "maxErrorsPercent"],
        )
        if max_errors_percent is not None:
            params["maxErrorsPercent"] = str(max_errors_percent)

        fis_pod_labels = self._optional_value(
            action_cfg,
            ["fis_pod_labels", "fisPodLabels"],
        )
        if fis_pod_labels is not None:
            params["fisPodLabels"] = self._stringify_parameter_value(fis_pod_labels)

        fis_pod_annotations = self._optional_value(
            action_cfg,
            ["fis_pod_annotations", "fisPodAnnotations"],
        )
        if fis_pod_annotations is not None:
            params["fisPodAnnotations"] = self._stringify_parameter_value(fis_pod_annotations)

        fis_pod_security_policy = self._optional_value(
            action_cfg,
            ["fis_pod_security_policy", "fisPodSecurityPolicy"],
        )
        if fis_pod_security_policy is not None:
            params["fisPodSecurityPolicy"] = str(fis_pod_security_policy)

        return params

    def _get_target_cfg(self, svc: ManifestService) -> Dict[str, Any]:
        target_cfg = svc.config.get("target")
        if isinstance(target_cfg, dict):
            return target_cfg
        raise ValueError(f"eks:{svc.action} requires services[].target to be an object.")

    def _get_action_cfg(self, svc: ManifestService) -> Dict[str, Any]:
        action_cfg = svc.config.get("parameters")
        if isinstance(action_cfg, dict):
            return action_cfg
        return {}

    def _require_str(self, source: Dict[str, Any], keys, field_name: str) -> str:
        value = self._optional_value(source, keys)
        if value is None or str(value).strip() == "":
            raise ValueError(f"eks action requires {field_name}.")
        return str(value).strip()

    def _require_service_str(self, svc: ManifestService, source: Dict[str, Any], keys, field_name: str) -> str:
        value = self._optional_service_value(svc, source, keys)
        if value is None or str(value).strip() == "":
            raise ValueError(f"eks action requires {field_name}.")
        return str(value).strip()

    def _optional_value(self, source: Dict[str, Any], keys) -> Optional[Any]:
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
        return None

    def _optional_service_value(self, svc: ManifestService, source: Dict[str, Any], keys) -> Optional[Any]:
        value = self._optional_value(source, keys)
        if value is not None:
            return value
        for key in keys:
            if key in svc.config and svc.config[key] is not None:
                return svc.config[key]
        return None

    def _stringify_parameter_value(self, value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)
