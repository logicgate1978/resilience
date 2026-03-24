from datetime import datetime, timezone
from typing import Any, Dict

from kubernetes.client.exceptions import ApiException

from component_actions.base import CustomComponentAction
from component_actions.k8s_auth import create_apps_v1_api
from utility import normalize_service_name


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_target_cfg(svc: Dict[str, Any]) -> Dict[str, Any]:
    target = svc.get("target")
    if not isinstance(target, dict):
        raise ValueError("eks:scale-deployment requires services[].target to be an object.")
    return target


def _get_parameters_cfg(svc: Dict[str, Any]) -> Dict[str, Any]:
    params = svc.get("parameters")
    if not isinstance(params, dict):
        raise ValueError("eks:scale-deployment requires services[].parameters to be an object.")
    return params


def _require_str(source: Dict[str, Any], key: str, field_name: str) -> str:
    value = source.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"eks:scale-deployment requires {field_name}.")
    return str(value).strip()


def _require_int(source: Dict[str, Any], key: str, field_name: str) -> int:
    value = source.get(key)
    try:
        parsed = int(value)
    except Exception:
        raise ValueError(f"eks:scale-deployment requires integer {field_name}.")
    if parsed < 0:
        raise ValueError(f"eks:scale-deployment requires non-negative {field_name}.")
    return parsed


def _optional_bool(source: Dict[str, Any], key: str, default: bool) -> bool:
    value = source.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def _optional_int(source: Dict[str, Any], key: str, default: int) -> int:
    value = source.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _is_deployment_ready(deployment, desired_replicas: int) -> bool:
    status = deployment.status
    metadata = deployment.metadata
    spec = deployment.spec

    observed_generation = int(status.observed_generation or 0)
    generation = int(metadata.generation or 0)
    spec_replicas = int(spec.replicas or 0)
    replicas = int(status.replicas or 0)
    updated_replicas = int(status.updated_replicas or 0)
    ready_replicas = int(status.ready_replicas or 0)
    available_replicas = int(status.available_replicas or 0)

    if observed_generation < generation:
        return False
    if spec_replicas != desired_replicas:
        return False

    if desired_replicas == 0:
        return replicas == 0 and ready_replicas == 0 and available_replicas == 0

    return (
        replicas == desired_replicas
        and updated_replicas == desired_replicas
        and ready_replicas == desired_replicas
        and available_replicas == desired_replicas
    )


