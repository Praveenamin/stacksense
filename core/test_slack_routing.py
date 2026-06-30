"""Slack alert routing (category × severity, no per-user dimension).

`slack_should_send()` is the shared gate every Slack send point uses; it's unit-tested across
the matrix here. Integration proves the wiring (a real send point honours the rule, and Slack
routing doesn't disturb email routing), and the editor persists / validates / is RBAC-gated.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, Client
from django.urls import reverse

from core import alert_routing
from core.alert_categories import AlertCategory
from core.models import (Server, EmailAlertConfig, SlackAlertConfig, SlackRoutingRule,
                         Role, UserACL)
from core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_CEO
from core.agent_api import _notify_unit


class SlackShouldSendMatrixTests(TestCase):
    def _set(self, category, sev):
        SlackRoutingRule.objects.update_or_create(category=category,
                                                  defaults={"min_severity": sev})

    def test_missing_rule_sends(self):
        # No rule yet -> behave as before (send), so turning on routing is non-disruptive.
        self.assertTrue(alert_routing.slack_should_send("resource", "LOW"))

    def test_off_never_sends(self):
        self._set("security", "OFF")
        for sev in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            self.assertFalse(alert_routing.slack_should_send("security", sev))

    def test_low_sends_everything(self):
        self._set("resource", "LOW")
        for sev in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            self.assertTrue(alert_routing.slack_should_send("resource", sev))

    def test_high_sends_high_and_critical_only(self):
        self._set("availability", "HIGH")
        self.assertFalse(alert_routing.slack_should_send("availability", "LOW"))
        self.assertFalse(alert_routing.slack_should_send("availability", "MEDIUM"))
        self.assertTrue(alert_routing.slack_should_send("availability", "HIGH"))
        self.assertTrue(alert_routing.slack_should_send("availability", "CRITICAL"))

    def test_critical_only(self):
        self._set("business", "CRITICAL")
        self.assertFalse(alert_routing.slack_should_send("business", "HIGH"))
        self.assertTrue(alert_routing.slack_should_send("business", "CRITICAL"))

    def test_ensure_defaults_seeds_low_for_all_categories(self):
        alert_routing.ensure_default_slack_rules()
        self.assertEqual(SlackRoutingRule.objects.count(), len(AlertCategory.choices))
        self.assertTrue(all(r.min_severity == "LOW" for r in SlackRoutingRule.objects.all()))


class SlackRoutingWiringTests(TestCase):
    """A real Slack send point honours the rule, and Slack routing leaves email routing alone."""

    def setUp(self):
        self.server = Server.objects.create(name="t-vm", ip_address="10.8.8.5", username="agent")
        SlackAlertConfig.objects.create(id=1, enabled=True,
                                        webhook_url="https://hooks.slack.com/services/T/B/x")

    @patch("core.agent_api.requests.post")
    def test_off_suppresses_slack(self, mock_post):
        SlackRoutingRule.objects.update_or_create(category="availability",
                                                  defaults={"min_severity": "OFF"})
        _notify_unit(self.server, "service", "nginx", down=True)   # availability / HIGH
        self.assertFalse(mock_post.called)

    @patch("core.agent_api.requests.post")
    def test_low_sends_slack(self, mock_post):
        SlackRoutingRule.objects.update_or_create(category="availability",
                                                  defaults={"min_severity": "LOW"})
        _notify_unit(self.server, "service", "nginx", down=True)
        self.assertTrue(mock_post.called)

    @patch("core.agent_api.requests.post")
    def test_slack_off_does_not_block_email(self, mock_post):
        # Slack availability OFF, but email routing still reaches admin + operator.
        roles = {n: Role.objects.get_or_create(name=n)[0]
                 for n in (ROLE_ADMIN, ROLE_OPERATOR, ROLE_CEO)}
        alert_routing.ensure_default_rules()
        for short, rn, email in [("a", ROLE_ADMIN, "a@x.test"), ("o", ROLE_OPERATOR, "o@x.test")]:
            u = User.objects.create(username=f"sk_{short}", email=email, is_active=True)
            UserACL.objects.update_or_create(user=u, defaults={"role": roles[rn]})
        EmailAlertConfig.objects.create(id=1, provider="gmail", username="s@x.test",
                                        smtp_host="smtp.gmail.com", smtp_port=587,
                                        use_tls=True, enabled=True)
        SlackRoutingRule.objects.update_or_create(category="availability",
                                                  defaults={"min_severity": "OFF"})
        _notify_unit(self.server, "service", "nginx", down=True)
        self.assertFalse(mock_post.called)                 # Slack suppressed
        self.assertEqual(len(mail.outbox), 1)              # email still routed
        self.assertEqual(set(mail.outbox[0].to), {"a@x.test", "o@x.test"})


class SlackRoutingEditorTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("sk_root", "root@x.test", "pw")
        self.client.force_login(self.admin)

    def test_post_persists_a_rule(self):
        self.client.post(reverse("save_slack_routing"), {"slackroute_security": "HIGH"})
        self.assertEqual(SlackRoutingRule.objects.get(category="security").min_severity, "HIGH")

    def test_invalid_inputs_are_ignored(self):
        self.client.post(reverse("save_slack_routing"), {
            "slackroute_security": "WAT",       # bad severity
            "slackroute_boguscat": "HIGH",      # bad category
            "not_a_field": "HIGH",              # ignored key
        })
        self.assertFalse(SlackRoutingRule.objects.filter(category="boguscat").exists())
        self.assertFalse(SlackRoutingRule.objects.filter(category="security").exists())

    def test_get_renders_slack_routing_table(self):
        html = self.client.get(reverse("alert_config")).content.decode()
        self.assertEqual(html.count('name="slackroute_'), len(AlertCategory.choices))  # one per category

    def test_rbac_blocks_user_without_manage_alerts(self):
        op = User.objects.create(username="sk_op", email="op@x.test", is_active=True, is_staff=True)
        UserACL.get_or_create_for_user(op)                 # role=None -> no MANAGE_ALERTS
        c = Client(); c.force_login(op)
        c.post(reverse("save_slack_routing"), {"slackroute_security": "OFF"})
        self.assertFalse(SlackRoutingRule.objects.filter(category="security").exists())
