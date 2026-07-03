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

    # License status: drives the Pro-only nav gating AND the app-wide banner.
    # Computed once here (verification is cached) so no page pays for it twice.
    license_features = frozenset()
    license_status = None
    license_banner = None
    if user is not None and getattr(user, "is_authenticated", False):
        try:
            from . import licensing
            st = licensing.current_license()
            license_status = st
            if st.state in ("none", "trial"):   # eval + active trial unlock everything
                license_features = frozenset({"windows", "executive", "ai", "security", "business"})
            elif st.info:
                license_features = frozenset(st.info.features)
            license_banner = _license_banner(st, perms.MANAGE_LICENSE in caps)
        except Exception:
            license_features = frozenset()

    return {
        "rbac_caps": caps,
        "is_impersonating": is_impersonating,
        "impersonated_user": request.user if is_impersonating else None,
        "real_user": getattr(request, "real_user", None),
        "impersonatable_users": impersonatable,
        "license_features": license_features,
        "license_status": license_status,
        "license_banner": license_banner,
    }


def _license_banner(st, is_license_admin):
    """Pick the single most important license banner for the current state (or None).

    Severity order: read-only/invalid (everyone) > grace > over-limit > expiring >
    node-mismatch > evaluation (admins only, so it doesn't nag every operator)."""
    if st.state == "expired":
        return {"level": "error",
                "msg": "Your StackSense license has expired — the app is in read-only mode. "
                       "Monitoring data still flows in, but changes are blocked until a "
                       "renewed license is installed."}
    if st.state == "trial_expired":
        return {"level": "error",
                "msg": "Your trial has ended — the app is in read-only mode. Monitoring data "
                       "still flows in, but changes are blocked until a license is installed."}
    if st.state == "invalid":
        return {"level": "error",
                "msg": "Your StackSense license is invalid — install a valid license to "
                       "restore full access."}
    if st.state == "expired_grace":
        n = st.grace_left or 0
        return {"level": "warning",
                "msg": f"Your StackSense license has expired — the app becomes read-only in "
                       f"{n} day{'' if n == 1 else 's'}. Renew now to avoid interruption."}
    if st.over_limit:
        return {"level": "warning",
                "msg": f"Server limit exceeded ({st.server_count}/{st.max_servers}). "
                       "Remove a server or upgrade your plan."}
    if st.state == "expiring":
        n = st.days_left or 0
        return {"level": "warning",
                "msg": f"Your StackSense license expires in {n} day{'' if n == 1 else 's'} — "
                       "renew soon to avoid interruption."}
    if st.state == "trial":
        n = st.days_left or 0
        return {"level": "warning",
                "msg": f"Trial: {n} day{'' if n == 1 else 's'} left. Install a license before it "
                       "ends to keep full access."}
    if st.node_mismatch:
        return {"level": "warning",
                "msg": "This license is bound to a different installation — it still works, "
                       "but ask the vendor to re-issue it for this install."}
    if st.state == "none" and is_license_admin:
        return {"level": "info",
                "msg": "Evaluation mode — no license installed. All features are unlocked "
                       "for evaluation."}
    return None
