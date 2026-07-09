from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

from providers.azure.artifacts import artifact_entry
from providers.azure.engines.chaos_studio import (
    build_azure_dry_run_rows,
    build_azure_execution_plan,
    collect_azure_impacted_resources,
    execute_chaos_studio_plan,
)
from providers.azure.observability import parse_observability, start_observability_collectors
from providers.azure.runtime import create_runtime_context
from providers.azure.validations import (
    ValidationError,
    manifest_skip_validation_enabled,
    validate_manifest_services,
)
from utility import log_message, pretty


DryRunTextBuilder = Callable[..., str]
DryRunSummaryWriter = Callable[..., str]


def run_azure_manifest(
    *,
    manifest: Dict[str, Any],
    manifest_path: str,
    outdir: str,
    subscription_id: Optional[str],
    timeout_seconds: int,
    poll_seconds: int,
    dry_run: bool,
    skip_validation: bool,
    control_account_id: Optional[str],
    build_dry_run_summary_text: DryRunTextBuilder,
    write_dry_run_summary: DryRunSummaryWriter,
) -> int:
    manifest_skip_validation = manifest_skip_validation_enabled(manifest)
    global_skip_validation = bool(skip_validation or manifest_skip_validation)
    runtime_context = create_runtime_context(
        manifest,
        subscription_id=subscription_id,
        require_credential=(not dry_run) or not global_skip_validation,
    )
    if runtime_context.resource_group:
        log_message("INFO", f"Azure runtime default resource_group={runtime_context.resource_group}.")
    if runtime_context.location:
        log_message("INFO", f"Azure runtime default location={runtime_context.location}.")
    log_message("INFO", f"Azure runtime subscription_id={runtime_context.subscription_id}.")

    if global_skip_validation:
        if skip_validation:
            log_message("WARN", "--skip-validation enabled: skipping Azure pre-execution validation.")
        elif manifest_skip_validation:
            log_message("WARN", "manifest.skip_validation enabled: skipping Azure pre-execution validation.")
    else:
        try:
            validate_manifest_services(
                manifest,
                runtime_context=runtime_context,
            )
        except ValidationError as e:
            print(f"[ERROR] {e}", flush=True)
            return 1

    execution_plan = build_azure_execution_plan(
        manifest,
        subscription_id=subscription_id,
        default_timeout_seconds=timeout_seconds,
        runtime_context=runtime_context,
    )
    plan_name = execution_plan["name"]
    execution_plan_path = os.path.join(outdir, f"azure_execution_plan_{plan_name}.json")
    with open(execution_plan_path, "w", encoding="utf-8") as f:
        f.write(pretty(execution_plan))
    log_message("OK", f"Wrote Azure execution plan JSON: {execution_plan_path}")

    artifact_entries: List[Dict[str, Any]] = [
        artifact_entry("manifest", local_path=os.path.abspath(manifest_path), content_json=manifest),
        artifact_entry("other", local_path=execution_plan_path, content_json=execution_plan),
    ]
    impacted_resources = collect_azure_impacted_resources(execution_plan)
    impacted_resources_content = {"impacted_resources": impacted_resources}
    impacted_resources_path = os.path.join(outdir, "impacted_resources.json")
    with open(impacted_resources_path, "w", encoding="utf-8") as f:
        f.write(pretty(impacted_resources_content))
    log_message("OK", f"Wrote impacted resources JSON: {impacted_resources_path}")
    artifact_entries.append(
        artifact_entry(
            "impacted_resources",
            local_path=impacted_resources_path,
            content_json=impacted_resources_content,
        )
    )

    if dry_run:
        dry_run_rows, dry_run_details = build_azure_dry_run_rows(execution_plan)
        dry_run_text = build_dry_run_summary_text(
            manifest_path=os.path.abspath(manifest_path),
            engine_family=str(execution_plan.get("engineFamily") or "azure"),
            rows=dry_run_rows,
            details=dry_run_details,
            account_id=control_account_id,
        )
        dry_run_summary_path = write_dry_run_summary(
            outdir=outdir,
            name=plan_name,
            text=dry_run_text,
        )
        print(dry_run_text, flush=True)
        log_message("OK", f"Wrote dry-run approval summary: {dry_run_summary_path}")
        artifact_entries.append(artifact_entry("other", local_path=dry_run_summary_path))
        log_message("INFO", "Dry-run enabled: skipping Azure create/execute.")
        return 0

    stop_event: Optional[Any] = None
    obs_results: Optional[Dict[str, Any]] = None
    obs_threads: List[Any] = []

    try:
        stop_event, obs_results, obs_threads = start_observability_collectors(
            manifest=manifest,
            runtime_context=runtime_context,
            outdir=outdir,
            impacted_resources=impacted_resources,
        )
        obs_cfg = parse_observability(manifest)
        start_before_min = int(obs_cfg.get("start_before") or 0)
        stop_after_min = int(obs_cfg.get("stop_after") or 0)

        if start_before_min > 0:
            log_message("INFO", f"start_before={start_before_min} minutes: waiting before starting Azure experiment...")
            time.sleep(start_before_min * 60)

        result = execute_chaos_studio_plan(
            execution_plan=execution_plan,
            runtime_context=runtime_context,
            outdir=outdir,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )

        if stop_after_min > 0:
            log_message("INFO", f"stop_after={stop_after_min} minutes: continuing Azure observability collection...")
            time.sleep(stop_after_min * 60)
    finally:
        if stop_event is not None:
            stop_event.set()
        for thread in obs_threads:
            thread.join(timeout=5)

    if obs_results is not None:
        result["observability"] = obs_results
    result_path = str(result.get("resultPath") or "").strip()
    if result_path:
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(pretty(result))
        artifact_entries.append(artifact_entry("other", local_path=result_path, content_json=result))
    status = str(result.get("status") or "").strip().lower()
    return 0 if status in {"success", "succeeded", "completed"} else 1
