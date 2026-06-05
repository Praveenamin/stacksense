"""
Authenticated ingest API for the push-based monitoring agent.

The agent on each monitored VM dials OUT to these endpoints over HTTPS and
authenticates with a per-server bearer token (see AgentCredential). The
monitoring server holds no credentials that can log in to the fleet, so a
compromise of this server cannot be used to access any monitored VM.

Endpoints (all machine-to-machine, CSRF-exempt, token-authenticated):
    GET  /api/agent/ping/       -> connectivity + auth check
    POST /api/agent/heartbeat/  -> lightweight "I'm alive" signal
    POST /api/agent/metrics/    -> full system-metrics push

Authentication: the agent sends an HTTP header
    Authorization: Bearer <token>
The raw token is hashed and looked up against AgentCredential; only the hash is
ever stored server-side.
"""

import json
import logging
import os

from django.conf import settings
from django.http import JsonResponse, HttpResponse, Http404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import (
    AgentCredential, SystemMetric, ServerHeartbeat, AgentVersion,
    BusinessKPI, BusinessKPIValue, BusinessMonitorConfig, Service, Container,
    AlertHistory, EmailAlertConfig, SlackAlertConfig, SSHAuthEvent,
)

logger = logging.getLogger("core")

# Maximum accepted request body size for a metrics push (defensive limit).
MAX_BODY_BYTES = 256 * 1024  # 256 KB

# Required metric fields a push must contain (non-null on SystemMetric).
REQUIRED_FIELDS = (
    "cpu_percent",
    "memory_total",
    "memory_available",
    "memory_percent",
    "memory_used",
)

# Whitelisted numeric fields accepted from the agent and their coercion type.
# Anything not listed here (or below) is ignored, so the agent can never write
# to arbitrary model fields.
FLOAT_FIELDS = (
    "cpu_percent",
    "memory_percent",
    "swap_percent",
    "cpu_load_avg_1m",
    "cpu_load_avg_5m",
    "cpu_load_avg_15m",
    "net_utilization_sent",
    "net_utilization_recv",
)

INT_FIELDS = (
    "cpu_count",
    "physical_cpu_count",
    "memory_total",
    "memory_available",
    "memory_used",
    "memory_buffers",
    "memory_cached",
    "memory_shared",
    "swap_total",
    "swap_used",
    "network_connections",
    "disk_io_read",
    "disk_io_write",
    "net_io_sent",
    "net_io_recv",
    "nic_max_speed_bits",
    "disk_read_bytes_total",
    "disk_write_bytes_total",
    "system_uptime_seconds",
)

# JSON/dict fields accepted from the agent.
JSON_FIELDS = (
    "disk_usage",
    "network_io",
    "top_processes",
    "ipc_stats",
)


