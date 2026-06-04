"""
Impersonation + audit tests (RBAC Phase 3).

Covers: who-can-impersonate-whom, the no-escalation invariant, audit entries on
start/exit/denied, and that exit restores the real account.
Run: python manage.py test core.test_rbac_impersonation
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core import permissions as perms
from core.models import AuditLog, Role, UserACL

SESSION_KEY = "impersonate_user_id"


class ImpersonationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        perms.sync_roles()
        cls.admin = User.objects.create_superuser("imp_admin", "a@x.com", "pw")
        cls.admin2 = User.objects.create_superuser("imp_admin2", "a2@x.com", "pw")
        cls.ceo = cls._staff("imp_ceo", perms.ROLE_CEO)
        cls.operator = cls._staff("imp_op", perms.ROLE_OPERATOR)
        cls.operator2 = cls._staff("imp_op2", perms.ROLE_OPERATOR)

    @staticmethod
    def _staff(username, role_name):
        u = User.objects.create_user(username, f"{username}@x.com", "pw", is_staff=True)
        acl = UserACL.get_or_create_for_user(u)
        acl.role = Role.objects.get(name=role_name)
        acl.save()
        return u

    def _start(self, target):
        return self.client.post(reverse("impersonate_start", kwargs={"user_id": target.id}))

    # --- who can impersonate whom -----------------------------------------
    def test_admin_can_impersonate_operator(self):
        self.client.force_login(self.admin)
        r = self._start(self.operator)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.session.get(SESSION_KEY), self.operator.id)

    def test_ceo_cannot_impersonate(self):
        # Impersonation is Admin-only; CEO lacks the capability -> 403, no swap.
        self.client.force_login(self.ceo)
        r = self._start(self.operator)
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.client.session.get(SESSION_KEY))

    def test_cannot_impersonate_peer_ceo(self):
        self.client.force_login(self.admin)
        self._start(self.ceo)
        self.assertIsNone(self.client.session.get(SESSION_KEY))

    def test_cannot_impersonate_another_admin(self):
        self.client.force_login(self.admin)
        self._start(self.admin2)
        self.assertIsNone(self.client.session.get(SESSION_KEY))

    def test_cannot_impersonate_self(self):
        self.client.force_login(self.admin)
        self._start(self.admin)
        self.assertIsNone(self.client.session.get(SESSION_KEY))

    def test_operator_cannot_impersonate(self):
        self.client.force_login(self.operator)
        r = self._start(self.operator2)
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.client.session.get(SESSION_KEY))

    # --- no escalation -----------------------------------------------------
    def test_no_escalation_executive_blocked(self):
        self.client.force_login(self.admin)
        self._start(self.operator)
        # Admin normally sees Executive; while impersonating an Operator -> 403.
        self.assertEqual(self.client.get(reverse("executive_dashboard_preview")).status_code, 403)

    def test_no_escalation_user_management_blocked(self):
        self.client.force_login(self.admin)
        self._start(self.operator)
        self.assertEqual(self.client.get(reverse("admin_users")).status_code, 403)

    def test_no_escalation_write_blocked(self):
        self.client.force_login(self.admin)
        self._start(self.operator)
        self.assertEqual(self.client.post(reverse("add_server"), {}).status_code, 403)

    def test_operator_view_still_works_while_impersonated(self):
        self.client.force_login(self.admin)
        self._start(self.operator)
        self.assertEqual(self.client.get(reverse("monitoring_dashboard")).status_code, 200)

    # --- exit restores -----------------------------------------------------
    def test_exit_restores_real_account(self):
        self.client.force_login(self.admin)
        self._start(self.operator)
        self.assertEqual(self.client.get(reverse("admin_users")).status_code, 403)  # as operator
        r = self.client.post(reverse("impersonate_exit"))
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.client.session.get(SESSION_KEY))
        self.assertEqual(self.client.get(reverse("admin_users")).status_code, 200)  # admin again

    # --- audit -------------------------------------------------------------
    def test_audit_start_and_exit(self):
        self.client.force_login(self.admin)
        self._start(self.operator)
        self.client.post(reverse("impersonate_exit"))
        start = AuditLog.objects.filter(action="impersonate_start").first()
        exit_ = AuditLog.objects.filter(action="impersonate_exit").first()
        self.assertIsNotNone(start)
        self.assertEqual(start.actor, self.admin)
        self.assertEqual(start.impersonated_target, self.operator)
        self.assertEqual(start.result, AuditLog.Result.ALLOWED)
        self.assertIsNotNone(exit_)
        self.assertEqual(exit_.impersonated_target, self.operator)

    def test_audit_denied_records_real_actor_and_target(self):
        self.client.force_login(self.admin)
        self._start(self.operator)
        self.client.post(reverse("add_server"), {})  # denied while impersonating
        denied = AuditLog.objects.filter(result=AuditLog.Result.DENIED,
                                         action__startswith="denied:").first()
        self.assertIsNotNone(denied)
        self.assertEqual(denied.actor, self.admin)            # real actor preserved
        self.assertEqual(denied.impersonated_target, self.operator)

    def test_audit_denied_on_blocked_impersonation(self):
        self.client.force_login(self.admin)
        self._start(self.ceo)  # peer -> denied
        self.assertTrue(
            AuditLog.objects.filter(action="impersonate_denied", actor=self.admin).exists())
