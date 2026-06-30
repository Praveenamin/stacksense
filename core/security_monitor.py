"""
Security detection engine (SIEM-style, authentication-based for now).

Analyzes recent LoginActivity to detect:
  - BRUTE_FORCE: many failed logins from one source IP within the window
  - LOGIN_FAILURE_SPIKE: many failed logins for one account within the window
  - SUCCESS_AFTER_FAILURES: a successful login from an IP that just had many
    failures (possible account takeover)

New events fire an alert via the existing Email + Slack configuration. Repeated
observations of the same ongoing incident update the open event instead of
spamming new ones (alert-fatigue control).
"""

import logging
from datetime import timedelta

import requests
from django.db.models import Count
from django.utils import timezone

from .models import (
    LoginActivity,
    SecurityEvent,
    SecurityMonitorConfig,
    EmailAlertConfig,
    SlackAlertConfig,
    SSHAuthEvent,
    Server,
)

logger = logging.getLogger("core")

# IPs we never flag (the app records 0.0.0.0 for programmatic/unknown-source logins).
_IGNORED_IPS = {"0.0.0.0"}


def _upsert_event(event_type, severity, title, description, source_ip=None,
                  target_email="", count=1, metadata=None, server=None):
    """Create a new open SecurityEvent or update the matching open one.

    Returns (event, created). When `server` is set (host/SSH events), it is part
    of the dedup key so the same IP attacking two servers yields two events.
    """
    now = timezone.now()
    existing = SecurityEvent.objects.filter(
        event_type=event_type,
        status=SecurityEvent.Status.OPEN,
        source_ip=source_ip,
        target_email=target_email or "",
        server=server,
    ).first()

    if existing:
        existing.event_count = count
        existing.last_seen = now
        existing.description = description
        existing.severity = severity
        if metadata:
            existing.metadata = metadata
        existing.save(update_fields=["event_count", "last_seen", "description", "severity", "metadata"])
        return existing, False

    event = SecurityEvent.objects.create(
        event_type=event_type,
        severity=severity,
        status=SecurityEvent.Status.OPEN,
        title=title,
        description=description,
        source_ip=source_ip,
        target_email=target_email or "",
        server=server,
        event_count=count,
        first_seen=now,
        last_seen=now,
        metadata=metadata or {},
    )
    return event, True


def detect_ssh_brute_force(cfg, now):
    """Detect SSH brute-force per server from recent SSHAuthEvent failures.

    Flags a source IP that exceeds the failed-attempt threshold against a given
    server within the window. Returns newly created events.
    """
    from django.db.models import Count
    window_start = now - timedelta(minutes=cfg.window_minutes)
    failed = (SSHAuthEvent.objects
              .filter(timestamp__gte=window_start, success=False)
              .exclude(source_ip__in=_IGNORED_IPS)
              .exclude(source_ip=""))

    rows = (failed.values("server_id", "source_ip")
            .annotate(c=Count("id"))
            .filter(c__gte=cfg.brute_force_ip_threshold))

    new_events = []
    # Map server_id -> Server for titles
    server_ids = {r["server_id"] for r in rows}
    servers = {s.id: s for s in Server.objects.filter(id__in=server_ids)}
    for r in rows:
        srv = servers.get(r["server_id"])
        if srv is None:
            continue
        if getattr(srv, "os_type", "linux") != "linux":
            continue  # SSH auth-log brute-force is Linux-only (Windows uses Event Log)
        ip, count = r["source_ip"], r["c"]
        event, created = _upsert_event(
            SecurityEvent.EventType.SSH_BRUTE_FORCE,
            SecurityEvent.Severity.HIGH,
            title=f"SSH brute-force on {srv.name} from {ip}",
            description=f"{count} failed SSH logins on {srv.name} from {ip} in the last {cfg.window_minutes} minutes.",
            source_ip=ip,
            count=count,
            metadata={"window_minutes": cfg.window_minutes, "failed_count": count, "server": srv.name},
            server=srv,
        )
        if created:
            new_events.append(event)
    return new_events


