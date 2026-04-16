from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import botocore.exceptions

from component_actions.base import CustomComponentAction
from resource import collect_service_resource_arns
from utility import normalize_service_name, parse_tags, resolve_service_zone


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _efs_id_from_arn(arn: str) -> str:
    marker = "file-system/"
    if marker not in arn:
        return ""
    return arn.split(marker, 1)[1] or ""


def _optional_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def _describe_replications_for_file_system(efs, file_system_id: str) -> List[Dict[str, Any]]:
    try:
        response = efs.describe_replication_configurations(FileSystemId=file_system_id)
    except botocore.exceptions.ClientError as e:
        code = str(e.response.get("Error", {}).get("Code") or "").strip()
        if code == "ReplicationNotFound":
            return []
        raise
    return list(response.get("Replications") or [])


def _describe_file_system(efs, file_system_id: str) -> Dict[str, Any]:
    response = efs.describe_file_systems(FileSystemId=file_system_id)
    file_systems = list(response.get("FileSystems") or [])
    if not file_systems:
        raise ValueError(f"EFS file system {file_system_id} was not found.")
    return file_systems[0]


def _replication_overwrite_protection(fs: Dict[str, Any]) -> str:
    protection = fs.get("FileSystemProtection") or {}
    return str(protection.get("ReplicationOverwriteProtection") or "").strip().upper()


