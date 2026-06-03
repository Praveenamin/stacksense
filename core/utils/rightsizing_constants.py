"""
Right-sizing engine constants — thresholds, verbatim confidence messages,
badges and standard resize steps.

PURE module: no Django imports, safe to import from the engine and from tests.
Threshold values can be overridden at the integration layer (Phase 4) by
constructing a ``Thresholds`` from Django settings; the defaults live here.
"""
from dataclasses import dataclass

# --- Minimum history + confidence tier day boundaries (per-VM) -------------
# Measured from a VM's earliest metric to "now".
MIN_DAYS = 7
CONF_LOW_DAYS = 7       # 7  .. <30  -> LOW
CONF_MED_DAYS = 30      # 30 .. <90  -> MEDIUM
CONF_HIGH_DAYS = 90     # >=90       -> HIGH

# --- Recommendation categories ---------------------------------------------
CAT_UNDER = "UNDERUTILIZED"
CAT_OVER = "OVERUTILIZED"
CAT_OPTIMIZED = "OPTIMIZED"
CAT_NEUTRAL = "NEUTRAL"
CAT_INSUFFICIENT = "INSUFFICIENT"

# --- Verbatim confidence messages (DO NOT EDIT THE WORDING) ----------------
MSG_INSUFFICIENT = (
    "Insufficient data available. Recommendations will be generated after a "
    "minimum of 7 days of usage metrics."
)
MSG_LOW = (
    "These recommendations are based on limited historical data and should be "
    "considered preliminary. Additional monitoring is recommended before "
    "taking action."
)
MSG_MEDIUM = (
    "Recommendations are based on sustained usage patterns and can generally "
    "be considered for planning and optimization."
)
MSG_HIGH = (
    "Recommendations are based on long-term usage trends and can be used for "
    "capacity planning and resource allocation decisions."
)
# Shown only in the opt-in early preview (under 7 days). Not a spec gate message.
MSG_EARLY = (
    "Early preview — based on less than 7 days of data. These figures are "
    "directional only and will change as more history is collected; do not use "
    "them for decisions yet."
)

CONFIDENCE_MESSAGES = {
    "NONE": MSG_INSUFFICIENT,
    "EARLY": MSG_EARLY,
    "LOW": MSG_LOW,
    "MEDIUM": MSG_MEDIUM,
    "HIGH": MSG_HIGH,
}

# --- Confidence badges (colorblind-safe: shape icon + text label) ----------
BADGES = {
    "EARLY": {"icon": "🔵", "label": "Early", "css": "conf-early"},
    "LOW": {"icon": "🟡", "label": "Preliminary", "css": "conf-low"},
    "MEDIUM": {"icon": "🟠", "label": "Recommended", "css": "conf-med"},
    "HIGH": {"icon": "🟢", "label": "High Confidence", "css": "conf-high"},
}

# "Data Period Used" bucket labels.
PERIOD_LABELS = {"EARLY": "<7", "LOW": "7", "MEDIUM": "30", "HIGH": "90+"}

# --- Standard resize steps (provider-agnostic) -----------------------------
VCPU_STEPS = [1, 2, 4, 8, 16, 32, 48, 64, 96, 128]
RAM_GB_STEPS = [1, 2, 4, 8, 16, 32, 64, 128, 192, 256, 384, 512]


@dataclass(frozen=True)
class Thresholds:
    """Tunable classification + resize thresholds (percent)."""
    # Underutilized: BOTH dimensions low (safe to shrink)
    under_avg: float = 30.0
    under_p95: float = 50.0
    # Overutilized: ANY dimension hot (worst-dimension wins, safety first)
    over_avg: float = 80.0
    over_p95: float = 90.0
    # Best-optimized band
    opt_avg_low: float = 40.0
    opt_avg_high: float = 70.0
    opt_p95_max: float = 85.0
    # Resize so the post-resize p95 lands near this ceiling
    target_ceiling: float = 60.0
    min_days: int = MIN_DAYS


DEFAULT_THRESHOLDS = Thresholds()
