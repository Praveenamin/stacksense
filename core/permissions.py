"""
Central RBAC definition — the single source of truth for capabilities, the
role→capability matrix, the route→capability map, and the resolution helpers.

Server middleware/decorators AND the UI all read from here. Roles remain
editable in the DB (Role/Privilege); this module seeds and interprets them.

Security model: deny-by-default. Role is resolved server-side from the verified
session user; a client can never supply it.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Capability vocabulary
# ---------------------------------------------------------------------------
VIEW_OPERATIONS = "view_operations"
VIEW_EXECUTIVE = "view_executive"
MANAGE_MONITORING = "manage_monitoring"   # servers/services/containers, thresholds, suppress
MANAGE_ALERTS = "manage_alerts"           # alert + slack config, resolve, synthetic checks
MANAGE_SECURITY = "manage_security"       # security/SIEM
MANAGE_BUSINESS = "manage_business"       # business KPIs
MANAGE_USERS = "manage_users"
MANAGE_ROLES = "manage_roles"
IMPERSONATE = "impersonate"

ALL_CAPABILITIES = frozenset({
    VIEW_OPERATIONS, VIEW_EXECUTIVE, MANAGE_MONITORING, MANAGE_ALERTS,
    MANAGE_SECURITY, MANAGE_BUSINESS, MANAGE_USERS,
    MANAGE_ROLES, IMPERSONATE,
})

# Capability labels (for the role-editor UI / seeding)
CAPABILITY_LABELS = {
    VIEW_OPERATIONS: "View Operations Dashboard",
    VIEW_EXECUTIVE: "View Executive Dashboard",
    MANAGE_MONITORING: "Manage Monitoring (servers, services, containers, thresholds)",
    MANAGE_ALERTS: "Manage Alerts & Synthetic Checks",
    MANAGE_SECURITY: "Manage Security / SIEM",
    MANAGE_BUSINESS: "Manage Business KPIs",
    MANAGE_USERS: "Manage Users",
    MANAGE_ROLES: "Manage Roles",
    IMPERSONATE: "Switch Between User Accounts",
}

# ---------------------------------------------------------------------------
# Roles → capabilities (default matrix; seeds the editable DB roles)
# ---------------------------------------------------------------------------
ROLE_ADMIN = "Admin"
ROLE_CEO = "CEO"
ROLE_OPERATOR = "Operator"

# CEO: everything except user & role administration AND impersonation
# (all Admin-only). CEO keeps operational/business management; defaults to Executive.
CEO_CAPABILITIES = ALL_CAPABILITIES - {MANAGE_USERS, MANAGE_ROLES, IMPERSONATE}

ROLE_CAPABILITIES = {
    ROLE_ADMIN: ALL_CAPABILITIES,
    ROLE_CEO: CEO_CAPABILITIES,
    ROLE_OPERATOR: frozenset({VIEW_OPERATIONS}),
}
PROTECTED_ROLES = {ROLE_ADMIN, ROLE_CEO, ROLE_OPERATOR}

# Default landing page per role.
LANDING_OPERATIONS = "operations"
LANDING_EXECUTIVE = "executive"
ROLE_LANDING = {
    ROLE_ADMIN: LANDING_OPERATIONS,
    ROLE_CEO: LANDING_EXECUTIVE,
    ROLE_OPERATOR: LANDING_OPERATIONS,
}

# ---------------------------------------------------------------------------
# Route → required capability
# ---------------------------------------------------------------------------
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Routes that bypass RBAC entirely (no auth or machine-auth).
PUBLIC_URL_NAMES = frozenset({
    "health", "ready", "logout",
    "password_reset", "password_reset_done",
    "password_reset_confirm", "password_reset_complete",
    # token-authenticated agent / KPI ingest + public installer
    "agent_ping", "agent_heartbeat", "agent_ingest_metrics",
    "agent_ingest_services", "agent_ingest_containers", "agent_ingest_ssh_auth",
    "kpi_ingest", "agent_install_script", "agent_script",
})

# Self-service routes: require authentication (staff) but no capability — every
# signed-in user may manage their own account (e.g. change their own password).
SELF_SERVICE_URL_NAMES = frozenset({"account_password"})

# Unmapped GET → VIEW_OPERATIONS; unmapped mutation → this (managers only) + a
# logged warning so we can catch routes we forgot to map. Never world-open.
WRITE_FALLBACK_CAPABILITY = MANAGE_MONITORING

CAPABILITY_BY_URL_NAME = {
    # Reports
    "operations_report": VIEW_OPERATIONS,
    "executive_report": VIEW_EXECUTIVE,
    # Executive
    "executive_dashboard_preview": VIEW_EXECUTIVE,
    # Persona switch is a per-user preference (executive choice re-checked in-view)
    "set_dashboard_view": VIEW_OPERATIONS,

    # Monitoring (servers / services / containers / thresholds / suppress)
    "add_server": MANAGE_MONITORING, "add_server_agent": MANAGE_MONITORING,
    "add_server_api": MANAGE_MONITORING, "regenerate_agent_token": MANAGE_MONITORING,
    "edit_server": MANAGE_MONITORING, "delete_server": MANAGE_MONITORING,
    "remove_server": MANAGE_MONITORING,
    "toggle_container_monitoring": MANAGE_MONITORING,
    "toggle_service_monitoring": MANAGE_MONITORING,
    "update_monitored_services": MANAGE_MONITORING,
    "update_service_thresholds": MANAGE_MONITORING,
    "update_monitored_disks": MANAGE_MONITORING,
    "save_monitored_disks": MANAGE_MONITORING,
    "update_thresholds": MANAGE_MONITORING,
    "toggle_alert_suppression": MANAGE_MONITORING,
    "toggle_monitoring": MANAGE_MONITORING,
    "server_slo_config_api": MANAGE_MONITORING,
    "server_slo_config_delete_api": MANAGE_MONITORING,

    # Alerts / synthetic
    "alert_config": MANAGE_ALERTS, "save_alert_config": MANAGE_ALERTS,
    "clear_alert_config": MANAGE_ALERTS, "test_alert_config": MANAGE_ALERTS,
    "slack_config": MANAGE_ALERTS, "save_slack_config": MANAGE_ALERTS,
    "clear_slack_config": MANAGE_ALERTS, "test_slack_config": MANAGE_ALERTS,
    "disk_alerts_config_api": MANAGE_ALERTS, "save_disk_alerts_config": MANAGE_ALERTS,
    "service_alerts_config_api": MANAGE_ALERTS, "server_services_api": MANAGE_ALERTS,
    "save_service_monitoring": MANAGE_ALERTS,
    "resolve_alert": MANAGE_ALERTS, "bulk_resolve_alerts": MANAGE_ALERTS,
    "bulk_delete_alerts": MANAGE_ALERTS,
    "anomaly_resolve_api": MANAGE_ALERTS, "anomaly_bulk_resolve_api": MANAGE_ALERTS,
    "synthetic_check_add": MANAGE_ALERTS, "synthetic_check_edit": MANAGE_ALERTS,
    "synthetic_check_delete": MANAGE_ALERTS, "synthetic_check_run": MANAGE_ALERTS,

    # Security
    "security_dashboard": MANAGE_SECURITY, "security_event_update": MANAGE_SECURITY,
    "security_run_now": MANAGE_SECURITY,

    # Business KPIs
    "business_dashboard": MANAGE_BUSINESS, "business_kpi_add": MANAGE_BUSINESS,
    "business_regenerate_token": MANAGE_BUSINESS, "business_kpi_detail": MANAGE_BUSINESS,
    "business_kpi_edit": MANAGE_BUSINESS, "business_kpi_record": MANAGE_BUSINESS,
    "business_kpi_delete": MANAGE_BUSINESS,

    # Pricing

    # Application settings (timezone/language/retention)
    "app_config": MANAGE_MONITORING,
    "app_config_legacy": MANAGE_MONITORING,

    # Users & roles
    "admin_users": MANAGE_USERS, "create_admin_user": MANAGE_USERS,
    "edit_admin_user": MANAGE_USERS, "delete_admin_user": MANAGE_USERS,
    "admin_users_api": MANAGE_USERS, "create_admin_user_api": MANAGE_USERS,
    "admin_user_api": MANAGE_USERS,
    "role_management": MANAGE_ROLES, "create_role": MANAGE_ROLES,
    "edit_role": MANAGE_ROLES, "delete_role": MANAGE_ROLES,

    # Impersonation: starting requires the capability; exiting must be reachable
    # by the (lower-privilege) impersonated session, so only needs view_operations.
    "impersonate_start": IMPERSONATE,
    "impersonate_exit": VIEW_OPERATIONS,
    # Post-login dispatcher — any authenticated staff.
    "home_redirect": VIEW_OPERATIONS,
}


def required_capability_for(url_name, method):
    """Return the capability a route needs, or None if public/exempt."""
    if url_name in PUBLIC_URL_NAMES:
        return None
    cap = CAPABILITY_BY_URL_NAME.get(url_name)
    if cap is not None:
        return cap
    if method in SAFE_METHODS:
        return VIEW_OPERATIONS
    return WRITE_FALLBACK_CAPABILITY


# ---------------------------------------------------------------------------
# Resolution helpers (server-side; never trust client input)
# ---------------------------------------------------------------------------
def effective_capabilities(user):
    """Capabilities for the given (already server-resolved) user. Deny-by-default:
    unauthenticated or role-less → no capabilities. Superuser → all (Admin),
    unless they are being impersonated down (handled by swapping request.user)."""
    if not user or not getattr(user, "is_authenticated", False):
        return frozenset()
    if getattr(user, "is_superuser", False):
        return ALL_CAPABILITIES
    acl = getattr(user, "acl", None)
    if acl is None or acl.role_id is None:
        return frozenset()
    keys = set(acl.role.role_privileges.values_list("privilege__key", flat=True))
    return frozenset(keys) & ALL_CAPABILITIES


def user_can(user, capability):
    return capability in effective_capabilities(user)


def can_be_impersonated(user):
    """A valid impersonation target: a strictly lower-privilege account — never a
    superuser, never an Admin/CEO role, never anyone who can impersonate."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return False
    acl = getattr(user, "acl", None)
    role_name = acl.role.name if (acl and acl.role) else None
    if role_name in (ROLE_ADMIN, ROLE_CEO):
        return False
    if IMPERSONATE in effective_capabilities(user):
        return False
    return True


