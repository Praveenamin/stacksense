"""
DEMO data for the Executive Dashboard (Phase 3 review only).

Builds a realistic set of VMAssessment objects + trend/forecast series so the
widgets can be previewed before the real data layer (Phase 4) is wired in.
NOT used in production paths — only the temporary preview view imports this.
"""
from __future__ import annotations

from .rightsizing_engine import DimStats, VMWindowStats, assess_vm
from .rightsizing_report import build_report


def _dim(avg, p95, peak=None):
    return DimStats(avg=avg, p95=p95, peak=peak if peak is not None else p95)


# (name, days, vcpu, gb, cpu(avg,p95), mem(avg,p95), disk_avg)
_DEMO_VMS = [
    ("web-prod-01",   210, 8, 16, (12, 22), (18, 28), 41),
    ("web-prod-02",   180, 4,  8, (9, 16),  (14, 24), 38),
    ("batch-night-01", 95, 16, 32, (7, 19), (11, 21), 55),
    ("api-prod-01",   140, 4,  8, (88, 96), (62, 74), 60),
    ("db-prod-01",    160, 8, 32, (74, 88), (86, 94), 71),
    ("cache-01",       45, 4, 16, (35, 58), (91, 97), 40),
    ("app-prod-03",   200, 8, 16, (55, 70), (58, 72), 47),
    ("app-prod-04",   120, 4,  8, (52, 68), (61, 73), 44),
    ("worker-02",      30, 2,  4, (10, 18), (12, 20), 33),
    ("worker-03",      22, 2,  8, (8, 15),  (9, 17),  29),
    ("staging-01",      9, 4,  8, (14, 26), (16, 30), 35),
    ("lb-edge-01",    260, 2,  4, (40, 78), (33, 49), 22),
    # newly added VMs — must be gated out (<7 days)
    ("new-vm-05",       4, 4,  8, (20, 35), (25, 40), 18),
    ("new-vm-06",       2, 8, 16, (15, 30), (20, 35), 12),
]


def _build_assessments(allow_early=False):
    out = []
    for i, (name, days, vcpu, gb, cpu, mem, disk_avg) in enumerate(_DEMO_VMS):
        stats = VMWindowStats(
            server_id=900 + i, name=name, data_days=days, sample_count=days * 288,
            cpu=_dim(*cpu), memory=_dim(*mem),
            disk=_dim(disk_avg, min(disk_avg + 8, 100)),
            current_vcpu=vcpu, current_gb=float(gb),
        )
        out.append(assess_vm(stats, allow_early=allow_early))
    return out


def _trend_series():
    """Fleet-average CPU/Mem over time for 7 / 30 / 90 day windows (demo)."""
    def series(points, base_cpu, base_mem):
        labels, cpu, mem = [], [], []
        for i in range(points):
            labels.append(f"-{points - i}")
            cpu.append(round(base_cpu + 8 * ((i % 5) - 2) / 2, 1))
            mem.append(round(base_mem + 6 * ((i % 4) - 1) / 2, 1))
        return {"labels": labels, "cpu": cpu, "mem": mem}
    return {
        "7": series(7, 34, 48),
        "30": series(30, 36, 50),
        "90": series(90, 33, 47),
    }


def _forecast_series():
    """Projected fleet-average CPU/Mem for 30 / 60 / 90 day horizons (demo)."""
    def proj(points, start_cpu, slope_cpu, start_mem, slope_mem):
        labels, cpu, mem = [], [], []
        for i in range(0, points + 1, max(1, points // 12)):
            labels.append(f"+{i}d")
            cpu.append(round(min(100, start_cpu + slope_cpu * i), 1))
            mem.append(round(min(100, start_mem + slope_mem * i), 1))
        return {"labels": labels, "cpu": cpu, "mem": mem}
    return {
        "30": proj(30, 36, 0.25, 50, 0.40),
        "60": proj(60, 36, 0.22, 50, 0.38),
        "90": proj(90, 36, 0.20, 50, 0.35),
    }


def build_demo_context(empty: bool = False, allow_early: bool = False) -> dict:
    """Full template context for the preview page. empty=True forces the gate;
    allow_early=True previews <7-day VMs as 'Early' (directional only)."""
    from . import rightsizing_constants as C

    if empty:
        assessments = [a for a in _build_assessments(allow_early=allow_early)
                       if a.data_days < 7]
    else:
        assessments = _build_assessments(allow_early=allow_early)

    report = build_report(assessments, pricing_configured=False)
    ctx = dict(report)
    ctx.update({
        "is_demo": True,
        "trend_data": _trend_series(),
        "forecast_data": _forecast_series(),
        "show_gate": report["eligible_count"] == 0,
        "early_mode": any(a.confidence == "EARLY" for a in assessments),
        "early_message": C.MSG_EARLY,
        "can_preview_early": any(0 < a.data_days < C.MIN_DAYS for a in assessments),
    })
    return ctx
