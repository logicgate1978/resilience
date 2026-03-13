import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import botocore
import urllib.error
import urllib.request

from utility import append_csv_row, get_account_id, parse_tags, sanitize_filename


SERVICE_CLOUDWATCH_METRICS_MAP: Dict[str, Dict[str, Any]] = {
    "asg": {
        "namespace": "AWS/AutoScaling",
        "dimension_name": "AutoScalingGroupName",
        "metrics": [
            "GroupDesiredCapacity",
            "GroupInServiceInstances",
            "GroupPendingInstances",
            "GroupTerminatingInstances",
        ],
    },
    "rds:db": {
        "namespace": "AWS/RDS",
        "dimension_name": "DBInstanceIdentifier",
        "metrics": [
            "CPUUtilization",
            "DatabaseConnections",
            "FreeableMemory",
            "FreeStorageSpace",
        ],
    },
    "rds:cluster": {
        "namespace": "AWS/RDS",
        "dimension_name": "DBClusterIdentifier",
        "metrics": [
            "DatabaseConnections",
            "VolumeReadIOPs",
            "VolumeWriteIOPs",
            "AuroraReplicaLagMaximum",
        ],
    },
}


def parse_observability(manifest: Dict[str, Any]) -> Dict[str, Any]:
    obs = manifest.get("observability") or {}
    if not isinstance(obs, dict):
        return {}

    out: Dict[str, Any] = {}

    # start_before / stop_after (minutes)
    start_before = obs.get("start_before")
    stop_after = obs.get("stop_after")
    if start_before is not None:
        out["start_before"] = int(start_before)
    if stop_after is not None:
        out["stop_after"] = int(stop_after)

    hc = obs.get("health_check")
    if isinstance(hc, dict):
        endpoint = hc.get("endpoint")
        http_method = (hc.get("http_method") or "get").strip().lower()
        healthy_status_code = hc.get("healthy_status_code")
        interval = hc.get("interval")

        if endpoint and isinstance(endpoint, str):
            if http_method not in ("get", "post"):
                raise ValueError("observability.health_check.http_method must be get or post.")
            if healthy_status_code is None:
                healthy_codes = [200]
            else:
                if isinstance(healthy_status_code, str):
                    healthy_codes = [int(x.strip()) for x in healthy_status_code.split(",") if x.strip()]
                else:
                    if isinstance(healthy_status_code, list):
                        healthy_codes = [int(x) for x in healthy_status_code]
                    else:
                        healthy_codes = [int(healthy_status_code)]
            if interval is None:
                interval_s = 10
            else:
                interval_s = int(interval)

            out["health_check"] = {
                "endpoint": endpoint,
                "http_method": http_method,
                "healthy_status_code": healthy_codes,
                "interval": interval_s,
            }

    cw = obs.get("cloudwatch")
    if isinstance(cw, dict):
        lb = cw.get("load_balancer")
        if isinstance(lb, dict):
            lb_type = (lb.get("type") or "").strip().lower()
            lb_name = lb.get("name")
            lb_tags = lb.get("tags")
            metrics = lb.get("metrics")

            if lb_type:
                if lb_type not in ("alb", "nlb", "lb"):
                    raise ValueError("observability.cloudwatch.load_balancer.type must be alb|nlb|lb.")
                if metrics is None:
                    metric_list: List[str] = []
                elif isinstance(metrics, list):
                    metric_list = [str(m) for m in metrics]
                else:
                    raise ValueError("observability.cloudwatch.load_balancer.metrics must be a list.")

                out["cloudwatch"] = {
                    "load_balancer": {
                        "type": lb_type,
                        "name": lb_name if isinstance(lb_name, str) and lb_name else None,
                        "tags": lb_tags if isinstance(lb_tags, str) and lb_tags else None,
                        "metrics": metric_list,
                    }
                }

    return out


def http_health_check_loop(
    stop_event: threading.Event,
    lock: threading.Lock,
    results: List[Dict[str, Any]],
    endpoint: str,
    http_method: str,
    healthy_codes: List[int],
    interval_s: int,
    outdir: str = ".",
) -> None:
    csv_path = os.path.join(outdir, "health_check.csv")
    csv_header = ["time", "http_status_code", "error"]

    while not stop_event.is_set():
        ts = datetime.now(timezone.utc).isoformat()
        status_code: Optional[int] = None
        error: Optional[str] = None

        try:
            if http_method == "get":
                req = urllib.request.Request(endpoint, method="GET")
            else:
                data = b"{}"
                req = urllib.request.Request(
                    endpoint,
                    data=data,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )

            with urllib.request.urlopen(req, timeout=10) as resp:
                status_code = int(getattr(resp, "status", None) or resp.getcode())
        except urllib.error.HTTPError as e:
            status_code = int(getattr(e, "code", None) or 0)
            error = f"HTTPError: {e}"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        record = {
            "timestamp": ts,
            "status_code": status_code,
            "healthy": (status_code in healthy_codes) if status_code is not None else False,
            "error": error,
        }

        with lock:
            results.append(record)

        append_csv_row(
            csv_path,
            csv_header,
            {
                "time": ts,
                "http_status_code": status_code if status_code is not None else "",
                "error": error or "",
            },
        )

        stop_event.wait(interval_s)


