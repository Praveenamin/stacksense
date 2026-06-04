"""
Template context for RBAC: effective capabilities + impersonation state.

The UI reads capabilities from the SAME source as the server (permissions.py),
so affordances never diverge from what's actually enforced.
"""
from . import permissions as perms


def rbac(request):
    user = getattr(request, "user", None)
    caps = perms.effective_capabilities(user) if user is not None else frozenset()
    return {
        "rbac_caps": caps,
        "is_impersonating": getattr(request, "impersonating", False),
        "impersonated_user": request.user if getattr(request, "impersonating", False) else None,
        "real_user": getattr(request, "real_user", None),
    }
