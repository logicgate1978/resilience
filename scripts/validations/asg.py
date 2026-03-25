from validations.base import BaseServiceValidator, ValidationContext


class ASGValidator(BaseServiceValidator):
    service_name = "asg"

    def verify_resource_existence(self, context: ValidationContext) -> None:
        arns = context.get_selected_resource_arns()
        if arns:
            return
        self.fail(
            context,
            f"no resources matched the selection criteria ({context.selection_summary()}).",
        )

    def verify_scale_values(self, context: ValidationContext) -> None:
        params = context.service.get("parameters")
        if not isinstance(params, dict):
            self.fail(context, "services[].parameters must be an object and include max.")

        try:
            max_size = int(params.get("max"))
        except Exception:
            self.fail(context, "services[].parameters.max must be an integer.")
            return

        if max_size < 0:
            self.fail(context, "services[].parameters.max must be >= 0.")
            return

        min_size_raw = params.get("min", 0)
        desired_raw = params.get("desired", max_size)

        try:
            min_size = int(min_size_raw)
        except Exception:
            self.fail(context, "services[].parameters.min must be an integer when provided.")
            return

        try:
            desired = int(desired_raw)
        except Exception:
            self.fail(context, "services[].parameters.desired must be an integer when provided.")
            return

        if min_size < 0:
            self.fail(context, "services[].parameters.min must be >= 0.")
            return
        if min_size > max_size:
            self.fail(context, "services[].parameters.min must be <= max.")
            return
        if desired < min_size or desired > max_size:
            self.fail(context, "services[].parameters.desired must be between min and max.")
