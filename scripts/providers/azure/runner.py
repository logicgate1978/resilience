from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from providers.azure.artifacts import artifact_entry
from providers.azure.engines.chaos_studio import (
    build_azure_dry_run_rows,
    build_azure_execution_plan,
    collect_azure_impacted_resources,
)
from providers.azure.runtime import create_runtime_context
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
    dry_run: bool,
    control_account_id: Optional[str],
    build_dry_run_summary_text: DryRunTextBuilder,
    write_dry_run_summary: DryRunSummaryWriter,
) -> int:
    runtime_context = create_runtime_context(
        manifest,
        subscription_id=subscription_id,
        require_credential=False,
    )
    if runtime_context.resource_group:
        log_message("INFO", f"Azure runtime default resource_group={runtime_context.resource_group}.")
    if runtime_context.location:
        log_message("INFO", f"Azure runtime default location={runtime_context.location}.")
    log_message("INFO", f"Azure runtime subscription_id={runtime_context.subscription_id}.")

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

    raise ValueError(
        "Azure execution is not enabled yet. Run with --dry-run to generate the Azure Chaos Studio approval plan."
    )
