"""
Role-based alert routing tests (Phase 2). Verifies recipients_for() resolves the right
people from the (category, severity) of an alert, honours the per-(role,category)
minimum severity, and that the seeded defaults deliver the agreed policy -- in
particular that the CEO is NOT emailed for routine operational alerts.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from core import alert_routing
from core.models import Role, UserACL, AlertRoutingRule
from core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_CEO


class RoutingResolverTests(TestCase):
    def setUp(self):
        # Roles may already be seeded by an RBAC migration -- fetch or create.
        self.admin_role, _ = Role.objects.get_or_create(name=ROLE_ADMIN)
        self.op_role, _ = Role.objects.get_or_create(name=ROLE_OPERATOR)
        self.ceo_role, _ = Role.objects.get_or_create(name=ROLE_CEO)
        alert_routing.ensure_default_rules()  # idempotently ensure the role-tailored matrix

        self.admin = self._user("rt_admin", "admin@x.test", self.admin_role)
        self.op = self._user("rt_op", "op@x.test", self.op_role)
        self.ceo = self._user("rt_ceo", "ceo@x.test", self.ceo_role)

    def _user(self, username, email, role):
        u = User.objects.create(username=username, email=email, is_active=True)
        UserACL.objects.update_or_create(user=u, defaults={"role": role})
        return u

    def _emails(self, category, severity):
        return set(alert_routing.recipients_for(category, severity))

    # --- the agreed default policy -------------------------------------------------
    def test_resource_high_goes_to_admin_and_operator_not_ceo(self):
        self.assertEqual(self._emails("resource", "HIGH"), {"admin@x.test", "op@x.test"})

    def test_availability_critical_includes_ceo(self):
        self.assertEqual(self._emails("availability", "CRITICAL"),
                         {"admin@x.test", "op@x.test", "ceo@x.test"})

    def test_availability_low_excludes_ceo(self):
        # CEO is Availability=CRITICAL only -> a LOW (recovered) availability alert skips them.
        self.assertEqual(self._emails("availability", "LOW"), {"admin@x.test", "op@x.test"})

    def test_business_goes_to_admin_and_ceo_not_operator(self):
        self.assertEqual(self._emails("business", "LOW"), {"admin@x.test", "ceo@x.test"})

    def test_security_goes_to_admin_and_operator_not_ceo(self):
        self.assertEqual(self._emails("security", "HIGH"), {"admin@x.test", "op@x.test"})

    # --- severity threshold semantics ---------------------------------------------
    def test_min_severity_is_inclusive_and_filters_below(self):
        # Make Operator only care about CRITICAL resource alerts.
        AlertRoutingRule.objects.filter(role=self.op_role, category="resource").update(
            min_severity="CRITICAL")
        self.assertNotIn("op@x.test", self._emails("resource", "HIGH"))   # HIGH < CRITICAL
        self.assertIn("op@x.test", self._emails("resource", "CRITICAL"))  # CRITICAL == CRITICAL

    def test_off_means_never(self):
        self.assertEqual(self._emails("resource", "CRITICAL") & {"ceo@x.test"}, set())

    # --- recipient hygiene ---------------------------------------------------------
    def test_inactive_user_excluded(self):
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        self.assertNotIn("admin@x.test", self._emails("resource", "HIGH"))

    def test_user_without_email_excluded(self):
        self._user("op2", "", self.op_role)  # no email
        result = alert_routing.recipients_for("resource", "HIGH")
        self.assertNotIn("", result)

    def test_dedup_case_insensitive(self):
        self._user("admin2", "ADMIN@x.test", self.admin_role)  # same address, different case
        result = alert_routing.recipients_for("resource", "HIGH")
        lowered = [e.lower() for e in result]
        self.assertEqual(len(lowered), len(set(lowered)))  # no duplicate addresses

    def test_unknown_category_returns_empty(self):
        self.assertEqual(alert_routing.recipients_for("nonsense", "HIGH"), [])

    def test_user_with_no_role_is_not_routed(self):
        u = User.objects.create(username="rr_norole", email="norole@x.test", is_active=True)
        UserACL.objects.update_or_create(user=u, defaults={"role": None})
        self.assertNotIn("norole@x.test", self._emails("resource", "HIGH"))

    def test_superuser_without_acl_receives_nothing(self):
        # Routing is purely role-based: a superuser with NO UserACL/role is invisible to
        # it and gets no alerts. Documents a real footgun (assign such accounts a role).
        User.objects.create_superuser("rr_super", "super@x.test", "pw")  # no UserACL
        reached = set()
        for cat in ("resource", "availability", "security", "capacity", "business"):
            reached |= set(alert_routing.recipients_for(cat, "CRITICAL"))
        self.assertNotIn("super@x.test", reached)


class DefaultSeedingTests(TestCase):
    def test_ensure_default_rules_is_idempotent_and_non_destructive(self):
        admin, _ = Role.objects.get_or_create(name=ROLE_ADMIN)
        Role.objects.get_or_create(name=ROLE_OPERATOR)
        Role.objects.get_or_create(name=ROLE_CEO)
        alert_routing.ensure_default_rules()
        # Each built-in role has exactly one cell per category (5).
        for name in (ROLE_ADMIN, ROLE_OPERATOR, ROLE_CEO):
            self.assertEqual(
                AlertRoutingRule.objects.filter(role__name=name).count(), 5)
        first_count = AlertRoutingRule.objects.count()

        # An admin edits one cell; re-seeding must NOT revert it or add duplicates.
        AlertRoutingRule.objects.filter(role=admin, category="resource").update(
            min_severity="CRITICAL")
        alert_routing.ensure_default_rules()
        self.assertEqual(AlertRoutingRule.objects.count(), first_count)
        self.assertEqual(
            AlertRoutingRule.objects.get(role=admin, category="resource").min_severity,
            "CRITICAL")
