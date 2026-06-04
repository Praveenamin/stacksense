"""
RBAC view decorator — defense-in-depth on the most sensitive views.

The RBACMiddleware already authorizes every route centrally; this decorator
makes the requirement explicit at the view and guards even if the route were
ever removed from the central map. Both read the same permission helpers.
"""
from __future__ import annotations

from functools import wraps

from django.http import JsonResponse, HttpResponseForbidden

from . import permissions as perms


def require_capability(capability):
    """Require `capability` (resolved server-side) to enter the view."""
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if not user or not user.is_authenticated or not user.is_staff:
                if request.path.startswith("/api/"):
                    return JsonResponse({"error": "authentication required"}, status=401)
                return HttpResponseForbidden("403 — Access denied")
            if not perms.user_can(user, capability):
                if request.path.startswith("/api/"):
                    return JsonResponse({"error": "permission denied"}, status=403)
                return HttpResponseForbidden("403 — Access denied")
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator
