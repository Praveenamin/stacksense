"""
Anomaly recovery ("back to normal") detection.

An anomaly's metric is considered back to normal once it returns to within the
detector's own tolerance of the server's pre-anomaly baseline -- i.e. the same
"this is no longer an anomaly" floor the detector uses (median + MIN_ABS_DELTA).
This yields the *true* incident window (start -> back to normal), independent of
when an admin later acknowledges/resolves it.
"""
from __future__ import annotations

import json
from statistics import median

from core.models import SystemMetric
from core.anomaly_detector import AnomalyDetector

BASELINE_WINDOW = AnomalyDetector.BASELINE_WINDOW
MIN_ABS_DELTA = AnomalyDetector.MIN_ABS_DELTA


def _metric_value(metric, metric_type, metric_name):
    """Read the scalar value this anomaly tracks from a SystemMetric, or None."""
    if metric_type == "cpu":
        return metric.cpu_percent
    if metric_type == "memory":
        return metric.memory_percent
    if metric_type == "disk":
        mount = (metric_name[len("disk_percent_"):]
                 if metric_name.startswith("disk_percent_") else None)
        du = metric.disk_usage or {}
        if isinstance(du, str):
            try:
                du = json.loads(du or "{}")
            except (ValueError, TypeError):
                du = {}
        info = du.get(mount) if mount else None
        if isinstance(info, dict):
            return info.get("percent")
    return None  # network / unknown: no baseline-based recovery


def back_to_normal_at(anomaly, max_scan=1500):
    """First timestamp after the anomaly started where its metric returned to
    normal, or None if still elevated / not determinable.

    Normal = value <= (pre-anomaly baseline median) + MIN_ABS_DELTA, matching the
    detector's own "not an anomaly" floor.
    """
    mt, name = anomaly.metric_type, anomaly.metric_name

    history = (SystemMetric.objects
               .filter(server_id=anomaly.server_id, timestamp__lt=anomaly.timestamp)
               .order_by("-timestamp")[:BASELINE_WINDOW])
    vals = [v for v in (_metric_value(m, mt, name) for m in history) if v is not None]
    if not vals:
        return None
    tol = median(vals) + MIN_ABS_DELTA

    forward = (SystemMetric.objects
               .filter(server_id=anomaly.server_id, timestamp__gt=anomaly.timestamp)
               .order_by("timestamp")
               .only("id", "timestamp", "cpu_percent", "memory_percent", "disk_usage"))
    for m in forward[:max_scan]:
        v = _metric_value(m, mt, name)
        if v is not None and v <= tol:
            return m.timestamp
    return None
