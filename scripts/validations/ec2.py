from validations.base import BaseServiceValidator, ValidationContext


class EC2Validator(BaseServiceValidator):
    service_name = "ec2"

    def verify_resource_existence(self, context: ValidationContext) -> None:
        arns = context.get_selected_resource_arns()
        if arns:
            return
        self.fail(
            context,
            f"no resources matched the selection criteria ({context.selection_summary()}).",
        )
