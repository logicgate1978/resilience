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
    normalize_service_name,
    parse_bool,
    pretty,
    resolve_service_primary_region,
    resolve_service_region,
    upload_files_to_artifactory,
)
from validations import ValidationError, validate_manifest_services

from chart import generate_report


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
        print(f"[INFO] FIS is running: experimentId={experiment_id} status={status} elapsed={int(time.time()-start)}s")
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


def main() -> int:
    env_defaults = load_env_file(ENV_PATH)

    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=_env_path(env_defaults, "MANIFEST", os.path.join("manifests", "geo-1.yml")), help="Path to manifest.yml")
    ap.add_argument("--fis-role-arn", default=_env_value(env_defaults, "FIS_ROLE_ARN", 'arn:aws:iam::065476698259:role/service-role/AWSFISIAMRole-1773418476063'), help="FIS IAM role ARN (required unless --dry-run)")
    ap.add_argument("--arc-role-arn", default=_env_value(env_defaults, "ARC_ROLE_ARN", "arn:aws:iam::065476698259:role/RegionSwitchPlanExecutionRole"), help="ARC Region switch execution role ARN (required for region tests unless --dry-run)")
    ap.add_argument("--outdir", default=_env_path(env_defaults, "OUTDIR", os.path.join("scripts", "fis_out")), help="Output directory for template/results JSON/CSVs")
    ap.add_argument("--dry-run", action="store_true", help="Generate JSON only; do not create or execute")
    ap.add_argument("--poll-seconds", type=int, default=_env_int(env_defaults, "POLL_SECONDS", 10), help="Polling interval while waiting for experiment")
    ap.add_argument("--timeout-seconds", type=int, default=_env_int(env_defaults, "TIMEOUT_SECONDS", 3600), help="Timeout per experiment in seconds")
    ap.add_argument("--upload-artifactory", default=parse_bool(env_defaults.get("UPLOAD_ARTIFACTORY"), False), action="store_true", help="Upload generated HTML report to Artifactory")
    args = ap.parse_args()

    manifest = load_manifest(args.manifest)
    engine_family = _resolve_manifest_engine_family(manifest)

    ensure_dir(args.outdir)

    if engine_family == "arc":
        validate_region_manifest(manifest)
        control_region = _default_session_region(manifest, engine_family)
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
        print(f"[OK] Wrote impacted resources JSON: {impacted_resources_path}")

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
        print(f"[OK] Wrote region execution plan JSON: {execution_plan_path}")

        if args.dry_run:
            print("[INFO] Dry-run enabled: skipping create/execute.")
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
                print(f"[INFO] start_before={start_before_min} minutes: waiting before starting region switch...")
                time.sleep(start_before_min * 60)

            summary = execute_region_plan(
                session=session,
                execution_plan=execution_plan,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )

            if stop_after_min > 0:
                print(f"[INFO] stop_after={stop_after_min} minutes: continuing observability collection...")
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
            print(f"[OK] Wrote result summary JSON: {result_path}")

            print("[RESULT] Summary:")
            print(pretty(summary))

            report_path = generate_report(args.outdir, html_filename=report_filename)
            print(f"[OK] Wrote HTML report: {report_path}")

            if args.upload_artifactory and report_path:
                upload_files_to_artifactory([report_path])

        except botocore.exceptions.ClientError as e:
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
                    print(f"[OK] Wrote HTML report: {rp}")
                    if args.upload_artifactory:
                        upload_files_to_artifactory([rp])
                except Exception:
                    pass

        return 0

    region = _default_session_region(manifest, engine_family)
    if not region and engine_family == "fis":
        raise ValueError("FIS actions require region at the top level or service level.")

    session = boto3.Session(region_name=region)
    try:
        validate_manifest_services(
            manifest,
            session=session,
            region=region,
        )
    except ValidationError as e:
        print(f"[ERROR] {e}")
        return 1
    except ValueError as e:
        print(f"[ERROR] {e}")
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
        print(f"[OK] Wrote custom execution plan JSON: {execution_plan_path}")

        impacted_resources = collect_custom_impacted_resources(execution_plan)
        impacted_resources_path = os.path.join(args.outdir, "impacted_resources.json")
        with open(impacted_resources_path, "w", encoding="utf-8") as f:
            f.write(pretty({"impacted_resources": impacted_resources}))
        print(f"[OK] Wrote impacted resources JSON: {impacted_resources_path}")

        if args.dry_run:
            print("[INFO] Dry-run enabled: skipping create/execute.")
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
                print(f"[INFO] start_before={start_before_min} minutes: waiting before starting custom action...")
                time.sleep(start_before_min * 60)

            summary = execute_custom_plan(
                session=session,
                execution_plan=execution_plan,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )

            if stop_after_min > 0:
                print(f"[INFO] stop_after={stop_after_min} minutes: continuing observability collection...")
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
            print(f"[OK] Wrote result summary JSON: {result_path}")

            print("[RESULT] Summary:")
            print(pretty(summary))

            report_path = generate_report(args.outdir, html_filename=report_filename)
            print(f"[OK] Wrote HTML report: {report_path}")

            if args.upload_artifactory and report_path:
                upload_files_to_artifactory([report_path])

        except botocore.exceptions.ClientError as e:
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
                    print(f"[OK] Wrote HTML report: {rp}")
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
    print(f"[OK] Wrote template payload JSON: {template_json_path}")

    impacted_resources = collect_impacted_resources(
        manifest=manifest,
        session=session,
        region=region,
    )
    impacted_resources_path = os.path.join(args.outdir, "impacted_resources.json")
    with open(impacted_resources_path, "w", encoding="utf-8") as f:
        f.write(pretty({"impacted_resources": impacted_resources}))
    print(f"[OK] Wrote impacted resources JSON: {impacted_resources_path}")

    if args.dry_run:
        print("[INFO] Dry-run enabled: skipping create/execute.")
        return 0

    fis_client = session.client("fis")

    stop_event: Optional[Any] = None
    obs_results: Optional[Dict[str, Any]] = None
    obs_threads: List[Any] = []

    report_path: Optional[str] = None  # NEW

    try:
        template_id = create_template(fis_client, payload)
        print(f"[OK] Created experimentTemplateId: {template_id}")

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
            print(f"[INFO] start_before={start_before_min} minutes: waiting before starting experiment...")
            time.sleep(start_before_min * 60)

        print("[INFO] FIS is running...")
        exp_id = start_experiment(fis_client, template_id)
        print(f"[OK] Started experimentId: {exp_id}")

        final_exp = wait_for_completion(
            fis_client,
            exp_id,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        summary = summarize_experiment(final_exp)

        if stop_after_min > 0:
            print(f"[INFO] stop_after={stop_after_min} minutes: continuing observability collection...")
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
        print(f"[OK] Wrote result summary JSON: {result_path}")

        print("[RESULT] Summary:")
        print(pretty(summary))

        report_path = generate_report(args.outdir, html_filename=report_filename)
        print(f"[OK] Wrote HTML report: {report_path}")

        if args.upload_artifactory and report_path:
            upload_files_to_artifactory([report_path])

    except botocore.exceptions.ClientError as e:
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
                print(f"[OK] Wrote HTML report: {rp}")
                if args.upload_artifactory:
                    upload_files_to_artifactory([rp])
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