def default_landing_for(user):
    """Landing page key ('operations'/'executive') for a user, from their role."""
    if not user or not getattr(user, "is_authenticated", False):
        return LANDING_OPERATIONS
    if getattr(user, "is_superuser", False):
        return LANDING_OPERATIONS
    acl = getattr(user, "acl", None)
    role_name = acl.role.name if (acl and acl.role) else None
    return ROLE_LANDING.get(role_name, LANDING_OPERATIONS)


# ---------------------------------------------------------------------------
# Seeding (idempotent) — creates Privileges + the three roles from the matrix
# ---------------------------------------------------------------------------
def sync_roles():
    """Create/update Privilege rows and the Admin/CEO/Operator roles to match the
    central matrix. Idempotent. Returns a short summary string."""
    from .models import Privilege, Role, RolePrivilege

    for key in ALL_CAPABILITIES:
        Privilege.objects.get_or_create(
            key=key, defaults={"label": CAPABILITY_LABELS.get(key, key)})

    for role_name, caps in ROLE_CAPABILITIES.items():
        role, _ = Role.objects.get_or_create(
            name=role_name,
            defaults={"is_protected": role_name in PROTECTED_ROLES,
                      "description": f"{role_name} role (managed by RBAC matrix)"})
        if not role.is_protected and role_name in PROTECTED_ROLES:
            role.is_protected = True
            role.save(update_fields=["is_protected"])
        wanted = set(caps)
        for key in wanted:
            priv = Privilege.objects.get(key=key)
            RolePrivilege.objects.get_or_create(role=role, privilege=priv)
        # Drop any privileges not in the wanted set (keep roles in sync).
        RolePrivilege.objects.filter(role=role).exclude(
            privilege__key__in=wanted).delete()

    return f"Synced {len(ALL_CAPABILITIES)} capabilities, {len(ROLE_CAPABILITIES)} roles"