def _resolve_replication_delete_plan(efs, selected_file_system_ids: List[str]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    replication_by_selected: Dict[str, Dict[str, Any]] = {}
    source_file_system_ids: List[str] = []
    seen_sources = set()

    for file_system_id in selected_file_system_ids:
        replications = _describe_replications_for_file_system(efs, file_system_id)
        if not replications:
            raise ValueError(f"No replication configuration found for EFS file system {file_system_id}.")

        replication = replications[0]
        replication_by_selected[file_system_id] = replication
        source_file_system_id = str(replication.get("SourceFileSystemId") or "").strip()
        if source_file_system_id and source_file_system_id not in seen_sources:
            seen_sources.add(source_file_system_id)
            source_file_system_ids.append(source_file_system_id)

    return replication_by_selected, source_file_system_ids


def _resolve_failback_destination(
    *,
    session,
    destination_region: str,
    destination_file_system_id: Optional[str],
    destination_tags: Dict[str, str],
) -> Tuple[str, str]:
    from resource import _collect_efs_file_systems

    if destination_file_system_id:
        destination_arns = _collect_efs_file_systems(
            session,
            destination_region,
            destination_tags,
            identifier=destination_file_system_id,
        )
    else:
        destination_arns = _collect_efs_file_systems(
            session,
            destination_region,
            destination_tags,
        )

    if not destination_arns:
        selector = f"identifier={destination_file_system_id}" if destination_file_system_id else "destination_tags"
        raise ValueError(
            f"efs:failback did not resolve any destination EFS file systems in {destination_region} using {selector}."
        )
    if len(destination_arns) > 1:
        raise ValueError(
            "efs:failback resolved multiple destination EFS file systems. "
            "Please narrow target.destination_file_system_id or target.destination_tags so exactly one destination is selected."
        )

    destination_arn = destination_arns[0]
    resolved_file_system_id = _efs_id_from_arn(destination_arn)
    if not resolved_file_system_id:
        raise ValueError("efs:failback could not extract the destination EFS file system ID from the resolved ARN.")
    return resolved_file_system_id, destination_arn


def _wait_for_destination_protection_state(
    *,
    efs,
    file_system_id: str,
    desired_state: str,
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    import time

    deadline = time.time() + timeout_seconds
    last_seen: Dict[str, Any] = {}
    desired = desired_state.strip().upper()
    while True:
        last_seen = _describe_file_system(efs, file_system_id)
        if _replication_overwrite_protection(last_seen) == desired:
            return last_seen
        if time.time() > deadline:
            raise TimeoutError(
                f"Timed out waiting for destination EFS file system {file_system_id} "
                f"to reach replication overwrite protection state {desired}."
            )
        time.sleep(max(1, poll_seconds))


def _find_replication_to_destination(replications: List[Dict[str, Any]], destination_file_system_id: str) -> Optional[Dict[str, Any]]:
    for replication in replications:
        for destination in replication.get("Destinations") or []:
            if str(destination.get("FileSystemId") or "").strip() == destination_file_system_id:
                return replication
    return None


class EFSFailoverAction(CustomComponentAction):
    service_name = "efs"
    action_names = ["failover"]

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
        arns = collect_service_resource_arns(
            svc,
            session=session,
            region=region,
            zone=resolve_service_zone(manifest, svc),
        )
        if not arns:
            raise ValueError("efs:failover did not resolve any EFS file systems from the manifest selection.")

        file_system_ids: List[str] = []
        impacted_resources: List[Dict[str, str]] = []
        for arn in arns:
            file_system_id = _efs_id_from_arn(arn)
            if not file_system_id:
                continue
            file_system_ids.append(file_system_id)
            impacted_resources.append(
                {
                    "service": "efs:failover",
                    "arn": arn,
                    "selection_mode": "CUSTOM",
                }
            )

        if not file_system_ids:
            raise ValueError("efs:failover could not extract EFS file system IDs from the resolved ARNs.")

        wait_for_ready = _optional_bool(svc.get("wait_for_ready"), True)
        timeout_seconds = int(default_timeout_seconds)
        efs = session.client("efs", region_name=region)
        _, source_file_system_ids = _resolve_replication_delete_plan(efs, file_system_ids)

        return {
            "name": f"a_efs_failover_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:failover",
            "action": "failover",
            "description": f"Delete EFS replication configuration for {len(file_system_ids)} selected file system(s)",
            "target": {
                "fileSystemIds": file_system_ids,
                "sourceFileSystemIds": source_file_system_ids,
            },
            "parameters": {
                "waitForReady": wait_for_ready,
                "timeoutSeconds": timeout_seconds,
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
        import time

        started_at = _utc_now_iso()
        target = item["target"]
        params = item["parameters"]
        file_system_ids = list(target.get("fileSystemIds") or [])
        source_file_system_ids = list(target.get("sourceFileSystemIds") or [])
        wait_for_ready = bool(params.get("waitForReady"))
        effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)
        efs = session.client("efs", region_name=item["region"])

        try:
            replication_before, resolved_source_ids = _resolve_replication_delete_plan(efs, file_system_ids)
            if resolved_source_ids:
                source_file_system_ids = resolved_source_ids

            for source_file_system_id in source_file_system_ids:
                efs.delete_replication_configuration(SourceFileSystemId=source_file_system_id)
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"EFS API error during efs:failover: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": target,
                    "parameters": params,
                },
            }

        last_observed: Dict[str, Any] = {}
        if wait_for_ready:
            deadline = time.time() + effective_timeout_seconds
            while True:
                ready = True
                last_observed = {}
                for file_system_id in file_system_ids:
                    replications = _describe_replications_for_file_system(efs, file_system_id)
                    last_observed[file_system_id] = replications
                    if replications:
                        ready = False

                if ready:
                    break

                if time.time() > deadline:
                    ended_at = _utc_now_iso()
                    return {
                        "name": item["name"],
                        "status": "failed",
                        "reason": "Timed out waiting for the EFS replication configuration to be deleted.",
                        "startTime": started_at,
                        "endTime": ended_at,
                        "details": {
                            "target": target,
                            "parameters": params,
                            "lastObservedReplicationState": last_observed,
                        },
                    }
                time.sleep(max(1, poll_seconds))

        ended_at = _utc_now_iso()
        return {
            "name": item["name"],
            "status": "completed",
            "reason": None,
            "startTime": started_at,
            "endTime": ended_at,
            "details": {
                "target": target,
                "parameters": params,
                "waitedForDeletion": wait_for_ready,
                "finalReplicationState": last_observed,
            },
        }


class EFSFailbackAction(CustomComponentAction):
    service_name = "efs"
    action_names = ["failback"]

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
        arns = collect_service_resource_arns(
            svc,
            session=session,
            region=region,
            zone=resolve_service_zone(manifest, svc),
        )
        if not arns:
            raise ValueError("efs:failback did not resolve any source EFS file systems from the manifest selection.")
        if len(arns) != 1:
            raise ValueError("efs:failback requires exactly one source EFS file system.")

        source_arn = arns[0]
        source_file_system_id = _efs_id_from_arn(source_arn)
        if not source_file_system_id:
            raise ValueError("efs:failback could not extract the source EFS file system ID from the resolved ARN.")

        target = svc.get("target") or {}
        if not isinstance(target, dict):
            raise ValueError("efs:failback requires a target block.")
        destination_region = str(target.get("destination_region") or "").strip()
        if not destination_region:
            raise ValueError("efs:failback requires target.destination_region.")

        destination_file_system_id = str(target.get("destination_file_system_id") or "").strip() or None
        destination_tags = parse_tags(target.get("destination_tags"))
        destination_resolved_id, destination_arn = _resolve_failback_destination(
            session=session,
            destination_region=destination_region,
            destination_file_system_id=destination_file_system_id,
            destination_tags=destination_tags,
        )

        if destination_region == region and destination_resolved_id == source_file_system_id:
            raise ValueError("efs:failback source and destination cannot be the same EFS file system.")

        wait_for_ready = _optional_bool(svc.get("wait_for_ready"), True)
        timeout_seconds = int(svc.get("timeout_seconds") or default_timeout_seconds)

        return {
            "name": f"a_efs_failback_{index}",
            "engine": "custom",
            "service": f"{normalize_service_name(svc.get('name'))}:failback",
            "action": "failback",
            "description": "Create reverse EFS replication back to the destination file system",
            "target": {
                "sourceFileSystemId": source_file_system_id,
                "sourceArn": source_arn,
                "destinationRegion": destination_region,
                "destinationFileSystemId": destination_resolved_id,
                "destinationArn": destination_arn,
            },
            "parameters": {
                "waitForReady": wait_for_ready,
                "timeoutSeconds": timeout_seconds,
            },
            "impacted_resources": [
                {
                    "service": "efs:failback",
                    "arn": source_arn,
                    "selection_mode": "CUSTOM",
                },
                {
                    "service": "efs:failback",
                    "arn": destination_arn,
                    "selection_mode": "CUSTOM",
                },
            ],
        }

    def execute_item(
        self,
        *,
        session,
        item: Dict[str, Any],
        poll_seconds: int,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        import time

        started_at = _utc_now_iso()
        target = item["target"]
        params = item["parameters"]
        source_file_system_id = str(target.get("sourceFileSystemId") or "").strip()
        destination_region = str(target.get("destinationRegion") or "").strip()
        destination_file_system_id = str(target.get("destinationFileSystemId") or "").strip()
        wait_for_ready = bool(params.get("waitForReady"))
        effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)

        source_region = str(item.get("region") or "").strip()
        source_efs = session.client("efs", region_name=source_region)
        destination_efs = session.client("efs", region_name=destination_region)

        last_observed: Dict[str, Any] = {}
        try:
            source_replications = _describe_replications_for_file_system(source_efs, source_file_system_id)
            if source_replications:
                raise ValueError(
                    f"Source EFS file system {source_file_system_id} is already part of a replication configuration."
                )

            destination_replications = _describe_replications_for_file_system(destination_efs, destination_file_system_id)
            if destination_replications:
                raise ValueError(
                    f"Destination EFS file system {destination_file_system_id} is already part of a replication configuration."
                )

            destination_fs = _describe_file_system(destination_efs, destination_file_system_id)
            protection_state = _replication_overwrite_protection(destination_fs)
            if protection_state == "REPLICATING":
                raise ValueError(
                    f"Destination EFS file system {destination_file_system_id} is already replicating and cannot be reused."
                )
            if protection_state != "DISABLED":
                destination_efs.update_file_system_protection(
                    FileSystemId=destination_file_system_id,
                    ReplicationOverwriteProtection="DISABLED",
                )
                destination_fs = _wait_for_destination_protection_state(
                    efs=destination_efs,
                    file_system_id=destination_file_system_id,
                    desired_state="DISABLED",
                    poll_seconds=poll_seconds,
                    timeout_seconds=effective_timeout_seconds,
                )

            source_efs.create_replication_configuration(
                SourceFileSystemId=source_file_system_id,
                Destinations=[
                    {
                        "Region": destination_region,
                        "FileSystemId": destination_file_system_id,
                    }
                ],
            )
        except Exception as e:
            ended_at = _utc_now_iso()
            return {
                "name": item["name"],
                "status": "failed",
                "reason": f"EFS API error during efs:failback: {e}",
                "startTime": started_at,
                "endTime": ended_at,
                "details": {
                    "target": target,
                    "parameters": params,
                },
            }

        if wait_for_ready:
            deadline = time.time() + effective_timeout_seconds
            while True:
                replications = _describe_replications_for_file_system(source_efs, source_file_system_id)
                replication = _find_replication_to_destination(replications, destination_file_system_id)
                last_observed = {
                    "sourceReplications": replications,
                }
                if replication is not None:
                    status = str(replication.get("Status") or "").strip().upper()
                    last_observed["replicationStatus"] = status
                    if status == "ENABLED":
                        break
                    if status in ("ERROR", "DELETING"):
                        ended_at = _utc_now_iso()
                        return {
                            "name": item["name"],
                            "status": "failed",
                            "reason": f"EFS replication entered unexpected status {status or 'UNKNOWN'} during failback.",
                            "startTime": started_at,
                            "endTime": ended_at,
                            "details": {
                                "target": target,
                                "parameters": params,
                                "lastObservedReplicationState": last_observed,
                            },
                        }

                if time.time() > deadline:
                    ended_at = _utc_now_iso()
                    return {
                        "name": item["name"],
                        "status": "failed",
                        "reason": "Timed out waiting for the EFS failback replication configuration to become ENABLED.",
                        "startTime": started_at,
                        "endTime": ended_at,
                        "details": {
                            "target": target,
                            "parameters": params,
                            "lastObservedReplicationState": last_observed,
                        },
                    }
                time.sleep(max(1, poll_seconds))

        ended_at = _utc_now_iso()
        return {
            "name": item["name"],
            "status": "completed",
            "reason": None,
            "startTime": started_at,
            "endTime": ended_at,
            "details": {
                "target": target,
                "parameters": params,
                "waitedForReady": wait_for_ready,
                "finalReplicationState": last_observed,
            },
        }
