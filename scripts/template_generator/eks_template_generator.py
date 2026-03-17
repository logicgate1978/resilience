from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ManifestService, ServiceTemplateGenerator


class EKSTemplateGenerator(ServiceTemplateGenerator):
    service_name = "eks"
    action_map = {
        "delete-pod": "aws:eks:pod-delete",
    }
    target_spec_map = {
        "delete-pod": {"resourceType": "aws:eks:pod", "target_key": "Pods"},
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
        if action_id != "aws:eks:pod-delete":
            return {}

        action_cfg = self._get_action_cfg(svc)
        params = {
            "kubernetesServiceAccount": self._require_str(
                action_cfg,
                ["kubernetes_service_account", "kubernetesServiceAccount"],
                "services[].parameters.kubernetes_service_account",
            )
        }

        grace_period_seconds = self._optional_value(
            action_cfg,
            ["grace_period_seconds", "gracePeriodSeconds"],
        )
        if grace_period_seconds is not None:
            params["gracePeriodSeconds"] = str(grace_period_seconds)

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

        return params

    def _get_target_cfg(self, svc: ManifestService) -> Dict[str, Any]:
        target_cfg = svc.config.get("target")
        if isinstance(target_cfg, dict):
            return target_cfg
        raise ValueError("eks:delete-pod requires services[].target to be an object.")

    def _get_action_cfg(self, svc: ManifestService) -> Dict[str, Any]:
        action_cfg = svc.config.get("parameters")
        if isinstance(action_cfg, dict):
            return action_cfg
        raise ValueError("eks:delete-pod requires services[].parameters to be an object.")

    def _require_str(self, source: Dict[str, Any], keys, field_name: str) -> str:
        value = self._optional_value(source, keys)
        if value is None or str(value).strip() == "":
            raise ValueError(f"eks:delete-pod requires {field_name}.")
        return str(value).strip()

    def _optional_value(self, source: Dict[str, Any], keys) -> Optional[Any]:
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
        return None
