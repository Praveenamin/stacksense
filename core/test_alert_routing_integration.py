"""
Phase 2 (role-based routing) -- INTEGRATION tests.

recipients_for() is unit-tested across the whole matrix in test_alert_routing.py. This
suite proves the *wiring*: that each real email send point routes by the correct
(category, severity) and reaches EXACTLY the people the policy says, that "nobody
subscribed -> no email and no crash" holds, that the From address is pinned to the SMTP
account (anti-spoofing), that the routing-matrix editor persists/validates/guards, and
that the test-email now goes to the signed-in user.

Two transports are exercised:
  * send_mail-based points (agent_api, synthetic, security, business) -> asserted via
    Django's mail.outbox.
  * raw-smtplib points (the views senders + test endpoints) -> SMTP is mocked and the
    composed message's To/From headers are asserted.
"""
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, Client
from django.urls import reverse

from core import alert_routing, security_monitor, business, synthetic
from core.models import (Server, MonitoringConfig, EmailAlertConfig, Service, Container,
                         AlertHistory, Role, UserACL, AlertRoutingRule)
from core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_CEO
from core.agent_api import evaluate_service_alerts, evaluate_container_alerts
from core.views import _send_connection_alert, _send_alert_email


def _seed_people():
    """Three users, one per built-in role, with the role-tailored default matrix."""
    roles = {ROLE_ADMIN: Role.objects.get_or_create(name=ROLE_ADMIN)[0],
             ROLE_OPERATOR: Role.objects.get_or_create(name=ROLE_OPERATOR)[0],
             ROLE_CEO: Role.objects.get_or_create(name=ROLE_CEO)[0]}
    alert_routing.ensure_default_rules()
    users = {}
    for short, role_name, email in [("admin", ROLE_ADMIN, "admin@x.test"),
                                    ("op", ROLE_OPERATOR, "op@x.test"),
                                    ("ceo", ROLE_CEO, "ceo@x.test")]:
        u = User.objects.create(username=f"r2_{short}", email=email, is_active=True)
        UserACL.objects.update_or_create(user=u, defaults={"role": roles[role_name]})
        users[short] = u
    return roles, users


def _email_config():
    return EmailAlertConfig.objects.create(
        id=1, provider="gmail", username="sender@x.test",
        smtp_host="smtp.gmail.com", smtp_port=587, use_tls=True, enabled=True)


class SendMailRoutingTests(TestCase):
    """send_mail-based send points reach exactly the policy-correct recipients."""

    def setUp(self):
        self.roles, self.users = _seed_people()
        _email_config()
        self.server = Server.objects.create(name="t-vm", ip_address="10.8.8.1", username="agent")

    def _to(self):
        self.assertEqual(len(mail.outbox), 1)
        return set(mail.outbox[0].to)

    def test_service_down_emails_admin_and_operator_not_ceo(self):
        # service down = Availability/HIGH; CEO is Availability=CRITICAL-only -> excluded.
        Service.objects.create(server=self.server, name="nginx", status="stopped",
                               monitoring_enabled=True)
        evaluate_service_alerts(self.server)
        self.assertEqual(self._to(), {"admin@x.test", "op@x.test"})

    def test_container_down_emails_admin_and_operator_not_ceo(self):
        Container.objects.create(server=self.server, name="db", state="exited",
                                 monitoring_enabled=True)
        evaluate_container_alerts(self.server)
        self.assertEqual(self._to(), {"admin@x.test", "op@x.test"})

    def test_security_event_emails_admin_and_operator_not_ceo(self):
        security_monitor._send_email("subj", "body", "HIGH")   # Security
        self.assertEqual(self._to(), {"admin@x.test", "op@x.test"})

    def test_business_alert_emails_admin_and_ceo_not_operator(self):
        business._send_email("subj", "body", "CRITICAL")        # Business
        self.assertEqual(self._to(), {"admin@x.test", "ceo@x.test"})

    def test_synthetic_critical_reaches_ceo_too(self):
        synthetic._send_email("subj", "body", "CRITICAL")       # Availability/CRITICAL
        self.assertEqual(self._to(), {"admin@x.test", "op@x.test", "ceo@x.test"})

    def test_from_address_is_pinned_to_the_smtp_account(self):
        synthetic._send_email("subj", "body", "CRITICAL")
        self.assertEqual(mail.outbox[0].from_email, "sender@x.test")  # anti-spoofing

    def test_no_subscribers_means_no_email_and_no_crash(self):
        # Turn Security OFF for every role -> a security alert routes to nobody.
        AlertRoutingRule.objects.filter(category="security").update(min_severity="OFF")
        security_monitor._send_email("subj", "body", "CRITICAL")
        self.assertEqual(len(mail.outbox), 0)


