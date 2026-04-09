from s3_mrap_utils import (
    get_mrap_routes,
    get_mrap_selector,
    get_mrap_target,
    get_mrap_target_region,
    resolve_mrap,
    resolve_mrap_control_region,
    validate_mrap_control_region,
)
from validations.base import BaseServiceValidator, ValidationContext


class S3Validator(BaseServiceValidator):
    service_name = "s3"

    def verify_resource_existence(self, context: ValidationContext) -> None:
        arns = context.get_selected_resource_arns()
        if arns:
            return
        self.fail(
            context,
            f"no resources matched the selection criteria ({context.selection_summary()}).",
        )

    def verify_mrap_selector(self, context: ValidationContext) -> None:
        target = get_mrap_target(context.service)
        get_mrap_selector(target)
        get_mrap_target_region(context.service)

        control_region = resolve_mrap_control_region(context.manifest, context.service, fallback_region=context.region)
        try:
            validate_mrap_control_region(control_region)
        except Exception as e:
            self.fail(context, str(e))

    def verify_mrap_failover_state(self, context: ValidationContext) -> None:
        try:
            target = get_mrap_target(context.service)
            selector_key, selector_value = get_mrap_selector(target)
            target_region = get_mrap_target_region(context.service)
            control_region = resolve_mrap_control_region(context.manifest, context.service, fallback_region=context.region)
            validate_mrap_control_region(control_region)

            mrap = resolve_mrap(context.session, selector_key=selector_key, selector_value=selector_value)
            status = str(mrap.get("status") or "").strip().upper()
            if status != "READY":
                self.fail(
                    context,
                    f"the selected Multi-Region Access Point must be READY. Current status: {mrap.get('status') or 'unknown'}.",
                )

            routes = get_mrap_routes(
                context.session,
                control_region=control_region,
                account_id=mrap["account_id"],
                mrap_arn=mrap["arn"],
            )
            if not routes:
                self.fail(context, "the selected Multi-Region Access Point does not have any routes configured.")
                return

            active_regions = []
            known_regions = []
            for route in routes:
                region = str(route.get("Region") or "").strip()
                dial = route.get("TrafficDialPercentage")
                if region:
                    known_regions.append(region)
                if dial not in (0, 100):
                    self.fail(
                        context,
                        "the selected Multi-Region Access Point must be in active/passive mode with route dials of 0 or 100 only.",
                    )
                    return
                if dial == 100 and region:
                    active_regions.append(region)

            if target_region not in known_regions:
                self.fail(
                    context,
                    f"target region '{target_region}' does not exist in the selected Multi-Region Access Point.",
                )
                return

            if len(known_regions) < 2:
                self.fail(
                    context,
                    "the selected Multi-Region Access Point has fewer than two regions, so failover is not meaningful.",
                )
                return

            if len(active_regions) != 1:
                self.fail(
                    context,
                    "the selected Multi-Region Access Point is not in active/passive mode with exactly one active region.",
                )
                return

            if active_regions[0] == target_region:
                self.fail(
                    context,
                    f"target region '{target_region}' is already active for the selected Multi-Region Access Point.",
                )
        except Exception as e:
            if isinstance(e, ValueError) and str(e).startswith("Validation failed for"):
                raise
            self.fail(context, str(e))
