from kubernetes.client.exceptions import ApiException

from component_actions.k8s_auth import create_apps_v1_api
from validations.base import BaseServiceValidator, ValidationContext


def _get_scale_deployment_target(context: ValidationContext):
    target = context.service.get("target")
    if not isinstance(target, dict):
        return None
    cluster_identifier = target.get("cluster_identifier")
    namespace = target.get("namespace")
    deployment_name = target.get("deployment_name")
    if not cluster_identifier or not namespace or not deployment_name:
        return None
    return {
        "cluster_identifier": str(cluster_identifier).strip(),
        "namespace": str(namespace).strip(),
        "deployment_name": str(deployment_name).strip(),
    }


class EKSValidator(BaseServiceValidator):
    service_name = "eks"

    def verify_deployment_existence(self, context: ValidationContext) -> None:
        if context.action != "scale-deployment":
            self.fail(context, f"'verify_deployment_existence' is not supported for action '{context.action}'.")

        target = _get_scale_deployment_target(context)
        if target is None:
            self.fail(
                context,
                "services[].target.cluster_identifier, services[].target.namespace, and "
                "services[].target.deployment_name are required.",
            )

        api = create_apps_v1_api(context.session, context.region, target["cluster_identifier"])
        try:
            api.read_namespaced_deployment(
                name=target["deployment_name"],
                namespace=target["namespace"],
            )
        except ApiException as e:
            self.fail(
                context,
                f"deployment {target['namespace']}/{target['deployment_name']} was not found or was not accessible: {e}",
            )
        except Exception as e:
            self.fail(
                context,
                f"unable to connect to the Kubernetes API for cluster {target['cluster_identifier']}: {e}",
            )

    def verify_replicas_value(self, context: ValidationContext) -> None:
        params = context.service.get("parameters")
        if not isinstance(params, dict):
            self.fail(context, "services[].parameters must be an object and include replicas.")

        value = params.get("replicas")
        try:
            replicas = int(value)
        except Exception:
            self.fail(context, "services[].parameters.replicas must be an integer.")
            return

        if replicas < 0:
            self.fail(context, "services[].parameters.replicas must be >= 0.")
