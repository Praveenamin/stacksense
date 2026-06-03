"""
Right-sizing recommendation engine — PURE functions (no DB, no Django).

Feed it ``VMWindowStats`` (built by the data layer in Phase 4) and it returns a
``VMAssessment``: category, confidence tier + verbatim message, suggested new
size, recommendation text, and cost/capacity savings.

Dimensions considered: CPU and Memory only (per product decision). Disk is
carried through as context but never drives a recommendation. Network is not
considered.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import rightsizing_constants as C


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class DimStats:
    """Utilization stats for one resource dimension over the window (percent)."""
    avg: float
    p95: float
    peak: float


@dataclass
class VMWindowStats:
    """Per-VM windowed inputs to the engine (built from raw metrics)."""
    server_id: int
    name: str
    data_days: float          # span from this VM's earliest metric to now
    sample_count: int
    cpu: DimStats             # percent
    memory: DimStats          # percent
    disk: Optional[DimStats]  # percent, context only
    current_vcpu: int         # logical cores (capacity)
    current_gb: float         # RAM GB (capacity)


@dataclass
class Pricing:
    price_per_vcpu_month: Optional[float] = None
    price_per_gb_month: Optional[float] = None
    currency: str = "$"

    @property
    def configured(self) -> bool:
        return (self.price_per_vcpu_month is not None
                and self.price_per_gb_month is not None)


@dataclass
class VMAssessment:
    server_id: int
    name: str
    category: str             # see C.CAT_*
    confidence: str           # NONE | LOW | MEDIUM | HIGH
    message: str              # verbatim confidence message
    data_days: float
    data_period_label: str    # "" | "7" | "30" | "90+"
    cpu: DimStats
    memory: DimStats
    disk: Optional[DimStats]
    current_vcpu: int
    current_gb: float
    suggested_vcpu: Optional[int]   # None = no change for that dimension
    suggested_gb: Optional[float]
    recommendation_text: str
    monthly_savings: Optional[float]  # +save / -added; None if pricing unset
    delta_vcpu: int                  # current - suggested (>0 = reclaimed)
    delta_gb: float


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def percentile(values: List[float], p: float) -> float:
    """Linear-interpolation percentile (p in 0..100). Empty -> 0.0."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(s[int(k)])
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def snap_up(value: float, steps: List) -> float:
    """Smallest step >= value (clamped to the largest step)."""
    for s in steps:
        if s >= value:
            return s
    return steps[-1]


def next_step_up(current: float, steps: List) -> float:
    """Smallest step strictly greater than current (clamped to largest)."""
    for s in steps:
        if s > current:
            return s
    return steps[-1]


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
def confidence_for_days(days: float, cfg: C.Thresholds = C.DEFAULT_THRESHOLDS,
                        allow_early: bool = False) -> Tuple[str, str]:
    """Return (confidence_tier, message) for a VM's data age.

    With allow_early=True, a VM that has *some* history but less than the 7-day
    minimum is tagged "EARLY" (opt-in preview) instead of being gated out.
    """
    if days < cfg.min_days:
        if allow_early and days > 0:
            return "EARLY", C.MSG_EARLY
        return "NONE", C.MSG_INSUFFICIENT
    if days < C.CONF_MED_DAYS:
        return "LOW", C.MSG_LOW
    if days < C.CONF_HIGH_DAYS:
        return "MEDIUM", C.MSG_MEDIUM
    return "HIGH", C.MSG_HIGH


# ---------------------------------------------------------------------------
# Classification (CPU + Memory)
# ---------------------------------------------------------------------------
def classify(cpu: DimStats, memory: DimStats,
             cfg: C.Thresholds = C.DEFAULT_THRESHOLDS) -> str:
    # OVER first — safety. Any dimension hot => overutilized.
    if (cpu.avg > cfg.over_avg or cpu.p95 > cfg.over_p95
            or memory.avg > cfg.over_avg or memory.p95 > cfg.over_p95):
        return C.CAT_OVER
    # UNDER — both dimensions low.
    if (cpu.avg < cfg.under_avg and cpu.p95 < cfg.under_p95
            and memory.avg < cfg.under_avg and memory.p95 < cfg.under_p95):
        return C.CAT_UNDER
    # OPTIMIZED — both averages in band, p95 under the optimized ceiling.
    if (cfg.opt_avg_low <= cpu.avg <= cfg.opt_avg_high
            and cfg.opt_avg_low <= memory.avg <= cfg.opt_avg_high
            and cpu.p95 < cfg.opt_p95_max and memory.p95 < cfg.opt_p95_max):
        return C.CAT_OPTIMIZED
    return C.CAT_NEUTRAL


def _is_hot(dim: DimStats, cfg: C.Thresholds) -> bool:
    return dim.avg > cfg.over_avg or dim.p95 > cfg.over_p95


