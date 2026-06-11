"""
Role-based alert routing (Phase 2). Resolves WHO gets emailed for a given alert,
from the (category, severity) the alert carries -- replacing the single
EmailAlertConfig.to_email recipient.

A routing rule is one (Role x Category) cell holding a *minimum severity*. A user
receives an alert when their role's rule for that alert's category is set at or below
the alert's severity (OFF = never). Recipients are the email addresses of the active
users whose role qualifies.
"""
from django.contrib.auth import get_user_model

from .alert_categories import AlertCategory
from .permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_CEO


# Severity ladder used to compare an alert's severity against a rule's minimum.
SEV_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

# Role-tailored defaults (user-approved). Value = minimum severity, or "OFF".
# Admin = everything; Operator = all operational but not Business;
# CEO = Business (all) + Availability only when Critical (server down).
_DEFAULT_MATRIX = {
    ROLE_ADMIN: {
        AlertCategory.RESOURCE: "LOW", AlertCategory.AVAILABILITY: "LOW",
        AlertCategory.SECURITY: "LOW", AlertCategory.CAPACITY: "LOW",
        AlertCategory.BUSINESS: "LOW",
    },
    ROLE_OPERATOR: {
        AlertCategory.RESOURCE: "LOW", AlertCategory.AVAILABILITY: "LOW",
        AlertCategory.SECURITY: "LOW", AlertCategory.CAPACITY: "LOW",
        AlertCategory.BUSINESS: "OFF",
    },
    ROLE_CEO: {
        AlertCategory.RESOURCE: "OFF", AlertCategory.AVAILABILITY: "CRITICAL",
        AlertCategory.SECURITY: "OFF", AlertCategory.CAPACITY: "OFF",
        AlertCategory.BUSINESS: "LOW",
    },
}


def ensure_default_rules():
    """Idempotently create the routing matrix for the three built-in roles. Only fills
    in cells that don't exist yet (never overwrites an admin's edits). Safe to call on
    every page load."""
    from .models import Role, AlertRoutingRule  # local import: avoids app-loading cycle
    for role_name, cells in _DEFAULT_MATRIX.items():
        role = Role.objects.filter(name=role_name).first()
        if not role:
            continue
        for category, min_sev in cells.items():
            AlertRoutingRule.objects.get_or_create(
                role=role, category=category,
                defaults={"min_severity": min_sev})


def recipients_for(category, severity):
    """Email addresses that should receive an alert of this (category, severity).

    Returns a de-duplicated list (case-insensitive) of the active users whose role's
    rule for `category` is set at or below `severity`. Empty list = nobody is routed
    this alert (caller should simply not send an email)."""
    from .models import AlertRoutingRule  # local import: keeps module import-light

    category = (category or "").strip().lower()
    rank = SEV_RANK.get((severity or "").strip().upper(), SEV_RANK["LOW"])

    rules = (AlertRoutingRule.objects
             .filter(category=category)
             .exclude(min_severity=AlertRoutingRule.OFF)
             .select_related("role"))
    role_ids = [r.role_id for r in rules if SEV_RANK.get(r.min_severity, 99) <= rank]
    if not role_ids:
        return []

    User = get_user_model()
    emails = (User.objects
              .filter(acl__role_id__in=role_ids, is_active=True)
              .exclude(email__isnull=True).exclude(email__exact="")
              .values_list("email", flat=True))

    seen, out = set(), []
    for e in emails:
        e = (e or "").strip()
        key = e.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(e)
    return out
