"""
Phase 3 (evaluation engine) -- when does an alert fire / resolve?

This is the decision logic that runs *before* categorization (Phase 1) and routing
(Phase 2): the sustained-breach threshold state machine, service/container dedup, and
suppression. Anomaly detection has its own suite (test_anomaly.py); this targets the
threshold + state-transition gaps.

Threshold tests use a hermetic locmem cache (the engine keeps its sustain/previous
state in the Django cache). No users are seeded, so recipients_for() resolves to nobody
and the senders short-circuit before any SMTP -- we assert on the AlertHistory rows the
engine writes.
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (Server, MonitoringConfig, EmailAlertConfig, Service, Container,
                         AlertHistory, ServerHeartbeat)
from core.agent_api import evaluate_service_alerts, evaluate_container_alerts
from core.views import _check_and_send_alerts


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class ThresholdStateMachineTests(TestCase):
    """The sustained-breach threshold engine: arm on 1st reading, fire on the 2nd,
    ignore transient spikes, resolve on recovery -- and how it behaves while a breach
    persists across many cycles."""

    def setUp(self):
        cache.clear()
        self.server = Server.objects.create(name="t-vm", ip_address="10.7.7.1", username="agent")
        MonitoringConfig.objects.create(server=self.server, enabled=True, cpu_threshold=80,
                                        memory_threshold=90, disk_threshold=90,
                                        monitoring_suspended=False, alert_suppressed=False)
        EmailAlertConfig.objects.create(id=1, provider="gmail", username="x@x.test",
                                        smtp_host="smtp.gmail.com", smtp_port=587, enabled=True)

    def _cycle(self, cpu):
        """One collection cycle at the given CPU%, through the real engine."""
        _check_and_send_alerts(self.server, self._metric(cpu))

    def _metric(self, cpu):
        from core.models import SystemMetric
        return SystemMetric.objects.create(
            server=self.server, cpu_percent=cpu, memory_total=8_000_000_000,
            memory_available=6_000_000_000, memory_used=2_000_000_000, memory_percent=20,
            disk_io_read=0, disk_io_write=0, net_io_sent=0, net_io_recv=0)

    def _triggered(self):
        return AlertHistory.objects.filter(server=self.server, alert_type="CPU",
                                           status="triggered").count()

    def _resolved(self):
        return AlertHistory.objects.filter(server=self.server, alert_type="CPU",
                                           status="resolved").count()

    def test_first_breach_only_arms(self):
        self._cycle(95)
        self.assertEqual(self._triggered(), 0)   # 1st reading just arms "pending"

    def test_second_consecutive_breach_fires(self):
        self._cycle(95)
        self._cycle(95)
        self.assertEqual(self._triggered(), 1)

    def test_transient_spike_does_not_fire_and_emits_no_resolved(self):
        self._cycle(95)   # arm
        self._cycle(10)   # back to normal before the 2nd sustained reading
        self.assertEqual(self._triggered(), 0)
        self.assertEqual(self._resolved(), 0)   # no spurious resolved (never actually alerted)

    def test_recovery_emits_a_single_resolved(self):
        self._cycle(95)   # arm
        self._cycle(95)   # fire
        self._cycle(10)   # recover
        self.assertEqual(self._resolved(), 1)

    def test_sustained_breach_fires_once_per_episode(self):
        # A breach persisting across many cycles must alert ONCE for the episode -- not
        # every collection interval (which was ~120 alerts/hour for a stuck server). The
        # engine fires on the rising edge only; subsequent breaching cycles stay quiet.
        for _ in range(5):       # arm + 4 sustained breaching cycles
            self._cycle(95)
        self.assertEqual(self._triggered(), 1)

    def test_sustained_then_recovery_is_one_alert_one_resolved(self):
        for _ in range(5):       # sustained breach episode -> exactly one triggered
            self._cycle(95)
        self._cycle(10)          # recovery -> exactly one resolved
        self.assertEqual(self._triggered(), 1)
        self.assertEqual(self._resolved(), 1)


class ServiceContainerDedupTests(TestCase):
    """A unit that stays down must not raise a new alert every cycle; recovery resolves
    the open one; a fresh down after recovery is a new episode."""

    def setUp(self):
        self.server = Server.objects.create(name="t-vm", ip_address="10.7.7.2", username="agent")

    def test_service_down_across_cycles_keeps_one_open_alert(self):
        Service.objects.create(server=self.server, name="nginx", status="stopped",
                               monitoring_enabled=True)
        for _ in range(3):
            evaluate_service_alerts(self.server)
        self.assertEqual(
            AlertHistory.objects.filter(server=self.server, alert_type="SERVICE",
                                        status="triggered").count(), 1)

    def test_container_down_across_cycles_keeps_one_open_alert(self):
        Container.objects.create(server=self.server, name="db", state="exited",
                                 monitoring_enabled=True)
        for _ in range(3):
            evaluate_container_alerts(self.server)
        self.assertEqual(
            AlertHistory.objects.filter(server=self.server, alert_type="CONTAINER",
                                        status="triggered").count(), 1)

    def test_service_flap_down_up_down_is_two_episodes(self):
        svc = Service.objects.create(server=self.server, name="nginx", status="stopped",
                                     monitoring_enabled=True)
        evaluate_service_alerts(self.server)                 # episode 1 fires
        svc.status = "running"; svc.save(update_fields=["status"])
        evaluate_service_alerts(self.server)                 # episode 1 resolves
        svc.status = "stopped"; svc.save(update_fields=["status"])
        evaluate_service_alerts(self.server)                 # episode 2 fires
        self.assertEqual(
            AlertHistory.objects.filter(server=self.server, alert_type="SERVICE",
                                        status="triggered").count(), 1)  # one OPEN now
        self.assertEqual(
            AlertHistory.objects.filter(server=self.server, alert_type="SERVICE").count(), 2)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class SuppressionTests(TestCase):
    """A suppressed / suspended server raises nothing, no matter what the metrics say."""

    def setUp(self):
        cache.clear()
        self.server = Server.objects.create(name="t-vm", ip_address="10.7.7.3", username="agent")
        EmailAlertConfig.objects.create(id=1, provider="gmail", username="x@x.test",
                                        smtp_host="smtp.gmail.com", smtp_port=587, enabled=True)

    def _metric(self, cpu):
        from core.models import SystemMetric
        return SystemMetric.objects.create(
            server=self.server, cpu_percent=cpu, memory_total=8_000_000_000,
            memory_available=6_000_000_000, memory_used=2_000_000_000, memory_percent=20,
            disk_io_read=0, disk_io_write=0, net_io_sent=0, net_io_recv=0)

    def test_suppressed_server_raises_no_threshold_alert(self):
        MonitoringConfig.objects.create(server=self.server, enabled=True, cpu_threshold=80,
                                        alert_suppressed=True)
        for _ in range(3):
            _check_and_send_alerts(self.server, self._metric(99))
        self.assertEqual(AlertHistory.objects.filter(server=self.server).count(), 0)

    def test_suppressed_server_raises_no_service_alert(self):
        MonitoringConfig.objects.create(server=self.server, enabled=True, alert_suppressed=True)
        Service.objects.create(server=self.server, name="nginx", status="stopped",
                               monitoring_enabled=True)
        evaluate_service_alerts(self.server)
        self.assertEqual(AlertHistory.objects.filter(server=self.server).count(), 0)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class DiskThresholdStateMachineTests(TestCase):
    """Disk thresholds are evaluated PER PARTITION (a dict of mountpoints), so each
    partition has its own rising/falling edge. No sustain window for disk."""

    def setUp(self):
        cache.clear()
        self.server = Server.objects.create(name="t-vm", ip_address="10.7.7.4", username="agent")
        MonitoringConfig.objects.create(server=self.server, enabled=True, cpu_threshold=80,
                                        memory_threshold=90, disk_threshold=90,
                                        monitoring_suspended=False, alert_suppressed=False)
        EmailAlertConfig.objects.create(id=1, provider="gmail", username="x@x.test",
                                        smtp_host="smtp.gmail.com", smtp_port=587, enabled=True)

    def _cycle(self, partitions):
        """One cycle with disk_usage = {mountpoint: percent}, through the real engine."""
        from core.models import SystemMetric
        m = SystemMetric.objects.create(
            server=self.server, cpu_percent=5, memory_total=8_000_000_000,
            memory_available=7_000_000_000, memory_used=1_000_000_000, memory_percent=10,
            disk_io_read=0, disk_io_write=0, net_io_sent=0, net_io_recv=0,
            disk_usage={mp: {"percent": pct} for mp, pct in partitions.items()})
        _check_and_send_alerts(self.server, m)

    def _triggered(self):
        return AlertHistory.objects.filter(server=self.server, alert_type="Disk",
                                           status="triggered").count()

    def _resolved(self):
        return AlertHistory.objects.filter(server=self.server, alert_type="Disk",
                                           status="resolved").count()

    def test_disk_breach_fires_once_not_every_cycle(self):
        self._cycle({"/": 95})
        self._cycle({"/": 95})
        self._cycle({"/": 95})
        self.assertEqual(self._triggered(), 1)

    def test_partitions_are_independent(self):
        self._cycle({"/": 95, "/data": 50})        # only / breaches
        self.assertEqual(self._triggered(), 1)
        self._cycle({"/": 95, "/data": 95})        # /data now breaches; / stays quiet
        self.assertEqual(self._triggered(), 2)

    def test_disk_recovery_resolves_that_partition(self):
        self._cycle({"/": 95})                     # fire
        self._cycle({"/": 50})                     # recover
        self.assertEqual(self._triggered(), 1)
        self.assertEqual(self._resolved(), 1)


class ConnectivityDetectionTests(TestCase):
    """check_server_connectivity decides DOWN/UP from heartbeat freshness and fires a
    CONNECTION alert once per transition (state marker = the latest CONNECTION row)."""

    def setUp(self):
        self.server = Server.objects.create(name="t-vm", ip_address="10.6.6.1", username="agent")
        MonitoringConfig.objects.create(server=self.server, enabled=True,
                                        monitoring_suspended=False, alert_suppressed=False)
        EmailAlertConfig.objects.create(id=1, provider="gmail", username="x@x.test",
                                        smtp_host="smtp.gmail.com", smtp_port=587, enabled=True)

    def _heartbeat(self, age_seconds):
        ServerHeartbeat.objects.update_or_create(
            server=self.server,
            defaults={"last_heartbeat": timezone.now() - timedelta(seconds=age_seconds)})

    def _run(self, down_seconds=90):
        call_command("check_server_connectivity", down_seconds=down_seconds)

    def _conn(self, status):
        return AlertHistory.objects.filter(server=self.server, alert_type="CONNECTION",
                                           status=status).count()

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_stale_heartbeat_fires_a_critical_down_alert(self, _routing):
        self._heartbeat(age_seconds=300)           # 5 min since last push, > 90s
        self._run()
        self.assertEqual(self._conn("triggered"), 1)
        ah = AlertHistory.objects.get(server=self.server, alert_type="CONNECTION",
                                      status="triggered")
        self.assertEqual(ah.severity, "CRITICAL")   # server-down = CRITICAL (Phase 1)

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_fresh_heartbeat_does_not_alert(self, _routing):
        self._heartbeat(age_seconds=10)
        self._run()
        self.assertEqual(self._conn("triggered"), 0)

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_down_does_not_refire_while_still_down(self, _routing):
        self._heartbeat(age_seconds=300)
        self._run()                                 # transition -> down
        self._run()                                 # still down -> must NOT re-fire
        self.assertEqual(self._conn("triggered"), 1)

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_recovery_fires_resolved(self, _routing):
        self._heartbeat(age_seconds=300)
        self._run()                                 # down
        self._heartbeat(age_seconds=5)              # agent pushing again
        self._run()                                 # recovery
        self.assertEqual(self._conn("resolved"), 1)

    def test_server_that_never_reported_is_skipped(self):
        # No ServerHeartbeat -> agent not installed yet -> never alerts.
        self._run()
        self.assertEqual(AlertHistory.objects.filter(server=self.server).count(), 0)

    @patch("core.views.alert_routing.recipients_for", return_value=[])
    def test_suspended_server_is_skipped(self, _routing):
        cfg = self.server.monitoring_config
        cfg.monitoring_suspended = True
        cfg.save(update_fields=["monitoring_suspended"])
        self._heartbeat(age_seconds=300)
        self._run()
        self.assertEqual(AlertHistory.objects.filter(server=self.server).count(), 0)