def _get_client_ip(request):
    """Best-effort source IP, honoring a single proxy hop (X-Forwarded-For)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _authenticate(request):
    """Return (credential, error_response). Exactly one is non-None."""
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
    if not token:
        return None, JsonResponse(
            {"error": "missing or malformed Authorization header"}, status=401
        )

    cred = AgentCredential.authenticate(token)
    if cred is None:
        return None, JsonResponse({"error": "invalid or disabled token"}, status=401)
    return cred, None


def _touch_credential(cred, request):
    """Record that a valid push was received with this token."""
    cred.last_used_at = timezone.now()
    cred.last_used_ip = _get_client_ip(request)
    cred.save(update_fields=["last_used_at", "last_used_ip"])


def _update_heartbeat(server, agent_version):
    """Refresh the server heartbeat and (optionally) the reported agent version."""
    ServerHeartbeat.objects.update_or_create(
        server=server,
        defaults={"last_heartbeat": timezone.now(), "agent_version": agent_version},
    )
    if agent_version:
        AgentVersion.objects.update_or_create(
            server=server,
            version=agent_version,
            defaults={"last_seen": timezone.now()},
        )


def _coerce_metrics(payload):
    """Validate and whitelist an agent payload into SystemMetric kwargs.

    Returns (kwargs, error_message). On success error_message is None.
    """
    if not isinstance(payload, dict):
        return None, "payload must be a JSON object"

    # Required fields must be present and numeric.
    for field in REQUIRED_FIELDS:
        if payload.get(field) is None:
            return None, f"missing required field: {field}"

    kwargs = {}
    try:
        for field in FLOAT_FIELDS:
            if payload.get(field) is not None:
                kwargs[field] = float(payload[field])
        for field in INT_FIELDS:
            if payload.get(field) is not None:
                kwargs[field] = int(payload[field])
    except (TypeError, ValueError):
        return None, f"field '{field}' is not a valid number"

    for field in JSON_FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        if field in ("disk_usage", "network_io") and not isinstance(value, dict):
            return None, f"field '{field}' must be a JSON object"
        kwargs[field] = value

    # disk_usage / network_io are non-null (default=dict) on the model.
    kwargs.setdefault("disk_usage", {})
    kwargs.setdefault("network_io", {})
    return kwargs, None


@csrf_exempt
@require_http_methods(["POST"])
def kpi_ingest(request):
    """Ingest a business KPI value.

    Auth: Authorization: Bearer <business ingest token>.
    Body: {"key": "<kpi key>", "value": <number>, "note": "<optional>"}
    The timestamp is stamped server-side.
    """
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else None
    cfg = BusinessMonitorConfig.get_config()
    if not token or not cfg.verify_token(token):
        return JsonResponse({"error": "invalid or missing token"}, status=401)

    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "payload too large"}, status=413)
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"error": "request body is not valid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "payload must be a JSON object"}, status=400)

    key = (payload.get("key") or "").strip()
    if not key:
        return JsonResponse({"error": "missing 'key'"}, status=400)
    if payload.get("value") is None:
        return JsonResponse({"error": "missing 'value'"}, status=400)
    try:
        value = float(payload["value"])
    except (TypeError, ValueError):
        return JsonResponse({"error": "'value' must be a number"}, status=400)

    kpi = BusinessKPI.objects.filter(key=key, enabled=True).first()
    if kpi is None:
        return JsonResponse({"error": f"no enabled KPI with key '{key}'"}, status=404)

    from .business import record_value
    note = (payload.get("note") or "")[:255]
    value_obj, transition = record_value(kpi, value, source=BusinessKPIValue.Source.API, note=note)
    return JsonResponse({
        "status": "ok",
        "kpi": kpi.key,
        "value": value_obj.value,
        "kpi_status": kpi.last_status,
        "transition": transition,
    })


def _notify_unit(server, kind, name, down):
    """Email/Slack a monitored service/container down/recovery notice (best-effort).

    `kind` is "service" or "container".
    """
    if down:
        subject = f"[StackSense] {kind.upper()} DOWN: {name} on {server.name}"
        body = f"Monitored {kind} '{name}' is NOT running on {server.name} (as of {timezone.now()})."
        emoji = ":red_circle:"
    else:
        subject = f"[StackSense] {kind.upper()} RECOVERED: {name} on {server.name}"
        body = f"Monitored {kind} '{name}' is running again on {server.name} (as of {timezone.now()})."
        emoji = ":large_green_circle:"
    try:
        ecfg = EmailAlertConfig.objects.filter(enabled=True).first()
        if ecfg and ecfg.to_email:
            from django.core.mail import send_mail
            recipients = [e.strip() for e in ecfg.to_email.split(",") if e.strip()]
            if recipients:
                send_mail(subject, body, ecfg.from_email or None, recipients, fail_silently=True)
    except Exception:
        logger.exception("%s email alert failed for %s/%s", kind, server.name, name)
    try:
        scfg = SlackAlertConfig.objects.filter(enabled=True).first()
        if scfg and scfg.webhook_url:
            payload = {"text": f"{emoji} {body}"}
            if scfg.channel:
                payload["channel"] = scfg.channel
            if scfg.username:
                payload["username"] = scfg.username
            if scfg.icon_emoji:
                payload["icon_emoji"] = scfg.icon_emoji
            requests.post(scfg.webhook_url, json=payload, timeout=10)
    except Exception:
        logger.exception("%s slack alert failed for %s/%s", kind, server.name, name)


def evaluate_service_alerts(server):
    """Raise/resolve alerts for this server's MONITORED services based on status.

    A monitored service that is not running raises a (single) triggered
    SERVICE AlertHistory; when it runs again the open alert is resolved. The
    service name is embedded as a `[svc:<name>]` marker so each service maps to
    its own alert.
    """
    config = getattr(server, "monitoring_config", None)
    suppressed = getattr(server, "suppress_alerts", False) or (config and getattr(config, "alert_suppressed", False))
    if config is not None and not getattr(config, "service_failure_alert", True):
        return
    now = timezone.now()
    for svc in Service.objects.filter(server=server, monitoring_enabled=True):
        marker = f"[svc:{svc.name}]"
        open_alert = AlertHistory.objects.filter(
            server=server, alert_type=AlertHistory.AlertType.SERVICE,
            status=AlertHistory.AlertStatus.TRIGGERED, message__contains=marker,
        ).first()
        is_down = svc.status != "running"
        if is_down and not open_alert and not suppressed:
            ecfg = EmailAlertConfig.objects.filter(enabled=True).first()
            AlertHistory.objects.create(
                server=server, alert_type=AlertHistory.AlertType.SERVICE,
                status=AlertHistory.AlertStatus.TRIGGERED, value=0, threshold=1,
                message=f"{marker} Service '{svc.name}' is not running on {server.name}.",
                recipients=(ecfg.to_email if ecfg else "") or "", sent_at=now,
            )
            _notify_unit(server, "service", svc.name, down=True)
        elif (not is_down) and open_alert:
            open_alert.status = AlertHistory.AlertStatus.RESOLVED
            open_alert.resolved_at = now
            open_alert.save(update_fields=["status", "resolved_at"])
            _notify_unit(server, "service", svc.name, down=False)


def evaluate_container_alerts(server):
    """Raise/resolve alerts for this server's MONITORED containers based on state.

    Mirrors evaluate_service_alerts: a monitored container that is not running
    raises a (single) triggered CONTAINER AlertHistory (marker `[ctr:<name>]`);
    when it runs again the open alert is resolved.
    """
    config = getattr(server, "monitoring_config", None)
    suppressed = getattr(server, "suppress_alerts", False) or (config and getattr(config, "alert_suppressed", False))
    now = timezone.now()
    for ctr in Container.objects.filter(server=server, monitoring_enabled=True):
        marker = f"[ctr:{ctr.name}]"
        open_alert = AlertHistory.objects.filter(
            server=server, alert_type=AlertHistory.AlertType.CONTAINER,
            status=AlertHistory.AlertStatus.TRIGGERED, message__contains=marker,
        ).first()
        is_down = ctr.state != "running"
        if is_down and not open_alert and not suppressed:
            ecfg = EmailAlertConfig.objects.filter(enabled=True).first()
            AlertHistory.objects.create(
                server=server, alert_type=AlertHistory.AlertType.CONTAINER,
                status=AlertHistory.AlertStatus.TRIGGERED, value=0, threshold=1,
                message=f"{marker} Container '{ctr.name}' is {ctr.state} on {server.name}.",
                recipients=(ecfg.to_email if ecfg else "") or "", sent_at=now,
            )
            _notify_unit(server, "container", ctr.name, down=True)
        elif (not is_down) and open_alert:
            open_alert.status = AlertHistory.AlertStatus.RESOLVED
            open_alert.resolved_at = now
            open_alert.save(update_fields=["status", "resolved_at"])
            _notify_unit(server, "container", ctr.name, down=False)


@csrf_exempt
@require_http_methods(["POST"])
def agent_ingest_services(request):
    """Receive the list of running services detected by the agent and sync them.

    Body: {"services": [{"name","status","service_type","port","bind_address","process_id"}, ...]}
    Upserts each reported service for the server; auto-detected services that are
    no longer reported are marked 'stopped'.
    """
    cred, err = _authenticate(request)
    if err:
        return err
    server = cred.server

    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "payload too large"}, status=413)
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"error": "request body is not valid JSON"}, status=400)

    agent_version = payload.get("agent_version") if isinstance(payload, dict) else None
    _update_heartbeat(server, agent_version)
    _touch_credential(cred, request)

    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, list):
        return JsonResponse({"error": "missing 'services' list"}, status=400)

    now = timezone.now()
    reported_names = set()
    for item in services:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()[:100]
        if not name:
            continue
        reported_names.add(name)
        port = item.get("port")
        try:
            port = int(port) if port not in (None, "") else None
        except (TypeError, ValueError):
            port = None
        Service.objects.update_or_create(
            server=server,
            name=name,
            defaults={
                "status": (item.get("status") or "running")[:50],
                "service_type": (item.get("service_type") or "systemd")[:50],
                "port": port,
                "bind_address": (item.get("bind_address") or "")[:50] or None,
                "process_id": (str(item.get("process_id")) or "")[:50] or None,
                "last_checked": now,
                "auto_detected": True,
            },
        )

    # Mark previously auto-detected services that are no longer reported as stopped.
    if reported_names:
        (Service.objects
         .filter(server=server, auto_detected=True)
         .exclude(name__in=reported_names)
         .exclude(status="stopped")
         .update(status="stopped", last_checked=now))

    # Raise/resolve alerts for monitored services based on their current status.
    try:
        evaluate_service_alerts(server)
    except Exception:
        logger.exception("Service alert evaluation failed for %s", server.name)

    return JsonResponse({"status": "ok", "received": len(reported_names)})


@csrf_exempt
@require_http_methods(["POST"])
def agent_ingest_containers(request):
    """Receive the list of containers detected by the agent and sync them.

    Body: {"containers": [{"container_id","name","image","state","status_text","ports"}, ...]}
    Upserts by server+name; containers no longer reported are marked 'gone'.
    """
    cred, err = _authenticate(request)
    if err:
        return err
    server = cred.server

    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "payload too large"}, status=413)
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"error": "request body is not valid JSON"}, status=400)

    agent_version = payload.get("agent_version") if isinstance(payload, dict) else None
    _update_heartbeat(server, agent_version)
    _touch_credential(cred, request)

    containers = payload.get("containers") if isinstance(payload, dict) else None
    if not isinstance(containers, list):
        return JsonResponse({"error": "missing 'containers' list"}, status=400)

    now = timezone.now()
    reported = set()
    for item in containers:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()[:200]
        if not name:
            continue
        reported.add(name)
        defaults = {
            "container_id": (item.get("container_id") or "")[:64],
            "runtime": (item.get("runtime") or "docker")[:20],
            "image": (item.get("image") or "")[:300],
            "state": (item.get("state") or "running")[:30],
            "status_text": (item.get("status_text") or "")[:200],
            "ports": (item.get("ports") or "")[:300],
            "last_checked": now,
            "auto_detected": True,
        }
        # Inspect summary rides along only on the slow inspect cycle; persist it
        # only when present so normal pushes don't wipe the last report.
        if isinstance(item.get("inspect"), dict):
            defaults["inspect_data"] = item["inspect"]
            defaults["inspect_at"] = now
        Container.objects.update_or_create(server=server, name=name, defaults=defaults)

    if reported:
        (Container.objects
         .filter(server=server, auto_detected=True)
         .exclude(name__in=reported)
         .exclude(state="gone")
         .update(state="gone", last_checked=now))

    # Raise/resolve alerts for monitored containers based on their current state.
    try:
        evaluate_container_alerts(server)
    except Exception:
        logger.exception("Container alert evaluation failed for %s", server.name)

    return JsonResponse({"status": "ok", "received": len(reported)})


@csrf_exempt
@require_http_methods(["POST"])
def agent_ingest_ssh_auth(request):
    """Receive SSH authentication events observed on the server by the agent.

    Body: {"events": [{"source_ip","username","success","raw"}, ...]}
    Stored as SSHAuthEvent rows (server-stamped time); fed to SSH brute-force
    detection on the next security pass.
    """
    cred, err = _authenticate(request)
    if err:
        return err
    server = cred.server

    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "payload too large"}, status=413)
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"error": "request body is not valid JSON"}, status=400)

    agent_version = payload.get("agent_version") if isinstance(payload, dict) else None
    _update_heartbeat(server, agent_version)
    _touch_credential(cred, request)

    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return JsonResponse({"error": "missing 'events' list"}, status=400)

    now = timezone.now()
    rows = []
    for item in events[:1000]:  # hard cap per push
        if not isinstance(item, dict):
            continue
        ip = (item.get("source_ip") or "").strip()[:64]
        if not ip:
            continue
        rows.append(SSHAuthEvent(
            server=server,
            timestamp=now,
            source_ip=ip,
            username=(item.get("username") or "")[:150],
            success=bool(item.get("success")),
            raw=(item.get("raw") or "")[:300],
        ))
    if rows:
        SSHAuthEvent.objects.bulk_create(rows)

    return JsonResponse({"status": "ok", "received": len(rows)})


def _serve_agent_file(filename, content_type):
    """Serve a file from the agent/ directory as plain text.

    The agent source and installer are not secret (the per-server token is the
    only secret, and it is supplied by the operator at install time), so these
    are intentionally unauthenticated -- like a typical `curl ... | bash`
    installer URL.
    """
    path = os.path.join(settings.BASE_DIR, "agent", filename)
    if not os.path.isfile(path):
        raise Http404("not found")
    with open(path, "r", encoding="utf-8") as f:
        return HttpResponse(f.read(), content_type=content_type)


@require_http_methods(["GET"])
def serve_install_script(request):
    """Serve the VM installer so it can be piped into bash on a monitored VM."""
    return _serve_agent_file("install.sh", "text/x-shellscript; charset=utf-8")


@require_http_methods(["GET"])
def serve_agent_script(request):
    """Serve the agent program, fetched by the installer during setup."""
    return _serve_agent_file("stacksense_agent.py", "text/x-python; charset=utf-8")


@csrf_exempt
@require_http_methods(["GET"])
def agent_ping(request):
    """Connectivity + auth check used by the agent/installer to verify setup."""
    cred, err = _authenticate(request)
    if err:
        return err
    return JsonResponse(
        {
            "status": "ok",
            "server_id": cred.server.id,
            "server_name": cred.server.name,
            "authenticated": True,
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def agent_heartbeat(request):
    """Lightweight authenticated heartbeat ('I'm alive')."""
    cred, err = _authenticate(request)
    if err:
        return err

    agent_version = None
    if request.body:
        try:
            data = json.loads(request.body)
            if isinstance(data, dict):
                agent_version = data.get("agent_version")
        except (ValueError, TypeError):
            pass

    _update_heartbeat(cred.server, agent_version)
    _touch_credential(cred, request)
    return JsonResponse({"status": "ok", "heartbeat_received": True})


@csrf_exempt
@require_http_methods(["POST"])
def agent_ingest_metrics(request):
    """Receive and store a full system-metrics push from the agent."""
    cred, err = _authenticate(request)
    if err:
        return err

    server = cred.server

    # Defensive body-size limit.
    if request.META.get("CONTENT_LENGTH"):
        try:
            if int(request.META["CONTENT_LENGTH"]) > MAX_BODY_BYTES:
                return JsonResponse({"error": "payload too large"}, status=413)
        except (TypeError, ValueError):
            pass
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "payload too large"}, status=413)

    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"error": "request body is not valid JSON"}, status=400)

    agent_version = None
    if isinstance(payload, dict):
        agent_version = payload.get("agent_version")

    # Always treat the token as a sign of life, even if monitoring is paused.
    _update_heartbeat(server, agent_version)
    _touch_credential(cred, request)

    # Respect per-server monitoring suspension: acknowledge but don't store.
    config = getattr(server, "monitoring_config", None)
    if config is not None and getattr(config, "monitoring_suspended", False):
        return JsonResponse(
            {"status": "ok", "stored": False, "reason": "monitoring suspended"}
        )

    kwargs, verr = _coerce_metrics(payload)
    if verr:
        return JsonResponse({"error": verr}, status=400)

    # Stamp the timestamp server-side so agents cannot backdate/forward-date data.
    metric = SystemMetric.objects.create(
        server=server, timestamp=timezone.now(), **kwargs
    )

    logger.info("Agent metrics stored for %s (metric_id=%s)", server.name, metric.id)
    return JsonResponse({"status": "ok", "stored": True, "metric_id": metric.id})