# ---------------------------------------------------------------------------
# Suggested size
# ---------------------------------------------------------------------------
def recommend_size(stats: VMWindowStats, category: str,
                   cfg: C.Thresholds = C.DEFAULT_THRESHOLDS
                   ) -> Tuple[Optional[int], Optional[float]]:
    """
    Return (suggested_vcpu, suggested_gb). None for a dimension means "leave as
    is". Sizes are chosen so the dimension's p95 would land near target_ceiling,
    snapped to standard steps.
    """
    sug_vcpu: Optional[int] = None
    sug_gb: Optional[float] = None

    if category == C.CAT_UNDER:
        need_vcpu = stats.current_vcpu * stats.cpu.p95 / cfg.target_ceiling
        need_gb = stats.current_gb * stats.memory.p95 / cfg.target_ceiling
        cand_vcpu = max(1, snap_up(need_vcpu, C.VCPU_STEPS))
        cand_gb = max(1, snap_up(need_gb, C.RAM_GB_STEPS))
        if cand_vcpu < stats.current_vcpu:
            sug_vcpu = int(cand_vcpu)
        if cand_gb < stats.current_gb:
            sug_gb = float(cand_gb)

    elif category == C.CAT_OVER:
        # Only grow the hot dimension(s); guarantee at least the next step up.
        if _is_hot(stats.cpu, cfg):
            need = stats.current_vcpu * stats.cpu.p95 / cfg.target_ceiling
            cand = max(snap_up(need, C.VCPU_STEPS),
                       next_step_up(stats.current_vcpu, C.VCPU_STEPS))
            sug_vcpu = int(cand)
        if _is_hot(stats.memory, cfg):
            need = stats.current_gb * stats.memory.p95 / cfg.target_ceiling
            cand = max(snap_up(need, C.RAM_GB_STEPS),
                       next_step_up(stats.current_gb, C.RAM_GB_STEPS))
            sug_gb = float(cand)

    return sug_vcpu, sug_gb


def _fmt_gb(v: float) -> str:
    return f"{int(v)}" if float(v).is_integer() else f"{v:g}"


def recommendation_text(category: str, stats: VMWindowStats,
                        sug_vcpu: Optional[int], sug_gb: Optional[float]) -> str:
    parts = []
    if sug_vcpu is not None:
        parts.append(f"{stats.current_vcpu}→{sug_vcpu} vCPU")
    if sug_gb is not None:
        parts.append(f"{_fmt_gb(stats.current_gb)}→{_fmt_gb(sug_gb)} GB RAM")

    if category == C.CAT_UNDER:
        return ("Downsize (" + ", ".join(parts) + ")" if parts
                else "Underutilized but already at minimum size — monitor")
    if category == C.CAT_OVER:
        return ("Upgrade (" + ", ".join(parts) + ")" if parts
                else "Near limits — review workload")
    if category == C.CAT_OPTIMIZED:
        return "Right-sized — no change needed (benchmark)"
    return "Balanced — no action needed"


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
def cost_savings(stats: VMWindowStats, sug_vcpu: Optional[int],
                 sug_gb: Optional[float], pricing: Pricing) -> Optional[float]:
    """Monthly savings (+) or added cost (-). None if pricing not configured."""
    if not pricing.configured:
        return None
    new_vcpu = sug_vcpu if sug_vcpu is not None else stats.current_vcpu
    new_gb = sug_gb if sug_gb is not None else stats.current_gb
    current = (stats.current_vcpu * pricing.price_per_vcpu_month
               + stats.current_gb * pricing.price_per_gb_month)
    suggested = (new_vcpu * pricing.price_per_vcpu_month
                 + new_gb * pricing.price_per_gb_month)
    return round(current - suggested, 2)


# ---------------------------------------------------------------------------
# Top-level assessment
# ---------------------------------------------------------------------------
def assess_vm(stats: VMWindowStats, pricing: Optional[Pricing] = None,
              cfg: C.Thresholds = C.DEFAULT_THRESHOLDS,
              allow_early: bool = False) -> VMAssessment:
    """Assess one VM. Enforces the strict <7-day gate before anything else,
    unless allow_early=True (opt-in preview) — then 0<days<7 yields an EARLY,
    directional-only assessment instead of INSUFFICIENT."""
    pricing = pricing or Pricing()
    confidence, message = confidence_for_days(stats.data_days, cfg,
                                              allow_early=allow_early)

    if confidence == "NONE":
        return VMAssessment(
            server_id=stats.server_id, name=stats.name,
            category=C.CAT_INSUFFICIENT, confidence="NONE", message=message,
            data_days=stats.data_days, data_period_label="",
            cpu=stats.cpu, memory=stats.memory, disk=stats.disk,
            current_vcpu=stats.current_vcpu, current_gb=stats.current_gb,
            suggested_vcpu=None, suggested_gb=None,
            recommendation_text="", monthly_savings=None,
            delta_vcpu=0, delta_gb=0.0,
        )

    category = classify(stats.cpu, stats.memory, cfg)
    sug_vcpu, sug_gb = recommend_size(stats, category, cfg)
    text = recommendation_text(category, stats, sug_vcpu, sug_gb)
    savings = cost_savings(stats, sug_vcpu, sug_gb, pricing)
    delta_vcpu = (stats.current_vcpu - sug_vcpu) if sug_vcpu is not None else 0
    delta_gb = (stats.current_gb - sug_gb) if sug_gb is not None else 0.0

    return VMAssessment(
        server_id=stats.server_id, name=stats.name,
        category=category, confidence=confidence, message=message,
        data_days=stats.data_days, data_period_label=C.PERIOD_LABELS[confidence],
        cpu=stats.cpu, memory=stats.memory, disk=stats.disk,
        current_vcpu=stats.current_vcpu, current_gb=stats.current_gb,
        suggested_vcpu=sug_vcpu, suggested_gb=sug_gb,
        recommendation_text=text, monthly_savings=savings,
        delta_vcpu=delta_vcpu, delta_gb=delta_gb,
    )
