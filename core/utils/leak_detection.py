"""
Memory-leak detection (server-side; the agent only reports raw stats).

Three leak classes, each detected from already-collected SystemMetric data:
  1. System-wide RAM growth        -> from `memory_used` (bytes)
  2. Per-process culprit           -> from `top_processes['memory'][*].rss` (+ start_time)
  3. SysV IPC / shared memory      -> from `ipc_stats` (orphaned shm, sem arrays, /dev/shm)

The math is a least-squares linear fit with an R² gate, so we only flag *sustained*
growth (a real leak), not normal sawtooth usage. Findings are returned as dicts whose
keys match the Anomaly model fields, so the caller can do `Anomaly.objects.create(**f)`.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from core.models import SystemMetric, Anomaly

logger = logging.getLogger("core.leak_detection")

# --- Tunable defaults (kept conservative to avoid noise) ---------------------
WINDOW_HOURS = 24
MIN_POINTS = 12                              # need this many samples in the window
MIN_R2 = 0.6                                 # require a reasonably linear (sustained) trend
SYS_MIN_RATE_BYTES_HR = 5 * 1024 * 1024      # ignore system growth < 5 MB/hr
PROC_MIN_RATE_BYTES_HR = 5 * 1024 * 1024     # ignore per-process growth < 5 MB/hr
PROC_MIN_TOTAL_BYTES = 50 * 1024 * 1024      # require >= 50 MB total growth for a process
PROC_HIGH_RATE_BYTES_HR = 50 * 1024 * 1024   # >= 50 MB/hr per-process -> HIGH
SHM_ORPHAN_MIN_BYTES = 64 * 1024 * 1024      # flag orphaned shm >= 64 MB
SHM_ORPHAN_HIGH_BYTES = 256 * 1024 * 1024    # orphaned shm >= 256 MB -> HIGH
SEM_MIN_GROWTH = 50                          # semaphore-array growth (count) over window


# --- math --------------------------------------------------------------------
def linear_trend(points):
    """Least-squares fit over [(x, y), ...]. Returns (slope, intercept, r2).

    x is seconds, y the metric value; slope is per-second. r2 in [0, 1].
    """
    n = len(points)
    if n < 2:
        return 0.0, 0.0, 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    if sxx == 0:
        return 0.0, my, 0.0
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((p[1] - my) ** 2 for p in points)
    if ss_tot == 0:
        return slope, intercept, 0.0
    ss_res = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in points)
    r2 = max(0.0, 1.0 - ss_res / ss_tot)
    return slope, intercept, r2


def _mb(b):
    return float(b) / (1024 * 1024)


def _fmt_bytes(b):
    b = float(b)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def _severity_from_days(days):
    if days is None:
        return Anomaly.Severity.MEDIUM
    if days < 3:
        return Anomaly.Severity.CRITICAL
    if days < 7:
        return Anomaly.Severity.HIGH
    if days < 30:
        return Anomaly.Severity.MEDIUM
    return Anomaly.Severity.LOW


# --- detectors ---------------------------------------------------------------
def system_memory_leak(metrics):
    """Sustained growth of system memory_used -> one finding dict, or None."""
    pts = [(m.timestamp.timestamp(), float(m.memory_used)) for m in metrics if m.memory_used]
    if len(pts) < MIN_POINTS:
        return None
    t0 = pts[0][0]
    rel = [(t - t0, y) for t, y in pts]
    slope, _, r2 = linear_trend(rel)
    rate_hr = slope * 3600
    if rate_hr < SYS_MIN_RATE_BYTES_HR or r2 < MIN_R2:
        return None

    span_hr = rel[-1][0] / 3600 if rel[-1][0] else 0
    last_used = rel[-1][1]
    mem_total = next((float(m.memory_total) for m in reversed(metrics) if m.memory_total), 0)
    headroom = mem_total - last_used if mem_total else 0
    days_to_full = (headroom / rate_hr / 24) if (rate_hr > 0 and headroom > 0) else None

    expl = f"System RAM climbing ~{_fmt_bytes(rate_hr)}/hr over {span_hr:.0f}h (R²={r2:.2f})"
    expl += f"; projected full in ~{days_to_full:.1f} days." if days_to_full else "."
    return {
        "metric_type": "memory",
        "metric_name": "memory_leak",
        "metric_value": _mb(last_used),
        "anomaly_score": min(r2, 1.0),
        "severity": _severity_from_days(days_to_full),
        "explanation": expl,
    }


def process_memory_leaks(metrics):
    """Per-process sustained RSS growth -> list of finding dicts.

    Needs the agent to report per-process `rss` (bytes); silently yields nothing
    for older agents that only send `memory_percent`.
    """
    series = {}  # key -> {name, pid, points: [(t, rss)]}
    for m in metrics:
        tp = m.top_processes if isinstance(m.top_processes, dict) else {}
        mem = tp.get("memory")
        if not isinstance(mem, list):
            continue
        for p in mem:
            if not isinstance(p, dict):
                continue
            rss = p.get("rss")
            if not rss:
                continue  # agent without RSS -> skip (graceful degrade)
            name = (p.get("name") or "").strip() or "unknown"
            start = p.get("start_time") or 0
            user = p.get("user") or ""
            key = (name, start) if start else (name, user)
            s = series.setdefault(key, {"name": name, "pid": p.get("pid"), "points": []})
            s["points"].append((m.timestamp.timestamp(), float(rss)))
            s["pid"] = p.get("pid")

    out = []
    for s in series.values():
        pts = s["points"]
        if len(pts) < MIN_POINTS:
            continue
        t0 = pts[0][0]
        rel = [(t - t0, y) for t, y in pts]
        slope, _, r2 = linear_trend(rel)
        rate_hr = slope * 3600
        total_growth = rel[-1][1] - rel[0][1]
        if rate_hr < PROC_MIN_RATE_BYTES_HR or r2 < MIN_R2 or total_growth < PROC_MIN_TOTAL_BYTES:
            continue
        span_hr = rel[-1][0] / 3600 if rel[-1][0] else 0
        sev = Anomaly.Severity.HIGH if rate_hr >= PROC_HIGH_RATE_BYTES_HR else Anomaly.Severity.MEDIUM
        out.append({
            "metric_type": "memory",
            "metric_name": f"memory_leak:{s['name']}",
            "metric_value": _mb(rel[-1][1]),
            "anomaly_score": min(r2, 1.0),
            "severity": sev,
            "explanation": (
                f"{s['name']} (pid {s['pid']}) memory up ~{_fmt_bytes(rate_hr)}/hr over "
                f"{span_hr:.0f}h (R²={r2:.2f}, +{_fmt_bytes(total_growth)} total) — likely leak."
            ),
        })
    return out


def _ipc_series(ipc_metrics, key):
    return [
        (m.timestamp.timestamp(), float(m.ipc_stats.get(key) or 0))
        for m in ipc_metrics if m.ipc_stats.get(key) is not None
    ]


def ipc_leaks(metrics):
    """SysV IPC / shared-memory / semaphore leaks -> list of finding dicts.

    Needs the agent to report `ipc_stats`; yields nothing for older agents.
    """
    ipc_metrics = [m for m in metrics if isinstance(m.ipc_stats, dict)]
    if not ipc_metrics:
        return []  # agent doesn't report IPC yet -> graceful degrade
    latest = ipc_metrics[-1].ipc_stats
    out = []
    shm_flagged = False

    # 1) Orphaned SysV shared memory (creator gone, nattch=0) — the classic leak.
    orphan_bytes = float(latest.get("shm_orphaned_bytes") or 0)
    orphan_cnt = int(latest.get("shm_orphaned") or 0)
    if orphan_bytes >= SHM_ORPHAN_MIN_BYTES and orphan_cnt > 0:
        shm_flagged = True
        sev = Anomaly.Severity.HIGH if orphan_bytes >= SHM_ORPHAN_HIGH_BYTES else Anomaly.Severity.MEDIUM
        out.append({
            "metric_type": "memory",
            "metric_name": "shm_leak",
            "metric_value": _mb(orphan_bytes),
            "anomaly_score": 1.0,
            "severity": sev,
            "explanation": (
                f"{orphan_cnt} orphaned SysV shared-memory segment(s) holding ~{_fmt_bytes(orphan_bytes)} "
                f"(nattch=0) — likely shared-memory leak; investigate with `ipcs -m`."
            ),
        })

    # 2) Total SysV shared-memory bytes trending up.
    pts = _ipc_series(ipc_metrics, "shm_bytes")
    if not shm_flagged and len(pts) >= MIN_POINTS:
        t0 = pts[0][0]
        rel = [(t - t0, y) for t, y in pts]
        slope, _, r2 = linear_trend(rel)
        rate_hr = slope * 3600
        if rate_hr >= SYS_MIN_RATE_BYTES_HR and r2 >= MIN_R2:
            span_hr = rel[-1][0] / 3600 if rel[-1][0] else 0
            out.append({
                "metric_type": "memory",
                "metric_name": "shm_leak",
                "metric_value": _mb(rel[-1][1]),
                "anomaly_score": min(r2, 1.0),
                "severity": Anomaly.Severity.MEDIUM,
                "explanation": (
                    f"SysV shared memory growing ~{_fmt_bytes(rate_hr)}/hr over {span_hr:.0f}h "
                    f"(R²={r2:.2f}) — possible shared-memory leak (`ipcs -m`)."
                ),
            })

    # 3) Semaphore arrays growing.
    spts = _ipc_series(ipc_metrics, "sem_arrays")
    if len(spts) >= MIN_POINTS:
        first, last = spts[0][1], spts[-1][1]
        t0 = spts[0][0]
        rel = [(t - t0, y) for t, y in spts]
        slope, _, r2 = linear_trend(rel)
        if (last - first) >= SEM_MIN_GROWTH and slope > 0 and r2 >= MIN_R2:
            span_hr = rel[-1][0] / 3600 if rel[-1][0] else 0
            out.append({
                "metric_type": "memory",
                "metric_name": "semaphore_leak",
                "metric_value": float(last),
                "anomaly_score": min(r2, 1.0),
                "severity": Anomaly.Severity.MEDIUM,
                "explanation": (
                    f"Semaphore arrays grew {int(first)}→{int(last)} over {span_hr:.0f}h "
                    f"— possible semaphore leak (`ipcs -s`)."
                ),
            })

    # 4) /dev/shm (POSIX shared memory) growing.
    dpts = _ipc_series(ipc_metrics, "devshm_bytes")
    if len(dpts) >= MIN_POINTS:
        t0 = dpts[0][0]
        rel = [(t - t0, y) for t, y in dpts]
        slope, _, r2 = linear_trend(rel)
        rate_hr = slope * 3600
        if rate_hr >= SYS_MIN_RATE_BYTES_HR and r2 >= MIN_R2:
            span_hr = rel[-1][0] / 3600 if rel[-1][0] else 0
            out.append({
                "metric_type": "memory",
                "metric_name": "devshm_leak",
                "metric_value": _mb(rel[-1][1]),
                "anomaly_score": min(r2, 1.0),
                "severity": Anomaly.Severity.MEDIUM,
                "explanation": (
                    f"/dev/shm (POSIX shared memory) growing ~{_fmt_bytes(rate_hr)}/hr over "
                    f"{span_hr:.0f}h (R²={r2:.2f}) — possible shared-memory leak."
                ),
            })

    return out


def detect_leaks(server, window_hours=WINDOW_HOURS):
    """Run all leak detectors for one server. Returns a list of Anomaly-field dicts."""
    since = timezone.now() - timedelta(hours=window_hours)
    metrics = list(
        SystemMetric.objects.filter(server=server, timestamp__gte=since)
        .order_by("timestamp")
        .only("timestamp", "memory_used", "memory_total", "top_processes", "ipc_stats")
    )
    if len(metrics) < MIN_POINTS:
        return []
    findings = []
    sysf = system_memory_leak(metrics)
    if sysf:
        findings.append(sysf)
    findings.extend(process_memory_leaks(metrics))
    # SysV IPC / shared-memory / semaphore leaks are a Linux-only concept; skip on
    # non-Linux servers (Windows agents send no ipc_stats, so this is also a no-op there).
    if getattr(server, "os_type", "linux") == "linux":
        findings.extend(ipc_leaks(metrics))
    return findings