def _lb_tags_match(tag_dict: Dict[str, str], actual_tags: Dict[str, str]) -> bool:
    if not tag_dict:
        return True
    return all(actual_tags.get(k) == v for k, v in tag_dict.items())


def lookup_load_balancer(
    *,
    session,
    region: str,
    lb_type: str,
    name: Optional[str],
    tags_str: Optional[str],
) -> Dict[str, Any]:
    tag_filter = parse_tags(tags_str)
    sts = session.client("sts")
    _ = get_account_id(sts)

    if lb_type in ("alb", "nlb"):
        elbv2 = session.client("elbv2", region_name=region)

        lbs: List[Dict[str, Any]] = []
        if name:
            try:
                resp = elbv2.describe_load_balancers(Names=[name])
                lbs = resp.get("LoadBalancers", [])
            except botocore.exceptions.ClientError:
                lbs = []
        if not lbs:
            paginator = elbv2.get_paginator("describe_load_balancers")
            for page in paginator.paginate():
                lbs.extend(page.get("LoadBalancers", []))

        matched: List[Dict[str, Any]] = []
        for lb in lbs:
            lb_name = lb.get("LoadBalancerName")
            if name and lb_name != name:
                continue
            arn = lb.get("LoadBalancerArn")
            if not arn:
                continue
            if tag_filter:
                tag_resp = elbv2.describe_tags(ResourceArns=[arn])
                tag_desc = (tag_resp.get("TagDescriptions") or [])
                tags_map: Dict[str, str] = {}
                if tag_desc and "Tags" in tag_desc[0]:
                    tags_map = {t["Key"]: t.get("Value", "") for t in tag_desc[0]["Tags"] if "Key" in t}
                if not _lb_tags_match(tag_filter, tags_map):
                    continue
            t = (lb.get("Type") or "").strip().lower()
            if lb_type == "alb" and t != "application":
                continue
            if lb_type == "nlb" and t != "network":
                continue
            matched.append(lb)

        if len(matched) == 0:
            raise ValueError("No load balancer matched the provided name/tags.")
        if len(matched) > 1:
            raise ValueError("Multiple load balancers matched. Please refine name/tags to match exactly one.")

        lb = matched[0]
        arn = lb["LoadBalancerArn"]
        full_name = arn.split("loadbalancer/")[-1]
        return {
            "type": lb_type,
            "namespace": "AWS/ApplicationELB",
            "dimensions": [{"Name": "LoadBalancer", "Value": full_name}],
            "id": arn,
            "name": lb.get("LoadBalancerName"),
            "csv_prefix": sanitize_filename(lb.get("LoadBalancerName") or full_name or "load_balancer"),
        }

    elb = session.client("elb", region_name=region)
    lbs: List[Dict[str, Any]] = []
    if name:
        try:
            resp = elb.describe_load_balancers(LoadBalancerNames=[name])
            lbs = resp.get("LoadBalancerDescriptions", [])
        except botocore.exceptions.ClientError:
            lbs = []
    if not lbs:
        paginator = elb.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            lbs.extend(page.get("LoadBalancerDescriptions", []))

    matched: List[Dict[str, Any]] = []
    for lb in lbs:
        lb_name = lb.get("LoadBalancerName")
        if not lb_name:
            continue
        if name and lb_name != name:
            continue
        if tag_filter:
            tag_resp = elb.describe_tags(LoadBalancerNames=[lb_name])
            tag_desc = (tag_resp.get("TagDescriptions") or [])
            tags_map: Dict[str, str] = {}
            if tag_desc and "Tags" in tag_desc[0]:
                tags_map = {t["Key"]: t.get("Value", "") for t in tag_desc[0]["Tags"] if "Key" in t}
            if not _lb_tags_match(tag_filter, tags_map):
                continue
        matched.append(lb)

    if len(matched) == 0:
        raise ValueError("No classic load balancer matched the provided name/tags.")
    if len(matched) > 1:
        raise ValueError("Multiple classic load balancers matched. Please refine name/tags to match exactly one.")

    lb = matched[0]
    lb_name = lb["LoadBalancerName"]
    return {
        "type": "lb",
        "namespace": "AWS/ELB",
        "dimensions": [{"Name": "LoadBalancerName", "Value": lb_name}],
        "id": lb_name,
        "name": lb_name,
        "csv_prefix": sanitize_filename(lb_name or "load_balancer"),
    }


