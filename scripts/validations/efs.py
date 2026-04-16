import botocore.exceptions

from resource import collect_service_resource_arns
from utility import parse_tags
from validations.base import BaseServiceValidator, ValidationContext


def _efs_id_from_arn(arn: str) -> str:
    marker = "file-system/"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


class EFSValidator(BaseServiceValidator):
    service_name = "efs"

    def _describe_replications(self, *, session, region: str, file_system_id: str):
        efs = session.client("efs", region_name=region)
        try:
            response = efs.describe_replication_configurations(FileSystemId=file_system_id)
        except botocore.exceptions.ClientError as e:
            code = str(e.response.get("Error", {}).get("Code") or "").strip()
            if code == "ReplicationNotFound":
                return []
            raise
        return list(response.get("Replications") or [])

    def _resolve_failback_destination(self, context: ValidationContext):
        target = context.service.get("target") or {}
        if not isinstance(target, dict):
            self.fail(context, "service.target is required for efs:failback.")

        destination_region = str(target.get("destination_region") or "").strip()
        if not destination_region:
            self.fail(context, "service.target.destination_region is required for efs:failback.")
        if destination_region == str(context.region or "").strip():
            self.fail(context, "service.target.destination_region must be different from the source region.")

        destination_file_system_id = str(target.get("destination_file_system_id") or "").strip()
        destination_tags_raw = target.get("destination_tags")
        destination_tags = parse_tags(destination_tags_raw)
        if not destination_file_system_id and not destination_tags:
            self.fail(
                context,
                "efs:failback requires service.target.destination_file_system_id or service.target.destination_tags.",
            )

        destination_arns = collect_service_resource_arns(
            {
                "name": "efs",
                "action": "failback",
                "identifier": destination_file_system_id or None,
                "tags": destination_tags_raw,
            },
            session=context.session,
            region=destination_region,
            zone=None,
        )
        if not destination_arns:
            self.fail(
                context,
                f"no destination EFS file system matched the failback selector in region {destination_region}.",
            )
        if len(destination_arns) > 1:
            self.fail(
                context,
                "multiple destination EFS file systems matched the failback selector. "
                "Please narrow destination_file_system_id or destination_tags.",
            )
        return destination_region, destination_arns[0]

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

        missing = []
        for arn in arns:
            file_system_id = _efs_id_from_arn(arn)
            if not file_system_id:
                continue
            replications = self._describe_replications(
                session=context.session,
                region=context.region,
                file_system_id=file_system_id,
            )
            if not replications:
                missing.append(file_system_id)

        if missing:
            self.fail(
                context,
                "the selected EFS file system(s) do not have a replication configuration: "
                + ", ".join(sorted(missing)),
            )

    def verify_failback_target(self, context: ValidationContext) -> None:
        source_arns = context.get_selected_resource_arns()
        if not source_arns:
            self.fail(
                context,
                f"no resources matched the selection criteria ({context.selection_summary()}).",
            )
        if len(source_arns) != 1:
            self.fail(context, "efs:failback requires exactly one source EFS file system.")

        destination_region, destination_arn = self._resolve_failback_destination(context)
        if destination_arn == source_arns[0]:
            self.fail(context, "source and destination EFS file systems must be different.")
        if not destination_region:
            self.fail(context, "service.target.destination_region is required for efs:failback.")

    def verify_failback_state(self, context: ValidationContext) -> None:
        source_arns = context.get_selected_resource_arns()
        if len(source_arns) != 1:
            self.fail(context, "efs:failback requires exactly one source EFS file system.")

        source_file_system_id = _efs_id_from_arn(source_arns[0])
        destination_region, destination_arn = self._resolve_failback_destination(context)
        destination_file_system_id = _efs_id_from_arn(destination_arn)

        source_replications = self._describe_replications(
            session=context.session,
            region=context.region,
            file_system_id=source_file_system_id,
        )
        if source_replications:
            self.fail(
                context,
                f"source EFS file system {source_file_system_id} is already part of a replication configuration.",
            )

        destination_replications = self._describe_replications(
            session=context.session,
            region=destination_region,
            file_system_id=destination_file_system_id,
        )
        if destination_replications:
            self.fail(
                context,
                f"destination EFS file system {destination_file_system_id} is already part of a replication configuration.",
            )

        efs = context.session.client("efs", region_name=destination_region)
        response = efs.describe_file_systems(FileSystemId=destination_file_system_id)
        file_systems = list(response.get("FileSystems") or [])
        if not file_systems:
            self.fail(context, f"destination EFS file system {destination_file_system_id} was not found.")

        protection = file_systems[0].get("FileSystemProtection") or {}
        overwrite_protection = str(protection.get("ReplicationOverwriteProtection") or "").strip().upper()
        if overwrite_protection == "REPLICATING":
            self.fail(
                context,
                f"destination EFS file system {destination_file_system_id} is already replicating and cannot be reused.",
            )
