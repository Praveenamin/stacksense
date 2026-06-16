"""
RBAC server-side enforcement tests (Phase 2).

Validates the deny-by-default middleware across role × endpoint, including
denied cases, unauthenticated, and unknown-role. Run:
    python manage.py test core.test_rbac
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core import permissions as perms
from core.models import Privilege, Role, UserACL


class RBACTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        perms.sync_roles()
        cls.admin = User.objects.create_superuser("rbac_admin", "a@x.com", "pw")
        cls.ceo = cls._staff("rbac_ceo", perms.ROLE_CEO)
        cls.operator = cls._staff("rbac_op", perms.ROLE_OPERATOR)
        cls.norole = cls._staff("rbac_norole", None)

    @staticmethod
    def _staff(username, role_name):
        u = User.objects.create_user(username, f"{username}@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(u)
        acl.role = Role.objects.get(name=role_name) if role_name else None
        acl.save()
        return u

    def _get(self, user, url_name, **kw):
        if user:
            self.client.force_login(user)
        else:
            self.client.logout()
        return self.client.get(reverse(url_name, kwargs=kw) if kw else reverse(url_name))


class CapabilityResolutionTests(TestCase):
    """Unit-level checks of the central resolution helpers (no HTTP)."""
    @classmethod
    def setUpTestData(cls):
        perms.sync_roles()

    def test_superuser_has_all(self):
        u = User.objects.create_superuser("su", "s@x.com", "pw")
        self.assertEqual(perms.effective_capabilities(u), perms.ALL_CAPABILITIES)

    def test_operator_is_view_only(self):
        u = User.objects.create_user("op", "o@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(u)
        acl.role = Role.objects.get(name=perms.ROLE_OPERATOR); acl.save()
        caps = perms.effective_capabilities(u)
        self.assertEqual(caps, frozenset({perms.VIEW_OPERATIONS}))
        self.assertFalse(perms.user_can(u, perms.VIEW_EXECUTIVE))
        self.assertFalse(perms.user_can(u, perms.MANAGE_MONITORING))

    def test_ceo_caps_exclude_user_and_role_admin(self):
        u = User.objects.create_user("ceo", "c@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(u)
        acl.role = Role.objects.get(name=perms.ROLE_CEO); acl.save()
        caps = perms.effective_capabilities(u)
        # CEO is not a user/role admin and cannot impersonate (all Admin-only).
        self.assertNotIn(perms.MANAGE_USERS, caps)
        self.assertNotIn(perms.MANAGE_ROLES, caps)
        self.assertNotIn(perms.IMPERSONATE, caps)
        # but keeps executive + operational/business management
        for c in (perms.VIEW_EXECUTIVE, perms.MANAGE_MONITORING,
                  perms.MANAGE_BUSINESS):
            self.assertIn(c, caps)

    def test_no_role_denied_everything(self):
        # New staff default to Operator; a deliberately role-less ACL must still
        # resolve to no capabilities (deny-by-default invariant).
        u = User.objects.create_user("nr", "n@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(u)
        acl.role = None
        acl.save()
        self.assertEqual(perms.effective_capabilities(u), frozenset())

    def test_new_staff_defaults_to_operator(self):
        u = User.objects.create_user("fresh", "f@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(u)
        self.assertEqual(acl.role.name, perms.ROLE_OPERATOR)
        self.assertEqual(perms.effective_capabilities(u), frozenset({perms.VIEW_OPERATIONS}))

    def test_landing_pages(self):
        self.assertEqual(perms.ROLE_LANDING[perms.ROLE_CEO], perms.LANDING_EXECUTIVE)
        self.assertEqual(perms.ROLE_LANDING[perms.ROLE_OPERATOR], perms.LANDING_OPERATIONS)

    def test_unmapped_mutation_is_managers_only(self):
        # A made-up mutation route name falls back to a manage capability, never view.
        self.assertEqual(
            perms.required_capability_for("some_new_unmapped_route", "POST"),
            perms.WRITE_FALLBACK_CAPABILITY)
        self.assertNotEqual(perms.WRITE_FALLBACK_CAPABILITY, perms.VIEW_OPERATIONS)


class ViewAccessTests(RBACTestBase):
    def test_operations_dashboard_all_staff_with_view(self):
        self.assertEqual(self._get(self.admin, "monitoring_dashboard").status_code, 200)
        self.assertEqual(self._get(self.ceo, "monitoring_dashboard").status_code, 200)
        self.assertEqual(self._get(self.operator, "monitoring_dashboard").status_code, 200)

    def test_no_role_denied_operations(self):
        self.assertEqual(self._get(self.norole, "monitoring_dashboard").status_code, 403)

    def test_unauthenticated_redirected(self):
        r = self._get(None, "monitoring_dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r.url)

    def test_unauthenticated_api_401(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("dashboard_fleet_status_api")).status_code, 401)

    def test_executive_only_for_view_executive(self):
        self.assertEqual(self._get(self.admin, "executive_dashboard_preview").status_code, 200)
        self.assertEqual(self._get(self.ceo, "executive_dashboard_preview").status_code, 200)
        self.assertEqual(self._get(self.operator, "executive_dashboard_preview").status_code, 403)

    def test_user_management_admin_only(self):
        # User/role administration is Admin-only; CEO and Operator are denied.
        self.assertEqual(self._get(self.admin, "admin_users").status_code, 200)
        self.assertEqual(self._get(self.ceo, "admin_users").status_code, 403)
        self.assertEqual(self._get(self.operator, "admin_users").status_code, 403)

    def test_role_management_admin_only(self):
        self.assertEqual(self._get(self.admin, "role_management").status_code, 200)
        self.assertEqual(self._get(self.ceo, "role_management").status_code, 403)
        self.assertEqual(self._get(self.operator, "role_management").status_code, 403)

    def test_security_business_denied_for_operator(self):
        for name in ("security_dashboard", "business_dashboard"):
            self.assertEqual(self._get(self.operator, name).status_code, 403, name)
            self.assertNotEqual(self._get(self.admin, name).status_code, 403, name)

    def test_public_health_no_auth(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)


class RolesManagementTests(RBACTestBase):
    def test_protected_role_cannot_be_edited(self):
        self.client.force_login(self.admin)
        role = Role.objects.get(name=perms.ROLE_ADMIN)
        before = set(role.role_privileges.values_list("privilege__key", flat=True))
        r = self.client.post(reverse("edit_role", kwargs={"role_id": role.id}),
                             {"name": "Admin", "description": "x", "privileges": []})
        self.assertEqual(r.status_code, 302)  # blocked -> redirected, not applied
        after = set(Role.objects.get(id=role.id).role_privileges.values_list("privilege__key", flat=True))
        self.assertEqual(before, after)

    def test_protected_role_cannot_be_deleted(self):
        self.client.force_login(self.admin)
        op = Role.objects.get(name=perms.ROLE_OPERATOR)
        self.client.post(reverse("delete_role", kwargs={"role_id": op.id}))
        self.assertTrue(Role.objects.filter(name=perms.ROLE_OPERATOR).exists())

    def test_create_custom_role_is_unprotected(self):
        self.client.force_login(self.admin)
        view_ops = Privilege.objects.get(key=perms.VIEW_OPERATIONS)
        r = self.client.post(reverse("create_role"),
                             {"name": "Analyst", "description": "custom",
                              "privileges": [str(view_ops.id)]})
        self.assertEqual(r.status_code, 302)
        role = Role.objects.get(name="Analyst")
        self.assertFalse(role.is_protected)
        self.assertEqual(list(role.role_privileges.values_list("privilege__key", flat=True)),
                         [perms.VIEW_OPERATIONS])

    def test_operator_cannot_open_role_editor(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("create_role")).status_code, 403)


class WriteEnforcementTests(RBACTestBase):
    def test_operator_cannot_post_mutation(self):
        self.client.force_login(self.operator)
        r = self.client.post(reverse("add_server"), {})
        self.assertEqual(r.status_code, 403)

    def test_admin_passes_middleware_on_mutation(self):
        # Middleware must NOT block admin (view may 200/302/400, but never 403).
        self.client.force_login(self.admin)
        r = self.client.post(reverse("add_server"), {})
        self.assertNotEqual(r.status_code, 403)

    def test_ceo_can_write(self):
        # CEO is a non-superuser with manage_monitoring — must NOT be 403 (the
        # canary that caught the has_privilege import bug).
        self.client.force_login(self.ceo)
        r = self.client.post(reverse("add_server"), {})
        self.assertNotEqual(r.status_code, 403)

    def test_norole_cannot_post_mutation(self):
        self.client.force_login(self.norole)
        self.assertEqual(self.client.post(reverse("add_server"), {}).status_code, 403)

    def test_operator_denied_each_monitoring_mutation(self):
        # The exact actions the support operator must not perform: edit/delete/regen-token,
        # toggle service/container monitoring, suspend alerts. Middleware denies (403)
        # before the view runs, so non-existent ids are fine.
        self.client.force_login(self.operator)
        routes = [
            ("edit_server", {"server_id": 1}),
            ("delete_server", {"server_id": 1}),
            ("regenerate_agent_token", {"server_id": 1}),
            ("toggle_service_monitoring", {"server_id": 1, "service_id": 1}),
            ("toggle_container_monitoring", {"server_id": 1, "container_id": 1}),
            ("toggle_alert_suppression", {"server_id": 1, "action": "suspend"}),
        ]
        for name, kw in routes:
            r = self.client.post(reverse(name, kwargs=kw))
            self.assertEqual(r.status_code, 403, f"{name} must be denied for operator")


class ExecutivePersonaGuardTests(RBACTestBase):
    def test_operator_forced_to_operations_even_if_persona_executive(self):
        acl = UserACL.get_or_create_for_user(self.operator)
        acl.dashboard_view = UserACL.DashboardView.EXECUTIVE
        acl.save()
        self.client.force_login(self.operator)
        r = self.client.get(reverse("monitoring_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["dashboard_view"], UserACL.DashboardView.OPERATIONS)

    def test_operator_cannot_switch_to_executive(self):
        self.client.force_login(self.operator)
        self.client.post(reverse("set_dashboard_view"), {"view": "executive"})
        acl = UserACL.objects.get(user=self.operator)
        self.assertEqual(acl.dashboard_view, UserACL.DashboardView.OPERATIONS)


class SelfServiceTests(RBACTestBase):
    def test_any_user_can_change_own_password(self):
        # Self-service: requires only authentication, no capability.
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("account_password")).status_code, 200)
        self.client.post(reverse("account_password"), {
            "old_password": "pw",
            "new_password1": "Brand-New-Pw-9",
            "new_password2": "Brand-New-Pw-9",
        })
        self.operator.refresh_from_db()
        self.assertTrue(self.operator.check_password("Brand-New-Pw-9"))

    def test_password_page_reachable_by_norole(self):
        # Even a role-less staff account can manage its own password.
        self.client.force_login(self.norole)
        self.assertEqual(self.client.get(reverse("account_password")).status_code, 200)