def _cw_period_seconds(interval_s: int) -> int:
    """
    CloudWatch ELB metrics are typically 60s resolution.
    Period must be >= 60 and a multiple of 60 for standard-resolution metrics.
    """
    try:
        n = int(interval_s)
    except Exception:
        n = 60
    if n <= 60:
        return 60
    return ((n + 59) // 60) * 60


def _extract_asg_name_from_arn(arn: str) -> Optional[str]:
    marker = "autoScalingGroupName/"
    if marker not in arn:
        return None
    return arn.split(marker, 1)[1] or None


def _extract_rds_identifier_from_arn(arn: str) -> Tuple[Optional[str], Optional[str]]:
    resource = arn.split(":", 5)[-1]
    if resource.startswith("db:"):
        return "db", resource.split("db:", 1)[1] or None
    if resource.startswith("cluster:"):
        return "cluster", resource.split("cluster:", 1)[1] or None
    return None, None


def _resolve_impacted_resource_cloudwatch(item: Dict[str, str]) -> Optional[Dict[str, Any]]:
    service = (item.get("service") or "").strip().lower()
    arn = (item.get("arn") or "").strip()
    if not service or not arn:
        return None

    if service.startswith("asg:"):
        spec = SERVICE_CLOUDWATCH_METRICS_MAP["asg"]
        asg_name = _extract_asg_name_from_arn(arn)
        if not asg_name:
            return None
        return {
            "service": item.get("service"),
            "arn": arn,
            "namespace": spec["namespace"],
            "dimensions": [{"Name": spec["dimension_name"], "Value": asg_name}],
            "metrics": list(spec["metrics"]),
            "csv_prefix": sanitize_filename(f"asg_{asg_name}"),
        }

    if service.startswith("rds:"):
        rds_type, identifier = _extract_rds_identifier_from_arn(arn)
        if not rds_type or not identifier:
            return None

        key = f"rds:{rds_type}"
        spec = SERVICE_CLOUDWATCH_METRICS_MAP.get(key)
        if not spec:
            return None

        prefix = f"rds_{rds_type}_{identifier}"
        return {
            "service": item.get("service"),
            "arn": arn,
            "namespace": spec["namespace"],
            "dimensions": [{"Name": spec["dimension_name"], "Value": identifier}],
            "metrics": list(spec["metrics"]),
            "csv_prefix": sanitize_filename(prefix),
        }

    return None


def cloudwatch_metrics_loop(
    stop_event: threading.Event,
    lock: threading.Lock,
    results: List[Dict[str, Any]],
    cw_client,
    namespace: str,
    dimensions: List[Dict[str, str]],
    metrics: List[str],
    interval_s: int,
    csv_prefix: str,
    outdir: str = ".",
) -> None:
    if not metrics:
        return

    period_s = _cw_period_seconds(interval_s)
    csv_header = ["time", "value"]

    while not stop_event.is_set():
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=max(period_s * 5, 300))

        queries = []
        for i, metric_name in enumerate(metrics, start=1):
            qid = f"m{i}"
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": dimensions,
                    },
                    "Period": period_s,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            })

        ts = end.isoformat()
        record: Dict[str, Any] = {
            "timestamp": ts,
            "namespace": namespace,
            "dimensions": dimensions,
            "period": period_s,
            "metrics": {},
        }

        try:
            resp = cw_client.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampDescending",
                MaxDatapoints=len(metrics) * 10,
            )
            for r in resp.get("MetricDataResults", []):
                rid = r.get("Id")
                metric_name = None
                if rid and rid.startswith("m"):
                    try:
                        idx = int(rid[1:]) - 1
                        metric_name = metrics[idx]
                    except Exception:
                        metric_name = None

                values = r.get("Values") or []
                timestamps = r.get("Timestamps") or []
                if metric_name:
                    if values and timestamps:
                        record["metrics"][metric_name] = {
                            "value": values[0],
                            "metric_timestamp": timestamps[0].isoformat() if hasattr(timestamps[0], "isoformat") else str(timestamps[0]),
                        }
                    else:
                        record["metrics"][metric_name] = None
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"

        with lock:
            results.append(record)

        for metric_name in metrics:
            safe_metric = sanitize_filename(metric_name)
            csv_path = os.path.join(outdir, f"{csv_prefix}_{safe_metric}.csv")

            val = ""
            m = record["metrics"].get(metric_name)
            if isinstance(m, dict) and "value" in m:
                val = m["value"]

            append_csv_row(
                csv_path,
                csv_header,
                {"time": ts, "value": val if val is not None else ""},
            )

        stop_event.wait(interval_s)


