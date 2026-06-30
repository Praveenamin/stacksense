"""
Alert categorization — the canonical taxonomy used to group alerts in the UI and
(later) route them to the right people by role. Pure mapping logic, no DB.

Every alert the system raises maps to exactly ONE of five categories and carries a
severity. Sources -> category:
  - AlertHistory threshold (CPU / Memory / Disk / Disk I/O / Network I/O) -> RESOURCE
  - AlertHistory connection / service / container                        -> AVAILABILITY
  - Anomaly cpu / memory / disk / network                                -> RESOURCE
  - Anomaly memory-leak (shm_leak / ipc_leak / process_rss_leak)         -> CAPACITY
  - SecurityEvent                                                        -> SECURITY
  - SyntheticCheck (uptime)                                              -> AVAILABILITY
  - BusinessKPI                                                          -> BUSINESS
"""
from django.db import models


class AlertCategory(models.TextChoices):
    RESOURCE = "resource", "Resource / Performance"
    AVAILABILITY = "availability", "Availability"
    SECURITY = "security", "Security"
    CAPACITY = "capacity", "Capacity & Health"
    BUSINESS = "business", "Business"


# Severity values, mirroring Anomaly.Severity (LOW/MEDIUM/HIGH/CRITICAL).
SEV_LOW, SEV_MEDIUM, SEV_HIGH, SEV_CRITICAL = "LOW", "MEDIUM", "HIGH", "CRITICAL"

# AlertHistory.alert_type (+ the Disk I/O / Network I/O variants) -> category.
_ALERT_TYPE_CATEGORY = {
    "cpu": AlertCategory.RESOURCE,
    "memory": AlertCategory.RESOURCE,
    "disk": AlertCategory.RESOURCE,
    "diskio": AlertCategory.RESOURCE,
    "disk_io": AlertCategory.RESOURCE,
    "networkio": AlertCategory.RESOURCE,
    "network_io": AlertCategory.RESOURCE,
    "network": AlertCategory.RESOURCE,
    "connection": AlertCategory.AVAILABILITY,
    "service": AlertCategory.AVAILABILITY,
    "container": AlertCategory.AVAILABILITY,
}

# Constant-category sources.
SECURITY = AlertCategory.SECURITY        # SecurityEvent
SYNTHETIC = AlertCategory.AVAILABILITY   # SyntheticCheck / uptime
BUSINESS = AlertCategory.BUSINESS        # BusinessKPI


def _norm(s):
    return (s or "").strip().lower()


def for_alert_type(alert_type):
    """Category for an AlertHistory.alert_type (defaults to RESOURCE)."""
    return _ALERT_TYPE_CATEGORY.get(_norm(alert_type), AlertCategory.RESOURCE)


def for_anomaly(metric_type):
    """Category for an Anomaly.metric_type: leak metrics -> CAPACITY, else RESOURCE."""
    return AlertCategory.CAPACITY if "leak" in _norm(metric_type) else AlertCategory.RESOURCE


def label(category):
    """Human label for a category value."""
    try:
        return AlertCategory(category).label
    except ValueError:
        return str(category)


# What feeds each category — shown as a tooltip on the Alert Routing matrix. Mirrors the
# source -> category mapping documented at the top of this module.
CATEGORY_HINTS = {
    AlertCategory.RESOURCE: (
        "CPU, memory, disk, disk I/O and network I/O threshold breaches — "
        "plus CPU / memory / disk / network anomalies."),
    AlertCategory.AVAILABILITY: (
        "Server unreachable (connection lost), and monitored service / container / "
        "uptime checks going down or recovering."),
    AlertCategory.SECURITY: (
        "Security events — e.g. failed SSH logins and other suspicious activity."),
    AlertCategory.CAPACITY: (
        "Memory-leak anomalies — shared-memory, IPC and process RSS leaks."),
    AlertCategory.BUSINESS: (
        "Business KPI alerts — breaching warning / critical thresholds, and recovery."),
}


def hint(category):
    """One-line description of what feeds a category (for UI tooltips)."""
    try:
        return CATEGORY_HINTS.get(AlertCategory(category), "")
    except ValueError:
        return ""


def default_severity_for_alert_type(alert_type, status="triggered"):
    """Severity for a severity-less AlertHistory alert.

    Resolved/recovered -> LOW (good news). Otherwise: connection down -> CRITICAL
    (server unreachable); service/container down and resource threshold breaches -> HIGH.
    """
    if _norm(status) == "resolved":
        return SEV_LOW
    return SEV_CRITICAL if _norm(alert_type) == "connection" else SEV_HIGH
