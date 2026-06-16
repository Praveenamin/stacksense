"""Anomalies are notifications, not alerts.

Covers the new dashboard anomalies surface and the decoupling:
  - /api/anomalies/notifications/ lists unresolved anomalies (global) + a count
  - /api/anomalies/clear-all/ marks them all resolved (kept as history)
  - unresolved anomalies no longer make a server "warning" (_calculate_server_status)
  - the alerts page (alert_history) no longer includes anomalies
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (Server, SystemMetric, Anomaly, AlertHistory,
                         ServerHeartbeat, MonitoringConfig)
from core.views import _calculate_server_status

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "b@x.test", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.server = Server.objects.create(name="s1", ip_address="10.0.0.1", username="agent")
        self.metric = SystemMetric.objects.create(
            server=self.server, cpu_percent=95.0, memory_total=8_000_000_000,
            memory_available=4_000_000_000, memory_percent=50.0, memory_used=4_000_000_000)

    def _anomaly(self, resolved=False, severity=Anomaly.Severity.MEDIUM, mtype="cpu"):
        return Anomaly.objects.create(
            server=self.server, metric=self.metric, metric_type=mtype,
            metric_name=f"{mtype}_percent", metric_value=99.0, anomaly_score=1.0,
            severity=severity, resolved=resolved,
            resolved_at=timezone.now() if resolved else None)

    def _fresh_heartbeat(self):
        ServerHeartbeat.objects.update_or_create(
            server=self.server, defaults={"last_heartbeat": timezone.now()})


class NotificationsApiTests(_Base):
    def test_lists_unresolved_with_count(self):
        self._anomaly()
        self._anomaly(severity=Anomaly.Severity.HIGH, mtype="memory")
        self._anomaly(resolved=True)                      # resolved -> excluded
        r = self.client.get(reverse("anomaly_notifications_api"))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual({i["metric_type"] for i in data["items"]}, {"CPU", "MEMORY"})
        self.assertIn("server", data["items"][0])

    def test_empty_when_none_unresolved(self):
        self._anomaly(resolved=True)
        data = self.client.get(reverse("anomaly_notifications_api")).json()
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["items"], [])

    def test_requires_auth(self):
        r = Client().get(reverse("anomaly_notifications_api"))   # no login
        self.assertNotEqual(r.status_code, 200)                  # gated (401/302/403)


class ClearAllApiTests(_Base):
    def test_clear_all_resolves_every_unresolved(self):
        self._anomaly(); self._anomaly(mtype="memory"); self._anomaly(resolved=True)
        r = self.client.post(reverse("anomaly_clear_all_api"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cleared_count"], 2)
        self.assertEqual(Anomaly.objects.filter(resolved=False).count(), 0)
        # kept as history (resolved), not deleted
        self.assertEqual(Anomaly.objects.count(), 3)
        self.assertEqual(Anomaly.objects.filter(resolved=True).count(), 3)

    def test_clear_all_is_idempotent(self):
        self._anomaly()
        self.client.post(reverse("anomaly_clear_all_api"))
        r = self.client.post(reverse("anomaly_clear_all_api"))   # nothing left
        self.assertEqual(r.json()["cleared_count"], 0)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("anomaly_clear_all_api")).status_code, 405)


class StatusDecouplingTests(_Base):
    def test_unresolved_anomaly_does_not_make_server_warning(self):
        self._fresh_heartbeat()
        self._anomaly()                                    # open anomaly, no real alert
        self.assertEqual(_calculate_server_status(self.server), "online")

    def test_real_alert_still_warns(self):
        self._fresh_heartbeat()
        AlertHistory.objects.create(
            server=self.server, alert_type=AlertHistory.AlertType.CPU,
            status="triggered", message="CPU high", value=95.0, threshold=90.0)
        self.assertEqual(_calculate_server_status(self.server), "warning")


class DashboardSummaryExcludesAnomaliesTests(_Base):
    def test_active_alerts_count_excludes_anomalies(self):
        self._fresh_heartbeat()
        self._anomaly(); self._anomaly()                  # 2 unresolved anomalies
        AlertHistory.objects.create(                       # 1 real triggered alert
            server=self.server, alert_type=AlertHistory.AlertType.CPU,
            status="triggered", message="cpu high", value=95.0, threshold=90.0)
        r = self.client.get(reverse("dashboard_summary_stats_api"))
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["active_alerts"], 1)         # alert only, anomalies excluded


class AlertsPageExcludesAnomaliesTests(_Base):
    def test_alerts_page_has_no_anomaly_rows(self):
        self._anomaly(severity=Anomaly.Severity.CRITICAL)
        r = self.client.get(reverse("alert_history"))
        self.assertEqual(r.status_code, 200)
        # every unified item is an alert, never an anomaly
        for item in r.context["unified_items"]:
            self.assertEqual(item["type"], "alert")
        self.assertNotIn("anomalies", r.context)
