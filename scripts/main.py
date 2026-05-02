import argparse
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3
import botocore

from component_actions import (
    build_custom_execution_plan,
    collect_custom_impacted_resources,
    execute_custom_plan,
    service_uses_custom_engine,
)
from fis_template_generator import create_template, generate_template_payload
from observability import parse_observability, start_observability_collectors
from persistence import PostgresRunStore
from region_switch import (
    build_execution_plan,
    execute_region_plan,
    resolve_region_targets,
    validate_region_manifest,
)
from resource import collect_impacted_resources
from utility import (
    coerce_bool,
    ensure_dir,
    load_env_file,
    load_manifest,
    log_message,
    normalize_service_name,
    parse_bool,
    pretty,
    resolve_service_primary_region,
    resolve_service_region,
    resolve_service_zone,
    upload_files_to_artifactory,
)
from validations import ValidationError, validate_manifest_services

from chart import generate_report
from auth import AccessController

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")


def _env_value(env: Dict[str, str], key: str, fallback: str) -> str:
    value = env.get(key)
    if value is None or str(value).strip() == "":
        return fallback
    return str(value).strip()


def _env_path(env: Dict[str, str], key: str, fallback: str) -> str:
    value = _env_value(env, key, fallback)
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(REPO_ROOT, value))


def _env_int(env: Dict[str, str], key: str, fallback: int) -> int:
    value = env.get(key)
    if value is None or str(value).strip() == "":
        return fallback
    try:
        return int(str(value).strip())
    except Exception:
        return fallback


