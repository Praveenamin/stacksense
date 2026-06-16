"""The Alerts page summary cards must agree with the list.

Anomalies are no longer shown on this page (they're dashboard notifications), so the
cards and list reflect AlertHistory only: anomalies -- whatever their severity -- never
count here, and "Critical Severity" counts actually-CRITICAL alerts, not "everything
unacknowledged".
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import Server, SystemMetric, Anomaly, AlertHistory

User = get_user_model()


class AlertHistoryCardsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "b@x.test", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.server = Server.objects.create(name="s1", ip_address="10.0.0.1", username="agent")
        self.metric = SystemMetric.objects.create(
            server=self.server, cpu_percent=95.0, memory_total=8_000_000_000,
            memory_available=4_000_000_000, memory_percent=50.0, memory_used=4_000_000_000)
        self.url = reverse("alert_history")           # /alerts/

    def _alert(self, severity, status="triggered"):
        return AlertHistory.objects.create(
            server=self.server, alert_type=AlertHistory.AlertType.CPU, status=status,
            severity=severity, value=95.0, threshold=90.0, message="cpu high",
            recipients="", sent_at=timezone.now(),
            resolved_at=timezone.now() if status == "resolved" else None)

    def _anom(self, severity, resolved=False):
        return Anomaly.objects.create(
            server=self.server, metric=self.metric, metric_type="cpu",
            metric_name="cpu_percent", metric_value=90.0, anomaly_score=1.0,
            severity=severity, resolved=resolved,
            resolved_at=timezone.now() if resolved else None, timestamp=timezone.now())

    def _ctx(self):
        return self.client.get(self.url).context        # default time_range=24h

    def test_anomalies_never_affect_cards_or_list(self):
        # A screen full of anomalies (any severity) shows nothing on the alerts page.
        self._anom(Anomaly.Severity.CRITICAL)
        self._anom(Anomaly.Severity.HIGH, resolved=True)
        self._anom(Anomaly.Severity.LOW)
        c = self._ctx()
        self.assertEqual(len(c["unified_items"]), 0)
        self.assertEqual(c["triggered_count"], 0)
        self.assertEqual(c["unacknowledged_count"], 0)
        self.assertEqual(c["acknowledged_count"], 0)
        self.assertEqual(c["critical_severity_count"], 0)

    def test_alerts_drive_the_cards(self):
        self._alert(Anomaly.Severity.MEDIUM, status="triggered")
        c = self._ctx()
        self.assertEqual(len(c["unified_items"]), 1)
        self.assertEqual(c["unacknowledged_count"], 1)
        self.assertEqual(c["critical_severity_count"], 0)    # MEDIUM is not CRITICAL

    def test_critical_severity_counts_only_critical_alerts(self):
        self._alert(Anomaly.Severity.CRITICAL, status="triggered")
        self._alert(Anomaly.Severity.HIGH, status="triggered")
        self._anom(Anomaly.Severity.CRITICAL)                # ignored on this page
        c = self._ctx()
        self.assertEqual(c["critical_severity_count"], 1)    # only the CRITICAL alert
        self.assertEqual(c["unacknowledged_count"], 2)       # CRITICAL + HIGH alerts
        self.assertEqual(len(c["unified_items"]), 2)

    def test_resolved_alert_is_acknowledged(self):
        self._alert(Anomaly.Severity.HIGH, status="resolved")
        c = self._ctx()
        self.assertEqual(c["acknowledged_count"], 1)
        self.assertEqual(c["unacknowledged_count"], 0)
