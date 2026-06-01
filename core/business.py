"""
Business monitoring engine.

Records KPI values (pushed via API or entered manually), evaluates each value
against the KPI's warning/critical thresholds, and alerts on a status change
(into warning/critical, or recovery to OK) via the existing Email + Slack config.
"""

import logging

import requests
from django.utils import timezone

from .models import BusinessKPI, BusinessKPIValue, EmailAlertConfig, SlackAlertConfig

logger = logging.getLogger("core")


def record_value(kpi, value, source=BusinessKPIValue.Source.API, note="", timestamp=None):
    """Store a KPI value, update live state, and alert on a status transition.

    Returns (value_obj, transition) where transition is the new status string
    if it changed into/within warning/critical or recovered, else None.
    """
    now = timestamp or timezone.now()
    value = float(value)

    value_obj = BusinessKPIValue.objects.create(
        kpi=kpi, value=value, source=source, note=note or "", timestamp=now,
    )

    new_status = kpi.evaluate(value)
    old_status = kpi.last_status

    kpi.last_value = value
    kpi.last_value_at = now
    kpi.last_status = new_status
    kpi.save(update_fields=["last_value", "last_value_at", "last_status", "updated_at"])

    transition = None
    if new_status != old_status:
        # Alert when entering warning/critical, or recovering to OK from a bad state.
        if new_status in (BusinessKPI.Status.WARNING, BusinessKPI.Status.CRITICAL):
            transition = new_status
        elif new_status == BusinessKPI.Status.OK and old_status in (
            BusinessKPI.Status.WARNING, BusinessKPI.Status.CRITICAL
        ):
            transition = BusinessKPI.Status.OK

    if transition and kpi.alert_enabled:
        try:
            notify(kpi, transition, value)
        except Exception:
            logger.exception("Failed to send business KPI alert for %s", kpi.key)

    return value_obj, transition


# --------------------------------------------------------------------------- #
# Alerting
# --------------------------------------------------------------------------- #
def _compose(kpi, status, value):
    unit = kpi.unit or ""
    if status == BusinessKPI.Status.OK:
        subject = f"[StackSense] KPI RECOVERED: {kpi.name}"
        headline = "recovered to OK"
    else:
        subject = f"[StackSense] KPI {status.upper()}: {kpi.name}"
        headline = f"is {status.upper()}"
    body = (
        f"Business KPI {headline}\n\n"
        f"KPI:      {kpi.name} ({kpi.key})\n"
        f"Value:    {value}{unit}\n"
        f"Warning:  {kpi.warning_threshold}\n"
        f"Critical: {kpi.critical_threshold}\n"
        f"Time:     {timezone.now()}\n"
    )
    return subject, body


def _send_email(subject, body):
    cfg = EmailAlertConfig.objects.filter(enabled=True).first()
    if not cfg or not cfg.to_email:
        return
    recipients = [e.strip() for e in cfg.to_email.split(",") if e.strip()]
    if not recipients:
        return
    from django.core.mail import send_mail
    send_mail(subject, body, cfg.from_email or None, recipients, fail_silently=True)


def _send_slack(status, body):
    cfg = SlackAlertConfig.objects.filter(enabled=True).first()
    if not cfg or not cfg.webhook_url:
        return
    emoji = ":large_green_circle:" if status == BusinessKPI.Status.OK else (
        ":red_circle:" if status == BusinessKPI.Status.CRITICAL else ":large_yellow_circle:"
    )
    payload = {"text": f"{emoji} {body}"}
    if cfg.channel:
        payload["channel"] = cfg.channel
    if cfg.username:
        payload["username"] = cfg.username
    if cfg.icon_emoji:
        payload["icon_emoji"] = cfg.icon_emoji
    requests.post(cfg.webhook_url, json=payload, timeout=10)


def notify(kpi, status, value):
    subject, body = _compose(kpi, status, value)
    try:
        _send_email(subject, body)
    except Exception:
        logger.exception("Business KPI email alert failed for %s", kpi.key)
    try:
        _send_slack(status, body)
    except Exception:
        logger.exception("Business KPI Slack alert failed for %s", kpi.key)
