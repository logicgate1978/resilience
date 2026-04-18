from __future__ import annotations

import hashlib
import os
import socket
import subprocess
from datetime import datetime, timezone
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
                    self._Jsonb(manifest),
                    dry_run,
                    skip_validation,
                    self._Jsonb(_build_region_context(manifest)),
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
                    action_summary = summary_actions.get(exec_name)
                    execution_target_json = {
                        "executionName": exec_name,
                        "target": item.get("target"),
                        "parameters": item.get("parameters"),
                        "startAfter": item.get("startAfter"),
                    }

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
                        self._Jsonb(svc),
                        self._Jsonb(execution_target_json or {}),
                        self._Jsonb(result_json) if result_json is not None else None,
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
                        self._Jsonb(item),
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
                        self._Jsonb(content_json) if content_json is not None else None,
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
                            self._Jsonb({"error": record.get("error")}),
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
                            self._Jsonb({"error": record.get("error")}),
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
                    self._Jsonb(dimensions),
                ),
            )
