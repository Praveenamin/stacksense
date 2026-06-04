"""
Template context for RBAC: effective capabilities + impersonation state.

The UI reads capabilities from the SAME source as the server (permissions.py),
so affordances never diverge from what's actually enforced.
"""
from . import permissions as perms


def rbac(request):
    user = getattr(request, "user", None)
    caps = perms.effective_capabilities(user) if user is not None else frozenset()
    is_impersonating = getattr(request, "impersonating", False)

    # Users the current actor may switch into (for the top-right account menu).
    # Only Admin/CEO (impersonate cap) and only lower-privilege targets.
    impersonatable = []
    if (user is not None and getattr(user, "is_authenticated", False)
            and not is_impersonating and perms.IMPERSONATE in caps):
        from django.contrib.auth.models import User
        impersonatable = list(
            User.objects.filter(is_staff=True, is_active=True, acl__role__isnull=False)
            .exclude(id=user.id).exclude(is_superuser=True)
            .exclude(acl__role__name__in=[perms.ROLE_ADMIN, perms.ROLE_CEO])
            .select_related("acl__role")
            .order_by("username")[:50]
        )

    return {
        "rbac_caps": caps,
        "is_impersonating": is_impersonating,
        "impersonated_user": request.user if is_impersonating else None,
        "real_user": getattr(request, "real_user", None),
        "impersonatable_users": impersonatable,
    }
