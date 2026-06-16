"""
RBAC Phase 4 tests: default landing per role + UI gating driven by capabilities.
Run: python manage.py test core.test_rbac_ui
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core import permissions as perms
from core.models import Role, UserACL, Server, Service, Container


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

    def test_new_ceo_user_gets_executive_persona_at_creation(self):
        # A CEO created via the form should default to the Executive persona,
        # independent of the login dispatcher (covers direct-URL logins).
        self.client.force_login(self.admin)
        ceo_role = Role.objects.get(name=perms.ROLE_CEO).id
        self.client.post(reverse("create_admin_user"),
                         {"username": "land_ceo", "email": "l@x.com",
                          "password": "pw12345678", "role": str(ceo_role)})
        u = User.objects.get(username="land_ceo")
        self.assertEqual(UserACL.objects.get(user=u).dashboard_view,
                         UserACL.DashboardView.EXECUTIVE)

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
        self.assertIn('href="/alert-config/"', html)
        # Business is a roadmap ("coming soon") item now -- admins see it as a label,
        # not a live /business/ link (which was retired). Operators don't see it at all.
        self.assertIn('Business', html)

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

    def test_help_docs_in_sidebar_for_everyone(self):
        for u in (self.admin, self.operator):
            self.client.force_login(u)
            self.assertIn('href="/help/"', self.client.get(reverse("monitoring_dashboard")).content.decode())

    def test_account_menu_switch_list(self):
        # Admin sees the impersonation switch list (operator is a valid target);
        # Operator has no switch list.
        self.client.force_login(self.admin)
        admin_html = self.client.get(reverse("monitoring_dashboard")).content.decode()
        self.assertIn("Switch to another user", admin_html)
        self.assertIn(self.operator.username, admin_html)
        self.client.force_login(self.operator)
        self.assertNotIn("Switch to another user",
                         self.client.get(reverse("monitoring_dashboard")).content.decode())


class _ControlsBase(TestCase):
    """Operator (no manage_monitoring) must SEE servers/services/containers but with the
    mutating controls shown DISABLED (faded), not hidden. Admin gets active controls."""
    @classmethod
    def setUpTestData(cls):
        perms.sync_roles()
        cls.admin = User.objects.create_superuser("ctl_admin", "a@x.com", "pw")
        cls.operator = User.objects.create_user("ctl_op", "o@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(cls.operator)
        acl.role = Role.objects.get(name=perms.ROLE_OPERATOR)
        acl.save()
        cls.server = Server.objects.create(name="s1", ip_address="10.0.0.1", username="agent")
        Service.objects.create(server=cls.server, name="nginx", service_type="systemd", status="running")
        Container.objects.create(server=cls.server, name="web", image="nginx:latest", state="running")

    def html(self, user, url):
        self.client.force_login(user)
        return self.client.get(url).content.decode()


class OperatorSeesDisabledControlsTests(_ControlsBase):
    def test_server_list_actions_disabled(self):
        h = self.html(self.operator, reverse("server_list"))
        self.assertIn('rbac-disabled"', h)   # the class applied to a control (not the CSS rule)
        self.assertNotIn(f'href="/server/edit/{self.server.id}/"', h)  # no active edit link
        self.assertIn(f'href="/server/{self.server.id}/"', h)          # View details still works

    def test_server_details_edit_and_token_disabled(self):
        h = self.html(self.operator, reverse("server_details", args=[self.server.id]))
        self.assertIn('rbac-disabled"', h)   # the class applied to a control (not the CSS rule)
        self.assertNotIn(f'href="/server/edit/{self.server.id}/"', h)
        self.assertNotIn("regenerate", h.lower())                      # no token POST form

    def test_services_toggle_disabled(self):
        h = self.html(self.operator, reverse("services_overview"))
        self.assertIn('rbac-disabled"', h)   # the class applied to a control (not the CSS rule)
        self.assertIn('<input type="checkbox" disabled', h)

    def test_containers_toggle_disabled_but_report_available(self):
        h = self.html(self.operator, reverse("containers_overview"))
        self.assertIn('rbac-disabled"', h)   # the class applied to a control (not the CSS rule)
        self.assertIn('<input type="checkbox" disabled', h)
        self.assertIn(">Report<", h)                                   # listing + report stay


class AdminSeesActiveControlsTests(_ControlsBase):
    def test_server_list_actions_active(self):
        h = self.html(self.admin, reverse("server_list"))
        self.assertNotIn('rbac-disabled"', h)   # no control carries the disabled class
        self.assertIn(f'href="/server/edit/{self.server.id}/"', h)

    def test_server_details_edit_and_token_active(self):
        h = self.html(self.admin, reverse("server_details", args=[self.server.id]))
        self.assertNotIn('rbac-disabled"', h)   # no control carries the disabled class
        self.assertIn(f'href="/server/edit/{self.server.id}/"', h)
        self.assertIn("regenerate", h.lower())

    def test_services_toggle_active(self):
        h = self.html(self.admin, reverse("services_overview"))
        self.assertNotIn('rbac-disabled"', h)   # no control carries the disabled class
        self.assertNotIn('<input type="checkbox" disabled', h)
