from validations.base import BaseServiceValidator, ValidationContext


class NetworkValidator(BaseServiceValidator):
    service_name = "network"
    _SUPPORTED_FIS_VPC_ENDPOINT_TYPES = {"interface"}

    def verify_vpc_endpoint_type(self, context: ValidationContext) -> None:
        if context.action != "disrupt-vpc-endpoint":
            return

        target = context.service.get("target") or {}
        if not isinstance(target, dict):
            return

        raw_value = str(target.get("vpc_endpoint_type") or "").strip()
        if not raw_value:
            return

        if raw_value.lower() not in self._SUPPORTED_FIS_VPC_ENDPOINT_TYPES:
            self.fail(
                context,
                "service.target.vpc_endpoint_type must be 'Interface' for the FIS-backed "
                "network:disrupt-vpc-endpoint action.",
            )

    def verify_resource_existence(self, context: ValidationContext) -> None:
        arns = context.get_selected_resource_arns()
        if arns:
            return
        self.fail(
            context,
            f"no resources matched the selection criteria ({context.selection_summary()}).",
        )
