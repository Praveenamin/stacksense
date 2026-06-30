"""
Synthetic (uptime) monitoring engine.

The monitoring server runs scheduled HTTP/TCP probes against configured targets,
records each result, maintains up/down state with a failure threshold, and fires
down/recovery alerts via the existing Email + Slack configuration.

Part of User Experience monitoring: catch outages/slowdowns before users do.
"""

import logging
import socket
import time
from datetime import timedelta

import requests
from django.utils import timezone

from .models import (
    SyntheticCheck,
    SyntheticCheckResult,
    EmailAlertConfig,
    SlackAlertConfig,
)

logger = logging.getLogger("core")


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def _status_matches(code, expected):
    """Return True if `code` matches the `expected` spec ('200', '200,301', '200-399')."""
    if code is None:
        return False
    for tok in (expected or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            if "-" in tok:
                lo, hi = tok.split("-", 1)
                if int(lo) <= code <= int(hi):
                    return True
            elif code == int(tok):
                return True
        except ValueError:
            continue
    return False


def _probe_http(check):
    start = time.monotonic()
    try:
        resp = requests.request(
            (check.method or "GET").upper(),
            check.url,
            timeout=check.timeout_seconds,
            verify=check.verify_tls,
            allow_redirects=True,
        )
        elapsed = (time.monotonic() - start) * 1000.0
        status_ok = _status_matches(resp.status_code, check.expected_status)
        body_ok = (not check.expected_substring) or (check.expected_substring in resp.text)
        success = status_ok and body_ok
        error = ""
        if not status_ok:
            error = f"unexpected status {resp.status_code}"
        elif not body_ok:
            error = "expected text not found in response body"
        return {
            "success": success,
            "status_code": resp.status_code,
            "response_time_ms": round(elapsed, 1),
            "error": error,
        }
    except requests.exceptions.SSLError as e:
        return {"success": False, "status_code": None, "response_time_ms": None, "error": f"TLS error: {e}"[:300]}
    except requests.exceptions.Timeout:
        return {"success": False, "status_code": None, "response_time_ms": None, "error": "request timed out"}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "status_code": None, "response_time_ms": None, "error": f"connection failed: {e}"[:300]}
    except Exception as e:
        return {"success": False, "status_code": None, "response_time_ms": None, "error": str(e)[:300]}


def _probe_tcp(check):
    start = time.monotonic()
    try:
        with socket.create_connection((check.host, check.port), timeout=check.timeout_seconds):
            elapsed = (time.monotonic() - start) * 1000.0
            return {"success": True, "status_code": None, "response_time_ms": round(elapsed, 1), "error": ""}
    except Exception as e:
        return {"success": False, "status_code": None, "response_time_ms": None, "error": str(e)[:300]}


def perform_probe(check):
    """Run a single probe (no DB writes). Returns a result dict."""
    if check.check_type == SyntheticCheck.CheckType.TCP:
        return _probe_tcp(check)
    return _probe_http(check)


# --------------------------------------------------------------------------- #
# Recording + state machine
# --------------------------------------------------------------------------- #
def record_and_evaluate(check, probe):
    """Store the probe result and update the check's up/down state.

    Returns (result, transition) where transition is 'DOWN', 'UP' or None.
    A 'DOWN' transition happens after `failure_threshold` consecutive failures;
    'UP' only fires when recovering from a DOWN state (never on the first run).
    """
    now = timezone.now()
    result = SyntheticCheckResult.objects.create(
        synthetic_check=check,
        timestamp=now,
        success=probe["success"],
        status_code=probe.get("status_code"),
        response_time_ms=probe.get("response_time_ms"),
        error_message=probe.get("error") or "",
    )

    transition = None
    if probe["success"]:
        check.consecutive_successes += 1
        check.consecutive_failures = 0
        if check.last_status != SyntheticCheck.Status.UP:
            if check.last_status == SyntheticCheck.Status.DOWN:
                transition = "UP"
            check.last_status = SyntheticCheck.Status.UP
            check.last_state_change_at = now
    else:
        check.consecutive_failures += 1
        check.consecutive_successes = 0
        if (
            check.last_status != SyntheticCheck.Status.DOWN
            and check.consecutive_failures >= check.failure_threshold
        ):
            transition = "DOWN"
            check.last_status = SyntheticCheck.Status.DOWN
            check.last_state_change_at = now

    check.last_checked_at = now
    check.save(update_fields=[
        "consecutive_successes", "consecutive_failures",
        "last_status", "last_state_change_at", "last_checked_at",
    ])
    return result, transition


