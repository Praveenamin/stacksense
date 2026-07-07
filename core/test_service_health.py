"""Per-service SLO Health (Availability + Latency).

Phase 1: the services ingest records one up/down ServiceAvailabilitySample per push for each
MONITORED service, AFTER the stopped-sweep, so a service that vanished from the push contributes
a DOWN sample (availability counts real downtime instead of reading 100% while dead)."""
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Server, AgentCredential, Service, ServiceAvailabilitySample, AppConfig, SyntheticCheck,
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

    def _svc_item(self, name="mysqld", status="running", **over):
        item = {"name": name, "status": status, "service_type": "systemd"}
        item.update(over)
        return item

    def _monitored(self, name="mysqld", **fields):
        # auto_detected=True so the stopped-sweep will mark it if it drops off a push.
        return Service.objects.create(
            server=self.server, name=name, status="running",
            monitoring_enabled=True, auto_detected=True, **fields)


class Phase1SamplingTests(_IngestBase):
    def _samples(self, name="mysqld"):
        return ServiceAvailabilitySample.objects.filter(service__name=name).order_by("timestamp")

    def test_monitored_running_service_samples_up(self):
        self._monitored("mysqld")
        r = self._push([self._svc_item("mysqld")])
        self.assertEqual(r.status_code, 200)
        s = self._samples()
        self.assertEqual(s.count(), 1)
        self.assertTrue(s.first().up)

    def test_unmonitored_service_not_sampled(self):
        # A reported-but-not-monitored service gets no availability samples.
        r = self._push([self._svc_item("nginx")])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ServiceAvailabilitySample.objects.count(), 0)

    def test_dropped_service_samples_down(self):
        # THE key case: a monitored service that vanishes from a push is swept to 'stopped'
        # AND recorded as a down sample (so downtime is counted, not silently dropped).
        self._monitored("mysqld")
        self._push([self._svc_item("mysqld")])            # up
        self._push([self._svc_item("other")])             # mysqld dropped -> swept -> down
        s = list(self._samples().values_list("up", flat=True))
        self.assertEqual(s, [True, False])

    def test_failed_probe_samples_down(self):
        # Running, but its port's most recent probe failed -> not accepting connections -> down.
        self._monitored("mysqld", port=3306)
        self._push([self._svc_item("mysqld", port=3306,
                                   latency_ms=None, latency_success=False, latency_type="TCP")])
        s = self._samples()
        self.assertEqual(s.count(), 1)
        self.assertFalse(s.first().up)

    def test_old_agent_payload_still_samples_from_status(self):
        # No latency keys at all (old agent). Availability still derives from reported status.
        self._monitored("mysqld")
        r = self._push([{"name": "mysqld", "status": "running", "service_type": "systemd"}])
        self.assertEqual(r.status_code, 200)
        s = self._samples()
        self.assertEqual(s.count(), 1)
        self.assertTrue(s.first().up)


class Phase2HealthTests(_IngestBase):
    """Health = Down (not up) -> Degraded (slow) -> Degraded (24h availability < target) ->
    Healthy, computed at ingest and denormalized onto Service."""

    def _reload(self, name="mysqld"):
        return Service.objects.get(server=self.server, name=name)

    def test_unknown_before_any_push(self):
        svc = self._monitored("mysqld")
        self.assertEqual(svc.health_status, "unknown")
        self.assertIsNone(svc.availability_24h_pct)

    def test_healthy_when_running_and_fast(self):
        self._monitored("mysqld", port=3306)
        self._push([self._svc_item("mysqld", port=3306,
                                   latency_ms=50, latency_success=True, latency_type="TCP")])
        s = self._reload()
        self.assertEqual(s.health_status, "healthy")
        self.assertEqual(s.availability_24h_pct, 100.0)

    def test_degraded_when_slow(self):
        self._monitored("mysqld", port=3306)
        for _ in range(3):   # sustained slow -> latency_status flips to 'slow' (N=3)
            self._push([self._svc_item("mysqld", port=3306,
                                       latency_ms=800, latency_success=True, latency_type="TCP")])
        s = self._reload()
        self.assertEqual(s.health_status, "degraded")
        self.assertEqual(s.health_reason, "responding slowly")
        self.assertEqual(s.availability_24h_pct, 100.0)   # slow but reachable = 100% available

    def test_down_when_stopped(self):
        self._monitored("mysqld")
        self._push([self._svc_item("mysqld")])            # healthy
        self._push([self._svc_item("other")])             # mysqld dropped -> swept -> down
        s = self._reload()
        self.assertEqual(s.health_status, "down")
        self.assertEqual(s.health_reason, "not running")

    def test_down_when_port_not_responding(self):
        self._monitored("mysqld", port=3306)
        self._push([self._svc_item("mysqld", port=3306,
                                   latency_ms=None, latency_success=False, latency_type="TCP")])
        s = self._reload()
        self.assertEqual(s.health_status, "down")
        self.assertEqual(s.health_reason, "port not responding")

    def test_degraded_when_availability_below_target(self):
        # Currently up + fast, but a rough recent history drags 24h availability under target.
        self._monitored("mysqld", port=3306)
        for _ in range(10):   # down samples (port failing), >= MIN_AVAIL_SAMPLES
            self._push([self._svc_item("mysqld", port=3306,
                                       latency_ms=None, latency_success=False, latency_type="TCP")])
        self._push([self._svc_item("mysqld", port=3306,        # now healthy again
                                   latency_ms=50, latency_success=True, latency_type="TCP")])
        s = self._reload()
        self.assertEqual(s.health_status, "degraded")         # 1 up / 11 total = 9% < 99%
        self.assertIn("availability", s.health_reason)

    def test_per_service_availability_target_overrides_global(self):
        # 90% availability: degraded under the 99% global, but healthy under a 80% override.
        self._monitored("mysqld", port=3306, availability_target_pct=80.0)
        self._push([self._svc_item("mysqld", port=3306,
                                   latency_ms=None, latency_success=False, latency_type="TCP")])
        for _ in range(9):
            self._push([self._svc_item("mysqld", port=3306,
                                       latency_ms=50, latency_success=True, latency_type="TCP")])
        s = self._reload()
        self.assertEqual(s.availability_24h_pct, 90.0)        # 9 up / 10 total
        self.assertEqual(s.health_status, "healthy")          # 90% >= 80% override


