"""The anomalies-log selection UI must only appear when there is something to resolve.

Bug: on the default ("All") filter, when every shown anomaly is already resolved, the
select-all header checkbox + bulk-resolve button still rendered -- but resolved rows
have no per-row checkbox, so clicking select-all did nothing and looked broken. The
checkbox column / select-all / button should render iff at least one *unresolved*
anomaly is shown (and the filter isn't 'resolved').
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import Server, SystemMetric, Anomaly

User = get_user_model()


class AnomaliesLogSelectAllTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "b@x.test", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.server = Server.objects.create(name="s1", ip_address="10.0.0.1", username="agent")
        self.metric = SystemMetric.objects.create(
            server=self.server, cpu_percent=95.0, memory_total=8_000_000_000,
            memory_available=4_000_000_000, memory_percent=50.0, memory_used=4_000_000_000)
        self.url = reverse("server_anomalies_log", args=[self.server.id])

    def _anomaly(self, resolved):
        return Anomaly.objects.create(
            server=self.server, metric=self.metric, metric_type="cpu",
            metric_name="cpu_percent", metric_value=99.0, anomaly_score=1.0,
            severity=Anomaly.Severity.MEDIUM, resolved=resolved,
            resolved_at=timezone.now() if resolved else None)

    def test_no_checkbox_when_all_resolved_default_filter(self):
        self._anomaly(resolved=True)
        self._anomaly(resolved=True)
        r = self.client.get(self.url)                       # default '' (All) filter
        self.assertFalse(r.context["show_anomaly_checkboxes"])
        self.assertNotContains(r, 'id="select-all-anomalies"')

    def test_no_checkbox_when_no_anomalies(self):
        r = self.client.get(self.url)
        self.assertFalse(r.context["show_anomaly_checkboxes"])
        self.assertNotContains(r, 'id="select-all-anomalies"')

    def test_checkbox_shown_when_an_unresolved_exists(self):
        self._anomaly(resolved=True)
        self._anomaly(resolved=False)
        r = self.client.get(self.url)                       # default '' filter, mixed
        self.assertTrue(r.context["show_anomaly_checkboxes"])
        self.assertContains(r, 'id="select-all-anomalies"')
        self.assertContains(r, 'class="anomaly-checkbox"')  # the one unresolved row

    def test_no_checkbox_on_resolved_filter_even_if_unresolved_exists(self):
        self._anomaly(resolved=False)
        r = self.client.get(self.url, {"status": "resolved"})
        self.assertFalse(r.context["show_anomaly_checkboxes"])
        self.assertNotContains(r, 'id="select-all-anomalies"')

    def test_checkbox_on_unresolved_filter(self):
        self._anomaly(resolved=False)
        r = self.client.get(self.url, {"status": "unresolved"})
        self.assertTrue(r.context["show_anomaly_checkboxes"])
        self.assertContains(r, 'id="select-all-anomalies"')