def cloudwatch_lb_metrics_loop(
    stop_event: threading.Event,
    lock: threading.Lock,
    results: List[Dict[str, Any]],
    cw_client,
    namespace: str,
    dimensions: List[Dict[str, str]],
    metrics: List[str],
    interval_s: int,
    outdir: str = ".",
) -> None:
    lb_name_for_file = None
    for d in dimensions or []:
        if d.get("Name") == "LoadBalancerName":
            lb_name_for_file = d.get("Value")
            break
        if d.get("Name") == "LoadBalancer" and not lb_name_for_file:
            lb_name_for_file = d.get("Value")

    safe_lb = sanitize_filename(lb_name_for_file or "load_balancer")
    cloudwatch_metrics_loop(
        stop_event=stop_event,
        lock=lock,
        results=results,
        cw_client=cw_client,
        namespace=namespace,
        dimensions=dimensions,
        metrics=metrics,
        interval_s=interval_s,
        csv_prefix=safe_lb,
        outdir=outdir,
    )


def start_observability_collectors(
    manifest: Dict[str, Any],
    session,
    region: str,
    outdir: str = ".",
    impacted_resources: Optional[List[Dict[str, str]]] = None,
) -> Tuple[threading.Event, Dict[str, Any], List[threading.Thread]]:
    obs_cfg = parse_observability(manifest)
    stop_event = threading.Event()
    lock = threading.Lock()
    threads: List[threading.Thread] = []

    obs_results: Dict[str, Any] = {
        "config": {
            "start_before": obs_cfg.get("start_before"),
            "stop_after": obs_cfg.get("stop_after"),
        },
        "health_check": [],
        "cloudwatch": {
            "load_balancer": {
                "resolved": None,
                "samples": [],
            },
            "resources": [],
        }
    }

    hc = obs_cfg.get("health_check")
    if isinstance(hc, dict):
        t = threading.Thread(
            target=http_health_check_loop,
            name="http_health_check_loop",
            daemon=True,
            args=(
                stop_event,
                lock,
                obs_results["health_check"],
                hc["endpoint"],
                hc["http_method"],
                hc["healthy_status_code"],
                hc["interval"],
                outdir,
            ),
        )
        t.start()
        threads.append(t)

    interval_s = 10
    if isinstance(hc, dict):
        interval_s = int(hc.get("interval", 10))

    cw_client = session.client("cloudwatch", region_name=region)

    cw = obs_cfg.get("cloudwatch")
    if isinstance(cw, dict):
        lb = cw.get("load_balancer")
        if isinstance(lb, dict):
            lb_type = lb.get("type")
            lb_name = lb.get("name")
            lb_tags = lb.get("tags")
            metrics = lb.get("metrics") or []

            if lb_type and metrics:
                resolved = lookup_load_balancer(
                    session=session,
                    region=region,
                    lb_type=lb_type,
                    name=lb_name,
                    tags_str=lb_tags,
                )
                obs_results["cloudwatch"]["load_balancer"]["resolved"] = resolved

                t = threading.Thread(
                    target=cloudwatch_lb_metrics_loop,
                    name="cloudwatch_lb_metrics_loop",
                    daemon=True,
                    args=(
                        stop_event,
                        lock,
                        obs_results["cloudwatch"]["load_balancer"]["samples"],
                        cw_client,
                        resolved["namespace"],
                        resolved["dimensions"],
                        metrics,
                        interval_s,
                        outdir,
                    ),
                )
                t.start()
                threads.append(t)

    seen_resource_keys = set()
    for item in impacted_resources or []:
        resolved = _resolve_impacted_resource_cloudwatch(item)
        if not resolved:
            continue

        key = (
            resolved["namespace"],
            tuple((d["Name"], d["Value"]) for d in resolved["dimensions"]),
            tuple(resolved["metrics"]),
        )
        if key in seen_resource_keys:
            continue
        seen_resource_keys.add(key)

        samples: List[Dict[str, Any]] = []
        obs_results["cloudwatch"]["resources"].append(
            {
                "service": resolved["service"],
                "arn": resolved["arn"],
                "resolved": {
                    "namespace": resolved["namespace"],
                    "dimensions": resolved["dimensions"],
                    "metrics": resolved["metrics"],
                    "csv_prefix": resolved["csv_prefix"],
                },
                "samples": samples,
            }
        )

        t = threading.Thread(
            target=cloudwatch_metrics_loop,
            name=f"cloudwatch_metrics_loop_{resolved['csv_prefix']}",
            daemon=True,
            args=(
                stop_event,
                lock,
                samples,
                cw_client,
                resolved["namespace"],
                resolved["dimensions"],
                resolved["metrics"],
                interval_s,
                resolved["csv_prefix"],
                outdir,
            ),
        )
        t.start()
        threads.append(t)

    return stop_event, obs_results, threads