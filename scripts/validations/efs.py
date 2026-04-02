from resource import collect_service_resource_arns
from validations.base import BaseServiceValidator, ValidationContext


def _efs_id_from_arn(arn: str) -> str:
    marker = "file-system/"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


class EFSValidator(BaseServiceValidator):
    service_name = "efs"

    def verify_resource_existence(self, context: ValidationContext) -> None:
        arns = context.get_selected_resource_arns()
        if arns:
            return
        self.fail(
            context,
            f"no resources matched the selection criteria ({context.selection_summary()}).",
        )

    def verify_replication_configuration_exists(self, context: ValidationContext) -> None:
        arns = collect_service_resource_arns(
            context.service,
            session=context.session,
            region=context.region,
            zone=context.zone,
        )
        if not arns:
            self.fail(
                context,
                f"no resources matched the selection criteria ({context.selection_summary()}).",
            )

        efs = context.session.client("efs", region_name=context.region)
        missing = []
        for arn in arns:
            file_system_id = _efs_id_from_arn(arn)
            if not file_system_id:
                continue
            response = efs.describe_replication_configurations(FileSystemId=file_system_id)
            replications = response.get("Replications") or []
            if not replications:
                missing.append(file_system_id)

        if missing:
            self.fail(
                context,
                "the selected EFS file system(s) do not have a replication configuration: "
                + ", ".join(sorted(missing)),
            )
