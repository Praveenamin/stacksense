"""
RBAC enforcement middleware — the real security boundary.

Runs after AuthenticationMiddleware. For every resolved route it computes the
required capability (deny-by-default) and authorizes the *server-resolved*
user. UI gating is convenience only; this is what actually protects the app.

Denied requests are logged (Phase 3 wires these into the AuditLog model).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse_lazy

from . import permissions as perms

logger = logging.getLogger("rbac")

# Paths that have their own auth and must not be gated here.
_SKIP_PREFIXES = ("/admin/", "/static/", "/media/")


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _is_api(request):
    return request.path.startswith("/api/")


def audit_denied(request, required, user):
    """Record a denied request. Phase 2: structured log. Phase 3: + AuditLog."""
    logger.warning(
        "RBAC DENIED actor=%s staff=%s method=%s path=%s url_name=%s required=%s ip=%s",
        getattr(user, "username", None) or "anonymous",
        getattr(user, "is_staff", False),
        request.method,
        request.path,
        getattr(getattr(request, "resolver_match", None), "url_name", None),
        required,
        _client_ip(request),
    )


class RBACMiddleware:
    """Capability-based, deny-by-default authorization for every core route."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        path = request.path

        # Own-auth / static surfaces are out of scope for app RBAC.
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return None

        match = getattr(request, "resolver_match", None)
        url_name = match.url_name if match else None
        if url_name is None:
            return None  # unresolved -> let Django 404 it

        if url_name in perms.PUBLIC_URL_NAMES:
            return None

        user = getattr(request, "user", None)

        # Must be an authenticated staff user to reach any app route.
        if not user or not user.is_authenticated:
            if _is_api(request):
                return JsonResponse({"error": "authentication required"}, status=401)
            login_url = str(getattr(settings, "LOGIN_URL", reverse_lazy("admin:login")))
            return redirect(f"{login_url}?next={path}")

        if not user.is_staff:
            audit_denied(request, "staff", user)
            return self._deny(request)

        required = perms.required_capability_for(url_name, request.method)
        if required is None:
            return None

        # Surface routes that fell through to the write fallback so we can map them.
        if (required == perms.WRITE_FALLBACK_CAPABILITY
                and url_name not in perms.CAPABILITY_BY_URL_NAME
                and request.method not in perms.SAFE_METHODS):
            logger.warning("RBAC unmapped mutation url_name=%s path=%s -> requires %s",
                           url_name, path, required)

        if not perms.user_can(user, required):
            audit_denied(request, required, user)
            return self._deny(request)

        return None

    def _deny(self, request):
        if _is_api(request):
            return JsonResponse({"error": "permission denied"}, status=403)
        return HttpResponseForbidden(
            "<h1>403 — Access denied</h1>"
            "<p>You don't have permission to access this page.</p>"
            "<p><a href=\"/\">Return to dashboard</a></p>"
        )
