"""Agent-side per-service response time + Slow/Degraded state.

Phase 2: the services ingest stores a ServiceLatencyMeasurement history row for MONITORED
services when the agent sends a latency sample (push-1.9.0+), stores nothing for unmonitored
services or old agents, and records a failed probe as latency 0 / success False."""
import json

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Server, AgentCredential, Service, ServiceLatencyMeasurement, AlertHistory, AppConfig,
)


class _IngestBase(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="db1", ip_address="10.0.0.5", username="agent")
        _, self.token = AgentCredential.generate_for_server(self.server)
        self.url = reverse("agent_ingest_services")
        self.client = Client()

    def _push(self, services):
        return self.client.post(
            self.url, data=json.dumps({"agent_version": "push-1.9.0", "services": services}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def _svc_item(self, name="mysqld", **over):
        item = {"name": name, "status": "running", "service_type": "port",
                "port": 3306, "bind_address": "0.0.0.0",
                "latency_ms": 12.5, "latency_success": True, "latency_type": "TCP"}
        item.update(over)
        return item


class Phase2IngestTests(_IngestBase):
    def test_latency_stored_for_monitored_service(self):
        # Monitoring is a server-side opt-in; enable it before the push.
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        r = self._push([self._svc_item()])
        self.assertEqual(r.status_code, 200)
        svc = Service.objects.get(server=self.server, name="mysqld")
        self.assertTrue(svc.monitoring_enabled)                 # not clobbered by the upsert
        rows = ServiceLatencyMeasurement.objects.filter(service=svc)
        self.assertEqual(rows.count(), 1)
        m = rows.first()
        self.assertEqual(m.latency_ms, 12.5)
        self.assertTrue(m.success)
        self.assertEqual(m.measurement_type, "TCP")

    def test_no_history_row_for_unmonitored_service(self):
        # New service -> monitoring_enabled defaults False -> no history row (bounds volume).
        r = self._push([self._svc_item()])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ServiceLatencyMeasurement.objects.count(), 0)

    def test_old_agent_payload_writes_no_latency(self):
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        # Old agent: service item carries NO latency_* keys.
        r = self._push([{"name": "mysqld", "status": "running", "service_type": "port", "port": 3306}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ServiceLatencyMeasurement.objects.count(), 0)

    def test_failed_probe_stored_as_zero(self):
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        r = self._push([self._svc_item(latency_ms=None, latency_success=False,
                                       latency_error="Connection refused")])
        self.assertEqual(r.status_code, 200)
        m = ServiceLatencyMeasurement.objects.get(service__name="mysqld")
        self.assertEqual(m.latency_ms, 0)
        self.assertFalse(m.success)
        self.assertIn("refused", (m.error_message or "").lower())


class Phase3DegradedTests(_IngestBase):
    """Anti-flap Slow/Degraded state: N consecutive over-threshold samples flip to 'slow';
    a single spike never does; hysteresis clears below 0.8x threshold. Defaults: 500 ms, N=3."""

    def _push_ms(self, ms, name="mysqld", success=True):
        return self._push([self._svc_item(name=name, latency_ms=ms, latency_success=success)])

    def _reload(self, name="mysqld"):
        return Service.objects.get(server=self.server, name=name)

    def test_slow_only_after_n_consecutive(self):
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        self._push_ms(800); self.assertEqual(self._reload().latency_status, "unknown")   # streak 1
        self._push_ms(800); self.assertEqual(self._reload().latency_status, "unknown")   # streak 2
        self._push_ms(800)                                                               # streak 3
        s = self._reload()
        self.assertEqual(s.latency_status, "slow")
        self.assertEqual(s.slow_streak, 3)

    def test_single_spike_does_not_flip(self):
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        self._push_ms(800)     # one spike
        self._push_ms(100)     # back to fast -> resets
        s = self._reload()
        self.assertEqual(s.latency_status, "ok")
        self.assertEqual(s.slow_streak, 0)

    def test_hysteresis_clears_and_holds(self):
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        for _ in range(3):
            self._push_ms(800)
        self.assertEqual(self._reload().latency_status, "slow")
        self._push_ms(450)     # in the 400..500 band -> hold slow (no flap)
        self.assertEqual(self._reload().latency_status, "slow")
        self._push_ms(100)     # below 0.8x threshold -> clears to ok
        self.assertEqual(self._reload().latency_status, "ok")

    def test_per_service_threshold_override_beats_global(self):
        # 200 ms is under the 500 ms global but over this service's 100 ms override.
        Service.objects.create(server=self.server, name="mysqld",
                               monitoring_enabled=True, latency_threshold_ms=100)
        for _ in range(3):
            self._push_ms(200)
        self.assertEqual(self._reload().latency_status, "slow")

    def test_snapshot_denormalized_even_for_unmonitored(self):
        # Unmonitored service: no history row, but the latest snapshot + state still update
        # so the Services page can show a response time.
        self._push_ms(120, name="redis")
        s = self._reload("redis")
        self.assertFalse(s.monitoring_enabled)
        self.assertEqual(s.last_latency_ms, 120)
        self.assertTrue(s.last_latency_success)
        self.assertEqual(s.latency_status, "ok")
        self.assertEqual(ServiceLatencyMeasurement.objects.count(), 0)

    def test_failed_probe_does_not_mark_slow(self):
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        self._push([self._svc_item(latency_ms=None, latency_success=False,
                                   latency_error="refused")])
        s = self._reload()
        self.assertEqual(s.latency_status, "unknown")
        self.assertFalse(s.last_latency_success)


class Phase4ServicesPageTests(TestCase):
    """The Services page shows a Response column with each service's latency. (The slow *state*
    and its filter now live in the Health column — see core/test_service_health.py.)"""

    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        self.server = Server.objects.create(name="db1", ip_address="10.0.0.5", username="agent")

    def test_page_shows_response_column(self):
        Service.objects.create(server=self.server, name="mysqld", status="running",
            service_type="systemd", monitoring_enabled=True, latency_status="slow",
            last_latency_ms=800, last_latency_success=True, last_latency_at=timezone.now())
        Service.objects.create(server=self.server, name="nginx", status="running",
            service_type="systemd", monitoring_enabled=True, latency_status="ok",
            last_latency_ms=30, last_latency_success=True, last_latency_at=timezone.now())
        r = self.client.get(reverse("services_overview"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn(">Response</th>", b)           # the Response column header
        self.assertIn("800 ms", b)                   # slow service response time
        self.assertIn("30 ms", b)                    # healthy service response time

    def test_never_measured_service_shows_dash(self):
        Service.objects.create(server=self.server, name="mysqld", status="running",
            service_type="systemd", monitoring_enabled=True)  # no latency yet
        r = self.client.get(reverse("services_overview"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("—", r.content.decode())       # em-dash placeholder for "not measured"


class Phase5SlowAlertTests(_IngestBase):
    """The opt-in slow-service alert: raised once when a monitored service is sustained-slow,
    resolved on recovery, and silent when disabled (default) or when the server is suppressed."""

    def _push_ms(self, ms, name="mysqld", success=True):
        return self._push([self._svc_item(name=name, latency_ms=ms, latency_success=success)])

    def _enable(self, on=True):
        cfg = AppConfig.get_config()
        cfg.slow_service_alert_enabled = on
        cfg.save()

    def _slow_alerts(self, status=None):
        qs = AlertHistory.objects.filter(alert_type=AlertHistory.AlertType.SERVICE,
                                         message__contains="[svc-slow:")
        return qs.filter(status=status) if status else qs

    def test_alert_raised_once_and_resolved(self):
        self._enable(True)
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        for _ in range(5):                       # sustained slow (well past N=3)
            self._push_ms(800)
        triggered = self._slow_alerts(AlertHistory.AlertStatus.TRIGGERED)
        self.assertEqual(triggered.count(), 1)   # single alert, not one per push
        self.assertEqual(triggered.first().severity, "MEDIUM")
        self._push_ms(100)                        # recover
        self.assertEqual(self._slow_alerts(AlertHistory.AlertStatus.TRIGGERED).count(), 0)
        self.assertEqual(self._slow_alerts(AlertHistory.AlertStatus.RESOLVED).count(), 1)

    def test_no_alert_when_disabled(self):
        self._enable(False)                       # default anyway
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        for _ in range(3):
            self._push_ms(800)
        self.assertEqual(self._slow_alerts().count(), 0)

    def test_no_alert_when_service_toggle_off(self):
        # Master on, but this service's per-service toggle is off -> no slow alert.
        self._enable(True)
        Service.objects.create(server=self.server, name="mysqld",
                               monitoring_enabled=True, slow_alert_enabled=False)
        for _ in range(3):
            self._push_ms(800)
        self.assertEqual(self._slow_alerts().count(), 0)

    def test_no_alert_when_server_suppressed(self):
        self._enable(True)
        self.server.suppress_alerts = True
        self.server.save(update_fields=["suppress_alerts"])
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        for _ in range(3):
            self._push_ms(800)
        self.assertEqual(self._slow_alerts(AlertHistory.AlertStatus.TRIGGERED).count(), 0)

    def test_master_toggle_endpoint_flips_appconfig(self):
        c = Client(); c.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        self.assertFalse(AppConfig.get_config().slow_service_alert_enabled)
        r = c.post(reverse("toggle_slow_alert_master"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["enabled"])
        self.assertTrue(AppConfig.get_config().slow_service_alert_enabled)

    def test_per_service_toggle_endpoint_flips_field(self):
        c = Client(); c.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        svc = Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True)
        r = c.post(reverse("toggle_service_slow_alert", args=[self.server.id, svc.id]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["slow_alert_enabled"])
        svc.refresh_from_db()
        self.assertTrue(svc.slow_alert_enabled)

    def test_per_service_column_only_shown_when_master_on(self):
        c = Client(); c.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        Service.objects.create(server=self.server, name="mysqld", service_type="port", port=3306,
                               monitoring_enabled=True, slow_alert_enabled=True)
        # Master OFF: the fleet control shows, but no per-service Slow-alert column/toggle.
        b = c.get(reverse("services_overview")).content.decode()
        self.assertIn("Slow-service alerts (fleet)", b)
        self.assertNotIn(">Slow alert</th>", b)
        self.assertNotIn('onchange="toggleServiceSlowAlert(', b)   # no per-service toggle rendered
        # Master ON: the per-service column + toggle appear.
        cfg = AppConfig.get_config(); cfg.slow_service_alert_enabled = True; cfg.save()
        b2 = c.get(reverse("services_overview")).content.decode()
        self.assertIn(">Slow alert</th>", b2)
        self.assertIn('onchange="toggleServiceSlowAlert(', b2)

    def test_slow_toggle_only_for_ported_services(self):
        c = Client(); c.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        cfg = AppConfig.get_config(); cfg.slow_service_alert_enabled = True; cfg.save()
        ported = Service.objects.create(server=self.server, name="webapp",
            service_type="port", port=8080, monitoring_enabled=True)
        portless = Service.objects.create(server=self.server, name="cron",
            service_type="systemd", monitoring_enabled=True)
        b = c.get(reverse("services_overview")).content.decode()
        self.assertIn('onchange="toggleServiceSlowAlert(%d, %d,' % (self.server.id, ported.id), b)
        self.assertNotIn('onchange="toggleServiceSlowAlert(%d, %d,' % (self.server.id, portless.id), b)
        self.assertIn("No listening port to time", b)   # the portless dash tooltip

    def test_slow_alert_coexists_with_down_alert(self):
        # A slow alert uses a distinct marker so it never collides with the down alert.
        self._enable(True)
        Service.objects.create(server=self.server, name="mysqld", monitoring_enabled=True, slow_alert_enabled=True)
        for _ in range(3):
            self._push_ms(800)
        self.assertEqual(self._slow_alerts(AlertHistory.AlertStatus.TRIGGERED).count(), 1)
        # Now the service goes fully down -> a separate [svc:] alert, slow one still distinct.
        self._push([{"name": "mysqld", "status": "stopped", "service_type": "systemd"}])
        down = AlertHistory.objects.filter(alert_type=AlertHistory.AlertType.SERVICE,
                                           message__contains="[svc:mysqld]",
                                           status=AlertHistory.AlertStatus.TRIGGERED)
        self.assertEqual(down.count(), 1)


class ServiceLabelTests(_IngestBase):
    """Operator-assigned name for a (custom) port service: overrides the auto label, survives
    agent pushes, and is set/cleared via the endpoint."""

    def test_user_label_wins_and_survives_agent_push(self):
        svc = Service.objects.create(server=self.server, name="port-3000", service_type="port",
            port=3000, display_name="port-3000", user_label="Checkout API")
        self.assertEqual(svc.label, "Checkout API")
        # An agent push for the same (server, name) must NOT clobber the user's name.
        self._push([{"name": "port-3000", "status": "running", "service_type": "port",
                     "port": 3000, "display_name": "node (:3000)"}])
        svc.refresh_from_db()
        self.assertEqual(svc.user_label, "Checkout API")
        self.assertEqual(svc.label, "Checkout API")

    def test_set_label_endpoint_sets_and_clears(self):
        c = Client(); c.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        svc = Service.objects.create(server=self.server, name="port-3000",
                                     service_type="port", port=3000)
        r = c.post(reverse("set_service_label", args=[self.server.id, svc.id]),
                   data=json.dumps({"label": "Checkout API"}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["label"], "Checkout API")
        svc.refresh_from_db()
        self.assertEqual(svc.user_label, "Checkout API")
        # Empty value clears it back to the auto-detected name.
        c.post(reverse("set_service_label", args=[self.server.id, svc.id]),
               data=json.dumps({"label": ""}), content_type="application/json")
        svc.refresh_from_db()
        self.assertIsNone(svc.user_label)


class ServiceThresholdTests(_IngestBase):
    """Per-service response-time SLO threshold, entered in SECONDS, stored as ms."""

    def _client(self):
        c = Client(); c.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        return c

    def _svc(self):
        return Service.objects.create(server=self.server, name="webapp",
                                      service_type="port", port=8080)

    def test_seconds_stored_as_ms(self):
        c, svc = self._client(), self._svc()
        r = c.post(reverse("set_service_threshold", args=[self.server.id, svc.id]),
                   data=json.dumps({"seconds": 2}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        svc.refresh_from_db()
        self.assertEqual(svc.latency_threshold_ms, 2000)         # 2 s -> 2000 ms
        c.post(reverse("set_service_threshold", args=[self.server.id, svc.id]),
               data=json.dumps({"seconds": 0.5}), content_type="application/json")
        svc.refresh_from_db()
        self.assertEqual(svc.latency_threshold_ms, 500)          # 0.5 s -> 500 ms

    def test_blank_clears_to_global_default(self):
        c = self._client()
        svc = Service.objects.create(server=self.server, name="webapp",
                                     service_type="port", port=8080, latency_threshold_ms=2000)
        c.post(reverse("set_service_threshold", args=[self.server.id, svc.id]),
               data=json.dumps({"seconds": ""}), content_type="application/json")
        svc.refresh_from_db()
        self.assertIsNone(svc.latency_threshold_ms)              # NULL -> use global 500 ms

    def test_out_of_range_rejected(self):
        c, svc = self._client(), self._svc()
        r = c.post(reverse("set_service_threshold", args=[self.server.id, svc.id]),
                   data=json.dumps({"seconds": 5000}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        svc.refresh_from_db()
        self.assertIsNone(svc.latency_threshold_ms)

    def test_default_slo_rendered_for_monitored_ported_service(self):
        c = self._client()
        Service.objects.create(server=self.server, name="webapp", service_type="port",
                               port=8080, monitoring_enabled=True)
        b = c.get(reverse("services_overview")).content.decode()
        self.assertIn("SLO ≤ 0.5s", b)             # global default 500 ms -> 0.5 s
        self.assertIn("editThreshold(", b)              # inline editor wired for admins

    def test_custom_slo_rendered(self):
        c = self._client()
        Service.objects.create(server=self.server, name="webapp", service_type="port",
                               port=8080, monitoring_enabled=True, latency_threshold_ms=1500)
        b = c.get(reverse("services_overview")).content.decode()
        self.assertIn("SLO ≤ 1.5s", b)             # per-service override, trailing zeros trimmed

    def test_no_slo_editor_for_portless_service(self):
        c = self._client()
        Service.objects.create(server=self.server, name="cron", service_type="systemd",
                               port=None, monitoring_enabled=True)
        b = c.get(reverse("services_overview")).content.decode()
        # Portless units are up/down only — no response-time SLO to time or edit.
        self.assertNotIn("SLO ≤", b)
