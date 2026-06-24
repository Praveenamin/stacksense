"""Precomputed dashboard panels: trend insights + AI recommendations.

Both panels run expensive O(servers) x ~30-day analyses (recurring alert-pattern
detection, right-sizing recommendations). They change slowly, so recomputing them on
every dashboard refresh spiked the web workers. Instead the scheduler precomputes them
periodically into Redis (see the `precompute_dashboard_panels` command) and the API
endpoints just read the cache. On a cold cache the endpoint computes once and caches
(self-heal), so it always returns data even before the first scheduler run.
"""
from django.core.cache import cache

# Default-parameter payloads only (what the dashboard requests on load). Custom-param
# requests bypass the cache and compute live (rare).
TREND_INSIGHTS_KEY = "dashboard:trend_insights:v1"
AI_RECS_KEY = "dashboard:ai_recommendations:v1"

# TTL is deliberately longer than the scheduler's precompute interval (10 min) so the
# cache never goes cold between runs even if one run is skipped.
PANEL_TTL = 1800  # 30 minutes

DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_ALERT_TYPES = ["CPU", "MEMORY", "DISK"]


def compute_trend_insights(lookback_days=DEFAULT_LOOKBACK_DAYS, alert_types=None):
    """The trend-insights payload (the `data` dict the API returns). Pure compute."""
    from core.trend_detection import detect_all_server_patterns
    if alert_types is None:
        alert_types = list(DEFAULT_ALERT_TYPES)
    insights = detect_all_server_patterns(
        alert_types=alert_types, lookback_days=lookback_days, min_alerts=5)
    formatted = []
    for insight in insights:
        pattern = insight["pattern"]
        formatted.append({
            "server_id": insight["server_id"],
            "server_name": insight["server_name"],
            "alert_type": insight["alert_type"],
            "pattern_type": pattern["pattern_type"],
            "pattern_description": pattern["pattern_description"],
            "confidence": round(pattern["confidence"], 1),
            "peak_hour": pattern.get("peak_hour"),
            "peak_day": pattern.get("peak_day"),
            "total_alerts": pattern["total_alerts"],
            "recommendation": pattern["recommendation"],
        })
    return {
        "insights": formatted,
        "total_patterns": len(formatted),
        "servers_with_patterns": len(set(i["server_id"] for i in insights)),
        "analysis_period_days": lookback_days,
    }


def compute_ai_recommendations():
    """The AI-recommendations payload (the list the API returns). Pure compute."""
    try:
        from core.utils.recommendation_engine import generate_recommendations
        return generate_recommendations()
    except ImportError:
        return []


def refresh_panels():
    """Recompute BOTH panels (default params) and store them in the cache. Called by the
    scheduler so user requests never pay the compute cost. Returns a small summary."""
    ti = compute_trend_insights()
    cache.set(TREND_INSIGHTS_KEY, ti, PANEL_TTL)
    ar = compute_ai_recommendations()
    cache.set(AI_RECS_KEY, ar, PANEL_TTL)
    return {"trend_insights_patterns": ti.get("total_patterns", 0),
            "ai_recommendations": len(ar)}


def get_trend_insights():
    """Cache-first read for the API; computes + caches on a cold cache (self-heal)."""
    data = cache.get(TREND_INSIGHTS_KEY)
    if data is None:
        data = compute_trend_insights()
        cache.set(TREND_INSIGHTS_KEY, data, PANEL_TTL)
    return data


def get_ai_recommendations():
    """Cache-first read for the API; computes + caches on a cold cache (self-heal)."""
    data = cache.get(AI_RECS_KEY)
    if data is None:
        data = compute_ai_recommendations()
        cache.set(AI_RECS_KEY, data, PANEL_TTL)
    return data
