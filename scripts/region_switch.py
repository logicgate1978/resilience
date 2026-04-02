import concurrent.futures
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from kubernetes.client.exceptions import ApiException

from component_actions.dns import DNSAction
from component_actions.k8s_auth import create_apps_v1_api
from utility import utc_ts

from resource import discover_rds_global_clusters


REGION_ACTION_CONFIG: Dict[str, Dict[str, Any]] = {
    "failover-global-db": {
        "arc": {
            "behavior": "failover",
            "mode": "ungraceful",
            "description": "Fail over Aurora global database with ARC Region switch",
        },
        "non_arc": {
            "engine": "sdk",
            "sdk_api": "failover_global_cluster",
            "description": "Fail over Aurora global database with the RDS SDK",
        },
    },
    "switchover-global-db": {
        "arc": {
            "behavior": "switchoverOnly",
            "mode": "graceful",
            "description": "Switchover Aurora global database with ARC Region switch",
        },
        "non_arc": {
            "engine": "sdk",
            "sdk_api": "switchover_global_cluster",
            "description": "Switchover Aurora global database with the RDS SDK",
        },
    },
}


def validate_region_manifest(manifest: Dict[str, Any]) -> None:
    rtype = (manifest.get("resilience_test_type") or "").strip().lower()
    if rtype != "region":
        raise ValueError("Region switch flow requires resilience_test_type to be 'region'.")

    primary_region = manifest.get("primary_region")
    secondary_region = manifest.get("secondary_region")
    if not primary_region or not isinstance(primary_region, str):
        raise ValueError("region resilience tests require top-level primary_region.")
    if not secondary_region or not isinstance(secondary_region, str):
        raise ValueError("region resilience tests require top-level secondary_region.")
    if primary_region == secondary_region:
        raise ValueError("primary_region and secondary_region must be different.")

    services = manifest.get("services")
    if not isinstance(services, list) or not services:
        raise ValueError("Top-level 'services' must be a non-empty list.")

    for i, svc in enumerate(services):
        if not isinstance(svc, dict):
            raise ValueError(f"services[{i}] must be an object.")
        svc["__primary_region__"] = primary_region
        svc["__secondary_region__"] = secondary_region
        name = (svc.get("name") or "").strip().lower()
        action = (svc.get("action") or "").strip().lower()
        if name == "rds" and action in REGION_ACTION_CONFIG:
            _validate_region_rds_service(svc, i)
            continue
        if name == "eks" and action == "scale-deployment":
            _validate_region_eks_service(svc, i)
            continue
        if name == "dns" and action in ("set-value", "set-weight"):
            _validate_region_dns_service(svc, i)
            continue
        raise ValueError(f"Unsupported region resilience service action: {name}:{action}")


def resolve_region_targets(manifest: Dict[str, Any], session) -> List[Dict[str, Any]]:
    validate_region_manifest(manifest)
    resolved: List[Dict[str, Any]] = []
    rds_targets = discover_rds_global_clusters(manifest=manifest, session=session)
    rds_iter = iter(rds_targets)
    dns_handler = DNSAction()
    primary_region = manifest["primary_region"]
    secondary_region = manifest["secondary_region"]

    for index, svc in enumerate(manifest.get("services") or [], start=1):
        if not isinstance(svc, dict):
            continue
        name = (svc.get("name") or "").strip().lower()
        action = (svc.get("action") or "").strip().lower()
        if name == "rds" and action in REGION_ACTION_CONFIG:
            resolved.append(next(rds_iter))
        elif name == "eks" and action == "scale-deployment":
            target = svc["target"]
            params = svc["parameters"]
            actual_region = _resolve_region_eks_target_region(
                str(target.get("region") or "").strip(),
                primary_region=primary_region,
                secondary_region=secondary_region,
            )
            resolved.append(
                {
                    "service": "eks:scale-deployment",
                    "action": "scale-deployment",
                    "execution_region": actual_region,
                    "cluster_identifier": str(target.get("cluster_identifier") or "").strip(),
                    "namespace": str(target.get("namespace") or "").strip(),
                    "deployment_name": str(target.get("deployment_name") or "").strip(),
                    "replicas": int(params.get("replicas")),
                    "wait_for_ready": _optional_bool(params.get("wait_for_ready"), True),
                    "timeout_seconds": _optional_int(params.get("timeout_seconds"), 600),
                }
            )
        elif name == "dns" and action in ("set-value", "set-weight"):
            plan_item = dns_handler.build_plan_item(
                manifest=manifest,
                svc=svc,
                session=session,
                region=primary_region,
                index=index,
                default_timeout_seconds=600,
            )
            resolved.append(
                {
                    "service": f"dns:{action}",
                    "action": action,
                    "plan_item": plan_item,
                }
            )
    return resolved


