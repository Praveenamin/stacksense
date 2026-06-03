"""
Data layer for the right-sizing engine — the ONLY part that touches the ORM.

Builds per-VM ``VMWindowStats`` from ``SystemMetric`` (efficiently, via a single
grouped Postgres ``PERCENTILE_CONT`` query), plus fleet trend + forecast series.
The engine itself (rightsizing_engine.py) stays pure.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.db.models import Aggregate, Avg, Count, FloatField, Max, Min
from django.db.models.functions import TruncDate
from django.utils import timezone

from core.models import PricingConfig, Server, SystemMetric

from .rightsizing_engine import DimStats, Pricing, VMWindowStats

ANALYSIS_CAP_DAYS = 90
BYTES_PER_GB = 1024 ** 3
# Minimum samples in the window before we trust percentiles enough to act.
MIN_SAMPLES = 12


class PercentileCont(Aggregate):
    """Postgres ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)`` aggregate."""
    function = "PERCENTILE_CONT"
    name = "percentile"
    template = "%(function)s(%(percentile)s) WITHIN GROUP (ORDER BY %(expressions)s)"

    def __init__(self, expression, percentile, **extra):
        super().__init__(expression, output_field=FloatField(),
                         percentile=percentile, **extra)


def get_pricing() -> Pricing:
    """Load saved pricing into the engine's Pricing value object."""
    cfg = PricingConfig.get_solo()
    return Pricing(
        price_per_vcpu_month=cfg.price_per_vcpu_month,
        price_per_gb_month=cfg.price_per_gb_month,
        currency=cfg.currency or "$",
    )


def _disk_context(metric) -> Optional[DimStats]:
    """Context-only disk %: mean and max mount fullness from the latest sample."""
    if not metric or not metric.disk_usage:
        return None
    pcts = [v.get("percent") for v in metric.disk_usage.values()
            if isinstance(v, dict) and v.get("percent") is not None]
    if not pcts:
        return None
    return DimStats(avg=round(sum(pcts) / len(pcts), 1),
                    p95=round(max(pcts), 1), peak=round(max(pcts), 1))


