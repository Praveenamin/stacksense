"""
RBAC Phase 4 tests: default landing per role + UI gating driven by capabilities.
Run: python manage.py test core.test_rbac_ui
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core import permissions as perms
from core.models import Role, UserACL


class RBACUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        perms.sync_roles()
        cls.admin = User.objects.create_superuser("ui_admin", "a@x.com", "pw")
        cls.ceo = cls._staff("ui_ceo", perms.ROLE_CEO)
        cls.operator = cls._staff("ui_op", perms.ROLE_OPERATOR)

    @staticmethod
    def _staff(username, role_name):
        u = User.objects.create_user(username, f"{username}@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(u)
        acl.role = Role.objects.get(name=role_name)
        acl.dashboard_view = UserACL.DashboardView.OPERATIONS
        acl.save()
        return u

    def _landing(self, user):
        self.client.force_login(user)
        self.client.get(reverse("home_redirect"))
        return UserACL.objects.get(user=user).dashboard_view

    # --- default landing per role -----------------------------------------
    def test_landing_admin_operations(self):
        self.assertEqual(self._landing(self.admin), UserACL.DashboardView.OPERATIONS)

    def test_landing_ceo_executive(self):
        self.assertEqual(self._landing(self.ceo), UserACL.DashboardView.EXECUTIVE)

    def test_landing_operator_operations(self):
        self.assertEqual(self._landing(self.operator), UserACL.DashboardView.OPERATIONS)

    # --- sidebar / nav gating (UI mirrors server caps) --------------------
    def test_operator_sidebar_hides_manage_sections(self):
        self.client.force_login(self.operator)
        html = self.client.get(reverse("monitoring_dashboard")).content.decode()
        self.assertNotIn('href="/security/"', html)
        self.assertNotIn('href="/business/"', html)
        self.assertNotIn('href="/alert-config/"', html)
        # read-only nav still present
        self.assertIn('href="/servers/"', html)
        self.assertIn('href="/alerts/"', html)

    def test_admin_sidebar_shows_manage_sections(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("monitoring_dashboard")).content.decode()
        self.assertIn('href="/security/"', html)
        self.assertIn('href="/business/"', html)
        self.assertIn('href="/alert-config/"', html)

    def test_operator_no_persona_toggle(self):
        self.client.force_login(self.operator)
        html = self.client.get(reverse("monitoring_dashboard")).content.decode()
        self.assertNotIn('value="executive"', html)

    def test_ceo_has_persona_toggle(self):
        self.client.force_login(self.ceo)
        html = self.client.get(reverse("monitoring_dashboard")).content.decode()
        self.assertIn('value="executive"', html)

    def test_operator_gear_hides_users_roles_pricing(self):
        self.client.force_login(self.operator)
        html = self.client.get(reverse("monitoring_dashboard")).content.decode()
        self.assertNotIn('href="/admin-users/"', html)
        self.assertNotIn('href="/roles/"', html)
        self.assertNotIn('href="/settings/pricing/"', html)