def build_execution_plan(
    manifest: Dict[str, Any],
    execution_role_arn: str,
    resolved_targets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    validate_region_manifest(manifest)

    plan_name = f"region-run-{utc_ts()}".lower()
    items: List[Dict[str, Any]] = []
    services = [svc for svc in (manifest.get("services") or []) if isinstance(svc, dict)]
    for idx, target in enumerate(resolved_targets, start=1):
        if str(target.get("service") or "").strip().lower() == "eks:scale-deployment":
            item = _build_region_eks_execution_item(target, idx)
        elif str(target.get("service") or "").strip().lower().startswith("dns:"):
            item = _build_region_dns_execution_item(target, idx)
        else:
            use_arc = bool(target.get("use_arc", True))
            if use_arc:
                if not execution_role_arn:
                    raise ValueError("ARC region switch requires an execution role ARN when use_arc=true.")
                item = _build_arc_execution_item(manifest, target, execution_role_arn, idx)
            else:
                item = _build_non_arc_execution_item(manifest, target, idx)
        items.append(item)

    start_after_map = _resolve_region_start_after(services, [str(item["name"]) for item in items])
    for index, item in enumerate(items, start=1):
        item["startAfter"] = start_after_map.get(index) or []

    if not items:
        raise ValueError("No region execution items were resolved from the manifest.")

    return {
        "name": plan_name,
        "description": "Region resilience execution plan",
        "resolvedTargets": resolved_targets,
        "items": items,
    }


def execute_region_plan(
    session,
    execution_plan: Dict[str, Any],
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    items = list(execution_plan.get("items") or [])
    items_by_name = {str(item["name"]): item for item in items}
    pending = {str(item["name"]) for item in items}
    running: Dict[concurrent.futures.Future, str] = {}
    results: Dict[str, Dict[str, Any]] = {}

    def _execute(item: Dict[str, Any]) -> Dict[str, Any]:
        if item["engine"] == "arc":
            return _execute_arc_item(
                session=session,
                item=item,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
        if item["engine"] == "sdk":
            return _execute_sdk_item(
                session=session,
                item=item,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
            )
        if item["engine"] == "custom":
            if str(item.get("service") or "").strip().lower().startswith("eks:"):
                return _execute_region_eks_item(
                    session=session,
                    item=item,
                    poll_seconds=poll_seconds,
                    timeout_seconds=timeout_seconds,
                )
            if str(item.get("service") or "").strip().lower().startswith("dns:"):
                return _execute_region_dns_item(
                    session=session,
                    item=item,
                    poll_seconds=poll_seconds,
                    timeout_seconds=timeout_seconds,
                )
            raise ValueError(f"Unsupported custom region execution service: {item.get('service')}")
        raise ValueError(f"Unsupported region execution engine: {item['engine']}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(items))) as executor:
        while pending or running:
            made_progress = False

            for item_name in list(pending):
                item = items_by_name[item_name]
                deps = list(item.get("startAfter") or [])
                if not all(dep in results for dep in deps):
                    continue

                failed_deps = [dep for dep in deps if results[dep].get("status") != "completed"]
                if failed_deps:
                    print(
                        f"[INFO] Skipping region action {item.get('service')} "
                        f"because dependency action(s) did not complete successfully: {', '.join(failed_deps)}"
                    )
                    results[item_name] = {
                        "name": item_name,
                        "engine": item.get("engine"),
                        "status": "skipped",
                        "reason": f"Skipped because dependency action(s) did not complete successfully: {', '.join(failed_deps)}",
                        "startTime": None,
                        "endTime": None,
                        "details": {
                            "target": item.get("target"),
                            "parameters": item.get("parameters"),
                            "startAfter": deps,
                        },
                    }
                    pending.remove(item_name)
                    made_progress = True
                    continue

                print(f"[INFO] Starting region action: {item.get('service')} ({item_name})")
                running[executor.submit(_execute, item)] = item_name
                pending.remove(item_name)
                made_progress = True

            if running:
                done, _ = concurrent.futures.wait(
                    running.keys(),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    item_name = running.pop(future)
                    try:
                        results[item_name] = future.result()
                    except Exception as e:
                        results[item_name] = {
                            "name": item_name,
                            "engine": items_by_name[item_name].get("engine"),
                            "status": "failed",
                            "reason": f"Unhandled region action error: {e}",
                            "startTime": None,
                            "endTime": None,
                            "details": {
                                "target": items_by_name[item_name].get("target"),
                                "parameters": items_by_name[item_name].get("parameters"),
                                "startAfter": items_by_name[item_name].get("startAfter") or [],
                            },
                        }
                    result = results[item_name]
                    print(
                        f"[INFO] Finished region action: {items_by_name[item_name].get('service')} "
                        f"({item_name}) status={result.get('status')}"
                    )
                    made_progress = True

            if not made_progress and pending:
                blocked = sorted(pending)
                raise ValueError(
                    "Region execution plan contains unresolved or cyclic start_after dependencies: "
                    + ", ".join(blocked)
                )

    item_summaries: List[Dict[str, Any]] = [results[str(item["name"])] for item in items]

    overall_status = "completed"
    if any(s["status"] not in ("completed", "completedWithExceptions") for s in item_summaries):
        overall_status = "failed"
    elif any(s["status"] == "completedWithExceptions" for s in item_summaries):
        overall_status = "completedWithExceptions"

    start_times = [s["startTime"] for s in item_summaries if s.get("startTime")]
    end_times = [s["endTime"] for s in item_summaries if s.get("endTime")]

    summary = {
        "experimentId": execution_plan["name"],
        "experimentTemplateId": execution_plan["name"],
        "status": overall_status,
        "reason": "region resilience execution",
        "startTime": min(start_times) if start_times else None,
        "endTime": max(end_times) if end_times else None,
        "actions": {},
        "regionSwitch": {
            "executionPlan": execution_plan,
            "items": item_summaries,
        },
    }

    for item_summary in item_summaries:
        summary["actions"][item_summary["name"]] = {
            "status": item_summary["status"],
            "reason": item_summary["reason"],
            "startTime": item_summary["startTime"],
            "endTime": item_summary["endTime"],
        }

    return summary


def _parse_start_after_refs(value: Any, field_name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        refs = [value]
    elif isinstance(value, list):
        refs = value
    else:
        raise ValueError(f"{field_name} must be a string or list of strings if provided.")

    out: List[str] = []
    for raw in refs:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        out.append(text)
    return out


def _build_service_reference_map(services: List[Dict[str, Any]], item_names: List[str]) -> Dict[str, Tuple[int, str]]:
    totals: Dict[str, int] = {}
    normalized: List[Tuple[str, str]] = []
    for svc in services:
        service_name = str(svc.get("name") or "").strip().lower()
        action = str(svc.get("action") or "").strip().lower()
        key = f"{service_name}:{action}"
        normalized.append((service_name, action))
        totals[key] = totals.get(key, 0) + 1

    refs: Dict[str, Tuple[int, str]] = {}
    occurrences: Dict[str, int] = {}
    for index, ((service_name, action), item_name) in enumerate(zip(normalized, item_names), start=1):
        key = f"{service_name}:{action}"
        occurrences[key] = occurrences.get(key, 0) + 1
        ordinal = occurrences[key]
        refs[f"{key}#{ordinal}"] = (index, item_name)
        if totals[key] == 1:
            refs[key] = (index, item_name)
    return refs


def _resolve_region_start_after(services: List[Dict[str, Any]], item_names: List[str]) -> Dict[int, List[str]]:
    ref_map = _build_service_reference_map(services, item_names)
    resolved: Dict[int, List[str]] = {}
    for index, svc in enumerate(services, start=1):
        dependencies: List[str] = []
        seen = set()
        refs = _parse_start_after_refs(svc.get("start_after"), f"services[{index - 1}].start_after")
        for ref in refs:
            target = ref_map.get(ref)
            if target is None:
                raise ValueError(
                    f"services[{index - 1}].start_after references unknown action '{ref}'. "
                    "Use '<service>:<action>' for unique actions or '<service>:<action>#<n>' when duplicates exist."
                )

            target_index, target_item_name = target
            if target_index >= index:
                raise ValueError(
                    f"services[{index - 1}].start_after reference '{ref}' must point to an earlier service action."
                )

            if target_item_name not in seen:
                dependencies.append(target_item_name)
                seen.add(target_item_name)
        resolved[index] = dependencies
    return resolved


def _build_arc_execution_item(
    manifest: Dict[str, Any],
    target: Dict[str, Any],
    execution_role_arn: str,
    index: int,
) -> Dict[str, Any]:
    primary_region = manifest["primary_region"]
    secondary_region = manifest["secondary_region"]
    action = str(target["action"])
    action_cfg = REGION_ACTION_CONFIG[action]["arc"]
    from_side = str(target["from"])
    target_region = _target_region(manifest, from_side)

    plan_name = f"rs-rds-{index}-{utc_ts()}".lower()
    plan_description = (
        f"{action} from {from_side} using ARC for Aurora global database "
        f"{target['global_cluster_identifier']}"
    )
    step_name = f"rds-{action}-{index}".replace("_", "-")

    global_aurora_config: Dict[str, Any] = {
        "behavior": action_cfg["behavior"],
        "globalClusterIdentifier": target["global_cluster_identifier"],
        "databaseClusterArns": [
            target["member_cluster_arns"][primary_region],
            target["member_cluster_arns"][secondary_region],
        ],
    }
    if action_cfg["mode"] == "ungraceful":
        global_aurora_config["ungraceful"] = {"ungraceful": "failover"}

    return {
        "name": step_name,
        "engine": "arc",
        "service": target["service"],
        "action": action,
        "use_arc": True,
        "planControlRegion": primary_region,
        "target": target,
        "payload": {
            "name": plan_name,
            "description": plan_description,
            "executionRole": execution_role_arn,
            "regions": [primary_region, secondary_region],
            "recoveryApproach": "activePassive",
            "primaryRegion": primary_region,
            "workflows": [
                {
                    "workflowTargetAction": "activate",
                    "workflowTargetRegion": target_region,
                    "workflowDescription": plan_description,
                    "steps": [
                        {
                            "name": step_name,
                            "description": action_cfg["description"],
                            "executionBlockType": "AuroraGlobalDatabase",
                            "executionBlockConfiguration": {
                                "globalAuroraConfig": global_aurora_config,
                            },
                        }
                    ],
                }
            ],
            "tags": {
                "managed-by": "fis.py",
                "resilience-test-type": "region",
                "service": "rds",
                "action": action,
            },
        },
        "request": {
            "targetRegion": target_region,
            "action": "activate",
            "mode": action_cfg["mode"],
            "comment": plan_description,
            "latestVersion": "true",
        },
    }


def _build_non_arc_execution_item(
    manifest: Dict[str, Any],
    target: Dict[str, Any],
    index: int,
) -> Dict[str, Any]:
    action = str(target["action"])
    action_cfg = REGION_ACTION_CONFIG[action]["non_arc"]
    from_side = str(target["from"])
    source_region = _source_region(manifest, from_side)
    target_region = _target_region(manifest, from_side)
    target_cluster_arn = target["member_cluster_arns"][target_region]

    if action == "failover-global-db":
        client_region = target_region
        params = {
            "GlobalClusterIdentifier": target["global_cluster_identifier"],
            "TargetDbClusterIdentifier": target_cluster_arn,
            "AllowDataLoss": True,
        }
    elif action == "switchover-global-db":
        client_region = source_region
        params = {
            "GlobalClusterIdentifier": target["global_cluster_identifier"],
            "TargetDbClusterIdentifier": target_cluster_arn,
        }
    else:
        raise ValueError(f"Unsupported non-ARC region action: {action}")

    return {
        "name": f"rds-{action}-{index}".replace("_", "-"),
        "engine": action_cfg["engine"],
        "service": target["service"],
        "action": action,
        "use_arc": False,
        "clientRegion": client_region,
        "targetRegion": target_region,
        "sourceRegion": source_region,
        "request": {
            "sdkApi": action_cfg["sdk_api"],
            "params": params,
            "description": action_cfg["description"],
        },
        "target": target,
    }


def _build_region_eks_execution_item(
    target: Dict[str, Any],
    index: int,
) -> Dict[str, Any]:
    return {
        "name": f"eks-scale-deployment-{index}",
        "engine": "custom",
        "service": target["service"],
        "action": "scale-deployment",
        "region": target["execution_region"],
        "target": {
            "clusterIdentifier": target["cluster_identifier"],
            "namespace": target["namespace"],
            "deploymentName": target["deployment_name"],
        },
        "parameters": {
            "replicas": target["replicas"],
            "waitForReady": target["wait_for_ready"],
            "timeoutSeconds": target["timeout_seconds"],
        },
    }


def _build_region_dns_execution_item(
    target: Dict[str, Any],
    index: int,
) -> Dict[str, Any]:
    _ = index
    plan_item = dict(target["plan_item"])
    params = dict(plan_item.get("parameters") or {})
    params.pop("timeoutSeconds", None)
    plan_item["parameters"] = params
    plan_item["region"] = None
    return plan_item


def _execute_arc_item(
    session,
    item: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    client = session.client("arc-region-switch", region_name=item["planControlRegion"])
    start_time = datetime.now(timezone.utc).isoformat()

    print(f"[INFO] ARC is running: {item['service']}")
    plan = client.create_plan(**item["payload"])["plan"]
    plan_arn = plan["arn"]
    print(f"[OK] Created ARC Region switch plan: {plan_arn}")

    execution = client.start_plan_execution(planArn=plan_arn, **item["request"])
    execution_id = execution["executionId"]
    print(f"[OK] Started ARC Region switch executionId: {execution_id}")

    final_execution = _wait_for_arc_execution(
        client=client,
        plan_arn=plan_arn,
        execution_id=execution_id,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )
    final_state = _wait_for_global_db_ready(
        session=session,
        target=item["target"],
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )

    return {
        "name": item["name"],
        "engine": "arc",
        "status": final_execution.get("executionState"),
        "reason": final_execution.get("comment"),
        "startTime": final_execution.get("startTime") or start_time,
        "endTime": datetime.now(timezone.utc).isoformat(),
        "details": {
            "planArn": plan_arn,
            "executionId": execution_id,
            "payload": item["payload"],
            "request": item["request"],
            "rawExecution": final_execution,
            "finalGlobalDbState": final_state,
        },
    }


def _execute_sdk_item(
    session,
    item: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    client = session.client("rds", region_name=item["clientRegion"])
    start_time = datetime.now(timezone.utc).isoformat()
    request = item["request"]

    print(
        f"[INFO] Starting non-ARC region action {item['action']} via {request['sdkApi']} "
        f"in region {item['clientRegion']}"
    )

    sdk_api = getattr(client, request["sdkApi"])
    response = sdk_api(**request["params"])
    final_state = _wait_for_global_db_ready(
        session=session,
        target=item["target"],
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )

    return {
        "name": item["name"],
        "engine": "sdk",
        "status": "completed",
        "reason": f"{request['sdkApi']} in {item['clientRegion']}",
        "startTime": start_time,
        "endTime": datetime.now(timezone.utc).isoformat(),
        "details": {
            "request": request,
            "initialResponse": response,
            "finalGlobalClusterState": final_state,
        },
    }


def _execute_region_eks_item(
    session,
    item: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    start_time = datetime.now(timezone.utc).isoformat()
    target = item["target"]
    params = item["parameters"]
    cluster_identifier = target["clusterIdentifier"]
    namespace = target["namespace"]
    deployment_name = target["deploymentName"]
    desired_replicas = int(params["replicas"])
    wait_for_ready = bool(params["waitForReady"])
    effective_timeout_seconds = int(params.get("timeoutSeconds") or timeout_seconds)

    print(
        f"[INFO] Starting region custom action {item['service']} in region {item['region']} "
        f"for deployment {namespace}/{deployment_name}"
    )

    try:
        api = create_apps_v1_api(session, item["region"], cluster_identifier)
        deployment = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    except ApiException as e:
        ended_at = datetime.now(timezone.utc).isoformat()
        return {
            "name": item["name"],
            "engine": "custom",
            "status": "failed",
            "reason": f"Kubernetes API error while reading deployment: {e}",
            "startTime": start_time,
            "endTime": ended_at,
            "details": {"target": target, "parameters": params},
        }
    except Exception as e:
        ended_at = datetime.now(timezone.utc).isoformat()
        return {
            "name": item["name"],
            "engine": "custom",
            "status": "failed",
            "reason": f"Failed to initialize Kubernetes API access for cluster {cluster_identifier}: {e}",
            "startTime": start_time,
            "endTime": ended_at,
            "details": {"target": target, "parameters": params},
        }

    original_replicas = int(deployment.spec.replicas or 0)

    try:
        api.patch_namespaced_deployment_scale(
            name=deployment_name,
            namespace=namespace,
            body={"spec": {"replicas": desired_replicas}},
        )
    except ApiException as e:
        ended_at = datetime.now(timezone.utc).isoformat()
        return {
            "name": item["name"],
            "engine": "custom",
            "status": "failed",
            "reason": f"Kubernetes API error while scaling deployment: {e}",
            "startTime": start_time,
            "endTime": ended_at,
            "details": {
                "target": target,
                "parameters": params,
                "originalReplicas": original_replicas,
            },
        }

    last_snapshot: Dict[str, Any] = {}
    if wait_for_ready:
        deadline = time.time() + effective_timeout_seconds
        while True:
            try:
                current = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
            except ApiException as e:
                ended_at = datetime.now(timezone.utc).isoformat()
                return {
                    "name": item["name"],
                    "engine": "custom",
                    "status": "failed",
                    "reason": f"Kubernetes API error while waiting for deployment readiness: {e}",
                    "startTime": start_time,
                    "endTime": ended_at,
                    "details": {
                        "target": target,
                        "parameters": params,
                        "originalReplicas": original_replicas,
                        "lastObservedStatus": last_snapshot,
                    },
                }

            last_snapshot = _deployment_snapshot(current)
            if _is_region_deployment_ready(current, desired_replicas):
                break

            if time.time() > deadline:
                ended_at = datetime.now(timezone.utc).isoformat()
                return {
                    "name": item["name"],
                    "engine": "custom",
                    "status": "failed",
                    "reason": (
                        f"Timed out waiting for deployment {namespace}/{deployment_name} "
                        f"in region {item['region']} to reach {desired_replicas} replica(s)."
                    ),
                    "startTime": start_time,
                    "endTime": ended_at,
                    "details": {
                        "target": target,
                        "parameters": params,
                        "originalReplicas": original_replicas,
                        "lastObservedStatus": last_snapshot,
                    },
                }
            time.sleep(max(1, poll_seconds))

    try:
        final_deployment = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        final_status = _deployment_snapshot(final_deployment)
    except Exception as e:
        final_status = {"error": f"Unable to read final deployment state: {e}"}

    return {
        "name": item["name"],
        "engine": "custom",
        "status": "completed",
        "reason": None,
        "startTime": start_time,
        "endTime": datetime.now(timezone.utc).isoformat(),
        "details": {
            "target": target,
            "parameters": params,
            "originalReplicas": original_replicas,
            "finalStatus": final_status,
        },
    }


def _execute_region_dns_item(
    session,
    item: Dict[str, Any],
    poll_seconds: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    print(f"[INFO] Starting region custom action {item['service']}")
    return DNSAction().execute_item(
        session=session,
        item=item,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )


def _wait_for_arc_execution(
    client,
    plan_arn: str,
    execution_id: str,
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    terminal = {
        "completed",
        "completedWithExceptions",
        "canceled",
        "planExecutionTimedOut",
        "failed",
    }
    start = time.time()
    while True:
        execution = client.get_plan_execution(planArn=plan_arn, executionId=execution_id)
        status = execution.get("executionState", "unknown")
        if status in terminal:
            return execution
        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                f"Region switch execution {execution_id} timed out after {timeout_seconds}s (last={status})."
            )
        print(
            f"[INFO] ARC is running: executionId={execution_id} status={status} "
            f"elapsed={int(time.time() - start)}s"
        )
        time.sleep(poll_seconds)


def _wait_for_global_cluster_role(
    client,
    global_cluster_identifier: str,
    target_cluster_arn: str,
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    start = time.time()
    while True:
        response = client.describe_global_clusters(GlobalClusterIdentifier=global_cluster_identifier)
        global_clusters = response.get("GlobalClusters") or []
        if global_clusters:
            global_cluster = global_clusters[0]
            status = str(global_cluster.get("Status") or "").strip().lower()
            members = global_cluster.get("GlobalClusterMembers") or []
            target_is_writer = any(
                m.get("DBClusterArn") == target_cluster_arn and bool(m.get("IsWriter"))
                for m in members
            )
            if status == "available" and target_is_writer:
                return global_cluster

        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                f"Global cluster {global_cluster_identifier} did not promote target {target_cluster_arn} "
                f"within {timeout_seconds}s."
            )
        time.sleep(poll_seconds)


def _wait_for_global_db_ready(
    session,
    target: Dict[str, Any],
    poll_seconds: int = 10,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    member_cluster_arns = target["member_cluster_arns"]
    primary_region = target["primary_region"]
    secondary_region = target["secondary_region"]
    target_region = secondary_region if str(target["from"]) == "primary" else primary_region
    target_cluster_arn = member_cluster_arns[target_region]
    control_client = session.client("rds", region_name=target_region)

    start = time.time()
    while True:
        global_cluster = _wait_for_global_cluster_role_once(
            client=control_client,
            global_cluster_identifier=target["global_cluster_identifier"],
            target_cluster_arn=target_cluster_arn,
        )

        cluster_states: Dict[str, Dict[str, Any]] = {}
        member_states: Dict[str, Dict[str, Any]] = {}
        all_available = True
        all_synchronized = True

        if global_cluster:
            for member in global_cluster.get("GlobalClusterMembers") or []:
                cluster_arn = str(member.get("DBClusterArn") or "")
                if cluster_arn in member_cluster_arns.values():
                    member_states[cluster_arn] = member

        for region, cluster_arn in member_cluster_arns.items():
            rds_client = session.client("rds", region_name=region)
            cluster = _describe_db_cluster_by_arn(rds_client, cluster_arn)
            cluster_states[region] = cluster

            cluster_status = str(cluster.get("Status") or "").strip().lower()
            if cluster_status != "available":
                all_available = False

            member = member_states.get(cluster_arn) or {}
            is_writer = bool(member.get("IsWriter"))
            sync_status = str(member.get("SynchronizationStatus") or "").strip().lower()

            if not is_writer and sync_status != "connected":
                all_synchronized = False
            elif is_writer and sync_status not in ("", "connected"):
                all_synchronized = False

        if global_cluster and all_available and all_synchronized:
            return {
                "globalCluster": global_cluster,
                "clusters": cluster_states,
                "members": member_states,
            }

        if time.time() - start > timeout_seconds:
            raise TimeoutError(
                "Aurora global database did not reach a fully synchronized and available state "
                f"in both Regions within {timeout_seconds}s."
            )

        time.sleep(poll_seconds)


def _wait_for_global_cluster_role_once(
    client,
    global_cluster_identifier: str,
    target_cluster_arn: str,
) -> Dict[str, Any]:
    response = client.describe_global_clusters(GlobalClusterIdentifier=global_cluster_identifier)
    global_clusters = response.get("GlobalClusters") or []
    if not global_clusters:
        return {}

    global_cluster = global_clusters[0]
    status = str(global_cluster.get("Status") or "").strip().lower()
    failover_state = global_cluster.get("FailoverState") or {}
    members = global_cluster.get("GlobalClusterMembers") or []
    target_is_writer = any(
        m.get("DBClusterArn") == target_cluster_arn and bool(m.get("IsWriter"))
        for m in members
    )
    in_progress = bool(failover_state.get("Status"))
    if status == "available" and target_is_writer and not in_progress:
        return global_cluster
    return {}


def _describe_db_cluster_by_arn(rds_client, cluster_arn: str) -> Dict[str, Any]:
    cluster_identifier = _cluster_identifier_from_arn(cluster_arn)
    response = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_identifier)
    clusters = response.get("DBClusters") or []
    if not clusters:
        raise ValueError(f"DB cluster not found for ARN: {cluster_arn}")
    return clusters[0]


def _cluster_identifier_from_arn(cluster_arn: str) -> str:
    marker = ":cluster:"
    if marker not in cluster_arn:
        raise ValueError(f"Invalid DB cluster ARN: {cluster_arn}")
    return cluster_arn.split(marker, 1)[1]




def _source_region(manifest: Dict[str, Any], from_side: str) -> str:
    return manifest["primary_region"] if from_side == "primary" else manifest["secondary_region"]


def _target_region(manifest: Dict[str, Any], from_side: str) -> str:
    return manifest["secondary_region"] if from_side == "primary" else manifest["primary_region"]


def _validate_region_rds_service(svc: Dict[str, Any], index: int) -> None:
    from_side = (svc.get("from") or "").strip().lower()
    use_arc = svc.get("use_arc", True)
    if from_side not in ("primary", "secondary"):
        raise ValueError(f"services[{index}].from must be 'primary' or 'secondary'.")
    if not isinstance(svc.get("tags"), str) or not str(svc.get("tags") or "").strip():
        raise ValueError(f"services[{index}].tags is required for Aurora global database discovery.")
    if not isinstance(use_arc, bool):
        raise ValueError(f"services[{index}].use_arc must be true or false when provided.")


def _validate_region_eks_service(svc: Dict[str, Any], index: int) -> None:
    target = svc.get("target")
    params = svc.get("parameters")
    if not isinstance(target, dict):
        raise ValueError(f"services[{index}].target must be an object for eks:scale-deployment.")
    if not isinstance(params, dict):
        raise ValueError(f"services[{index}].parameters must be an object for eks:scale-deployment.")

    region_value = str(target.get("region") or "").strip()
    if not region_value:
        raise ValueError(f"services[{index}].target.region is required for eks:scale-deployment.")

    primary_region = str(svc.get("__primary_region__") or "").strip()
    secondary_region = str(svc.get("__secondary_region__") or "").strip()
    try:
        _resolve_region_eks_target_region(
            region_value,
            primary_region=primary_region,
            secondary_region=secondary_region,
        )
    except ValueError as e:
        raise ValueError(f"services[{index}].target.region {e}")

    for field in ("cluster_identifier", "namespace", "deployment_name"):
        value = str(target.get(field) or "").strip()
        if not value:
            raise ValueError(f"services[{index}].target.{field} is required for eks:scale-deployment.")

    try:
        replicas = int(params.get("replicas"))
    except Exception:
        raise ValueError(f"services[{index}].parameters.replicas must be an integer.")
    if replicas < 0:
        raise ValueError(f"services[{index}].parameters.replicas must be non-negative.")


def _validate_region_dns_service(svc: Dict[str, Any], index: int) -> None:
    target = svc.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"services[{index}].target must be an object for dns actions.")

    for field in ("hosted_zone", "record_name", "record_type"):
        value = str(target.get(field) or "").strip()
        if not value:
            raise ValueError(f"services[{index}].target.{field} is required for dns actions.")

    value = str(svc.get("value") or "").strip()
    if not value:
        raise ValueError(f"services[{index}].value is required for dns actions.")


def _resolve_region_eks_target_region(
    region_value: str,
    *,
    primary_region: str,
    secondary_region: str,
) -> str:
    text = str(region_value or "").strip()
    lowered = text.lower()
    if lowered == "primary":
        return primary_region
    if lowered == "secondary":
        return secondary_region
    if text in (primary_region, secondary_region):
        return text
    raise ValueError(
        f"must be '{primary_region}' or '{secondary_region}'. "
        "The legacy aliases 'primary' and 'secondary' are also accepted."
    )


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


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _is_region_deployment_ready(deployment, desired_replicas: int) -> bool:
    status = deployment.status
    metadata = deployment.metadata
    spec = deployment.spec

    observed_generation = int(status.observed_generation or 0)
    generation = int(metadata.generation or 0)
    spec_replicas = int(spec.replicas or 0)
    replicas = int(status.replicas or 0)
    updated_replicas = int(status.updated_replicas or 0)
    ready_replicas = int(status.ready_replicas or 0)
    available_replicas = int(status.available_replicas or 0)

    if observed_generation < generation:
        return False
    if spec_replicas != desired_replicas:
        return False
    if desired_replicas == 0:
        return replicas == 0 and ready_replicas == 0 and available_replicas == 0
    return (
        replicas == desired_replicas
        and updated_replicas == desired_replicas
        and ready_replicas == desired_replicas
        and available_replicas == desired_replicas
    )


def _deployment_snapshot(deployment) -> Dict[str, Any]:
    status = deployment.status
    metadata = deployment.metadata
    spec = deployment.spec
    return {
        "generation": int(metadata.generation or 0),
        "observedGeneration": int(status.observed_generation or 0),
        "specReplicas": int(spec.replicas or 0),
        "replicas": int(status.replicas or 0),
        "updatedReplicas": int(status.updated_replicas or 0),
        "readyReplicas": int(status.ready_replicas or 0),
        "availableReplicas": int(status.available_replicas or 0),
    }