def gather_vm_window_stats(now=None, cap_days=ANALYSIS_CAP_DAYS):
    """Return a list[VMWindowStats], one per Server. 3 queries total."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=cap_days)

    # 1) Grouped utilization stats over the analysis window.
    agg_rows = (
        SystemMetric.objects.filter(timestamp__gte=cutoff)
        .values("server_id")
        .annotate(
            n=Count("id"),
            cpu_avg=Avg("cpu_percent"),
            cpu_p95=PercentileCont("cpu_percent", 0.95),
            cpu_peak=Max("cpu_percent"),
            mem_avg=Avg("memory_percent"),
            mem_p95=PercentileCont("memory_percent", 0.95),
            mem_peak=Max("memory_percent"),
        )
    )
    stats_by_server = {r["server_id"]: r for r in agg_rows}

    # 2) Earliest metric per server over ALL history -> data age (gap-tolerant).
    earliest = {
        r["server_id"]: r["first"]
        for r in SystemMetric.objects.values("server_id").annotate(first=Min("timestamp"))
    }

    # 3) Latest metric per server -> capacity + disk context (one query).
    latest_by_server = {
        m.server_id: m
        for m in SystemMetric.objects.order_by("server_id", "-timestamp").distinct("server_id")
    }

    out = []
    for server in Server.objects.all():
        s = stats_by_server.get(server.id)
        first_ts = earliest.get(server.id)
        latest = latest_by_server.get(server.id)

        data_days = (now - first_ts).total_seconds() / 86400.0 if first_ts else 0.0

        vcpu = (latest.cpu_count if latest and latest.cpu_count else 0)
        gb = (round(latest.memory_total / BYTES_PER_GB, 1)
              if latest and latest.memory_total else 0.0)

        if s and s["n"] and s["n"] >= MIN_SAMPLES and (s["cpu_avg"] is not None):
            cpu = DimStats(avg=round(s["cpu_avg"], 1),
                           p95=round(s["cpu_p95"] or s["cpu_avg"], 1),
                           peak=round(s["cpu_peak"] or s["cpu_avg"], 1))
            mem = DimStats(avg=round(s["mem_avg"], 1),
                           p95=round(s["mem_p95"] or s["mem_avg"], 1),
                           peak=round(s["mem_peak"] or s["mem_avg"], 1))
            n = s["n"]
        else:
            # Too few samples to trust -> force the insufficient gate downstream.
            cpu = DimStats(0.0, 0.0, 0.0)
            mem = DimStats(0.0, 0.0, 0.0)
            n = s["n"] if s else 0
            data_days = min(data_days, 0.0)  # sparse window -> treat as no data

        out.append(VMWindowStats(
            server_id=server.id, name=server.name,
            data_days=data_days, sample_count=n,
            cpu=cpu, memory=mem, disk=_disk_context(latest),
            current_vcpu=vcpu or 1, current_gb=gb or 1.0,
        ))
    return out


# ---------------------------------------------------------------------------
# Fleet trend + forecast (for the chart widgets)
# ---------------------------------------------------------------------------
def _daily_fleet_averages(since, now):
    rows = (
        SystemMetric.objects.filter(timestamp__gte=since, timestamp__lte=now)
        .annotate(day=TruncDate("timestamp"))
        .values("day")
        .annotate(cpu=Avg("cpu_percent"), mem=Avg("memory_percent"))
        .order_by("day")
    )
    return [(r["day"], round(r["cpu"] or 0, 1), round(r["mem"] or 0, 1)) for r in rows]


def fleet_trend(now=None):
    """Fleet-average CPU/Memory per day for 7 / 30 / 90 day windows."""
    now = now or timezone.now()
    out = {}
    for key, days in (("7", 7), ("30", 30), ("90", 90)):
        rows = _daily_fleet_averages(now - timedelta(days=days), now)
        out[key] = {
            "labels": [d.strftime("%b %d") for d, _c, _m in rows],
            "cpu": [c for _d, c, _m in rows],
            "mem": [m for _d, _c, m in rows],
        }
    return out


def _linfit(values):
    """Least-squares fit of values vs index. Returns (slope, value_at_last_point)
    so the forecast is anchored at "now". None if <2 points or flat x."""
    n = len(values)
    if n < 2:
        return None
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((xs[i] - mx) * (values[i] - my) for i in range(n)) / denom
    value_at_last = my + slope * ((n - 1) - mx)
    return slope, value_at_last


def fleet_forecast(now=None):
    """Project fleet-average CPU/Memory forward 30 / 60 / 90 days via linear fit
    on up to 90 days of daily averages. Empty series if not enough history."""
    now = now or timezone.now()
    basis = _daily_fleet_averages(now - timedelta(days=90), now)
    out = {"30": _empty_fc(), "60": _empty_fc(), "90": _empty_fc()}
    if len(basis) < 2:
        return out

    cpu_vals = [c for _d, c, _m in basis]
    mem_vals = [m for _d, _c, m in basis]
    cpu_fit = _linfit(cpu_vals)
    mem_fit = _linfit(mem_vals)
    if not cpu_fit or not mem_fit:
        return out

    cpu_slope, cpu_last = cpu_fit
    mem_slope, mem_last = mem_fit
    for key, horizon in (("30", 30), ("60", 60), ("90", 90)):
        step = max(1, horizon // 12)
        labels, cpu, mem = [], [], []
        for d in range(0, horizon + 1, step):
            labels.append(f"+{d}d")
            cpu.append(round(min(100, max(0, cpu_last + cpu_slope * d)), 1))
            mem.append(round(min(100, max(0, mem_last + mem_slope * d)), 1))
        out[key] = {"labels": labels, "cpu": cpu, "mem": mem}
    return out


def _empty_fc():
    return {"labels": [], "cpu": [], "mem": []}
