from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from component_actions.base import CustomComponentAction
from resource import collect_service_resource_arns
from utility import normalize_service_name, resolve_service_zone


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
    response = efs.describe_replication_configurations(FileSystemId=file_system_id)
    return list(response.get("Replications") or [])


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