class ViewsSmtplibRoutingTests(TestCase):
    """The raw-smtplib views senders compose a message addressed to the routed recipients."""

    def setUp(self):
        self.roles, self.users = _seed_people()
        self.cfg = _email_config()
        self.server = Server.objects.create(name="t-vm", ip_address="10.8.8.2", username="agent")
        MonitoringConfig.objects.create(server=self.server, enabled=True,
                                        monitoring_suspended=False, alert_suppressed=False)

    @staticmethod
    def _sent_message(mock_smtp):
        """The email.message passed to send_message on the mocked SMTP connection."""
        inst = mock_smtp.return_value
        assert inst.send_message.called, "send_message was never called"
        return inst.send_message.call_args.args[0]

    @patch("core.views.smtplib.SMTP")
    def test_connection_down_critical_reaches_all_three(self, mock_smtp):
        _send_connection_alert(self.server, "offline")          # Availability/CRITICAL
        msg = self._sent_message(mock_smtp)
        self.assertEqual(set(msg["To"].split(", ")),
                         {"admin@x.test", "op@x.test", "ceo@x.test"})
        self.assertEqual(msg["From"], "sender@x.test")          # anti-spoofing

    @patch("core.views.smtplib.SMTP")
    def test_connection_restored_low_excludes_ceo(self, mock_smtp):
        _send_connection_alert(self.server, "online")           # Availability/LOW
        msg = self._sent_message(mock_smtp)
        self.assertEqual(set(msg["To"].split(", ")), {"admin@x.test", "op@x.test"})

    @patch("core.views.smtplib.SMTP")
    def test_resource_threshold_email_excludes_ceo(self, mock_smtp):
        # CPU/Mem/Disk thresholds = Resource/HIGH; CEO is Resource=OFF -> excluded.
        _send_alert_email(self.cfg, self.server,
                          [{"message": "CPU usage is 95% (threshold: 80%)"}])
        msg = self._sent_message(mock_smtp)
        self.assertEqual(set(msg["To"].split(", ")), {"admin@x.test", "op@x.test"})

    @patch("core.views.smtplib.SMTP")
    def test_no_subscribers_skips_smtp_entirely(self, mock_smtp):
        AlertRoutingRule.objects.filter(category="availability").update(min_severity="OFF")
        _send_connection_alert(self.server, "offline")
        self.assertFalse(mock_smtp.called)                      # never opened a connection
        # ...but the alert is still recorded.
        self.assertTrue(AlertHistory.objects.filter(server=self.server,
                                                    alert_type="CONNECTION").exists())


class RoutingMatrixEditorTests(TestCase):
    """The Alert Routing editor (save_alert_routing) persists, validates, and is RBAC-gated."""

    def setUp(self):
        self.roles, self.users = _seed_people()
        self.ceo = self.roles[ROLE_CEO]
        self.admin = User.objects.create_superuser("r2_root", "root@x.test", "pw")
        self.client.force_login(self.admin)

    def _ceo_business(self):
        return AlertRoutingRule.objects.get(role=self.ceo, category="business").min_severity

    def test_post_updates_a_cell(self):
        self.assertEqual(self._ceo_business(), "LOW")           # seeded default
        self.client.post(reverse("save_alert_routing"),
                         {f"route_{self.ceo.id}_business": "OFF"})
        self.assertEqual(self._ceo_business(), "OFF")

    def test_invalid_inputs_are_ignored_without_crashing(self):
        r = self.client.post(reverse("save_alert_routing"), {
            f"route_{self.ceo.id}_business": "WAT",        # bad severity
            f"route_{self.ceo.id}_boguscat": "HIGH",       # bad category
            "route_999999_business": "HIGH",               # nonexistent role
            "not_a_route_field": "HIGH",                   # ignored key
        })
        self.assertIn(r.status_code, (302, 200))
        self.assertEqual(self._ceo_business(), "LOW")           # unchanged, no bad row
        self.assertFalse(AlertRoutingRule.objects.filter(category="boguscat").exists())

    def test_get_renders_full_matrix(self):
        html = self.client.get(reverse("alert_config")).content.decode()
        self.assertEqual(html.count('name="route_'), 15)        # 3 roles x 5 categories

    def test_operator_without_manage_alerts_cannot_edit(self):
        op_user = User.objects.create(username="r2_op_staff", email="ops@x.test",
                                      is_active=True, is_staff=True)
        UserACL.objects.update_or_create(user=op_user,
                                         defaults={"role": self.roles[ROLE_OPERATOR]})
        c = Client()
        c.force_login(op_user)
        c.post(reverse("save_alert_routing"), {f"route_{self.ceo.id}_business": "OFF"})
        # RBAC blocks the mutation -> the cell is untouched.
        self.assertEqual(self._ceo_business(), "LOW")


class TestEmailRecipientTests(TestCase):
    """With no 'To Email' field, the 'Send test email' button (test_alert_config, which
    uses the saved SMTP config) sends to the signed-in user."""

    def setUp(self):
        EmailAlertConfig.objects.create(
            id=1, provider="gmail", username="sender@x.test", smtp_host="smtp.gmail.com",
            smtp_port=587, use_tls=True, password="app-password", enabled=True)
        self.client = Client()

    @patch("core.views.smtplib.SMTP")
    def test_test_email_goes_to_logged_in_user(self, mock_smtp):
        admin = User.objects.create_superuser("r2_tester", "tester@x.test", "pw")
        self.client.force_login(admin)
        r = self.client.post(reverse("test_alert_config"))
        self.assertEqual(r.status_code, 302)                    # redirects back with a message
        msg = mock_smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "tester@x.test")            # the signed-in user
        self.assertEqual(msg["From"], "sender@x.test")          # the SMTP account

    @patch("core.views.smtplib.SMTP")
    def test_user_without_email_gets_no_send(self, mock_smtp):
        noemail = User.objects.create_superuser("r2_noemail", "", "pw")
        self.client.force_login(noemail)
        r = self.client.post(reverse("test_alert_config"))
        self.assertEqual(r.status_code, 302)                    # error message + redirect
        self.assertFalse(mock_smtp.called)                      # nowhere to send -> no connection
