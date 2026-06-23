from __future__ import annotations

from providers.azure.resource import chaos_target_id, parse_resource_id, resource_label
from providers.azure.validations.base import BaseServiceValidator, ValidationContext


COMPUTE_VM_API_VERSION = "2024-07-01"
CHAOS_STUDIO_API_VERSION = "2025-01-01"
VM_SHUTDOWN_TARGET_TYPE = "Microsoft-VirtualMachine"
VM_RESOURCE_TYPE = ("microsoft.compute", "virtualmachines")
VM_ACTION_CAPABILITIES = {
    "stop": "Shutdown-1.0",
    "shutdown": "Shutdown-1.0",
    "redeploy": "Redeploy-1.0",
}


class VMValidator(BaseServiceValidator):
    service_name = "vm"

    def _target_vm_resource_ids(self, context: ValidationContext):
        resource_ids = context.target_resource_ids()
        if not resource_ids:
            self.fail(context, f"no Azure VM resource IDs were provided ({context.selection_summary()}).")
        for resource_id in resource_ids:
            parsed = parse_resource_id(resource_id)
            if not parsed:
                self.fail(context, f"target resource ID is empty ({context.selection_summary()}).")
            actual = (parsed.provider_namespace.lower(), parsed.resource_type.lower())
            if actual != VM_RESOURCE_TYPE:
                self.fail(
                    context,
                    "target must be a Microsoft.Compute/virtualMachines resource ID; "
                    f"received {parsed.provider_namespace}/{parsed.resource_type} for {parsed.name}.",
                )
            yield resource_id

    def verify_vm_exists(self, context: ValidationContext) -> None:
        for resource_id in self._target_vm_resource_ids(context):
            context.get_resource(resource_id, COMPUTE_VM_API_VERSION)

    def verify_chaos_target_exists(self, context: ValidationContext) -> None:
        for resource_id in self._target_vm_resource_ids(context):
            target_id = chaos_target_id(resource_id, VM_SHUTDOWN_TARGET_TYPE)
            try:
                context.get_resource(target_id, CHAOS_STUDIO_API_VERSION)
            except Exception as exc:
                self.fail(
                    context,
                    f"Chaos Studio target {VM_SHUTDOWN_TARGET_TYPE} was not found for {resource_label(resource_id)}. "
                    f"Onboard the VM to Chaos Studio before running {context.action_key}. "
                    f"Details: {exc}",
                )

    def verify_chaos_capability_exists(self, context: ValidationContext) -> None:
        capability_name = VM_ACTION_CAPABILITIES.get(context.action)
        if not capability_name:
            self.fail(context, f"no Chaos Studio capability mapping is configured for {context.action_key}.")

        for resource_id in self._target_vm_resource_ids(context):
            capability_id = f"{chaos_target_id(resource_id, VM_SHUTDOWN_TARGET_TYPE)}/capabilities/{capability_name}"
            try:
                context.get_resource(capability_id, CHAOS_STUDIO_API_VERSION)
            except Exception as exc:
                self.fail(
                    context,
                    f"Chaos Studio capability {capability_name} was not found for {resource_label(resource_id)}. "
                    f"Enable the {capability_name} action on the VM target before running {context.action_key}. "
                    f"Details: {exc}",
                )
