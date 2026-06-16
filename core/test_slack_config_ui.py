"""Regression: the Slack Test/Clear buttons must be wired to defined handlers.

The buttons call testSlackConfiguration()/clearSlackConfiguration() via onclick; those
functions were never defined, so clicking did nothing (a JS ReferenceError) -- the test
message never sent. Guard that the page both references and DEFINES those handlers and
ships the hidden POST forms they submit.
"""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from core.models import SlackAlertConfig

User = get_user_model()


class SlackConfigButtonsWiredTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "b@x.test", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        # The Configuration Actions card (with Test/Clear) only renders once a config exists.
        SlackAlertConfig.objects.create(
            id=1, webhook_url="https://hooks.slack.com/services/T/B/x", enabled=True)
        self.html = self.client.get(reverse("alert_config")).content.decode()

    def test_test_button_handler_is_defined(self):
        self.assertIn("onclick=\"testSlackConfiguration()\"", self.html)
        self.assertIn("function testSlackConfiguration", self.html)
        self.assertIn('id="test-slack-form"', self.html)

    def test_clear_button_handler_is_defined(self):
        self.assertIn("onclick=\"clearSlackConfiguration()\"", self.html)
        self.assertIn("function clearSlackConfiguration", self.html)
        self.assertIn('id="clear-slack-form"', self.html)

    def test_no_onclick_references_an_undefined_function(self):
        # Every onclick="fn()" used on this page must have a matching "function fn".
        called = set(re.findall(r'onclick="([a-zA-Z_]\w*)\(', self.html))
        defined = set(re.findall(r'function ([a-zA-Z_]\w*)\s*\(', self.html))
        # builtins/globals that are defined elsewhere (base.html) are allowed
        allowed = {"switchTab", "appConfirm", "appAlert"}
        missing = called - defined - allowed
        self.assertEqual(missing, set(), f"onclick handlers with no definition: {missing}")
