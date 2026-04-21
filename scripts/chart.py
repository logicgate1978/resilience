import base64
import csv
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt


@dataclass
class ChartImage:
    title: str
    filename: str
    png_base64: str
    group: str


@dataclass
class ExperimentWindow:
    start: Optional[datetime]
    end: Optional[datetime]
    experiment_id: Optional[str] = None
    template_id: Optional[str] = None
    template_name: Optional[str] = None  # derived from result filename when possible


def _parse_iso_time(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _parse_time_any(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_health_check_csv(path: str) -> bool:
    return os.path.basename(path) == "health_check.csv"


def _read_csv_rows(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r:
                rows.append({k: (v if v is not None else "") for k, v in r.items()})
    return rows


def _fig_to_base64_png(fig) -> str:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def _load_experiment_window(outdir: str) -> ExperimentWindow:
    try:
        candidates = []
        for fn in os.listdir(outdir):
            if fn.startswith("result_") and fn.lower().endswith(".json"):
                p = os.path.join(outdir, fn)
                try:
                    candidates.append((os.path.getmtime(p), p))
                except Exception:
                    pass
        if not candidates:
            return ExperimentWindow(start=None, end=None)
        candidates.sort(reverse=True)
        _, latest = candidates[0]

        with open(latest, "r", encoding="utf-8") as f:
            data = json.load(f)

        start = _parse_time_any(str(data.get("startTime") or ""))
        end = _parse_time_any(str(data.get("endTime") or ""))
        exp_id = data.get("experimentId")
        tpl_id = data.get("experimentTemplateId")

        base = os.path.basename(latest)
        template_name = base[len("result_") : -len(".json")] if base.startswith("result_") else None

        return ExperimentWindow(
            start=start,
            end=end,
            experiment_id=str(exp_id) if exp_id else None,
            template_id=str(tpl_id) if tpl_id else None,
            template_name=template_name,
        )
    except Exception:
        return ExperimentWindow(start=None, end=None)


def _load_impacted_resources(outdir: str) -> List[Dict[str, str]]:
    path = os.path.join(outdir, "impacted_resources.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("impacted_resources") or []
        if not isinstance(rows, list):
            return []
        out: List[Dict[str, str]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append(
                {
                    "service": str(r.get("service") or ""),
                    "arn": str(r.get("arn") or ""),
                    "selection_mode": str(r.get("selection_mode") or ""),
                }
            )
        return out
    except Exception:
        return []


def _impacted_resource_counts(impacted_resources: List[Dict[str, str]]) -> Dict[str, Any]:
    total = len(impacted_resources)
    by_service: Dict[str, int] = {}
    for r in impacted_resources:
        s = r.get("service") or ""
        by_service[s] = by_service.get(s, 0) + 1

    items = sorted(by_service.items(), key=lambda x: (-x[1], x[0].lower()))
    return {"total": total, "by_service": items}


def _infer_group_from_filename(csv_filename: str) -> str:
    fn = os.path.basename(csv_filename)
    if fn == "health_check.csv":
        return "Health Check"

    base = fn[:-4] if fn.lower().endswith(".csv") else fn

    if base.startswith("asg_"):
        return "Auto Scaling"

    if base.startswith("rds_db_"):
        return "RDS DB"

    if base.startswith("rds_cluster_"):
        return "RDS Cluster"

    if "_" not in base:
        return "Metrics"

    prefix = base.split("_", 1)[0]
    return f"Load Balancer: {prefix}"


def _annotate_experiment_window(ax, win: ExperimentWindow) -> None:
    if win.start:
        ax.axvline(win.start, linestyle="--", linewidth=1)
        ax.text(
            win.start,
            1.02,
            "Experiment Start",
            transform=ax.get_xaxis_transform(),
            fontsize=9,
            rotation=90,
            va="bottom",
            ha="right",
        )
    if win.end:
        ax.axvline(win.end, linestyle="--", linewidth=1)
        ax.text(
            win.end,
            1.02,
            "Experiment End",
            transform=ax.get_xaxis_transform(),
            fontsize=9,
            rotation=90,
            va="bottom",
            ha="left",
        )
    if win.start and win.end:
        ax.axvspan(win.start, win.end, alpha=0.08)


def _chart_health_check(path: str, win: ExperimentWindow) -> Optional[ChartImage]:
    rows = _read_csv_rows(path)
    if not rows:
        return None

    times: List[datetime] = []
    status: List[float] = []
    healthy: List[int] = []

    for r in rows:
        t = _parse_iso_time(r.get("time", ""))
        if not t:
            t = _parse_time_any(r.get("time", ""))
        if not t:
            continue
        sc_raw = (r.get("http_status_code") or "").strip()
        sc = None
        if sc_raw != "":
            try:
                sc = int(float(sc_raw))
            except Exception:
                sc = None

        times.append(t)
        status.append(float(sc) if sc is not None else float("nan"))

        if sc is not None and (200 <= sc < 400):
            healthy.append(1)
        else:
            healthy.append(0)

    if not times:
        return None

    fig, ax1 = plt.subplots(figsize=(10, 4.8))
    ax1.plot(times, status, marker="o", linestyle="-")
    ax1.set_xlabel("Time")
    ax1.set_ylabel("HTTP Status Code")
    _annotate_experiment_window(ax1, win)

    ax2 = ax1.twinx()
    ax2.plot(times, healthy, marker="x", linestyle="--")
    ax2.set_ylabel("Healthy (1/0)")
    ax2.set_ylim(-0.1, 1.1)

    fig.text(0.5, 0.02, "Health Check: HTTP Status Code over Time", ha="center", va="bottom", fontsize=12)

    png_b64 = _fig_to_base64_png(fig)
    return ChartImage(
        title="health_check.csv",
        filename=os.path.basename(path),
        png_base64=png_b64,
        group=_infer_group_from_filename(path),
    )


def _chart_metric(path: str, win: ExperimentWindow) -> Optional[ChartImage]:
    rows = _read_csv_rows(path)
    if not rows:
        return None

    times: List[datetime] = []
    values: List[float] = []

    for r in rows:
        t = _parse_iso_time(r.get("time", ""))
        if not t:
            t = _parse_time_any(r.get("time", ""))
        if not t:
            continue
        v_raw = (r.get("value") or "").strip()
        if v_raw == "":
            v = float("nan")
        else:
            try:
                v = float(v_raw)
            except Exception:
                v = float("nan")
        times.append(t)
        values.append(v)

    if not times:
        return None

    title = os.path.basename(path)
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.plot(times, values, marker="o", linestyle="-")
    ax.set_xlabel("Time")
    ax.set_ylabel("Value")
    _annotate_experiment_window(ax, win)

    fig.text(0.5, 0.02, f"Metric: {title}", ha="center", va="bottom", fontsize=12)

    png_b64 = _fig_to_base64_png(fig)
    return ChartImage(
        title=title,
        filename=os.path.basename(path),
        png_base64=png_b64,
        group=_infer_group_from_filename(path),
    )


def _compute_slo_health_check(path: str, win: ExperimentWindow) -> Dict[str, Any]:
    rows = _read_csv_rows(path)
    if not rows:
        return {}

    points: List[Tuple[datetime, int]] = []
    for r in rows:
        t = _parse_iso_time(r.get("time", "")) or _parse_time_any(r.get("time", ""))
        if not t:
            continue
        sc_raw = (r.get("http_status_code") or "").strip()
        sc = None
        if sc_raw != "":
            try:
                sc = int(float(sc_raw))
            except Exception:
                sc = None
        is_healthy = 1 if (sc is not None and (200 <= sc < 400)) else 0
        points.append((t, is_healthy))

    if len(points) < 2:
        return {}

    points.sort(key=lambda x: x[0])

    deltas = []
    for i in range(1, len(points)):
        dt = (points[i][0] - points[i - 1][0]).total_seconds()
        if dt > 0:
            deltas.append(dt)
    interval_s = int(sorted(deltas)[len(deltas) // 2]) if deltas else 0

    total = len(points)
    healthy_cnt = sum(p[1] for p in points)
    availability = (healthy_cnt / total) if total else 0.0

    longest_unhealthy_samples = 0
    cur_unhealthy = 0
    total_unhealthy_samples = 0
    first_unhealthy_after_start: Optional[datetime] = None
    first_recovery_after_failure: Optional[datetime] = None

    for t, h in points:
        if h == 0:
            cur_unhealthy += 1
            total_unhealthy_samples += 1
            if win.start and t >= win.start and first_unhealthy_after_start is None:
                first_unhealthy_after_start = t
        else:
            if cur_unhealthy > 0 and first_unhealthy_after_start and first_recovery_after_failure is None:
                if t >= first_unhealthy_after_start:
                    first_recovery_after_failure = t
            longest_unhealthy_samples = max(longest_unhealthy_samples, cur_unhealthy)
            cur_unhealthy = 0
    longest_unhealthy_samples = max(longest_unhealthy_samples, cur_unhealthy)

    total_outage_s = (total_unhealthy_samples * interval_s) if interval_s else None
    longest_outage_s = (longest_unhealthy_samples * interval_s) if interval_s else None
    recovery_s = None
    if first_unhealthy_after_start and first_recovery_after_failure:
        recovery_s = int((first_recovery_after_failure - first_unhealthy_after_start).total_seconds())

    out: Dict[str, Any] = {
        "samples": total,
        "healthy_samples": healthy_cnt,
        "availability": availability,
        "sample_interval_seconds": interval_s if interval_s else None,
        "total_outage_seconds_approx": total_outage_s,
        "longest_outage_seconds_approx": longest_outage_s,
        "first_failure_after_experiment_start": first_unhealthy_after_start.isoformat() if first_unhealthy_after_start else None,
        "recovery_seconds_approx": recovery_s,
    }
    return out


def _compute_slo_metric(path: str, win: ExperimentWindow) -> Dict[str, Any]:
    rows = _read_csv_rows(path)
    if not rows:
        return {}

    points: List[Tuple[datetime, float]] = []
    for r in rows:
        t = _parse_iso_time(r.get("time", "")) or _parse_time_any(r.get("time", ""))
        if not t:
            continue
        v_raw = (r.get("value") or "").strip()
        if v_raw == "":
            continue
        try:
            v = float(v_raw)
        except Exception:
            continue
        points.append((t, v))

    if not points:
        return {}

    points.sort(key=lambda x: x[0])

    all_vals = [v for _, v in points]
    out: Dict[str, Any] = {
        "min": min(all_vals),
        "max": max(all_vals),
        "avg": (sum(all_vals) / len(all_vals)) if all_vals else None,
        "points": len(all_vals),
    }

    if win.start and win.end:
        win_vals = [v for t, v in points if win.start <= t <= win.end]
        if win_vals:
            out["during_experiment"] = {
                "min": min(win_vals),
                "max": max(win_vals),
                "avg": (sum(win_vals) / len(win_vals)) if win_vals else None,
                "points": len(win_vals),
            }

    return out


def generate_report(outdir: str, html_filename: str = "report.html") -> str:
    if not outdir or not os.path.isdir(outdir):
        raise ValueError(f"outdir is not a directory: {outdir}")

    win = _load_experiment_window(outdir)
    impacted_resources = _load_impacted_resources(outdir)
    impacted_counts = _impacted_resource_counts(impacted_resources)

    csv_files = sorted(
        [
            os.path.join(outdir, f)
            for f in os.listdir(outdir)
            if f.lower().endswith(".csv")
        ]
    )

    charts: List[ChartImage] = []
    slo: Dict[str, Any] = {"health_check": None, "metrics": {}}

    for p in csv_files:
        try:
            if _is_health_check_csv(p):
                c = _chart_health_check(p, win)
                slo["health_check"] = _compute_slo_health_check(p, win)
            else:
                c = _chart_metric(p, win)
                slo["metrics"][os.path.basename(p)] = _compute_slo_metric(p, win)
            if c:
                charts.append(c)
        except Exception as e:
            err_fig, ax = plt.subplots(figsize=(10, 2.2))
            ax.axis("off")
            ax.text(0.01, 0.6, f"Failed to chart: {os.path.basename(p)}", fontsize=12)
            ax.text(0.01, 0.25, f"{type(e).__name__}: {e}", fontsize=10)
            png_b64 = _fig_to_base64_png(err_fig)
            charts.append(
                ChartImage(
                    title=f"{os.path.basename(p)} (chart failed)",
                    filename=os.path.basename(p),
                    png_base64=png_b64,
                    group=_infer_group_from_filename(p),
                )
            )

    html_path = os.path.join(outdir, html_filename)

    grouped: Dict[str, List[ChartImage]] = {}
    for c in charts:
        grouped.setdefault(c.group, []).append(c)

    parts: List[str] = []
    parts.append("<!doctype html>")
    parts.append("<html>")
    parts.append("<head>")
    parts.append('<meta charset="utf-8"/>')
    parts.append("<title>FIS Observability Report</title>")
    parts.append("""
<style>
  body { font-family: Arial, sans-serif; margin: 24px; }
  h1 { margin-bottom: 8px; }
  h2.section { margin-top: 22px; margin-bottom: 10px; font-size: 18px; }
  .meta { color: #555; margin-bottom: 18px; }
  .kv { display: grid; grid-template-columns: 220px 1fr; gap: 6px 12px; margin: 10px 0 18px 0; }
  .k { color: #555; }
  .v { color: #111; overflow-wrap: anywhere; }
  .grid { display: grid; grid-template-columns: 1fr; gap: 18px; }
  .card { border: 1px solid #ddd; border-radius: 10px; padding: 14px; }
  .card h3 { font-size: 16px; margin: 0 0 10px 0; }
  .card img { max-width: 100%; height: auto; border-radius: 6px; }
  .slo { border: 1px solid #eee; border-radius: 10px; padding: 14px; background: #fafafa; }
  .slo pre { white-space: pre-wrap; margin: 0; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  th, td { border: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }
  th { background: #f3f3f3; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eee; font-size: 12px; margin-right: 8px; }
  .foot { color: #666; margin-top: 18px; font-size: 12px; }
</style>
""")
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<h1>FIS Observability Report</h1>")
    parts.append(f'<div class="meta">Generated at: {datetime.utcnow().isoformat()}Z</div>')

    parts.append('<div class="slo">')
    parts.append('<h2 class="section">Experiment Timeline</h2>')
    parts.append('<div class="kv">')
    parts.append(f'<div class="k">Experiment Start</div><div class="v">{win.start.isoformat() if win.start else "(not found)"}</div>')
    parts.append(f'<div class="k">Experiment End</div><div class="v">{win.end.isoformat() if win.end else "(not found)"}</div>')
    if win.experiment_id:
        parts.append(f'<div class="k">Experiment ID</div><div class="v">{win.experiment_id}</div>')
    if win.template_id:
        parts.append(f'<div class="k">Template ID</div><div class="v">{win.template_id}</div>')
    if win.template_name:
        parts.append(f'<div class="k">Template Name</div><div class="v">{win.template_name}</div>')
    parts.append('</div>')

    parts.append('<h2 class="section">SLO Summary (Approx.)</h2>')
    parts.append('<pre>')
    parts.append(json.dumps(slo, indent=2, sort_keys=True, default=str))
    parts.append('</pre>')
    parts.append('</div>')

    parts.append('<div class="slo">')
    parts.append('<h2 class="section">Impacted Resources</h2>')

    parts.append('<div style="margin-top:6px;">')
    parts.append(f'<span class="pill">Total: {impacted_counts["total"]}</span>')
    for svc, cnt in impacted_counts["by_service"]:
        parts.append(f'<span class="pill">{svc}: {cnt}</span>')
    parts.append('</div>')

    if not impacted_resources:
        parts.append('<p>No impacted resources found.</p>')
    else:
        parts.append('<table>')
        parts.append('<thead><tr><th>Service</th><th>ARN</th><th>Selection Mode</th></tr></thead>')
        parts.append('<tbody>')
        for r in impacted_resources:
            parts.append(
                f"<tr><td>{r['service']}</td><td>{r['arn']}</td><td>{r['selection_mode']}</td></tr>"
            )
        parts.append('</tbody>')
        parts.append('</table>')
    parts.append('</div>')

    if not charts:
        parts.append("<p>No CSV files found (or no data to chart).</p>")
    else:
        group_names = list(grouped.keys())
        group_names.sort(key=lambda x: (0 if x == "Health Check" else 1, x.lower()))

        for g in group_names:
            parts.append(f'<h2 class="section">{g}</h2>')
            parts.append('<div class="grid">')
            for c in grouped[g]:
                parts.append('<div class="card">')
                parts.append(f"<h3>{c.title}</h3>")
                parts.append(f'<img alt="{c.filename}" src="data:image/png;base64,{c.png_base64}"/>')
                parts.append("</div>")
            parts.append("</div>")

    parts.append('<div class="foot">Charts are generated from CSV files in the output directory.</div>')
    parts.append("</body>")
    parts.append("</html>")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    return html_path


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True, help="Directory containing CSV files")
    ap.add_argument("--html", default="report.html", help="Output HTML filename")
    args = ap.parse_args()

    p = generate_report(args.outdir, args.html)
    print(f"[OK] Wrote report: {p}", flush=True)
