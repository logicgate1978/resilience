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


def _get_scale_nodegroup_target(context: ValidationContext):
    target = context.service.get("target")
    if not isinstance(target, dict):
        return None
    cluster_identifier = target.get("cluster_identifier")
    nodegroup_name = target.get("nodegroup_name")
    if not cluster_identifier or not nodegroup_name:
        return None
    return {
        "cluster_identifier": str(cluster_identifier).strip(),
        "nodegroup_name": str(nodegroup_name).strip(),
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

    def verify_nodegroup_existence(self, context: ValidationContext) -> None:
        if context.action != "scale-nodegroup":
            self.fail(context, f"'verify_nodegroup_existence' is not supported for action '{context.action}'.")

        target = _get_scale_nodegroup_target(context)
        if target is None:
            self.fail(
                context,
                "services[].target.cluster_identifier and services[].target.nodegroup_name are required.",
            )

        eks = context.session.client("eks", region_name=context.region)
        try:
            eks.describe_nodegroup(
                clusterName=target["cluster_identifier"],
                nodegroupName=target["nodegroup_name"],
            )
        except Exception as e:
            self.fail(
                context,
                f"managed node group {target['cluster_identifier']}/{target['nodegroup_name']} was not found or was not accessible: {e}",
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

    def verify_nodegroup_scale_values(self, context: ValidationContext) -> None:
        params = context.service.get("parameters")
        if not isinstance(params, dict):
            self.fail(context, "services[].parameters must be an object and include max.")

        max_value = params.get("max")
        try:
            max_size = int(max_value)
        except Exception:
            self.fail(context, "services[].parameters.max must be an integer.")
            return

        if max_size < 0:
            self.fail(context, "services[].parameters.max must be >= 0.")

        min_value = params.get("min", 0)
        desired_value = params.get("desired", max_size)
        try:
            min_size = int(min_value)
        except Exception:
            self.fail(context, "services[].parameters.min must be an integer when provided.")
            return
        try:
            desired_size = int(desired_value)
        except Exception:
            self.fail(context, "services[].parameters.desired must be an integer when provided.")
            return

        if min_size < 0:
            self.fail(context, "services[].parameters.min must be >= 0.")
        if desired_size < 0:
            self.fail(context, "services[].parameters.desired must be >= 0.")
        if min_size > max_size:
            self.fail(context, "services[].parameters.min must be <= services[].parameters.max.")
        if desired_size < min_size or desired_size > max_size:
            self.fail(
                context,
                "services[].parameters.desired must be between services[].parameters.min and services[].parameters.max.",
            )
