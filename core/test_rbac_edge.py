"""
RBAC Phase 5 — edge cases & security pass.

Direct-URL bypass attempts, mid-session role changes, impersonation session
edge cases (target deactivated/deleted/elevated), and confirmation that a
client-supplied role is never trusted. Run: python manage.py test core.test_rbac_edge
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core import permissions as perms
from core.models import Role, UserACL

SESSION_KEY = "impersonate_user_id"


class EdgeBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        perms.sync_roles()
        cls.admin = User.objects.create_superuser("edge_admin", "a@x.com", "pw")
        cls.operator = cls._staff("edge_op", perms.ROLE_OPERATOR)

    @staticmethod
    def _staff(username, role_name):
        u = User.objects.create_user(username, f"{username}@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(u)
        acl.role = Role.objects.get(name=role_name)
        acl.save()
        return u


class DirectUrlTests(EdgeBase):
    """Hidden ≠ protected: blocked server-side even via direct URL (object need
    not exist — the capability check runs before the view)."""
    def test_operator_direct_edit_server(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("edit_server", kwargs={"server_id": 99999})).status_code, 403)

    def test_operator_direct_business_kpi(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("business_kpi_detail", kwargs={"kpi_id": 99999})).status_code, 403)

    def test_operator_direct_monitoring_toggle_api(self):
        self.client.force_login(self.operator)
        r = self.client.post(reverse("toggle_monitoring", kwargs={"server_id": 1, "action": "suppress"}))
        self.assertEqual(r.status_code, 403)

    def test_operator_direct_executive(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("executive_dashboard_preview")).status_code, 403)


class MidSessionRoleChangeTests(EdgeBase):
    def test_upgrade_takes_effect_without_relogin(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(reverse("executive_dashboard_preview")).status_code, 403)
        acl = UserACL.objects.get(user=self.operator)
        acl.role = Role.objects.get(name=perms.ROLE_CEO); acl.save()
        # Same session — capabilities are resolved fresh from the DB each request.
        self.assertEqual(self.client.get(reverse("executive_dashboard_preview")).status_code, 200)

    def test_downgrade_takes_effect_without_relogin(self):
        u = self._staff("edge_demote", perms.ROLE_CEO)
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse("executive_dashboard_preview")).status_code, 200)
        acl = UserACL.objects.get(user=u)
        acl.role = Role.objects.get(name=perms.ROLE_OPERATOR); acl.save()
        self.assertEqual(self.client.get(reverse("executive_dashboard_preview")).status_code, 403)


class ClientSuppliedRoleTests(EdgeBase):
    def test_role_in_request_body_is_ignored(self):
        self.client.force_login(self.operator)
        r = self.client.post(reverse("add_server"),
                             {"role": "Admin", "is_superuser": "on", "name": "x"})
        self.assertEqual(r.status_code, 403)  # still an Operator


class ImpersonationSessionEdgeTests(EdgeBase):
    def _impersonate(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("impersonate_start", kwargs={"user_id": self.operator.id}))

    def test_target_deactivated_mid_session_reverts(self):
        self._impersonate()
        self.assertEqual(self.client.get(reverse("admin_users")).status_code, 403)  # as operator
        self.operator.is_active = False
        self.operator.save()
        # Middleware can no longer resolve the target -> session dropped, back to admin.
        self.assertEqual(self.client.get(reverse("admin_users")).status_code, 200)
        self.assertIsNone(self.client.session.get(SESSION_KEY))

    def test_target_deleted_mid_session_reverts(self):
        victim = self._staff("edge_victim", perms.ROLE_OPERATOR)
        self.client.force_login(self.admin)
        self.client.post(reverse("impersonate_start", kwargs={"user_id": victim.id}))
        self.assertEqual(self.client.get(reverse("admin_users")).status_code, 403)
        victim.delete()
        self.assertEqual(self.client.get(reverse("admin_users")).status_code, 200)
        self.assertIsNone(self.client.session.get(SESSION_KEY))

    def test_target_elevated_mid_session_is_dropped(self):
        self._impersonate()
        # Operator is promoted to CEO mid-impersonation -> gains impersonate cap.
        acl = UserACL.objects.get(user=self.operator)
        acl.role = Role.objects.get(name=perms.ROLE_CEO); acl.save()
        # Defensive guard: never impersonate a peer -> session dropped, back to admin.
        self.assertEqual(self.client.get(reverse("admin_users")).status_code, 200)
        self.assertIsNone(self.client.session.get(SESSION_KEY))


class DenyByDefaultTests(EdgeBase):
    def test_unauthenticated_unknown_path_not_leaked(self):
        # Unauthenticated hitting a real protected page -> redirect to login.
        self.client.logout()
        r = self.client.get(reverse("server_list"))
        self.assertEqual(r.status_code, 302)

    def test_403_page_is_accessible(self):
        self.client.force_login(self.operator)
        r = self.client.get(reverse("security_dashboard"))
        self.assertEqual(r.status_code, 403)
        html = r.content.decode()
        self.assertIn('lang="en"', html)
        self.assertIn("Access denied", html)