def run_check(check):
    """Probe a check, record it, and alert on a state transition."""
    probe = perform_probe(check)
    result, transition = record_and_evaluate(check, probe)
    if transition and check.alert_on_failure:
        try:
            notify(check, transition, probe)
        except Exception:
            logger.exception("Failed to send synthetic alert for %s", check.name)
    return result, transition


# --------------------------------------------------------------------------- #
# Alerting (reuses the existing Email + Slack configuration)
# --------------------------------------------------------------------------- #
def _compose(check, event, probe):
    if event == "DOWN":
        subject = f"[StackSense] DOWN: {check.name}"
        body = (
            f"Synthetic check DOWN\n\n"
            f"Check:   {check.name}\n"
            f"Target:  {check.target}\n"
            f"Error:   {probe.get('error') or 'check failed'}\n"
            f"After:   {check.consecutive_failures} consecutive failure(s)\n"
            f"Time:    {timezone.now()}\n"
        )
    else:  # UP / recovery
        subject = f"[StackSense] RECOVERED: {check.name}"
        body = (
            f"Synthetic check RECOVERED\n\n"
            f"Check:   {check.name}\n"
            f"Target:  {check.target}\n"
            f"Latency: {probe.get('response_time_ms')} ms\n"
            f"Time:    {timezone.now()}\n"
        )
    return subject, body


def _send_email(subject, body, severity):
    from . import alert_routing
    cfg = EmailAlertConfig.objects.filter(enabled=True).first()
    if not cfg:
        return
    # Synthetic uptime checks are an Availability alert; route by (category, severity).
    recipients = alert_routing.recipients_for("availability", severity)
    if not recipients:
        return
    # Routes through DatabaseEmailBackend, which reads SMTP settings from EmailAlertConfig.
    from django.core.mail import send_mail
    send_mail(subject, body, cfg.from_email or None, recipients, fail_silently=True)


def _send_slack(check, event, body):
    cfg = SlackAlertConfig.objects.filter(enabled=True).first()
    if not cfg or not cfg.webhook_url:
        return
    emoji = ":red_circle:" if event == "DOWN" else ":large_green_circle:"
    payload = {"text": f"{emoji} {body}"}
    if cfg.channel:
        payload["channel"] = cfg.channel
    if cfg.username:
        payload["username"] = cfg.username
    if cfg.icon_emoji:
        payload["icon_emoji"] = cfg.icon_emoji
    requests.post(cfg.webhook_url, json=payload, timeout=10)


def notify(check, event, probe):
    """Send down/recovery notifications via configured channels."""
    subject, body = _compose(check, event, probe)
    severity = "CRITICAL" if event == "DOWN" else "LOW"
    try:
        _send_email(subject, body, severity)
    except Exception:
        logger.exception("Synthetic email alert failed for %s", check.name)
    try:
        from . import alert_routing
        if alert_routing.slack_should_send("availability", severity):
            _send_slack(check, event, body)
    except Exception:
        logger.exception("Synthetic Slack alert failed for %s", check.name)


# --------------------------------------------------------------------------- #
# Read helpers (for dashboards)
# --------------------------------------------------------------------------- #
def uptime_percentage(check, hours=24):
    """Uptime % over the last `hours`, or None if there are no results yet."""
    since = timezone.now() - timedelta(hours=hours)
    qs = check.results.filter(timestamp__gte=since)
    total = qs.count()
    if not total:
        return None
    ok = qs.filter(success=True).count()
    return round(ok / total * 100.0, 2)


def avg_response_ms(check, hours=24):
    """Average response time (ms) over the last `hours` for successful probes."""
    from django.db.models import Avg
    since = timezone.now() - timedelta(hours=hours)
    val = check.results.filter(
        timestamp__gte=since, success=True, response_time_ms__isnull=False
    ).aggregate(a=Avg("response_time_ms"))["a"]
    return round(val, 1) if val is not None else None