def detect_security_events():
    """Run one detection pass. Returns the list of newly created events."""
    cfg = SecurityMonitorConfig.get_config()
    if not cfg.enabled:
        return []

    now = timezone.now()
    window_start = now - timedelta(minutes=cfg.window_minutes)
    recent = LoginActivity.objects.filter(timestamp__gte=window_start)
    failed = recent.filter(status=LoginActivity.StatusChoices.FAILED)

    new_events = []

    # 1. Brute force: failures grouped by source IP
    ip_counts = (
        failed.exclude(ip_address__in=_IGNORED_IPS)
        .values("ip_address")
        .annotate(c=Count("id"))
        .filter(c__gte=cfg.brute_force_ip_threshold)
    )
    for row in ip_counts:
        ip = row["ip_address"]
        count = row["c"]
        event, created = _upsert_event(
            SecurityEvent.EventType.BRUTE_FORCE,
            SecurityEvent.Severity.HIGH,
            title=f"Brute-force login attempts from {ip}",
            description=f"{count} failed login attempts from {ip} in the last {cfg.window_minutes} minutes.",
            source_ip=ip,
            count=count,
            metadata={"window_minutes": cfg.window_minutes, "failed_count": count},
        )
        if created:
            new_events.append(event)

    # 2. Account failure spike: failures grouped by targeted email
    email_counts = (
        failed.exclude(email="")
        .values("email")
        .annotate(c=Count("id"))
        .filter(c__gte=cfg.account_failure_threshold)
    )
    for row in email_counts:
        email = row["email"]
        count = row["c"]
        event, created = _upsert_event(
            SecurityEvent.EventType.LOGIN_FAILURE_SPIKE,
            SecurityEvent.Severity.MEDIUM,
            title=f"Login failure spike for {email}",
            description=f"{count} failed login attempts for account '{email}' in the last {cfg.window_minutes} minutes.",
            target_email=email,
            count=count,
            metadata={"window_minutes": cfg.window_minutes, "failed_count": count},
        )
        if created:
            new_events.append(event)

    # 3. Successful login from an IP that just had many failures (possible takeover)
    successes = recent.filter(status=LoginActivity.StatusChoices.SUCCESS).exclude(ip_address__in=_IGNORED_IPS)
    for s in successes:
        fail_count = failed.filter(ip_address=s.ip_address).count()
        if fail_count >= cfg.brute_force_ip_threshold:
            event, created = _upsert_event(
                SecurityEvent.EventType.SUCCESS_AFTER_FAILURES,
                SecurityEvent.Severity.CRITICAL,
                title=f"Successful login after {fail_count} failures from {s.ip_address}",
                description=(
                    f"Account '{s.email}' logged in successfully from {s.ip_address} "
                    f"after {fail_count} failed attempts in the last {cfg.window_minutes} minutes. "
                    f"Possible account takeover."
                ),
                source_ip=s.ip_address,
                target_email=s.email,
                count=fail_count,
                metadata={"window_minutes": cfg.window_minutes, "preceding_failures": fail_count},
            )
            if created:
                event.user = s.user
                event.save(update_fields=["user"])
                new_events.append(event)

    # 4. SSH brute-force against monitored servers (from agent auth-log events)
    new_events.extend(detect_ssh_brute_force(cfg, now))

    if cfg.alert_enabled:
        for event in new_events:
            try:
                notify(event)
            except Exception:
                logger.exception("Failed to send security alert for event %s", event.id)

    return new_events


# --------------------------------------------------------------------------- #
# Alerting (reuses existing Email + Slack configuration)
# --------------------------------------------------------------------------- #
def _compose(event):
    subject = f"[StackSense Security] {event.get_severity_display()}: {event.title}"
    body = (
        f"Security event detected\n\n"
        f"Type:     {event.get_event_type_display()}\n"
        f"Severity: {event.get_severity_display()}\n"
        f"Source:   {event.source_ip or 'n/a'}\n"
        f"Account:  {event.target_email or 'n/a'}\n"
        f"Detail:   {event.description}\n"
        f"Time:     {event.last_seen}\n"
    )
    return subject, body


def _send_email(subject, body, severity):
    from . import alert_routing
    cfg = EmailAlertConfig.objects.filter(enabled=True).first()
    if not cfg:
        return
    # Security events route by (security, event severity).
    recipients = alert_routing.recipients_for("security", severity)
    if not recipients:
        return
    from django.core.mail import send_mail
    send_mail(subject, body, cfg.from_email or None, recipients, fail_silently=True)


def _send_slack(body):
    cfg = SlackAlertConfig.objects.filter(enabled=True).first()
    if not cfg or not cfg.webhook_url:
        return
    payload = {"text": f":rotating_light: {body}"}
    if cfg.channel:
        payload["channel"] = cfg.channel
    if cfg.username:
        payload["username"] = cfg.username
    if cfg.icon_emoji:
        payload["icon_emoji"] = cfg.icon_emoji
    requests.post(cfg.webhook_url, json=payload, timeout=10)


def notify(event):
    subject, body = _compose(event)
    try:
        _send_email(subject, body, event.severity)
    except Exception:
        logger.exception("Security email alert failed for event %s", event.id)
    try:
        from . import alert_routing
        if alert_routing.slack_should_send("security", event.severity):
            _send_slack(body)
    except Exception:
        logger.exception("Security Slack alert failed for event %s", event.id)
