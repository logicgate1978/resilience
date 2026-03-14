import argparse
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3
import botocore

from fis_template_generator import create_template, generate_template_payload
from observability import parse_observability, start_observability_collectors
from resource import collect_impacted_resources
from utility import ensure_dir, load_manifest, pretty, upload_files_to_artifactory

from chart import generate_report  # NEW


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
        print(f"[INFO] Experiment {experiment_id} status={status} elapsed={int(time.time()-start)}s")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default='../manifests/component-1.yml', help="Path to manifest.yml")
    ap.add_argument("--fis-role-arn", default='arn:aws:iam::065476698259:role/service-role/AWSFISIAMRole-1773418476063', help="FIS IAM role ARN (required unless --dry-run)")
    ap.add_argument("--outdir", default="fis_out", help="Output directory for template/results JSON/CSVs")
    ap.add_argument("--dry-run", action="store_true", help="Generate JSON only; do not create or execute")
    ap.add_argument("--poll-seconds", type=int, default=10, help="Polling interval while waiting for experiment")
    ap.add_argument("--timeout-seconds", type=int, default=3600, help="Timeout per experiment in seconds")
    ap.add_argument("--upload-artifactory", default=True, action="store_true", help="Upload generated HTML report to Artifactory")
    args = ap.parse_args()

    manifest = load_manifest(args.manifest)
    region = manifest.get("region")
    if not region:
        raise ValueError("manifest.yml must include top-level region")

    ensure_dir(args.outdir)

    if args.dry_run:
        fis_role_arn = args.fis_role_arn or "REPLACE_ME"
    else:
        if not args.fis_role_arn:
            raise ValueError("--fis-role-arn is required unless --dry-run")
        fis_role_arn = args.fis_role_arn

    payload = generate_template_payload(manifest, fis_role_arn=fis_role_arn, selection_mode="ALL")

    template_name = payload["description"]
    report_date = datetime.utcnow().strftime("%Y%m%d")
    report_filename = f"report_{template_name}_{report_date}.html"

    template_json_path = os.path.join(args.outdir, f"template_payload_{template_name}.json")
    with open(template_json_path, "w", encoding="utf-8") as f:
        f.write(pretty(payload))
    print(f"[OK] Wrote template payload JSON: {template_json_path}")

    session = boto3.Session(region_name=region)

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