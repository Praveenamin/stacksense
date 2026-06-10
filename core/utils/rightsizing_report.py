"""
Right-sizing report builder — PURE presentation logic (no DB, no Django).

Takes a list of ``VMAssessment`` (from rightsizing_engine.assess_vm) and groups
them into the widget buckets the Executive Dashboard renders: top-10 lists,
upgrade/reduction sets, cost opportunities, and fleet totals.
"""
from __future__ import annotations

from typing import List

from . import rightsizing_constants as C


def _avg_load(a) -> float:
    return (a.cpu.avg + a.memory.avg) / 2.0


def _peak_load(a) -> float:
    return max(a.cpu.p95, a.memory.p95)


def build_report(assessments: List, pricing_configured: bool = False) -> dict:
    """Group assessments into widget buckets + fleet totals. Pure function."""
    pending = [a for a in assessments if a.category == C.CAT_INSUFFICIENT]
    eligible = [a for a in assessments if a.category != C.CAT_INSUFFICIENT]

    # "Early" (0-7 days): just-added VMs with some history but below the 7-day
    # minimum -- shown as their own category with directional recommendations,
    # kept OUT of the mature counts/totals so those stay confident.
    early = [a for a in eligible if a.confidence == "EARLY"]
    mature = [a for a in eligible if a.confidence != "EARLY"]
    # Newest first (closest to graduating to a confident assessment).
    top_early = sorted(early, key=lambda a: a.data_days, reverse=True)[:10]

    under = [a for a in mature if a.category == C.CAT_UNDER]
    over = [a for a in mature if a.category == C.CAT_OVER]
    optimized = [a for a in mature if a.category == C.CAT_OPTIMIZED]

    # Top 10 underutilized: lowest combined load first.
    top_underutilized = sorted(under, key=_avg_load)[:10]
    # Top 10 overloaded: highest peak first.
    top_overloaded = sorted(over, key=_peak_load, reverse=True)[:10]
    # Top 10 most efficient: closest to the middle of the optimized band (~55%).
    top_optimized = sorted(optimized, key=lambda a: abs(_avg_load(a) - 55.0))[:10]

    # Actionable sets (have a concrete size change).
    upgrade_required = sorted(
        [a for a in over if a.suggested_vcpu is not None or a.suggested_gb is not None],
        key=_peak_load, reverse=True,
    )
    reduction_eligible = sorted(
        [a for a in under if a.delta_vcpu > 0 or a.delta_gb > 0],
        key=lambda a: (a.delta_vcpu, a.delta_gb), reverse=True,
    )

    # Cost opportunities: when priced, anything with positive savings (sorted by
    # $). When unpriced, fall back to the reduction set (capacity reclaimed).
    if pricing_configured:
        cost_opportunities = sorted(
            [a for a in mature if (a.monthly_savings or 0) > 0],
            key=lambda a: a.monthly_savings, reverse=True,
        )
    else:
        cost_opportunities = reduction_eligible

    total_monthly_savings = (
        round(sum(a.monthly_savings for a in mature
                  if a.monthly_savings and a.monthly_savings > 0), 2)
        if pricing_configured else None
    )
    total_reclaim_vcpu = sum(a.delta_vcpu for a in mature if a.delta_vcpu > 0)
    total_reclaim_gb = round(
        sum(a.delta_gb for a in mature if a.delta_gb > 0), 1)

    return {
        "pending": pending,
        "pending_count": len(pending),
        "eligible_count": len(eligible),
        "counts": {
            "underutilized": len(under),
            "overloaded": len(over),
            "optimized": len(optimized),
            "early": len(early),
        },
        "early_count": len(early),
        "top_early": top_early,
        "top_underutilized": top_underutilized,
        "top_overloaded": top_overloaded,
        "top_optimized": top_optimized,
        "upgrade_required": upgrade_required,
        "reduction_eligible": reduction_eligible,
        "cost_opportunities": cost_opportunities,
        "total_monthly_savings": total_monthly_savings,
        "total_reclaim_vcpu": total_reclaim_vcpu,
        "total_reclaim_gb": total_reclaim_gb,
        "pricing_configured": pricing_configured,
    }
