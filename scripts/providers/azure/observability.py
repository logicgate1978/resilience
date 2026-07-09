from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import urllib.error
import urllib.request

from providers.azure.resource import parse_resource_id, resource_label
from providers.azure.runtime import AzureRuntimeContext
from utility import append_csv_row, coerce_bool, sanitize_filename


AZURE_MANAGEMENT_SCOPE = "https://management.azure.com/.default"
AZURE_MONITOR_METRICS_API_VERSION = "2023-10-01"

SERVICE_AZURE_MONITOR_METRICS_MAP: Dict[str, Dict[str, Any]] = {
    "vm": {
        "namespace": "Microsoft.Compute/virtualMachines",
        "metrics": [
            "Percentage CPU",
            "Network In Total",
            "Network Out Total",
            "Disk Read Bytes",
            "Disk Write Bytes",
        ],
    },
}


def parse_observability(manifest: Dict[str, Any]) -> Dict[str, Any]:
    obs = manifest.get("observability") or {}
    if not isinstance(obs, dict):
        return {}

    out: Dict[str, Any] = {}

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
            elif isinstance(healthy_status_code, str):
                healthy_codes = [int(x.strip()) for x in healthy_status_code.split(",") if x.strip()]
            elif isinstance(healthy_status_code, list):
                healthy_codes = [int(x) for x in healthy_status_code]
            else:
                healthy_codes = [int(healthy_status_code)]

            out["health_check"] = {
                "endpoint": endpoint,
                "http_method": http_method,
                "healthy_status_code": healthy_codes,
                "interval": int(interval) if interval is not None else 10,
            }

    azure_monitor = obs.get("azure_monitor")
    if isinstance(azure_monitor, dict):
        metrics = azure_monitor.get("metrics")
        metric_overrides: Dict[str, List[str]] = {}
        if isinstance(metrics, dict):
            for service_name, service_metrics in metrics.items():
                if isinstance(service_metrics, list):
                    metric_overrides[str(service_name).strip().lower()] = [
                        str(metric).strip()
                        for metric in service_metrics
                        if str(metric).strip()
                    ]
        elif metrics is not None:
            raise ValueError("observability.azure_monitor.metrics must be a map of service/resource type to metric-name list.")

        out["azure_monitor"] = {
            "enabled": coerce_bool(azure_monitor.get("enabled"), True),
            "interval": int(azure_monitor.get("interval") or (out.get("health_check") or {}).get("interval") or 60),
            "aggregation": str(azure_monitor.get("aggregation") or "Average").strip(),
            "granularity": str(azure_monitor.get("granularity") or "PT1M").strip(),
            "metrics": metric_overrides,
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
                req = urllib.request.Request(
                    endpoint,
                    data=b"{}",
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


def _monitor_headers(runtime_context: AzureRuntimeContext) -> Dict[str, str]:
    if runtime_context.credential is None:
        raise ValueError("Azure Monitor observability requires an authenticated Azure credential.")
    token = runtime_context.credential.get_token(AZURE_MANAGEMENT_SCOPE)
    return {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json",
    }


def _duration_seconds_from_iso(value: str) -> int:
    text = str(value or "").strip().upper()
    if text.startswith("PT") and text.endswith("M"):
        return max(60, int(float(text[2:-1]) * 60))
    if text.startswith("PT") and text.endswith("S"):
        return max(60, int(float(text[2:-1])))
    return 60


def _timespan(start: datetime, end: datetime) -> str:
    return f"{start.isoformat().replace('+00:00', 'Z')}/{end.isoformat().replace('+00:00', 'Z')}"


def _azure_monitor_metrics_url(
    *,
    resource_id: str,
    namespace: str,
    metrics: List[str],
    start: datetime,
    end: datetime,
    granularity: str,
    aggregation: str,
) -> str:
    metric_names = ",".join(metrics)
    query = "&".join(
        [
            f"api-version={AZURE_MONITOR_METRICS_API_VERSION}",
            f"metricnamespace={quote(namespace, safe='')}",
            f"metricnames={quote(metric_names, safe=',')}",
            f"timespan={quote(_timespan(start, end), safe='/,:TZ+-')}",
            f"interval={quote(granularity, safe='')}",
            f"aggregation={quote(aggregation, safe='')}",
        ]
    )
    return f"https://management.azure.com{resource_id}/providers/Microsoft.Insights/metrics?{query}"


def _latest_metric_value(metric: Dict[str, Any], aggregation: str) -> Optional[Dict[str, Any]]:
    aggregation_key = str(aggregation or "Average").strip().lower()
    timeseries = metric.get("timeseries") or []
    for series in timeseries:
        for point in reversed(series.get("data") or []):
            value = point.get(aggregation_key)
            if value is None:
                continue
            return {
                "value": value,
                "metric_timestamp": point.get("timeStamp") or point.get("timestamp"),
            }
    return None


def azure_monitor_metrics_loop(
    stop_event: threading.Event,
    lock: threading.Lock,
    results: List[Dict[str, Any]],
    runtime_context: AzureRuntimeContext,
    resource_id: str,
    namespace: str,
    metrics: List[str],
    interval_s: int,
    granularity: str,
    aggregation: str,
    csv_prefix: str,
    outdir: str = ".",
) -> None:
    if not metrics:
        return

    csv_header = ["time", "value"]
    headers = _monitor_headers(runtime_context)
    window_s = max(_duration_seconds_from_iso(granularity) * 5, 300)

    while not stop_event.is_set():
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=window_s)
        ts = end.isoformat()
        record: Dict[str, Any] = {
            "timestamp": ts,
            "resource_id": resource_id,
            "namespace": namespace,
            "aggregation": aggregation,
            "granularity": granularity,
            "metrics": {},
        }

        try:
            url = _azure_monitor_metrics_url(
                resource_id=resource_id,
                namespace=namespace,
                metrics=metrics,
                start=start,
                end=end,
                granularity=granularity,
                aggregation=aggregation,
            )
            response = requests.get(url, headers=headers, timeout=60)
            if response.status_code >= 400:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text
                raise RuntimeError(f"Azure Monitor metrics query failed: status={response.status_code} detail={detail}")
            payload = response.json()
            for metric in payload.get("value") or []:
                metric_name = str((metric.get("name") or {}).get("value") or metric.get("name") or "").strip()
                if not metric_name:
                    continue
                record["metrics"][metric_name] = _latest_metric_value(metric, aggregation)
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"

        with lock:
            results.append(record)

        for metric_name in metrics:
            safe_metric = sanitize_filename(metric_name)
            csv_path = os.path.join(outdir, f"{csv_prefix}_{safe_metric}.csv")
            value = ""
            metric_payload = record["metrics"].get(metric_name)
            if isinstance(metric_payload, dict) and "value" in metric_payload:
                value = metric_payload["value"]
            append_csv_row(
                csv_path,
                csv_header,
                {"time": ts, "value": value if value is not None else ""},
            )

        stop_event.wait(interval_s)


def _resolve_impacted_resource_azure_monitor(
    item: Dict[str, str],
    metric_overrides: Dict[str, List[str]],
) -> Optional[Dict[str, Any]]:
    service = str(item.get("service") or "").strip().lower()
    resource_id = str(item.get("arn") or item.get("resource_id") or "").strip()
    if not service or not resource_id.startswith("/subscriptions/"):
        return None

    service_name = service.split(":", 1)[0]
    if service_name in {"virtual-machine", "virtual_machine"}:
        service_name = "vm"

    spec = SERVICE_AZURE_MONITOR_METRICS_MAP.get(service_name)
    if not spec:
        return None

    parsed = parse_resource_id(resource_id)
    actual_type = f"{parsed.provider_namespace}/{parsed.resource_type}" if parsed else ""
    if actual_type.lower() != str(spec["namespace"]).lower():
        return None

    metrics = metric_overrides.get(service) or metric_overrides.get(service_name) or list(spec["metrics"])
    return {
        "service": item.get("service"),
        "resource_id": resource_id,
        "namespace": spec["namespace"],
        "metrics": list(metrics),
        "csv_prefix": sanitize_filename(f"azure_{service_name}_{resource_label(resource_id)}"),
    }


def start_observability_collectors(
    manifest: Dict[str, Any],
    runtime_context: AzureRuntimeContext,
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
        "azure_monitor": {
            "resources": [],
        },
    }

    hc = obs_cfg.get("health_check")
    if isinstance(hc, dict):
        t = threading.Thread(
            target=http_health_check_loop,
            name="azure_http_health_check_loop",
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

    azure_monitor_cfg = obs_cfg.get("azure_monitor") if isinstance(obs_cfg.get("azure_monitor"), dict) else {}
    if coerce_bool(azure_monitor_cfg.get("enabled"), True):
        interval_s = int(azure_monitor_cfg.get("interval") or (hc or {}).get("interval") or 60)
        aggregation = str(azure_monitor_cfg.get("aggregation") or "Average").strip()
        granularity = str(azure_monitor_cfg.get("granularity") or "PT1M").strip()
        metric_overrides = azure_monitor_cfg.get("metrics") if isinstance(azure_monitor_cfg.get("metrics"), dict) else {}

        seen_resource_keys = set()
        for item in impacted_resources or []:
            resolved = _resolve_impacted_resource_azure_monitor(item, metric_overrides)
            if not resolved:
                continue

            key = (resolved["resource_id"], resolved["namespace"], tuple(resolved["metrics"]))
            if key in seen_resource_keys:
                continue
            seen_resource_keys.add(key)

            samples: List[Dict[str, Any]] = []
            obs_results["azure_monitor"]["resources"].append(
                {
                    "service": resolved["service"],
                    "resource_id": resolved["resource_id"],
                    "resolved": {
                        "namespace": resolved["namespace"],
                        "metrics": resolved["metrics"],
                        "aggregation": aggregation,
                        "granularity": granularity,
                        "csv_prefix": resolved["csv_prefix"],
                    },
                    "samples": samples,
                }
            )

            t = threading.Thread(
                target=azure_monitor_metrics_loop,
                name=f"azure_monitor_metrics_loop_{resolved['csv_prefix']}",
                daemon=True,
                args=(
                    stop_event,
                    lock,
                    samples,
                    runtime_context,
                    resolved["resource_id"],
                    resolved["namespace"],
                    resolved["metrics"],
                    interval_s,
                    granularity,
                    aggregation,
                    resolved["csv_prefix"],
                    outdir,
                ),
            )
            t.start()
            threads.append(t)

    return stop_event, obs_results, threads
