from typing import Any, Dict, List

from validations.base import BaseServiceValidator, ValidationContext


def _rds_identifier_from_arn(resource_arn: str) -> str:
    parts = (resource_arn or "").split(":")
    if len(parts) < 7:
        return ""
    return parts[-1]


class RDSValidator(BaseServiceValidator):
    service_name = "rds"

    def verify_resource_existence(self, context: ValidationContext) -> None:
        arns = context.get_selected_resource_arns()
        if arns:
            return
        self.fail(
            context,
            f"no resources matched the selection criteria ({context.selection_summary()}).",
        )

    def verify_replica(self, context: ValidationContext) -> None:
        if context.action == "reboot":
            self._verify_db_instance_replica(context)
            return
        if context.action == "failover":
            self._verify_db_cluster_replica(context)
            return
        self.fail(
            context,
            f"'verify_replica' is not supported for action '{context.action}'.",
        )

    def _verify_db_instance_replica(self, context: ValidationContext) -> None:
        rds = context.session.client("rds", region_name=context.region)
        failures: List[str] = []

        for arn in context.get_selected_resource_arns():
            identifier = _rds_identifier_from_arn(arn)
            if not identifier:
                failures.append(arn)
                continue

            resp = rds.describe_db_instances(DBInstanceIdentifier=identifier)
            instances: List[Dict[str, Any]] = resp.get("DBInstances", [])
            if not instances:
                failures.append(identifier)
                continue

            db = instances[0]
            has_replica = bool(db.get("MultiAZ")) or bool(db.get("ReadReplicaSourceDBInstanceIdentifier")) or bool(
                db.get("ReadReplicaDBInstanceIdentifiers")
            )
            if not has_replica:
                failures.append(identifier)

        if failures:
            failed_list = ", ".join(failures)
            self.fail(
                context,
                "the selected DB instance must have Multi-AZ enabled or at least one read replica. "
                f"Failed resources: {failed_list}.",
            )

    def _verify_db_cluster_replica(self, context: ValidationContext) -> None:
        rds = context.session.client("rds", region_name=context.region)
        failures: List[str] = []

        for arn in context.get_selected_resource_arns():
            identifier = _rds_identifier_from_arn(arn)
            if not identifier:
                failures.append(arn)
                continue

            resp = rds.describe_db_clusters(DBClusterIdentifier=identifier)
            clusters: List[Dict[str, Any]] = resp.get("DBClusters", [])
            if not clusters:
                failures.append(identifier)
                continue

            cluster = clusters[0]
            members = cluster.get("DBClusterMembers") or []
            replica_members = [member for member in members if not member.get("IsClusterWriter")]
            if not replica_members:
                failures.append(identifier)

        if failures:
            failed_list = ", ".join(failures)
            self.fail(
                context,
                "the selected DB cluster must have at least one replica/reader instance before failover. "
                f"Failed resources: {failed_list}.",
            )