class Phase3ServicesPageTests(TestCase):
    """The Services page shows Health + Availability columns, Degraded/Down filter chips (the old
    Slow chip is gone), a per-server rollup badge, and a dash for unmonitored services."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        self.server = Server.objects.create(name="db1", ip_address="10.0.0.5", username="agent")

    def test_page_shows_health_and_availability(self):
        Service.objects.create(server=self.server, name="mysqld", status="running",
            service_type="systemd", monitoring_enabled=True, health_status="degraded",
            health_reason="responding slowly", availability_24h_pct=97.5,
            last_latency_ms=800, last_latency_success=True, latency_status="slow")
        Service.objects.create(server=self.server, name="nginx", status="running",
            service_type="systemd", monitoring_enabled=True, health_status="healthy",
            availability_24h_pct=100.0, last_latency_ms=20, last_latency_success=True)
        r = self.client.get(reverse("services_overview"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn(">Health</th>", b)
        self.assertIn(">Availability</th>", b)
        self.assertIn("● Degraded", b)
        self.assertIn("● Healthy", b)
        self.assertIn("97.5%", b)
        self.assertIn('data-health="degraded"', b)
        # New health filter chips replace the old Slow chip.
        self.assertIn('data-status="degraded"', b)
        self.assertIn('data-status="down"', b)
        self.assertNotIn('data-status="slow"', b)

    def test_unmonitored_service_shows_dash(self):
        Service.objects.create(server=self.server, name="mysqld", status="running",
            service_type="systemd", monitoring_enabled=False)   # not monitored
        r = self.client.get(reverse("services_overview"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("—", b)
        self.assertNotIn("● Healthy", b)   # no health badge for unmonitored


class Phase4RetentionTests(TestCase):
    """Availability samples are pruned by the data-retention window, like the latency history."""

    def test_old_samples_pruned_recent_kept(self):
        cfg = AppConfig.get_config()
        cfg.data_retention_days = 30
        cfg.save()
        server = Server.objects.create(name="db1", ip_address="10.0.0.5", username="agent")
        svc = Service.objects.create(server=server, name="mysqld", monitoring_enabled=True)
        old = ServiceAvailabilitySample.objects.create(
            service=svc, up=True, timestamp=timezone.now() - timedelta(days=45))
        recent = ServiceAvailabilitySample.objects.create(
            service=svc, up=False, timestamp=timezone.now() - timedelta(days=1))
        call_command("prune_old_data")
        ids = set(ServiceAvailabilitySample.objects.values_list("id", flat=True))
        self.assertNotIn(old.id, ids)      # older than 30d -> pruned
        self.assertIn(recent.id, ids)      # within the window -> kept


class DashboardServiceHealthTests(TestCase):
    """The dashboard always shows the agent-side Service-health summary; the synthetic
    (outside-in) Reliability row only appears when an uptime check is enabled."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))
        self.server = Server.objects.create(name="db1", ip_address="10.0.0.5", username="agent")

    def test_service_health_shown_and_synthetic_hidden_by_default(self):
        Service.objects.create(server=self.server, name="mysqld", status="running",
            service_type="systemd", monitoring_enabled=True, health_status="healthy",
            availability_24h_pct=99.9, last_latency_ms=20, last_latency_success=True)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("Service health · monitored services", b)
        self.assertIn("All services healthy", b)   # the calm verdict banner
        # No synthetic checks -> the external uptime row is hidden.
        self.assertNotIn("Reliability &amp; SLOs · last 30 days", b)

    def test_problem_banner_names_degraded_and_down_services(self):
        Service.objects.create(server=self.server, name="checkout-api", status="running",
            service_type="systemd", monitoring_enabled=True, health_status="down",
            health_reason="port not responding")
        Service.objects.create(server=self.server, name="search", status="running",
            service_type="systemd", monitoring_enabled=True, health_status="degraded",
            latency_status="slow", last_latency_ms=820, last_latency_success=True)
        Service.objects.create(server=self.server, name="nginx", status="running",
            service_type="systemd", monitoring_enabled=True, health_status="healthy")
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("2 services need attention", b)          # verdict, not counts
        self.assertIn("checkout-api", b)                       # problems named
        self.assertIn("search", b)
        self.assertIn("port not responding", b)                # down reason
        self.assertIn("820 ms (target 500)", b)                # degraded latency detail
        self.assertIn("1 other healthy", b)                    # demoted healthy count
        self.assertNotIn("All services healthy", b)            # not the calm banner

    def test_synthetic_row_appears_when_uptime_check_enabled(self):
        Service.objects.create(server=self.server, name="mysqld",
            monitoring_enabled=True, health_status="healthy")
        SyntheticCheck.objects.create(name="site", check_type="HTTP",
                                      url="https://x.test", enabled=True)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Reliability &amp; SLOs · last 30 days", r.content.decode())

    def test_empty_state_when_no_monitored_services(self):
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("No services monitored yet", r.content.decode())
