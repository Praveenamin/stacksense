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
    """Record a denied request to the log and the AuditLog table.

    Under impersonation the real actor is preserved and the target recorded."""
    url_name = getattr(getattr(request, "resolver_match", None), "url_name", None)
    logger.warning(
        "RBAC DENIED actor=%s staff=%s method=%s path=%s url_name=%s required=%s ip=%s",
        getattr(user, "username", None) or "anonymous",
        getattr(user, "is_staff", False),
        request.method, request.path, url_name, required, _client_ip(request),
    )
    real = getattr(request, "real_user", None)
    target = user if getattr(request, "impersonating", False) else None
    from . import audit
    audit.record(real or user, f"denied:{url_name or request.path}",
                 resource=request.path, method=request.method,
                 result=audit.DENIED, ip=_client_ip(request), target=target)


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
        from django.shortcuts import render
        try:
            return render(request, "403.html", status=403)
        except Exception:
            return HttpResponseForbidden("403 — Access denied")


class ImpersonationMiddleware:
    """If the session carries a validated impersonation target, swap
    request.user → target for this request and stash the real actor in
    request.real_user. Must run AFTER AuthenticationMiddleware and BEFORE
    RBACMiddleware so authorization uses the (lower-privilege) target — the
    impersonator can never escalate.
    """
    SESSION_KEY = "impersonate_user_id"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.real_user = None
        request.impersonating = False

        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            target_id = request.session.get(self.SESSION_KEY)
            if target_id:
                target = self._resolve_target(target_id)
                # Defensive: never impersonate a peer/privileged account.
                if target is None or perms.user_can(target, perms.IMPERSONATE):
                    request.session.pop(self.SESSION_KEY, None)
                else:
                    request.real_user = user
                    request.user = target
                    request.impersonating = True

        return self.get_response(request)

    @staticmethod
    def _resolve_target(target_id):
        from django.contrib.auth.models import User
        return User.objects.filter(id=target_id, is_staff=True, is_active=True).first()
