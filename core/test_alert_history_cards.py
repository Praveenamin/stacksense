"""The Alerts page summary cards must agree with the list.

The page is a "critical alerts" view: LOW-severity anomalies are demoted off the list.
Previously the summary cards counted them anyway (and labelled them "Critical"), so a
screen full of Low anomalies showed "2 Total Critical Alerts" over an empty list. The
cards now (a) exclude LOW anomalies like the list does, and (b) count "Critical
Severity" as actually-CRITICAL items, not "everything unacknowledged".
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import Server, SystemMetric, Anomaly

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

    def _anom(self, severity, resolved=False):
        return Anomaly.objects.create(
            server=self.server, metric=self.metric, metric_type="cpu",
            metric_name="cpu_percent", metric_value=90.0, anomaly_score=1.0,
            severity=severity, resolved=resolved,
            resolved_at=timezone.now() if resolved else None, timestamp=timezone.now())

    def _ctx(self):
        return self.client.get(self.url).context        # default time_range=24h

    def test_only_low_anomalies_all_cards_zero_and_list_empty(self):
        self._anom(Anomaly.Severity.LOW, resolved=False)
        self._anom(Anomaly.Severity.LOW, resolved=True)
        c = self._ctx()
        self.assertEqual(len(c["unified_items"]), 0)         # hidden from the list
        self.assertEqual(c["triggered_count"], 0)            # "Total Critical Alerts"
        self.assertEqual(c["unacknowledged_count"], 0)
        self.assertEqual(c["acknowledged_count"], 0)
        self.assertEqual(c["critical_severity_count"], 0)    # cards now agree with list

    def test_medium_anomaly_counted_but_not_critical_severity(self):
        self._anom(Anomaly.Severity.MEDIUM, resolved=False)
        c = self._ctx()
        self.assertEqual(len(c["unified_items"]), 1)
        self.assertEqual(c["unacknowledged_count"], 1)
        self.assertEqual(c["critical_severity_count"], 0)    # MEDIUM is not CRITICAL

    def test_critical_severity_counts_only_critical(self):
        self._anom(Anomaly.Severity.CRITICAL, resolved=False)
        self._anom(Anomaly.Severity.HIGH, resolved=False)
        self._anom(Anomaly.Severity.LOW, resolved=False)     # excluded entirely
        c = self._ctx()
        self.assertEqual(c["critical_severity_count"], 1)    # only the CRITICAL one
        self.assertEqual(c["unacknowledged_count"], 2)       # CRITICAL + HIGH (LOW dropped)
        self.assertEqual(len(c["unified_items"]), 2)

    def test_resolved_high_anomaly_is_acknowledged(self):
        self._anom(Anomaly.Severity.HIGH, resolved=True)
        c = self._ctx()
        self.assertEqual(c["acknowledged_count"], 1)
        self.assertEqual(c["unacknowledged_count"], 0)
