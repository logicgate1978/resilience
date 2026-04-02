import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from component_actions.base import CustomComponentAction
from resource import collect_service_resource_arns
from utility import normalize_service_name


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_instance_identifier_from_arn(arn: str) -> str:
    marker = ":db:"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


def _db_cluster_identifier_from_arn(arn: str) -> str:
    marker = ":cluster:"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


def _cluster_writer_identifier(cluster: Dict[str, Any]) -> str:
    for member in cluster.get("DBClusterMembers") or []:
        if bool(member.get("IsClusterWriter")):
            return str(member.get("DBInstanceIdentifier") or "")
    return ""


def _describe_db_cluster(rds, cluster_identifier: str) -> Dict[str, Any]:
    resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_identifier)
    clusters = resp.get("DBClusters") or []
    if not clusters:
        raise ValueError(f"DB cluster not found: {cluster_identifier}")
    return clusters[0]


def _describe_db_instance(rds, instance_identifier: str) -> Dict[str, Any]:
    resp = rds.describe_db_instances(DBInstanceIdentifier=instance_identifier)
    instances = resp.get("DBInstances") or []
    if not instances:
        raise ValueError(f"DB instance not found: {instance_identifier}")
    return instances[0]


class RDSAction(CustomComponentAction):
    service_name = "rds"
    action_names = ["reboot", "failover"]

    def build_plan_item(
        self,
        *,
        manifest: Dict[str, Any],
        svc: Dict[str, Any],
        session,
        region: str,
        index: int,
        default_timeout_seconds: int,
    ) -> Dict[str, Any]:
        _ = manifest
        action = str(svc.get("action") or "").strip().lower()
        arns = collect_service_resource_arns(
            svc,
            session=session,
            region=region,
            zone=None,
        )
        if not arns:
            raise ValueError(f"rds:{action} did not resolve any RDS resources from the manifest selection.")

        identifiers: List[str] = []
        impacted_resources: List[Dict[str, str]] = []
        if action == "reboot":
            for arn in arns:
                identifier = _db_instance_identifier_from_arn(arn)
                if not identifier:
                    continue
                identifiers.append(identifier)
                impacted_resources.append(
                    {
                        "service": "rds:reboot",
                        "arn": arn,
                        "selection_mode": "CUSTOM",
                    }
                )
        elif action == "failover":
            for arn in arns:
                identifier = _db_cluster_identifier_from_arn(arn)
                if not identifier:
                    continue
                identifiers.append(identifier)
                impacted_resources.append(
                    {
                        "service": "rds:failover",
                        "arn": arn,
                        "selection_mode": "CUSTOM",
                    }
                )
        else:
            raise ValueError(f"Unsupported custom RDS action: {action}")

        if not identifiers:
            raise ValueError(f"rds:{action} could not extract RDS identifiers from the resolved ARNs.")

        return {
            "name": f"a_rds_{action}_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:{action}",
            "action": action,
            "description": f"{action.title()} {len(identifiers)} RDS resource(s)",
            "target": {
                "identifiers": identifiers,
            },
            "parameters": {
                "timeoutSeconds": int(default_timeout_seconds),
                "useFis": False,
            },
            "impacted_resources": impacted_resources,
        }

    def execute_item(
        self,
        *,
        session,
        item: Dict[str, Any],
        poll_seconds: int,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        started_at = _utc_now_iso()
        action = str(item.get("action") or "").strip().lower()
        identifiers = list((item.get("target") or {}).get("identifiers") or [])
        params = dict(item.get("parameters") or {})
        region = item["region"]
        effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)
        rds = session.client("rds", region_name=region)

        original_state: Dict[str, Any] = {}

        try:
            if action == "reboot":
                for identifier in identifiers:
                    original_state[identifier] = _describe_db_instance(rds, identifier)
                    rds.reboot_db_instance(DBInstanceIdentifier=identifier)

                deadline = time.time() + effective_timeout_seconds
                while True:
                    ready = True
                    latest_state: Dict[str, Any] = {}
                    for identifier in identifiers:
                        instance = _describe_db_instance(rds, identifier)
                        latest_state[identifier] = instance
                        status = str(instance.get("DBInstanceStatus") or "").strip().lower()
                        if status != "available":
                            ready = False

                    if ready:
                        original_state["__latest__"] = latest_state
                        break

                    if time.time() > deadline:
                        raise TimeoutError(
                            "Timed out waiting for RDS DB instance reboot to complete and the instance to return to available state."
                        )
                    time.sleep(max(1, poll_seconds))
            elif action == "failover":
                original_writers: Dict[str, str] = {}
                for identifier in identifiers:
                    cluster = _describe_db_cluster(rds, identifier)
                    original_state[identifier] = cluster
                    original_writers[identifier] = _cluster_writer_identifier(cluster)
                    rds.failover_db_cluster(DBClusterIdentifier=identifier)

                deadline = time.time() + effective_timeout_seconds
                while True:
                    ready = True
                    latest_state: Dict[str, Any] = {}
                    for identifier in identifiers:
                        cluster = _describe_db_cluster(rds, identifier)
                        latest_state[identifier] = cluster
                        status = str(cluster.get("Status") or "").strip().lower()
                        current_writer = _cluster_writer_identifier(cluster)
                        original_writer = original_writers.get(identifier) or ""
                        writer_changed = bool(current_writer) and current_writer != original_writer
                        if status != "available":
                            ready = False
                            continue
                        if original_writer and not writer_changed:
                            ready = False

                    if ready:
                        original_state["__latest__"] = latest_state
                        break

                    if time.time() > deadline:
                        raise TimeoutError(
                            "Timed out waiting for RDS DB cluster failover to complete and the cluster to return to available state."
                        )
                    time.sleep(max(1, poll_seconds))
            else:
                raise ValueError(f"Unsupported custom RDS action: {action}")
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"RDS API error during rds:{action}: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": item.get("target"),
                    "parameters": params,
                    "originalState": original_state,
                },
            }

        try:
            if action == "reboot":
                final_status = {identifier: _describe_db_instance(rds, identifier) for identifier in identifiers}
            else:
                final_status = {
                    identifier: _describe_db_cluster(rds, identifier)
                    for identifier in identifiers
                }
        except Exception as e:
            final_status = {"error": f"Unable to read final RDS state: {e}"}

        ended_at = _utc_now_iso()
        return {
            "name": item["name"],
            "status": "completed",
            "reason": None,
            "startTime": started_at,
            "endTime": ended_at,
            "details": {
                "target": item.get("target"),
                "parameters": params,
                "originalState": original_state,
                "finalStatus": final_status,
            },
        }
