"""
Phase 1 (alert taxonomy & severity) -- INTEGRATION tests.

The pure mapping logic is unit-tested in test_alert_categories.py. This suite proves
the *wiring* that those unit tests can't: that severity is actually stamped at the
real AlertHistory create-sites when an alert is raised through production code paths,
and that the Alerts page derives each row's category/severity and filters by category
correctly.

Routing (Phase 2) is mocked to "no recipients" in the views tests so they stay focused
on severity stamping and never open an SMTP connection.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import alert_categories
from core.alert_categories import AlertCategory
from core.models import (Server, MonitoringConfig, EmailAlertConfig, Service, Container,
                         AlertHistory, Anomaly, SystemMetric)
from core.agent_api import evaluate_service_alerts, evaluate_container_alerts
from core.views import _send_connection_alert, _send_service_alert, _check_and_send_alerts


class CreateSiteSeverityTests(TestCase):
    """Severity is stamped correctly when alerts are raised through real code paths."""

    def setUp(self):
        self.server = Server.objects.create(name="t-vm", ip_address="10.9.9.9", username="agent")

    # --- agent_api: monitored service / container down -> HIGH, Availability ------
    def test_service_down_raises_high_availability_alert(self):
        Service.objects.create(server=self.server, name="nginx", status="stopped",
                               monitoring_enabled=True)
        evaluate_service_alerts(self.server)
        ah = AlertHistory.objects.get(server=self.server,
                                      alert_type=AlertHistory.AlertType.SERVICE)
        self.assertEqual(ah.severity, "HIGH")
        self.assertEqual(alert_categories.for_alert_type(ah.alert_type),
                         AlertCategory.AVAILABILITY)

    def test_container_down_raises_high_availability_alert(self):
        Container.objects.create(server=self.server, name="db", state="exited",
                                 monitoring_enabled=True)
        evaluate_container_alerts(self.server)
        ah = AlertHistory.objects.get(server=self.server,
                                      alert_type=AlertHistory.AlertType.CONTAINER)
        self.assertEqual(ah.severity, "HIGH")
        self.assertEqual(alert_categories.for_alert_type(ah.alert_type),
                         AlertCategory.AVAILABILITY)

    def test_service_recovery_resolves_and_retains_firing_severity(self):
        svc = Service.objects.create(server=self.server, name="nginx", status="stopped",
                                     monitoring_enabled=True)
        evaluate_service_alerts(self.server)               # raise
        svc.status = "running"
        svc.save(update_fields=["status"])
        evaluate_service_alerts(self.server)               # recover
        ah = AlertHistory.objects.get(server=self.server,
                                      alert_type=AlertHistory.AlertType.SERVICE)
        self.assertEqual(ah.status, AlertHistory.AlertStatus.RESOLVED)
        self.assertIsNotNone(ah.resolved_at)
        # Severity reflects the state at firing time and is kept on resolve.
        self.assertEqual(ah.severity, "HIGH")

    def test_unmonitored_service_does_not_alert(self):
        Service.objects.create(server=self.server, name="nginx", status="stopped",
                               monitoring_enabled=False)
        evaluate_service_alerts(self.server)
        self.assertFalse(AlertHistory.objects.filter(server=self.server).exists())

    # --- views: connection / service create-sites (routing mocked away) -----------
    def _enable_alert_channels(self):
        MonitoringConfig.objects.create(server=self.server, enabled=True,
                                        monitoring_suspended=False, alert_suppressed=False)
        EmailAlertConfig.objects.create(id=1, provider="gmail", username="x@x.test",
                                        smtp_host="smtp.gmail.com", smtp_port=587, enabled=True)

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_connection_offline_is_critical_and_online_is_low(self, _routing):
        self._enable_alert_channels()
        _send_connection_alert(self.server, "offline")
        down = AlertHistory.objects.get(server=self.server, alert_type="CONNECTION",
                                        status="triggered")
        self.assertEqual(down.severity, "CRITICAL")

        _send_connection_alert(self.server, "online")
        up = AlertHistory.objects.get(server=self.server, alert_type="CONNECTION",
                                      status="resolved")
        self.assertEqual(up.severity, "LOW")

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_service_view_alert_triggered_high_resolved_low(self, _routing):
        self._enable_alert_channels()
        svc = Service.objects.create(server=self.server, name="redis", status="stopped",
                                     monitoring_enabled=True)
        _send_service_alert(self.server, svc, "triggered")
        self.assertEqual(
            AlertHistory.objects.get(server=self.server, alert_type="SERVICE",
                                     status="triggered").severity, "HIGH")
        _send_service_alert(self.server, svc, "resolved")
        self.assertEqual(
            AlertHistory.objects.get(server=self.server, alert_type="SERVICE",
                                     status="resolved").severity, "LOW")


class AlertsPageCategoryTests(TestCase):
    """The Alerts page derives category/severity per row and filters by category."""

    def setUp(self):
        self.server = Server.objects.create(name="t-vm", ip_address="10.9.9.8", username="agent")
        self.admin = User.objects.create_superuser("p1admin", "p1admin@x.test", "pw")
        self.client.force_login(self.admin)
        now = timezone.now()

        AlertHistory.objects.create(server=self.server, alert_type="CONNECTION",
            status="triggered", severity="CRITICAL", value=0, threshold=0,
            message="server down", recipients="", sent_at=now)
        AlertHistory.objects.create(server=self.server, alert_type="CPU",
            status="triggered", severity="HIGH", value=95, threshold=80,
            message="cpu high", recipients="", sent_at=now)

        m = SystemMetric.objects.create(server=self.server, timestamp=now, cpu_percent=95,
            memory_total=8_000_000_000, memory_available=1_000_000_000,
            memory_used=7_000_000_000, memory_percent=88)
        # cpu anomaly -> Resource; leak anomaly -> Capacity; a LOW one to prove exclusion.
        Anomaly.objects.create(server=self.server, metric=m, timestamp=now, metric_type="cpu",
            metric_name="cpu_percent", metric_value=95, anomaly_score=0.9,
            severity="HIGH", resolved=False)
        Anomaly.objects.create(server=self.server, metric=m, timestamp=now,
            metric_type="process_rss_leak", metric_name="rss", metric_value=123,
            anomaly_score=0.9, severity="HIGH", resolved=False)
        Anomaly.objects.create(server=self.server, metric=m, timestamp=now, metric_type="cpu",
            metric_name="cpu_percent", metric_value=50, anomaly_score=0.5,
            severity="LOW", resolved=False)

    def _get(self, extra=""):
        return self.client.get(reverse("alert_history") + "?time_range=all" + extra)

    def _key(self, item):
        obj = item["object"]
        return (item["type"], getattr(obj, "alert_type", None) or obj.metric_type)

    def test_every_row_carries_the_right_category(self):
        items = self._get().context["unified_items"]
        cat = {self._key(i): i["category"] for i in items}
        self.assertEqual(cat[("alert", "CONNECTION")], AlertCategory.AVAILABILITY)
        self.assertEqual(cat[("alert", "CPU")], AlertCategory.RESOURCE)
        self.assertEqual(cat[("anomaly", "cpu")], AlertCategory.RESOURCE)
        self.assertEqual(cat[("anomaly", "process_rss_leak")], AlertCategory.CAPACITY)

    def test_severity_flows_through_to_each_item(self):
        items = self._get().context["unified_items"]
        sev = {self._key(i): i["severity"] for i in items}
        self.assertEqual(sev[("alert", "CONNECTION")], "CRITICAL")
        self.assertEqual(sev[("alert", "CPU")], "HIGH")

    def test_low_severity_anomaly_excluded_from_alerts_page(self):
        anomalies = [i for i in self._get().context["unified_items"] if i["type"] == "anomaly"]
        self.assertNotIn("LOW", [i["severity"] for i in anomalies])
        self.assertEqual(len(anomalies), 2)  # the two HIGH ones only

    def test_category_badges_render(self):
        html = self._get().content.decode()
        self.assertIn(">Availability</span>", html)
        self.assertIn(">Resource</span>", html)
        self.assertIn(">Capacity</span>", html)

    def test_category_filter_narrows_the_list(self):
        avail = self._get("&category=availability").content.decode()
        self.assertIn(">Availability</span>", avail)
        self.assertNotIn(">Resource</span>", avail)

        resource = self._get("&category=resource").content.decode()
        self.assertIn(">Resource</span>", resource)
        self.assertNotIn(">Availability</span>", resource)

        capacity = self._get("&category=capacity").content.decode()
        self.assertIn(">Capacity</span>", capacity)
        self.assertNotIn(">Resource</span>", capacity)

    def test_security_filter_is_empty_but_page_still_renders(self):
        r = self._get("&category=security")
        self.assertEqual(r.status_code, 200)
        # No AlertHistory/Anomaly maps to Security on this page.
        self.assertEqual(len(r.context["unified_items"]), 0)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ThresholdCreateSiteSeverityTests(TestCase):
    """The CPU/Mem/Disk threshold create-site (resource alerts). Driven through the real
    _check_and_send_alerts engine; the sustained-reading cache is primed so a single call
    fires. Hermetic locmem cache so it can't read/write the app's Redis."""

    def setUp(self):
        cache.clear()
        self.server = Server.objects.create(name="t-vm", ip_address="10.9.9.7", username="agent")
        MonitoringConfig.objects.create(server=self.server, enabled=True, cpu_threshold=80,
                                        monitoring_suspended=False, alert_suppressed=False)
        EmailAlertConfig.objects.create(id=1, provider="gmail", username="x@x.test",
                                        smtp_host="smtp.gmail.com", smtp_port=587, enabled=True)

    def _metric(self, cpu):
        return SystemMetric.objects.create(
            server=self.server, cpu_percent=cpu, memory_total=8_000_000_000,
            memory_available=6_000_000_000, memory_used=2_000_000_000, memory_percent=25,
            disk_io_read=0, disk_io_write=0, net_io_sent=0, net_io_recv=0)

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_sustained_cpu_breach_stamps_high_resource(self, _routing):
        # Pretend the first breach reading was already seen, so this one triggers.
        cache.set(f"alert_pending:{self.server.id}", {"CPU": True})
        _check_and_send_alerts(self.server, self._metric(95))
        ah = AlertHistory.objects.get(server=self.server, alert_type="CPU", status="triggered")
        self.assertEqual(ah.severity, "HIGH")
        self.assertEqual(alert_categories.for_alert_type(ah.alert_type), AlertCategory.RESOURCE)

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_cpu_recovery_stamps_low(self, _routing):
        # Previous state shows CPU was breaching; now it's back to normal -> resolved/LOW.
        cache.set(f"alert_state:{self.server.id}",
                  {"CPU": True, "Memory": False, "Disk": {}, "DiskIO": False, "NetworkIO": False})
        _check_and_send_alerts(self.server, self._metric(10))
        ah = AlertHistory.objects.get(server=self.server, alert_type="CPU", status="resolved")
        self.assertEqual(ah.severity, "LOW")

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_first_breach_is_pending_and_does_not_alert(self, _routing):
        # No primed pending state -> the first breach only arms, it must not create a row.
        _check_and_send_alerts(self.server, self._metric(95))
        self.assertFalse(AlertHistory.objects.filter(server=self.server).exists())
