"""Per-user email-alerts mute.

A user with `UserACL.email_alerts_enabled = False` is never included in alert emails,
regardless of the role-based routing rules. The edit/create user forms persist the toggle.
"""
from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse

from core import alert_routing
from core.models import Role, UserACL
from core.permissions import ROLE_ADMIN


class EmailAlertMuteRoutingTests(TestCase):
    def setUp(self):
        self.admin_role = Role.objects.get_or_create(name=ROLE_ADMIN)[0]
        alert_routing.ensure_default_rules()      # Admin routes every category at LOW

    def _user(self, name, email, enabled=True):
        u = User.objects.create(username=name, email=email, is_active=True, is_staff=True)
        UserACL.objects.update_or_create(
            user=u, defaults={"role": self.admin_role, "email_alerts_enabled": enabled})
        return u

    def test_recipients_excludes_muted_user(self):
        self._user("on_user", "on@x.test", enabled=True)
        self._user("off_user", "off@x.test", enabled=False)
        recips = alert_routing.recipients_for("resource", "HIGH")
        self.assertIn("on@x.test", recips)
        self.assertNotIn("off@x.test", recips)        # muted -> excluded

    def test_default_is_enabled(self):
        u = self._user("dflt", "d@x.test")            # default True
        self.assertTrue(UserACL.objects.get(user=u).email_alerts_enabled)
        self.assertIn("d@x.test", alert_routing.recipients_for("resource", "HIGH"))


class EmailAlertMuteEditorTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("ea_root", "root@x.test", "pw")
        self.client.force_login(self.admin)
        self.role = Role.objects.get_or_create(name=ROLE_ADMIN)[0]
        self.target = User.objects.create(username="target", email="t@x.test",
                                          is_active=True, is_staff=True)
        UserACL.objects.update_or_create(user=self.target, defaults={"role": self.role})

    def _muted(self):
        return not UserACL.objects.get(user=self.target).email_alerts_enabled

    def test_edit_unchecking_mutes_user(self):
        # Submitting the edit form WITHOUT the email_alerts checkbox -> muted.
        self.client.post(reverse("edit_admin_user", args=[self.target.id]), {
            "username": "target", "email": "t@x.test", "is_active": "on",
            "role": self.role.id,
        })
        self.assertTrue(self._muted())

    def test_edit_checking_enables_user(self):
        UserACL.objects.filter(user=self.target).update(email_alerts_enabled=False)
        self.client.post(reverse("edit_admin_user", args=[self.target.id]), {
            "username": "target", "email": "t@x.test", "is_active": "on",
            "role": self.role.id, "email_alerts": "on",
        })
        self.assertFalse(self._muted())

    def test_create_user_persists_toggle(self):
        self.client.post(reverse("create_admin_user"), {
            "username": "newbie", "password": "pw12345678", "email": "n@x.test",
            "role": self.role.id, "email_alerts": "on",
        })
        u = User.objects.get(username="newbie")
        self.assertTrue(UserACL.objects.get(user=u).email_alerts_enabled)
