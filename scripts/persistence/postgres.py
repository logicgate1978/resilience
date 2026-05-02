from __future__ import annotations

import hashlib
import os
import socket
import subprocess
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

from utility import (
    normalize_service_name,
    resolve_service_primary_region,
    resolve_service_region,
    resolve_service_secondary_region,
    resolve_service_zone,
)
from validations.registry import load_action_validations


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> Optional[str]:
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return [_json_safe(v) for v in sorted(value, key=lambda x: str(x))]
    return value


def _status_for_db(value: Optional[str], *, dry_run: bool = False) -> str:
    if dry_run:
        return "skipped"
    text = str(value or "").strip().lower()
    if text in {"running", "completed", "failed", "stopped", "skipped"}:
        return text
    if text == "stopping":
        return "stopped"
    return "failed" if text else "running"


def _normalize_start_after(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _resource_type_from_arn(arn: str) -> Optional[str]:
    text = str(arn or "")
    if text.startswith("arn:aws:ec2:") and ":instance/" in text:
        return "ec2-instance"
    if text.startswith("arn:aws:ec2:") and ":subnet/" in text:
        return "ec2-subnet"
    if text.startswith("arn:aws:ec2:") and ":vpc-endpoint/" in text:
        return "vpc-endpoint"
    if text.startswith("arn:aws:rds:") and ":db:" in text:
        return "rds-instance"
    if text.startswith("arn:aws:rds:") and ":cluster:" in text:
        return "rds-cluster"
    if text.startswith("arn:aws:s3:::"):
        return "s3-bucket"
    if text.startswith("arn:aws:elasticfilesystem:"):
        return "efs-file-system"
    if text.startswith("arn:aws:autoscaling:"):
        return "autoscaling-group"
    if text.startswith("arn:aws:eks:") and ":nodegroup/" in text:
        return "eks-nodegroup"
    if text.startswith("arn:aws:iam::"):
        return "iam-role"
    if text.startswith("eks://"):
        return "eks-deployment"
    if text.startswith("route53://"):
        return "route53-record"
    return None


def _build_region_context(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "region": manifest.get("region"),
        "zone": manifest.get("zone"),
        "primary_region": manifest.get("primary_region"),
        "secondary_region": manifest.get("secondary_region"),
    }


def _git_value(args: List[str]) -> Optional[str]:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None
    value = (completed.stdout or "").strip()
    return value or None


def _git_context(repo_root: str) -> Dict[str, Any]:
    branch = _git_value(["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"])
    commit = _git_value(["git", "-C", repo_root, "rev-parse", "HEAD"])
    dirty_output = _git_value(["git", "-C", repo_root, "status", "--porcelain"])
    return {
        "git_branch": branch,
        "git_commit": commit,
        "git_dirty": bool(dirty_output) if dirty_output is not None else None,
    }


def _service_occurrence_refs(manifest: Dict[str, Any]) -> Dict[int, str]:
    services = manifest.get("services") or []
    totals: Dict[str, int] = {}
    normalized: List[Tuple[str, str]] = []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        service_name = normalize_service_name(svc.get("name"))
        action_name = str(svc.get("action") or "").strip().lower()
        normalized.append((service_name, action_name))
        key = f"{service_name}:{action_name}"
        totals[key] = totals.get(key, 0) + 1

    refs: Dict[int, str] = {}
    seen: Dict[str, int] = {}
    for index, (service_name, action_name) in enumerate(normalized, start=1):
        key = f"{service_name}:{action_name}"
        seen[key] = seen.get(key, 0) + 1
        refs[index] = key if totals[key] == 1 else f"{key}#{seen[key]}"
    return refs


def _supported_rollback_mode(service_name: str, action_name: str) -> Optional[str]:
    action_key = f"{service_name}:{action_name}"
    if action_key in {
        "asg:scale",
        "eks:scale-deployment",
        "eks:scale-nodegroup",
        "dns:set-value",
        "dns:set-weight",
        "s3:failover",
    }:
        return "automatic"
    return None


def _map_custom_summary_items(summary: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(summary, dict):
        return out
    items = (((summary.get("customExecution") or {}).get("items")) or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            out[name] = item
    return out


def _normalize_group_sizes(group_map: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(group_map, dict):
        return out
    for group_name, values in sorted(group_map.items()):
        if not isinstance(values, dict):
            continue
        out.append(
            {
                "groupName": group_name,
                "min": values.get("min"),
                "max": values.get("max"),
                "desired": values.get("desired"),
            }
        )
    return out


def _extract_rollback_state(
    *,
    service_name: str,
    action_name: str,
    requested_region: Optional[str],
    plan_item: Optional[Dict[str, Any]],
    result_json: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    rollback_mode = _supported_rollback_mode(service_name, action_name)
    if rollback_mode is None:
        return None

    item = plan_item or {}
    result = result_json or {}
    details = result.get("details") or {}
    action_key = f"{service_name}:{action_name}"

    if action_key == "asg:scale":
        before_groups = _normalize_group_sizes(details.get("originalSizes") or {})
        after_groups = _normalize_group_sizes((details.get("finalStatus") or {})) if isinstance(details.get("finalStatus"), dict) else []
        return {
            "rollback_mode": rollback_mode,
            "rollback_supported": bool(before_groups),
            "before_state_json": {"groups": before_groups} if before_groups else None,
            "after_state_json": {"groups": after_groups} if after_groups else None,
            "rollback_plan_json": {
                "service": action_key,
                "region": requested_region,
                "groups": before_groups,
            } if before_groups else None,
        }

    if action_key == "eks:scale-deployment":
        target = item.get("target") or {}
        original_replicas = details.get("originalReplicas")
        if original_replicas is None:
            return {
                "rollback_mode": rollback_mode,
                "rollback_supported": False,
                "before_state_json": None,
                "after_state_json": None,
                "rollback_plan_json": None,
            }
        before_state = {
            "clusterIdentifier": target.get("clusterIdentifier"),
            "namespace": target.get("namespace"),
            "deploymentName": target.get("deploymentName"),
            "replicas": original_replicas,
        }
        return {
            "rollback_mode": rollback_mode,
            "rollback_supported": True,
            "before_state_json": before_state,
            "after_state_json": details.get("finalStatus"),
            "rollback_plan_json": {
                "service": action_key,
                "region": requested_region,
                "target": {
                    "cluster_identifier": target.get("clusterIdentifier"),
                    "namespace": target.get("namespace"),
                    "deployment_name": target.get("deploymentName"),
                },
                "parameters": {
                    "replicas": original_replicas,
                },
            },
        }

    if action_key == "eks:scale-nodegroup":
        target = item.get("target") or {}
        original_scaling = details.get("originalScaling")
        if not isinstance(original_scaling, dict):
            return {
                "rollback_mode": rollback_mode,
                "rollback_supported": False,
                "before_state_json": None,
                "after_state_json": None,
                "rollback_plan_json": None,
            }
        return {
            "rollback_mode": rollback_mode,
            "rollback_supported": True,
            "before_state_json": original_scaling,
            "after_state_json": details.get("finalStatus"),
            "rollback_plan_json": {
                "service": action_key,
                "region": requested_region,
                "target": {
                    "cluster_identifier": target.get("clusterIdentifier"),
                    "nodegroup_name": target.get("nodegroupName"),
                },
                "parameters": {
                    "min": original_scaling.get("min"),
                    "max": original_scaling.get("max"),
                    "desired": original_scaling.get("desired"),
                },
            },
        }

    if action_key == "dns:set-value":
        before_rrset = item.get("recordSetBefore")
        after_rrset = item.get("recordSetAfter")
        return {
            "rollback_mode": rollback_mode,
            "rollback_supported": bool(before_rrset),
            "before_state_json": {"recordSet": before_rrset} if before_rrset else None,
            "after_state_json": {"recordSet": after_rrset} if after_rrset else None,
            "rollback_plan_json": {
                "service": action_key,
                "hostedZoneId": (item.get("target") or {}).get("hostedZoneId"),
                "recordSet": before_rrset,
            } if before_rrset else None,
        }

    if action_key == "dns:set-weight":
        record_sets = item.get("recordSets") or []
        before_sets = [
            {"setIdentifier": entry.get("setIdentifier"), "recordSet": entry.get("before")}
            for entry in record_sets
            if isinstance(entry, dict) and entry.get("before")
        ]
        after_sets = [
            {"setIdentifier": entry.get("setIdentifier"), "recordSet": entry.get("after")}
            for entry in record_sets
            if isinstance(entry, dict) and entry.get("after")
        ]
        return {
            "rollback_mode": rollback_mode,
            "rollback_supported": bool(before_sets),
            "before_state_json": {"recordSets": before_sets} if before_sets else None,
            "after_state_json": {"recordSets": after_sets} if after_sets else None,
            "rollback_plan_json": {
                "service": action_key,
                "hostedZoneId": (item.get("target") or {}).get("hostedZoneId"),
                "recordSets": [entry.get("recordSet") for entry in before_sets],
            } if before_sets else None,
        }

    if action_key == "s3:failover":
        routes_before = item.get("routesBefore")
        routes_after = details.get("routesAfter") or item.get("routeUpdates")
        return {
            "rollback_mode": rollback_mode,
            "rollback_supported": bool(routes_before),
            "before_state_json": {
                "activeRegion": item.get("activeRegionBefore"),
                "routes": routes_before,
            } if routes_before else None,
            "after_state_json": {
                "routes": routes_after,
            } if routes_after else None,
            "rollback_plan_json": {
                "service": action_key,
                "region": (item.get("target") or {}).get("controlRegion"),
                "mrapArn": (item.get("target") or {}).get("mrapArn"),
                "routeUpdates": routes_before,
            } if routes_before else None,
        }

    return None


class PostgresRunStore:
    def __init__(self, dsn: str):
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except Exception as e:
            raise RuntimeError(
                "Database persistence requires psycopg. Install it with the project requirements before using --db-dsn."
            ) from e
        self._psycopg = psycopg
        self._Jsonb = Jsonb
        self._dsn = dsn

    @classmethod
    def from_dsn(cls, dsn: Optional[str]) -> Optional["PostgresRunStore"]:
        text = str(dsn or "").strip()
        if not text:
            return None
        return cls(text)

    def _connect(self):
        return self._psycopg.connect(self._dsn)

    def create_run(
        self,
        *,
        manifest: Dict[str, Any],
        manifest_path: str,
        engine_family: str,
        dry_run: bool,
        skip_validation: bool,
        repo_root: str,
    ) -> str:
        manifest_yaml = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False)
        manifest_sha256 = _sha256_text(manifest_yaml)
        git_ctx = _git_context(repo_root)
        initiated_by = (
            os.environ.get("RESILIENCE_RUN_BY")
            or os.environ.get("USER")
            or os.environ.get("USERNAME")
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO resilience.test_run (
                    status,
                    engine_family,
                    manifest_path,
                    manifest_sha256,
                    manifest_yaml,
                    manifest_json,
                    dry_run,
                    skip_validation,
                    region_context,
                    initiated_by,
                    host_name,
                    host_ip,
                    git_commit,
                    git_branch,
                    git_dirty,
                    runner_version
                )
                VALUES (
                    'running',
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                RETURNING run_id
                """,
                (
                    engine_family,
                    manifest_path,
                    manifest_sha256,
                    manifest_yaml,
                    self._Jsonb(_json_safe(manifest)),
                    dry_run,
                    skip_validation,
                    self._Jsonb(_json_safe(_build_region_context(manifest))),
                    initiated_by,
                    socket.gethostname(),
                    None,
                    git_ctx.get("git_commit"),
                    git_ctx.get("git_branch"),
                    git_ctx.get("git_dirty"),
                    "scripts/main.py",
                ),
            )
            row = cur.fetchone()
        return str(row[0])

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        report_path: Optional[str] = None,
        report_url: Optional[str] = None,
        notes: Optional[str] = None,
        ended_at: bool = False,
    ) -> None:
        timestamp = _utc_now() if ended_at else None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE resilience.test_run
                SET status = %s,
                    report_path = COALESCE(%s, report_path),
                    report_url = COALESCE(%s, report_url),
                    notes = COALESCE(%s, notes),
                    ended_at = CASE WHEN %s IS NOT NULL THEN %s ELSE ended_at END,
                    updated_at = NOW()
                WHERE run_id = %s
                """,
                (status, report_path, report_url, notes, timestamp, timestamp, run_id),
            )

    def replace_validation_results(
        self,
        run_id: str,
        *,
        manifest: Dict[str, Any],
        validation_failed_message: Optional[str] = None,
        skipped: bool = False,
    ) -> None:
        validations = load_action_validations()
        rows: List[Tuple[str, str, Optional[str]]] = []
        services = manifest.get("services") or []
        for svc in services:
            if not isinstance(svc, dict):
                continue
            action_key = f"{normalize_service_name(svc.get('name'))}:{str(svc.get('action') or '').strip().lower()}"
            for validation_name in validations.get(action_key) or []:
                if skipped:
                    rows.append((validation_name, "skipped", "Validation skipped by CLI flag."))
                else:
                    rows.append((validation_name, "passed", None))

        if validation_failed_message:
            rows.append(("validation_error", "failed", validation_failed_message))

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM resilience.validation_result WHERE run_id = %s", (run_id,))
            for validation_name, status, message in rows:
                cur.execute(
                    """
                    INSERT INTO resilience.validation_result (
                        run_id,
                        validation_name,
                        status,
                        message
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (run_id, validation_name, status, message),
                )

    def replace_actions(
        self,
        run_id: str,
        *,
        manifest: Dict[str, Any],
        engine_family: str,
        dry_run: bool,
        fis_payload: Optional[Dict[str, Any]] = None,
        execution_plan: Optional[Dict[str, Any]] = None,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        services = [svc for svc in (manifest.get("services") or []) if isinstance(svc, dict)]
        refs = _service_occurrence_refs(manifest)
        summary_actions = (summary or {}).get("actions") or {}
        custom_summary_items = _map_custom_summary_items(summary)
        plan_items = list((execution_plan or {}).get("items") or [])
        payload_actions = (fis_payload or {}).get("actions") or {}
        payload_targets = (fis_payload or {}).get("targets") or {}

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM resilience.test_action WHERE run_id = %s", (run_id,))

            for index, svc in enumerate(services, start=1):
                service_name = normalize_service_name(svc.get("name"))
                action_name = str(svc.get("action") or "").strip().lower()
                requested_region = resolve_service_region(manifest, svc)
                requested_zone = resolve_service_zone(manifest, svc)
                action_ref = refs.get(index) or f"{service_name}:{action_name}"
                exec_name = f"a_{service_name}_{action_name}_{index}"
                execution_target_json: Dict[str, Any] = {}
                action_summary = summary_actions.get(exec_name)
                item: Optional[Dict[str, Any]] = None

                if fis_payload:
                    action_obj = payload_actions.get(exec_name) or {}
                    target_refs = list((action_obj.get("targets") or {}).values())
                    execution_target_json = {
                        "executionName": exec_name,
                        "actionId": action_obj.get("actionId"),
                        "parameters": action_obj.get("parameters"),
                        "targets": action_obj.get("targets"),
                        "resolvedTargets": {ref: payload_targets.get(ref) for ref in target_refs},
                    }
                elif index <= len(plan_items):
                    item = plan_items[index - 1]
                    exec_name = str(item.get("name") or exec_name)
                    action_summary = custom_summary_items.get(exec_name) or summary_actions.get(exec_name)
                    execution_target_json = dict(item)

                if action_summary:
                    status = _status_for_db(action_summary.get("status"))
                    reason = action_summary.get("reason")
                    started_at = action_summary.get("startTime")
                    ended_at = action_summary.get("endTime")
                    result_json = action_summary
                elif dry_run:
                    status = "skipped"
                    reason = "Dry run; action was not executed."
                    started_at = None
                    ended_at = None
                    result_json = None
                else:
                    status = "running"
                    reason = None
                    started_at = None
                    ended_at = None
                    result_json = None

                cur.execute(
                    """
                    INSERT INTO resilience.test_action (
                        run_id,
                        sequence_no,
                        action_ref,
                        service_name,
                        action_name,
                        engine_family,
                        start_after,
                        requested_region,
                        requested_zone,
                        status,
                        reason,
                        started_at,
                        ended_at,
                        service_config_json,
                        execution_target_json,
                        result_json
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING action_id
                    """,
                    (
                        run_id,
                        index,
                        action_ref,
                        service_name,
                        action_name,
                        engine_family,
                        _normalize_start_after(svc.get("start_after")),
                        requested_region,
                        requested_zone,
                        status,
                        reason,
                        started_at,
                        ended_at,
                        self._Jsonb(_json_safe(svc)),
                        self._Jsonb(_json_safe(execution_target_json or {})),
                        self._Jsonb(_json_safe(result_json)) if result_json is not None else None,
                    ),
                )
                action_row = cur.fetchone()
                action_id = str(action_row[0]) if action_row else None

                rollback_state = _extract_rollback_state(
                    service_name=service_name,
                    action_name=action_name,
                    requested_region=requested_region,
                    plan_item=item,
                    result_json=result_json,
                )
                if action_id and rollback_state is not None:
                    cur.execute(
                        """
                        INSERT INTO resilience.rollback_state (
                            run_id,
                            action_id,
                            rollback_mode,
                            rollback_supported,
                            before_state_json,
                            after_state_json,
                            rollback_plan_json,
                            rollback_status,
                            rollback_reason
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_id,
                            action_id,
                            rollback_state.get("rollback_mode"),
                            bool(rollback_state.get("rollback_supported")),
                            self._Jsonb(_json_safe(rollback_state.get("before_state_json"))) if rollback_state.get("before_state_json") is not None else None,
                            self._Jsonb(_json_safe(rollback_state.get("after_state_json"))) if rollback_state.get("after_state_json") is not None else None,
                            self._Jsonb(_json_safe(rollback_state.get("rollback_plan_json"))) if rollback_state.get("rollback_plan_json") is not None else None,
                            None,
                            None,
                        ),
                    )

    def replace_impacted_resources(self, run_id: str, impacted_resources: List[Dict[str, Any]]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM resilience.impacted_resource WHERE run_id = %s", (run_id,))
            for item in impacted_resources or []:
                if not isinstance(item, dict):
                    continue
                arn = str(item.get("arn") or "").strip()
                if not arn:
                    continue
                cur.execute(
                    """
                    INSERT INTO resilience.impacted_resource (
                        run_id,
                        service_action,
                        resource_arn,
                        resource_type,
                        selection_mode,
                        resource_region,
                        resource_metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        str(item.get("service") or ""),
                        arn,
                        _resource_type_from_arn(arn),
                        item.get("selection_mode"),
                        item.get("resource_region"),
                        self._Jsonb(_json_safe(item)),
                    ),
                )

    def replace_artifacts(self, run_id: str, artifacts: List[Dict[str, Any]]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM resilience.execution_artifact WHERE run_id = %s", (run_id,))
            for artifact in artifacts or []:
                if not isinstance(artifact, dict):
                    continue
                local_path = str(artifact.get("local_path") or "").strip() or None
                content_json = artifact.get("content_json")
                content_sha256 = artifact.get("content_sha256") or _sha256_file(local_path) if local_path else None
                file_size_bytes = None
                if local_path and os.path.isfile(local_path):
                    try:
                        file_size_bytes = os.path.getsize(local_path)
                    except Exception:
                        file_size_bytes = None

                cur.execute(
                    """
                    INSERT INTO resilience.execution_artifact (
                        run_id,
                        artifact_type,
                        local_path,
                        object_url,
                        content_sha256,
                        content_json,
                        file_size_bytes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        artifact.get("artifact_type"),
                        local_path,
                        artifact.get("object_url"),
                        content_sha256,
                        self._Jsonb(_json_safe(content_json)) if content_json is not None else None,
                        file_size_bytes,
                    ),
                )

    def replace_metric_series(self, run_id: str, observability: Optional[Dict[str, Any]]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM resilience.metric_series WHERE run_id = %s", (run_id,))
            if not isinstance(observability, dict):
                return

            for record in observability.get("health_check") or []:
                if not isinstance(record, dict):
                    continue
                timestamp = record.get("timestamp")
                status_code = record.get("status_code")
                healthy = record.get("healthy")
                if timestamp and status_code is not None:
                    cur.execute(
                        """
                        INSERT INTO resilience.metric_series (
                            run_id, namespace, metric_name, stat, observed_at, value, dimensions_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_id,
                            "custom.health_check",
                            "status_code",
                            "Latest",
                            timestamp,
                            float(status_code),
                            self._Jsonb(_json_safe({"error": record.get("error")})),
                        ),
                    )
                if timestamp and healthy is not None:
                    cur.execute(
                        """
                        INSERT INTO resilience.metric_series (
                            run_id, namespace, metric_name, stat, observed_at, value, dimensions_json
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_id,
                            "custom.health_check",
                            "healthy",
                            "Latest",
                            timestamp,
                            1.0 if bool(healthy) else 0.0,
                            self._Jsonb(_json_safe({"error": record.get("error")})),
                        ),
                    )

            cloudwatch = observability.get("cloudwatch") or {}
            lb = cloudwatch.get("load_balancer") or {}
            for sample in lb.get("samples") or []:
                self._insert_cloudwatch_sample(cur, run_id, None, sample)

            for resource in cloudwatch.get("resources") or []:
                if not isinstance(resource, dict):
                    continue
                arn = resource.get("arn")
                for sample in resource.get("samples") or []:
                    self._insert_cloudwatch_sample(cur, run_id, arn, sample)

    def _insert_cloudwatch_sample(self, cur, run_id: str, resource_arn: Optional[str], sample: Dict[str, Any]) -> None:
        timestamp = sample.get("timestamp")
        namespace = sample.get("namespace")
        dimensions = sample.get("dimensions") or []
        metrics = sample.get("metrics") or {}
        if not timestamp or not namespace or not isinstance(metrics, dict):
            return
        for metric_name, payload in metrics.items():
            if not isinstance(payload, dict):
                continue
            value = payload.get("value")
            metric_timestamp = payload.get("metric_timestamp") or timestamp
            if value is None:
                continue
            cur.execute(
                """
                INSERT INTO resilience.metric_series (
                    run_id, resource_arn, namespace, metric_name, stat, observed_at, value, dimensions_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    resource_arn,
                    namespace,
                    metric_name,
                    "Sum",
                    metric_timestamp,
                    float(value),
                    self._Jsonb(_json_safe(dimensions)),
                ),
            )