class EKSScaleDeploymentAction(CustomComponentAction):
    service_name = "eks"
    action_names = ["scale-deployment"]

    def build_plan_item(
        self,
        *,
        manifest: Dict[str, Any],
        svc: Dict[str, Any],
        index: int,
        default_timeout_seconds: int,
    ) -> Dict[str, Any]:
        _ = manifest
        target = _get_target_cfg(svc)
        params = _get_parameters_cfg(svc)

        cluster_identifier = _require_str(target, "cluster_identifier", "services[].target.cluster_identifier")
        namespace = _require_str(target, "namespace", "services[].target.namespace")
        deployment_name = _require_str(target, "deployment_name", "services[].target.deployment_name")
        replicas = _require_int(params, "replicas", "services[].parameters.replicas")
        wait_for_ready = _optional_bool(params, "wait_for_ready", True)
        item_timeout_seconds = _optional_int(params, "timeout_seconds", default_timeout_seconds)

        return {
            "name": f"a_eks_scale-deployment_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:{str(svc.get('action') or '').strip().lower()}",
            "action": "scale-deployment",
            "description": f"Scale Kubernetes deployment {namespace}/{deployment_name} to {replicas} replica(s)",
            "target": {
                "clusterIdentifier": cluster_identifier,
                "namespace": namespace,
                "deploymentName": deployment_name,
            },
            "parameters": {
                "replicas": replicas,
                "waitForReady": wait_for_ready,
                "timeoutSeconds": item_timeout_seconds,
            },
            "impacted_resource": {
                "service": "eks:scale-deployment",
                "arn": f"eks://{cluster_identifier}/{namespace}/deployment/{deployment_name}",
                "selection_mode": "CUSTOM",
            },
        }

    def execute_item(
        self,
        *,
        session,
        item: Dict[str, Any],
        poll_seconds: int,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        import time

        started_at = _utc_now_iso()
        target = item["target"]
        params = item["parameters"]
        cluster_identifier = target["clusterIdentifier"]
        namespace = target["namespace"]
        deployment_name = target["deploymentName"]
        desired_replicas = int(params["replicas"])
        wait_for_ready = bool(params["waitForReady"])
        effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)

        try:
            api = create_apps_v1_api(session, item["region"], cluster_identifier)
            deployment = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        except ApiException as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"Kubernetes API error while reading deployment: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {"target": target, "parameters": params},
            }
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"Failed to initialize Kubernetes API access for cluster {cluster_identifier}: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {"target": target, "parameters": params},
            }

        original_replicas = int(deployment.spec.replicas or 0)
        body = {"spec": {"replicas": desired_replicas}}

        try:
            api.patch_namespaced_deployment_scale(
                name=deployment_name,
                namespace=namespace,
                body=body,
            )
        except ApiException as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"Kubernetes API error while scaling deployment: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": target,
                    "parameters": params,
                    "originalReplicas": original_replicas,
                },
            }

        if wait_for_ready:
            deadline = time.time() + effective_timeout_seconds
            last_snapshot: Dict[str, Any] = {}
            while True:
                try:
                    deployment = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
                except Exception as e:
                    ended_at = _utc_now_iso()
                    return {
                        "name": item["name"],
                        "status": "failed",
                        "reason": f"Kubernetes API error while waiting for deployment readiness: {e}",
                        "startTime": started_at,
                        "endTime": ended_at,
                        "details": {
                            "target": target,
                            "parameters": params,
                            "originalReplicas": original_replicas,
                        },
                    }
                status = deployment.status
                last_snapshot = {
                    "replicas": int(status.replicas or 0),
                    "updatedReplicas": int(status.updated_replicas or 0),
                    "readyReplicas": int(status.ready_replicas or 0),
                    "availableReplicas": int(status.available_replicas or 0),
                    "observedGeneration": int(status.observed_generation or 0),
                    "generation": int(deployment.metadata.generation or 0),
                }
                if _is_deployment_ready(deployment, desired_replicas):
                    break
                if time.time() > deadline:
                    ended_at = _utc_now_iso()
                    return {
                        "name": item["name"],
                        "status": "failed",
                        "reason": (
                            f"Timed out waiting for deployment {namespace}/{deployment_name} "
                            f"to reach {desired_replicas} replica(s)."
                        ),
                        "startTime": started_at,
                        "endTime": ended_at,
                        "details": {
                            "target": target,
                            "parameters": params,
                            "originalReplicas": original_replicas,
                            "lastObservedStatus": last_snapshot,
                        },
                    }
                time.sleep(max(1, poll_seconds))

        try:
            final_deployment = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
            final_status = final_deployment.status
            final_status_out = {
                "replicas": int(final_status.replicas or 0),
                "updatedReplicas": int(final_status.updated_replicas or 0),
                "readyReplicas": int(final_status.ready_replicas or 0),
                "availableReplicas": int(final_status.available_replicas or 0),
                "observedGeneration": int(final_status.observed_generation or 0),
                "generation": int(final_deployment.metadata.generation or 0),
            }
        except Exception as e:
            final_status_out = {
                "error": f"Unable to read final deployment state: {e}",
            }

        ended_at = _utc_now_iso()
        return {
            "name": item["name"],
            "status": "completed",
            "reason": None,
            "startTime": started_at,
            "endTime": ended_at,
            "details": {
                "target": target,
                "parameters": params,
                "originalReplicas": original_replicas,
                "finalStatus": final_status_out,
            },
        }
