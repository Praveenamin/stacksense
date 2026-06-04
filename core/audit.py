"""
Audit helper — writes security-relevant events to the AuditLog model.

Centralized so middleware and views record entries the same way. Never raises
into the request path (audit failure must not break the app).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("rbac.audit")

ALLOWED = "allowed"
DENIED = "denied"


def client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def record(actor, action, *, resource="", method="", result=ALLOWED, ip=None, target=None):
    """Write one audit row. `actor` = the real user; `target` = impersonated user."""
    from .models import AuditLog
    try:
        AuditLog.objects.create(
            actor=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
            impersonated_target=target,
            action=action,
            resource=(resource or "")[:255],
            method=(method or "")[:10],
            result=result,
            ip_address=ip,
        )
    except Exception:  # pragma: no cover - audit must never break the request
        logger.exception("Failed to write AuditLog (action=%s resource=%s)", action, resource)