def _manifest_services(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    services = manifest.get("services") or []
    if not isinstance(services, list) or not services:
        raise ValueError("Top-level 'services' must be a non-empty list.")
    return [svc for svc in services if isinstance(svc, dict)]


def _service_engine_family(svc: Dict[str, Any]) -> str:
    service_name = normalize_service_name(svc.get("name"))
    action = str(svc.get("action") or "").strip().lower()

    if service_name == "rds" and action in ("failover-global-db", "switchover-global-db"):
        return "arc" if coerce_bool(svc.get("use_arc"), True) else "custom"

    return "custom" if service_uses_custom_engine(svc) else "fis"


def _resolve_manifest_engine_family(manifest: Dict[str, Any]) -> str:
    services = _manifest_services(manifest)
    engine_families = {_service_engine_family(svc) for svc in services}
    if len(engine_families) > 1:
        raise ValueError(
            "Mixing FIS, ARC, and custom implementations in one manifest is not supported. "
            "Keep all service actions in a manifest on the same execution engine family."
        )
    return next(iter(engine_families))


def _default_session_region(manifest: Dict[str, Any], engine_family: str) -> Optional[str]:
    services = _manifest_services(manifest)

    if engine_family == "arc":
        for svc in services:
            if _service_engine_family(svc) == "arc":
                region = resolve_service_primary_region(manifest, svc)
                if region:
                    return region
        return None

    for svc in services:
        region = resolve_service_region(manifest, svc)
        if region:
            return region

    for svc in services:
        region = resolve_service_primary_region(manifest, svc)
        if region:
            return region

    if engine_family == "custom":
        return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

    return None


def start_experiment(fis_client, template_id: str) -> str:
    resp = fis_client.start_experiment(experimentTemplateId=template_id)
    return resp["experiment"]["id"]


def get_experiment(fis_client, experiment_id: str) -> Dict[str, Any]:
    return fis_client.get_experiment(id=experiment_id)["experiment"]


def wait_for_completion(
    fis_client,
    experiment_id: str,
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    terminal = {"completed", "stopped", "failed"}
    start = time.time()
    while True:
        exp = get_experiment(fis_client, experiment_id)
        status = exp.get("state", {}).get("status", "unknown")
        if status in terminal:
            return exp
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Experiment {experiment_id} timed out after {timeout_seconds}s (last={status}).")
        log_message("INFO", f"FIS is running: experimentId={experiment_id} status={status} elapsed={int(time.time()-start)}s")
        time.sleep(poll_seconds)


def summarize_experiment(exp: Dict[str, Any]) -> Dict[str, Any]:
    state = exp.get("state", {})
    actions = exp.get("actions", {})
    out = {
        "experimentId": exp.get("id"),
        "experimentTemplateId": exp.get("experimentTemplateId"),
        "status": state.get("status"),
        "reason": state.get("reason"),
        "startTime": exp.get("startTime"),
        "endTime": exp.get("endTime"),
        "actions": {},
    }
    for name, a in actions.items():
        s = a.get("state", {})
        out["actions"][name] = {
            "status": s.get("status"),
            "reason": s.get("reason"),
            "startTime": a.get("startTime"),
            "endTime": a.get("endTime"),
        }
    return out


def _get_report_filename(base_name: str) -> str:
    report_date = datetime.utcnow().strftime("%Y%m%d")
    return f"report_{base_name}_{report_date}.html"


def _db_safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log_message("WARN", f"Database persistence error: {e}")
        return None


def _artifact_entry(
    artifact_type: str,
    *,
    local_path: Optional[str] = None,
    content_json: Optional[Dict[str, Any]] = None,
    object_url: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "local_path": local_path,
        "content_json": content_json,
        "object_url": object_url,
    }


def _db_run_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"completed", "failed", "stopped", "skipped", "running"}:
        return text
    return "failed" if text else "completed"


def _service_occurrence_refs(manifest: Dict[str, Any]) -> Dict[int, str]:
    services = _manifest_services(manifest)
    totals: Dict[str, int] = {}
    normalized: List[tuple[str, str]] = []
    for svc in services:
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


def _build_single_service_manifest(manifest: Dict[str, Any], svc: Dict[str, Any]) -> Dict[str, Any]:
    scoped = dict(manifest)
    scoped["services"] = [svc]
    return scoped


def _collect_service_impacted_resources(
    *,
    manifest: Dict[str, Any],
    svc: Dict[str, Any],
    session,
    default_region: Optional[str],
) -> List[Dict[str, Any]]:
    scoped_manifest = _build_single_service_manifest(manifest, svc)
    return collect_impacted_resources(
        manifest=scoped_manifest,
        session=session,
        region=default_region,
    )


def _short_resource_label(arn: str) -> str:
    text = str(arn or "").strip()
    if not text:
        return "-"
    if text.startswith("arn:"):
        if "/" in text:
            return text.rsplit("/", 1)[-1]
        if ":" in text:
            return text.rsplit(":", 1)[-1]
    if text.startswith("route53://"):
        return text.replace("route53://", "", 1)
    if text.startswith("eks://"):
        return text.replace("eks://", "", 1)
    return text


def _summarize_impacted_resources(resources: List[Dict[str, Any]]) -> str:
    if not resources:
        return "-"
    labels = [_short_resource_label(item.get("arn") or "") for item in resources]
    labels = [label for label in labels if label]
    if not labels:
        return "-"
    if len(labels) <= 3:
        return ", ".join(labels)
    return ", ".join(labels[:3]) + f" (+{len(labels) - 3} more)"


def _format_start_after(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(parts) if parts else "-"
    text = str(value).strip()
    return text or "-"


def _stringify_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "-"
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return ",".join(f"{k}={_stringify_value(v)}" for k, v in value.items())
    return str(value)


_NON_RESOURCE_PARAMETER_KEYS = {
    "waitForReady",
    "timeoutSeconds",
    "requireQuiesce",
    "finalSyncGraceSeconds",
    "wait_for_ready",
    "timeout_seconds",
    "require_quiesce",
    "final_sync_grace_seconds",
    "action",
    "mode",
    "comment",
}


def _format_key_parameters(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict) or not data:
        return "-"
    parts = []
    for key, value in data.items():
        if str(key) in _NON_RESOURCE_PARAMETER_KEYS:
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        parts.append(f"{key}={_stringify_value(value)}")
    return ", ".join(parts) if parts else "-"


def _truncate(value: str, width: int) -> str:
    text = str(value or "")
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _render_ascii_table(headers: List[str], rows: List[List[str]]) -> str:
    max_widths = {
        "No": 4,
        "Action": 28,
        "Engine": 8,
        "Exec Type": 34,
        "Region": 18,
        "Zone": 16,
        "Start After": 24,
        "Impacted Resources": 52,
        "Key Parameters": 52,
    }
    widths: List[int] = []
    for idx, header in enumerate(headers):
        values = [header] + [str(row[idx]) for row in rows]
        width = max(len(v) for v in values) if values else len(header)
        widths.append(min(width, max_widths.get(header, width)))

    def format_row(values: List[str]) -> str:
        cells = []
        for idx, value in enumerate(values):
            cells.append(" " + _truncate(str(value), widths[idx]).ljust(widths[idx]) + " ")
        return "|" + "|".join(cells) + "|"

    separator = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    out = [separator, format_row(headers), separator]
    for row in rows:
        out.append(format_row(row))
    out.append(separator)
    return "\n".join(out)


def _build_fis_dry_run_rows(
    *,
    manifest: Dict[str, Any],
    payload: Dict[str, Any],
    session,
    default_region: Optional[str],
) -> List[List[str]]:
    rows: List[List[str]] = []
    refs = _service_occurrence_refs(manifest)
    services = _manifest_services(manifest)
    actions = payload.get("actions") or {}

    for index, svc in enumerate(services, start=1):
        service_name = normalize_service_name(svc.get("name"))
        action_name = str(svc.get("action") or "").strip().lower()
        action_key = refs.get(index) or f"{service_name}:{action_name}"
        execution_name = f"a_{service_name}_{action_name}_{index}"
        action_obj = actions.get(execution_name) or {}
        impacted = _collect_service_impacted_resources(
            manifest=manifest,
            svc=svc,
            session=session,
            default_region=default_region,
        )
        rows.append(
            [
                str(index),
                action_key,
                "FIS",
                str(action_obj.get("actionId") or "-"),
                str(resolve_service_region(manifest, svc) or "-"),
                str(resolve_service_zone(manifest, svc) or "-"),
                _format_start_after(svc.get("start_after")),
                _summarize_impacted_resources(impacted),
                _format_key_parameters(action_obj.get("parameters") or {}),
            ]
        )
    return rows


def _custom_exec_type(item: Dict[str, Any]) -> str:
    target = item.get("target") or {}
    if item.get("service") == "efs:failover":
        return "delete replication config"
    if item.get("service") == "efs:failback":
        return "create reverse replication"
    if item.get("service") == "efs:failback-safe":
        return "reverse-sync and restore replication"
    if item.get("service") == "asg:scale":
        return "update ASG capacity"
    if item.get("service") == "dns:set-value":
        return "update Route53 record"
    if item.get("service") == "dns:set-weight":
        return "update Route53 weights"
    if item.get("service") == "s3:failover":
        return "update MRAP routing"
    if item.get("service") == "common:wait":
        return "python sleep"
    if item.get("service") == "eks:scale-deployment":
        return f"scale deployment {target.get('deploymentName') or ''}".strip()
    if item.get("service") == "eks:scale-nodegroup":
        return f"scale nodegroup {target.get('nodegroupName') or ''}".strip()
    if item.get("service") == "rds:failover-global-db":
        return "failover_global_cluster"
    if item.get("service") == "rds:switchover-global-db":
        return "switchover_global_cluster"
    return str(item.get("description") or item.get("action") or "custom")


def _build_custom_dry_run_rows(
    *,
    manifest: Dict[str, Any],
    execution_plan: Dict[str, Any],
    session,
    default_region: Optional[str],
) -> List[List[str]]:
    rows: List[List[str]] = []
    refs = _service_occurrence_refs(manifest)
    services = _manifest_services(manifest)
    items = list(execution_plan.get("items") or [])

    for index, (svc, item) in enumerate(zip(services, items), start=1):
        impacted = list(item.get("impacted_resources") or [])
        if not impacted and isinstance(item.get("impacted_resource"), dict):
            impacted = [item.get("impacted_resource")]
        if not impacted:
            impacted = _collect_service_impacted_resources(
                manifest=manifest,
                svc=svc,
                session=session,
                default_region=default_region,
            )
        rows.append(
            [
                str(index),
                refs.get(index) or str(item.get("service") or "-"),
                "Custom",
                _custom_exec_type(item),
                str(item.get("region") or resolve_service_region(manifest, svc) or "-"),
                str(resolve_service_zone(manifest, svc) or "-"),
                _format_start_after(item.get("startAfter") or svc.get("start_after")),
                _summarize_impacted_resources(impacted),
                _format_key_parameters(item.get("parameters") or {}),
            ]
        )
    return rows


def _build_arc_dry_run_rows(
    *,
    manifest: Dict[str, Any],
    execution_plan: Dict[str, Any],
    session,
    default_region: Optional[str],
) -> List[List[str]]:
    rows: List[List[str]] = []
    refs = _service_occurrence_refs(manifest)
    services = _manifest_services(manifest)
    items = list(execution_plan.get("items") or [])

    for index, (svc, item) in enumerate(zip(services, items), start=1):
        impacted = _collect_service_impacted_resources(
            manifest=manifest,
            svc=svc,
            session=session,
            default_region=default_region,
        )
        if item.get("engine") == "arc":
            exec_type = "AuroraGlobalDatabase/activate"
            region_value = item.get("request", {}).get("targetRegion") or item.get("planControlRegion") or "-"
            params = {
                "action": item.get("request", {}).get("action"),
                "mode": item.get("request", {}).get("mode"),
            }
        else:
            exec_type = str(item.get("request", {}).get("sdkApi") or item.get("action") or "-")
            region_value = item.get("clientRegion") or item.get("region") or "-"
            params = item.get("request", {}).get("params") or item.get("parameters") or {}

        rows.append(
            [
                str(index),
                refs.get(index) or str(item.get("service") or "-"),
                "ARC",
                exec_type,
                str(region_value),
                str(resolve_service_zone(manifest, svc) or "-"),
                _format_start_after(item.get("startAfter") or svc.get("start_after")),
                _summarize_impacted_resources(impacted),
                _format_key_parameters(params),
            ]
        )
    return rows


def _build_dry_run_summary_text(
    *,
    manifest_path: str,
    engine_family: str,
    rows: List[List[str]],
) -> str:
    headers = [
        "No",
        "Action",
        "Engine",
        "Exec Type",
        "Region",
        "Zone",
        "Start After",
        "Impacted Resources",
        "Key Parameters",
    ]
    parallel_note = "actions without start_after will run in parallel"

    lines = [
        "DRY RUN APPROVAL SUMMARY",
        f"Manifest: {manifest_path}",
        f"Engine family: {engine_family.upper()}",
        f"Actions: {len(rows)}",
        f"Note: {parallel_note}",
        "",
        _render_ascii_table(headers, rows),
    ]
    return "\n".join(lines)


def _write_dry_run_summary(
    *,
    outdir: str,
    name: str,
    text: str,
) -> str:
    path = os.path.join(outdir, f"dry_run_approval_summary_{name}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.write("\n")
    return path


def main() -> int:
    env_defaults = load_env_file(ENV_PATH)

    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=_env_path(env_defaults, "MANIFEST", os.path.join("manifests", "main.yml")), help="Path to manifest.yml")
    ap.add_argument("--account-id", default=_env_value(env_defaults, "ACCOUNT_ID", None), help="AWS Account ID to run the experiment in")
    ap.add_argument("--username", default=_env_value(env_defaults, "USERNAME", None), help="Username of the service account")
    ap.add_argument("--password", help="Password of the service account")
    ap.add_argument("--fis-role-arn", default=_env_value(env_defaults, "FIS_ROLE_ARN", None), help="FIS IAM role ARN (required unless --dry-run)")
    ap.add_argument("--arc-role-arn", default=_env_value(env_defaults, "ARC_ROLE_ARN", None), help="ARC Region switch execution role ARN (required for region tests unless --dry-run)")
    ap.add_argument("--outdir", default=_env_path(env_defaults, "OUTDIR", os.path.join("scripts", "fis_out")), help="Output directory for template/results JSON/CSVs")
    ap.add_argument("--db-dsn", default=_env_value(env_defaults, "DB_DSN", _env_value(env_defaults, "DATABASE_URL", "")), help="Optional PostgreSQL DSN for persisting run metadata and artifacts")
    ap.add_argument("--dry-run", action="store_true", help="Generate JSON only; do not create or execute")
    ap.add_argument("--skip-validation", action="store_true", help="Skip pre-execution action validation and continue directly to planning/execution")
    ap.add_argument("--poll-seconds", type=int, default=_env_int(env_defaults, "POLL_SECONDS", 10), help="Polling interval while waiting for experiment")
    ap.add_argument("--timeout-seconds", type=int, default=_env_int(env_defaults, "TIMEOUT_SECONDS", 7200), help="Timeout per experiment in seconds")
    ap.add_argument("--upload-artifactory", default=parse_bool(env_defaults.get("UPLOAD_ARTIFACTORY"), False), action="store_true", help="Upload generated HTML report to Artifactory")
    args = ap.parse_args()

    manifest = load_manifest(args.manifest)
    engine_family = _resolve_manifest_engine_family(manifest)
    artifact_entries: List[Dict[str, Any]] = [
        _artifact_entry("manifest", local_path=os.path.abspath(args.manifest), content_json=manifest)
    ]
    db_store = _db_safe_call(PostgresRunStore.from_dsn, args.db_dsn)
    run_id = None
    if db_store is not None:
        run_id = _db_safe_call(
            db_store.create_run,
            manifest=manifest,
            manifest_path=os.path.abspath(args.manifest),
            engine_family=engine_family,
            dry_run=args.dry_run,
            skip_validation=args.skip_validation,
            repo_root=REPO_ROOT,
        )

    ensure_dir(args.outdir)

    if engine_family == "arc":
        if args.skip_validation:
            log_message("WARN", "--skip-validation enabled: skipping ARC pre-execution validation.")
            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.replace_validation_results,
                    run_id,
                    manifest=manifest,
                    skipped=True,
                )
        else:
            try:
                validate_region_manifest(manifest)
                if db_store is not None and run_id:
                    _db_safe_call(
                        db_store.replace_validation_results,
                        run_id,
                        manifest=manifest,
                    )
            except ValueError as e:
                print(f"[ERROR] {e}", flush=True)
                if db_store is not None and run_id:
                    _db_safe_call(
                        db_store.replace_validation_results,
                        run_id,
                        manifest=manifest,
                        validation_failed_message=str(e),
                    )
                    _db_safe_call(
                        db_store.update_run,
                        run_id,
                        status="failed",
                        notes=str(e),
                        ended_at=True,
                    )
                return 1
        control_region = _default_session_region(manifest, engine_family)

        if args.account_id and args.username and args.password:
            session = AccessController(args.account_id, control_region, 'PubCloud_NonProd_Admin', args.username, args.password).getServiceSession()
        else:
            session = boto3.Session(region_name=control_region)

        resolved_targets = resolve_region_targets(manifest, session)
        impacted_resources = collect_impacted_resources(
            manifest=manifest,
            session=session,
            region=None,
        )
        impacted_resources_path = os.path.join(args.outdir, "impacted_resources.json")
        with open(impacted_resources_path, "w", encoding="utf-8") as f:
            f.write(pretty({"impacted_resources": impacted_resources}))
        log_message("OK", f"Wrote impacted resources JSON: {impacted_resources_path}")
        artifact_entries.append(
            _artifact_entry(
                "impacted_resources",
                local_path=impacted_resources_path,
                content_json={"impacted_resources": impacted_resources},
            )
        )

        arc_role_arn = args.arc_role_arn or ("REPLACE_ME" if args.dry_run else "")
        execution_plan = build_execution_plan(
            manifest=manifest,
            execution_role_arn=arc_role_arn,
            resolved_targets=resolved_targets,
        )
        plan_name = execution_plan["name"]
        report_filename = _get_report_filename(plan_name)

        execution_plan_path = os.path.join(args.outdir, f"region_execution_plan_{plan_name}.json")
        with open(execution_plan_path, "w", encoding="utf-8") as f:
            f.write(pretty(execution_plan))
        log_message("OK", f"Wrote region execution plan JSON: {execution_plan_path}")
        artifact_entries.append(
            _artifact_entry(
                "region_execution_plan",
                local_path=execution_plan_path,
                content_json=execution_plan,
            )
        )

        if db_store is not None and run_id:
            _db_safe_call(
                db_store.replace_actions,
                run_id,
                manifest=manifest,
                engine_family=engine_family,
                dry_run=args.dry_run,
                execution_plan=execution_plan,
            )
            _db_safe_call(db_store.replace_impacted_resources, run_id, impacted_resources)
            _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)

        if args.dry_run:
            dry_run_rows = _build_arc_dry_run_rows(
                manifest=manifest,
                execution_plan=execution_plan,
                session=session,
                default_region=control_region,
            )
            dry_run_text = _build_dry_run_summary_text(
                manifest_path=os.path.abspath(args.manifest),
                engine_family=engine_family,
                rows=dry_run_rows,
            )
            dry_run_summary_path = _write_dry_run_summary(
                outdir=args.outdir,
                name=plan_name,
                text=dry_run_text,
            )
            print(dry_run_text, flush=True)
            log_message("OK", f"Wrote dry-run approval summary: {dry_run_summary_path}")
            artifact_entries.append(_artifact_entry("other", local_path=dry_run_summary_path))
            if db_store is not None and run_id:
                _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)
            log_message("INFO", "Dry-run enabled: skipping create/execute.")
            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.update_run,
                    run_id,
                    status="completed",
                    notes="Dry run completed; ARC plan was generated but not executed.",
                    ended_at=True,
                )
            return 0

        stop_event: Optional[Any] = None
        obs_results: Optional[Dict[str, Any]] = None
        obs_threads: List[Any] = []
        report_path: Optional[str] = None

        try:
            stop_event, obs_results, obs_threads = start_observability_collectors(
                manifest=manifest,
                session=session,
                region=control_region,
                outdir=args.outdir,
                impacted_resources=[],
            )

            obs_cfg = parse_observability(manifest)
            start_before_min = int(obs_cfg.get("start_before") or 0)
            stop_after_min = int(obs_cfg.get("stop_after") or 0)

            if start_before_min > 0:
                log_message("INFO", f"start_before={start_before_min} minutes: waiting before starting region switch...")
                time.sleep(start_before_min * 60)

            summary = execute_region_plan(
                session=session,
                execution_plan=execution_plan,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )

            if stop_after_min > 0:
                log_message("INFO", f"stop_after={stop_after_min} minutes: continuing observability collection...")
                time.sleep(stop_after_min * 60)

            if stop_event is not None:
                stop_event.set()
            for t in obs_threads:
                t.join(timeout=5)

            if obs_results is not None:
                summary["observability"] = obs_results

            result_path = os.path.join(args.outdir, f"result_{plan_name}.json")
            with open(result_path, "w", encoding="utf-8") as f:
                f.write(pretty(summary))
            log_message("OK", f"Wrote result summary JSON: {result_path}")
            artifact_entries.append(
                _artifact_entry(
                    "result_summary",
                    local_path=result_path,
                    content_json=summary,
                )
            )

            print("[RESULT] Summary:", flush=True)
            print(pretty(summary), flush=True)

            report_path = generate_report(args.outdir, html_filename=report_filename)
            log_message("OK", f"Wrote HTML report: {report_path}")
            artifact_entries.append(
                _artifact_entry(
                    "html_report",
                    local_path=report_path,
                )
            )
            if isinstance(summary.get("observability"), dict):
                artifact_entries.append(
                    _artifact_entry(
                        "observability_summary",
                        content_json=summary.get("observability"),
                    )
                )

            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.replace_actions,
                    run_id,
                    manifest=manifest,
                    engine_family=engine_family,
                    dry_run=False,
                    execution_plan=execution_plan,
                    summary=summary,
                )
                _db_safe_call(db_store.replace_impacted_resources, run_id, impacted_resources)
                _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)
                _db_safe_call(db_store.replace_metric_series, run_id, summary.get("observability"))
                _db_safe_call(
                    db_store.update_run,
                    run_id,
                    status=_db_run_status(summary.get("status")),
                    report_path=report_path,
                    notes=summary.get("reason"),
                    ended_at=True,
                )

            if args.upload_artifactory and report_path:
                upload_files_to_artifactory([report_path])

        except botocore.exceptions.ClientError as e:
            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.update_run,
                    run_id,
                    status="failed",
                    notes=str(e),
                    ended_at=True,
                )
            raise RuntimeError(f"AWS API error: {e}") from e
        finally:
            if stop_event is not None:
                stop_event.set()
            for t in obs_threads:
                try:
                    t.join(timeout=2)
                except Exception:
                    pass

            if report_path is None:
                try:
                    rp = generate_report(args.outdir, html_filename=report_filename)
                    log_message("OK", f"Wrote HTML report: {rp}")
                    if args.upload_artifactory:
                        upload_files_to_artifactory([rp])
                except Exception:
                    pass

        return 0

    region = _default_session_region(manifest, engine_family)
    if not region and engine_family == "fis":
        raise ValueError("FIS actions require region at the top level or service level.")

    if args.account_id and args.username and args.password:
        session = AccessController(args.account_id, region, 'PubCloud_NonProd_Admin', args.username, args.password).getServiceSession()
    else:
        session = boto3.Session(region_name=region)

    if args.skip_validation:
        log_message("WARN", "--skip-validation enabled: skipping pre-execution action validation.")
        if db_store is not None and run_id:
            _db_safe_call(
                db_store.replace_validation_results,
                run_id,
                manifest=manifest,
                skipped=True,
            )
    else:
        try:
            validate_manifest_services(
                manifest,
                session=session,
                region=region,
            )
            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.replace_validation_results,
                    run_id,
                    manifest=manifest,
                )
        except ValidationError as e:
            print(f"[ERROR] {e}", flush=True)
            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.replace_validation_results,
                    run_id,
                    manifest=manifest,
                    validation_failed_message=str(e),
                )
                _db_safe_call(
                    db_store.update_run,
                    run_id,
                    status="failed",
                    notes=str(e),
                    ended_at=True,
                )
            return 1
        except ValueError as e:
            print(f"[ERROR] {e}", flush=True)
            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.replace_validation_results,
                    run_id,
                    manifest=manifest,
                    validation_failed_message=str(e),
                )
                _db_safe_call(
                    db_store.update_run,
                    run_id,
                    status="failed",
                    notes=str(e),
                    ended_at=True,
                )
            return 1

    if engine_family == "custom":
        execution_plan = build_custom_execution_plan(
            manifest,
            session=session,
            region=region,
            default_timeout_seconds=args.timeout_seconds,
        )
        plan_name = execution_plan["name"]
        report_filename = _get_report_filename(plan_name)

        execution_plan_path = os.path.join(args.outdir, f"custom_execution_plan_{plan_name}.json")
        with open(execution_plan_path, "w", encoding="utf-8") as f:
            f.write(pretty(execution_plan))
        log_message("OK", f"Wrote custom execution plan JSON: {execution_plan_path}")
        artifact_entries.append(
            _artifact_entry(
                "custom_execution_plan",
                local_path=execution_plan_path,
                content_json=execution_plan,
            )
        )

        impacted_resources = collect_custom_impacted_resources(execution_plan)
        impacted_resources_path = os.path.join(args.outdir, "impacted_resources.json")
        with open(impacted_resources_path, "w", encoding="utf-8") as f:
            f.write(pretty({"impacted_resources": impacted_resources}))
        log_message("OK", f"Wrote impacted resources JSON: {impacted_resources_path}")
        artifact_entries.append(
            _artifact_entry(
                "impacted_resources",
                local_path=impacted_resources_path,
                content_json={"impacted_resources": impacted_resources},
            )
        )

        if db_store is not None and run_id:
            _db_safe_call(
                db_store.replace_actions,
                run_id,
                manifest=manifest,
                engine_family=engine_family,
                dry_run=args.dry_run,
                execution_plan=execution_plan,
            )
            _db_safe_call(db_store.replace_impacted_resources, run_id, impacted_resources)
            _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)

        if args.dry_run:
            dry_run_rows = _build_custom_dry_run_rows(
                manifest=manifest,
                execution_plan=execution_plan,
                session=session,
                default_region=region,
            )
            dry_run_text = _build_dry_run_summary_text(
                manifest_path=os.path.abspath(args.manifest),
                engine_family=engine_family,
                rows=dry_run_rows,
            )
            dry_run_summary_path = _write_dry_run_summary(
                outdir=args.outdir,
                name=plan_name,
                text=dry_run_text,
            )
            print(dry_run_text, flush=True)
            log_message("OK", f"Wrote dry-run approval summary: {dry_run_summary_path}")
            artifact_entries.append(_artifact_entry("other", local_path=dry_run_summary_path))
            if db_store is not None and run_id:
                _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)
            log_message("INFO", "Dry-run enabled: skipping create/execute.")
            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.update_run,
                    run_id,
                    status="completed",
                    notes="Dry run completed; custom execution plan was generated but not executed.",
                    ended_at=True,
                )
            return 0

        stop_event: Optional[Any] = None
        obs_results: Optional[Dict[str, Any]] = None
        obs_threads: List[Any] = []
        report_path: Optional[str] = None

        try:
            stop_event, obs_results, obs_threads = start_observability_collectors(
                manifest=manifest,
                session=session,
                region=region,
                outdir=args.outdir,
                impacted_resources=impacted_resources,
            )

            obs_cfg = parse_observability(manifest)
            start_before_min = int(obs_cfg.get("start_before") or 0)
            stop_after_min = int(obs_cfg.get("stop_after") or 0)

            if start_before_min > 0:
                log_message("INFO", f"start_before={start_before_min} minutes: waiting before starting custom action...")
                time.sleep(start_before_min * 60)

            summary = execute_custom_plan(
                session=session,
                execution_plan=execution_plan,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )

            if stop_after_min > 0:
                log_message("INFO", f"stop_after={stop_after_min} minutes: continuing observability collection...")
                time.sleep(stop_after_min * 60)

            if stop_event is not None:
                stop_event.set()
            for t in obs_threads:
                t.join(timeout=5)

            if obs_results is not None:
                summary["observability"] = obs_results

            result_path = os.path.join(args.outdir, f"result_{plan_name}.json")
            with open(result_path, "w", encoding="utf-8") as f:
                f.write(pretty(summary))
            log_message("OK", f"Wrote result summary JSON: {result_path}")
            artifact_entries.append(
                _artifact_entry(
                    "result_summary",
                    local_path=result_path,
                    content_json=summary,
                )
            )

            print("[RESULT] Summary:", flush=True)
            print(pretty(summary), flush=True)

            report_path = generate_report(args.outdir, html_filename=report_filename)
            log_message("OK", f"Wrote HTML report: {report_path}")
            artifact_entries.append(_artifact_entry("html_report", local_path=report_path))
            if isinstance(summary.get("observability"), dict):
                artifact_entries.append(
                    _artifact_entry(
                        "observability_summary",
                        content_json=summary.get("observability"),
                    )
                )

            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.replace_actions,
                    run_id,
                    manifest=manifest,
                    engine_family=engine_family,
                    dry_run=False,
                    execution_plan=execution_plan,
                    summary=summary,
                )
                _db_safe_call(db_store.replace_impacted_resources, run_id, impacted_resources)
                _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)
                _db_safe_call(db_store.replace_metric_series, run_id, summary.get("observability"))
                _db_safe_call(
                    db_store.update_run,
                    run_id,
                    status=_db_run_status(summary.get("status")),
                    report_path=report_path,
                    notes=summary.get("reason"),
                    ended_at=True,
                )

            if args.upload_artifactory and report_path:
                upload_files_to_artifactory([report_path])

        except botocore.exceptions.ClientError as e:
            if db_store is not None and run_id:
                _db_safe_call(
                    db_store.update_run,
                    run_id,
                    status="failed",
                    notes=str(e),
                    ended_at=True,
                )
            raise RuntimeError(f"AWS API error: {e}") from e
        finally:
            if stop_event is not None:
                stop_event.set()
            for t in obs_threads:
                try:
                    t.join(timeout=2)
                except Exception:
                    pass

            if report_path is None:
                try:
                    rp = generate_report(args.outdir, html_filename=report_filename)
                    log_message("OK", f"Wrote HTML report: {rp}")
                    if args.upload_artifactory:
                        upload_files_to_artifactory([rp])
                except Exception:
                    pass

        return 0

    if args.dry_run:
        fis_role_arn = args.fis_role_arn or "REPLACE_ME"
    else:
        if not args.fis_role_arn:
            raise ValueError("--fis-role-arn is required unless --dry-run")
        fis_role_arn = args.fis_role_arn

    payload = generate_template_payload(manifest, fis_role_arn=fis_role_arn, selection_mode="ALL")

    template_name = payload["description"]
    report_filename = _get_report_filename(template_name)

    template_json_path = os.path.join(args.outdir, f"template_payload_{template_name}.json")
    with open(template_json_path, "w", encoding="utf-8") as f:
        f.write(pretty(payload))
    log_message("OK", f"Wrote template payload JSON: {template_json_path}")
    artifact_entries.append(
        _artifact_entry(
            "fis_template",
            local_path=template_json_path,
            content_json=payload,
        )
    )

    impacted_resources = collect_impacted_resources(
        manifest=manifest,
        session=session,
        region=region,
    )
    impacted_resources_path = os.path.join(args.outdir, "impacted_resources.json")
    with open(impacted_resources_path, "w", encoding="utf-8") as f:
        f.write(pretty({"impacted_resources": impacted_resources}))
    log_message("OK", f"Wrote impacted resources JSON: {impacted_resources_path}")
    artifact_entries.append(
        _artifact_entry(
            "impacted_resources",
            local_path=impacted_resources_path,
            content_json={"impacted_resources": impacted_resources},
        )
    )

    if db_store is not None and run_id:
        _db_safe_call(
            db_store.replace_actions,
            run_id,
            manifest=manifest,
            engine_family=engine_family,
            dry_run=args.dry_run,
            fis_payload=payload,
        )
        _db_safe_call(db_store.replace_impacted_resources, run_id, impacted_resources)
        _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)

    if args.dry_run:
        dry_run_rows = _build_fis_dry_run_rows(
            manifest=manifest,
            payload=payload,
            session=session,
            default_region=region,
        )
        dry_run_text = _build_dry_run_summary_text(
            manifest_path=os.path.abspath(args.manifest),
            engine_family=engine_family,
            rows=dry_run_rows,
        )
        dry_run_summary_path = _write_dry_run_summary(
            outdir=args.outdir,
            name=template_name,
            text=dry_run_text,
        )
        print(dry_run_text, flush=True)
        log_message("OK", f"Wrote dry-run approval summary: {dry_run_summary_path}")
        artifact_entries.append(_artifact_entry("other", local_path=dry_run_summary_path))
        if db_store is not None and run_id:
            _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)
        log_message("INFO", "Dry-run enabled: skipping create/execute.")
        if db_store is not None and run_id:
            _db_safe_call(
                db_store.update_run,
                run_id,
                status="completed",
                notes="Dry run completed; FIS template was generated but not executed.",
                ended_at=True,
            )
        return 0

    fis_client = session.client("fis")

    stop_event: Optional[Any] = None
    obs_results: Optional[Dict[str, Any]] = None
    obs_threads: List[Any] = []

    report_path: Optional[str] = None  # NEW

    try:
        template_id = create_template(fis_client, payload)
        log_message("OK", f"Created experimentTemplateId: {template_id}")

        stop_event, obs_results, obs_threads = start_observability_collectors(
            manifest=manifest,
            session=session,
            region=region,
            outdir=args.outdir,
            impacted_resources=impacted_resources,
        )

        obs_cfg = parse_observability(manifest)
        start_before_min = int(obs_cfg.get("start_before") or 0)
        stop_after_min = int(obs_cfg.get("stop_after") or 0)

        if start_before_min > 0:
            log_message("INFO", f"start_before={start_before_min} minutes: waiting before starting experiment...")
            time.sleep(start_before_min * 60)

        log_message("INFO", "FIS is running...")
        exp_id = start_experiment(fis_client, template_id)
        log_message("OK", f"Started experimentId: {exp_id}")

        final_exp = wait_for_completion(
            fis_client,
            exp_id,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        summary = summarize_experiment(final_exp)

        if stop_after_min > 0:
            log_message("INFO", f"stop_after={stop_after_min} minutes: continuing observability collection...")
            time.sleep(stop_after_min * 60)

        if stop_event is not None:
            stop_event.set()
        for t in obs_threads:
            t.join(timeout=5)

        if obs_results is not None:
            summary["observability"] = obs_results

        result_path = os.path.join(args.outdir, f"result_{template_name}.json")
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(pretty(summary))
        log_message("OK", f"Wrote result summary JSON: {result_path}")
        artifact_entries.append(
            _artifact_entry(
                "result_summary",
                local_path=result_path,
                content_json=summary,
            )
        )

        print("[RESULT] Summary:", flush=True)
        print(pretty(summary), flush=True)

        report_path = generate_report(args.outdir, html_filename=report_filename)
        log_message("OK", f"Wrote HTML report: {report_path}")
        artifact_entries.append(_artifact_entry("html_report", local_path=report_path))
        if isinstance(summary.get("observability"), dict):
            artifact_entries.append(
                _artifact_entry(
                    "observability_summary",
                    content_json=summary.get("observability"),
                )
            )

        if db_store is not None and run_id:
            _db_safe_call(
                db_store.replace_actions,
                run_id,
                manifest=manifest,
                engine_family=engine_family,
                dry_run=False,
                fis_payload=payload,
                summary=summary,
            )
            _db_safe_call(db_store.replace_impacted_resources, run_id, impacted_resources)
            _db_safe_call(db_store.replace_artifacts, run_id, artifact_entries)
            _db_safe_call(db_store.replace_metric_series, run_id, summary.get("observability"))
            _db_safe_call(
                db_store.update_run,
                run_id,
                status=_db_run_status(summary.get("status")),
                report_path=report_path,
                notes=summary.get("reason"),
                ended_at=True,
            )

        if args.upload_artifactory and report_path:
            upload_files_to_artifactory([report_path])

    except botocore.exceptions.ClientError as e:
        if db_store is not None and run_id:
            _db_safe_call(
                db_store.update_run,
                run_id,
                status="failed",
                notes=str(e),
                ended_at=True,
            )
        raise RuntimeError(f"AWS API error: {e}") from e
    finally:
        if stop_event is not None:
            stop_event.set()
        for t in obs_threads:
            try:
                t.join(timeout=2)
            except Exception:
                pass

        if report_path is None:
            try:
                rp = generate_report(args.outdir, html_filename=report_filename)
                log_message("OK", f"Wrote HTML report: {rp}")
                if args.upload_artifactory:
                    upload_files_to_artifactory([rp])
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
