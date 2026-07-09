"""
Phase 5 (data retention / pruning) -- the data-loss surface.

prune_old_data is the live, scheduled retention (metrics_scheduler runs it daily). It
enforces a sliding window: keep the last N days (AppConfig.data_retention_days, clamped
7..365), delete everything strictly older. The cardinal sin to guard against is deleting
data INSIDE the window; the secondary risks are off-by-one boundaries, the wrong models,
config not being honored, and daily roll-ups (kept 365d) being pruned with the raw window.
"""
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import (
    AppConfig, Server, Service, Container, ServerHeartbeat,
    SystemMetric, Anomaly, ServiceLatencyMeasurement, SSHAuthEvent, AlertHistory,
    SecurityEvent, LoginActivity, AggregatedMetric,
)


def _set_retention(days):
    cfg = AppConfig.get_config()
    cfg.data_retention_days = days
    cfg.save()


def _force_retention(days):
    """Write an out-of-range value straight to the DB, bypassing the model's 7..365
    validation -- to exercise the command's own defensive clamp."""
    cfg = AppConfig.get_config()
    AppConfig.objects.filter(pk=cfg.pk).update(data_retention_days=days)


class _Base(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="r-vm", ip_address="10.3.3.1", username="agent")
        self.service = Service.objects.create(server=self.server, name="nginx")

    def _metric(self, ts):
        return SystemMetric.objects.create(
            server=self.server, timestamp=ts, cpu_percent=10, memory_total=8_000_000_000,
            memory_available=4_000_000_000, memory_used=4_000_000_000, memory_percent=50)

    def _seed_every_pruned_model(self, ts):
        """One row, stamped at ts, in each of the 8 pruned time-series models."""
        m = self._metric(ts)
        Anomaly.objects.create(server=self.server, metric=m, timestamp=ts, metric_type="cpu",
                               metric_name="cpu_percent", metric_value=10, anomaly_score=0.5,
                               severity="LOW")
        ServiceLatencyMeasurement.objects.create(service=self.service, timestamp=ts,
                                                 latency_ms=12.0, success=True,
                                                 measurement_type="ping")
        SSHAuthEvent.objects.create(server=self.server, timestamp=ts, source_ip="1.2.3.4",
                                    username="root", success=False, raw="x")
        AlertHistory.objects.create(server=self.server, alert_type="CPU", status="triggered",
                                    severity="HIGH", value=1, threshold=1, message="x",
                                    recipients="", sent_at=ts)
        SecurityEvent.objects.create(event_type="BRUTE_FORCE", title="x", last_seen=ts)
        LoginActivity.objects.create(email="a@b.c", ip_address="1.2.3.4", status="failed",
                                     timestamp=ts)
        AggregatedMetric.objects.create(server=self.server, aggregation_type="hourly", timestamp=ts)

    def _pruned_counts(self):
        return {
            "SystemMetric": SystemMetric.objects.count(),
            "Anomaly": Anomaly.objects.count(),
            "ServiceLatencyMeasurement": ServiceLatencyMeasurement.objects.count(),
            "SSHAuthEvent": SSHAuthEvent.objects.count(),
            "AlertHistory": AlertHistory.objects.count(),
            "SecurityEvent": SecurityEvent.objects.count(),
            "LoginActivity": LoginActivity.objects.count(),
            "AggregatedMetric_hourly": AggregatedMetric.objects.filter(
                aggregation_type="hourly").count(),
        }


class RetentionWindowTests(_Base):
    """The data-loss properties: never delete inside the window; do delete beyond it."""

    def test_within_window_is_never_deleted(self):
        _set_retention(60)
        now = timezone.now()
        for d in (0, 1, 30, 59):
            self._metric(now - timedelta(days=d))
        call_command("prune_old_data")
        self.assertEqual(SystemMetric.objects.count(), 4)        # ALL kept

    def test_beyond_window_is_deleted(self):
        _set_retention(60)
        now = timezone.now()
        self._metric(now - timedelta(days=61))
        self._metric(now - timedelta(days=200))
        call_command("prune_old_data")
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_boundary_direction(self):
        _set_retention(30)
        now = timezone.now()
        keep = self._metric(now - timedelta(days=30) + timedelta(hours=1))   # 1h inside
        drop = self._metric(now - timedelta(days=30) - timedelta(hours=1))   # 1h beyond
        call_command("prune_old_data")
        self.assertTrue(SystemMetric.objects.filter(pk=keep.pk).exists())
        self.assertFalse(SystemMetric.objects.filter(pk=drop.pk).exists())

    def test_respects_configured_days(self):
        _set_retention(7)
        now = timezone.now()
        keep = self._metric(now - timedelta(days=5))
        drop = self._metric(now - timedelta(days=10))
        call_command("prune_old_data")
        self.assertTrue(SystemMetric.objects.filter(pk=keep.pk).exists())
        self.assertFalse(SystemMetric.objects.filter(pk=drop.pk).exists())

    def test_retention_clamped_below_minimum_7(self):
        _force_retention(1)                                      # below floor -> clamps to 7
        now = timezone.now()
        keep = self._metric(now - timedelta(days=5))            # inside the clamped 7d
        drop = self._metric(now - timedelta(days=10))
        call_command("prune_old_data")
        self.assertTrue(SystemMetric.objects.filter(pk=keep.pk).exists())
        self.assertFalse(SystemMetric.objects.filter(pk=drop.pk).exists())

    def test_retention_clamped_above_maximum_365(self):
        _force_retention(1000)                                  # above ceiling -> clamps to 365
        now = timezone.now()
        keep = self._metric(now - timedelta(days=300))
        drop = self._metric(now - timedelta(days=400))
        call_command("prune_old_data")
        self.assertTrue(SystemMetric.objects.filter(pk=keep.pk).exists())
        self.assertFalse(SystemMetric.objects.filter(pk=drop.pk).exists())

    def test_config_enforces_7_to_365_bounds(self):
        # First line of defense: the config itself refuses out-of-range retention.
        from django.core.exceptions import ValidationError
        for bad in (6, 366):
            with self.assertRaises(ValidationError):
                _set_retention(bad)
        for ok in (7, 365):                                     # inclusive bounds accepted
            _set_retention(ok)


class PerModelCoverageTests(_Base):
    """Closed set: every pruned model is actually pruned when old, and kept when recent;
    current-state models are never time-pruned."""

    def test_all_pruned_models_deleted_when_old(self):
        _set_retention(30)
        self._seed_every_pruned_model(timezone.now() - timedelta(days=100))
        call_command("prune_old_data")
        self.assertEqual(self._pruned_counts(), {k: 0 for k in self._pruned_counts()})

    def test_all_pruned_models_kept_when_recent(self):
        _set_retention(30)
        self._seed_every_pruned_model(timezone.now() - timedelta(days=1))
        call_command("prune_old_data")
        self.assertEqual(self._pruned_counts(), {k: 1 for k in self._pruned_counts()})

    def test_current_state_models_are_never_pruned(self):
        _set_retention(30)
        old = timezone.now() - timedelta(days=500)
        Container.objects.create(server=self.server, name="c", state="running")
        ServerHeartbeat.objects.create(server=self.server, last_heartbeat=old)
        call_command("prune_old_data")
        self.assertTrue(Server.objects.filter(pk=self.server.pk).exists())
        self.assertTrue(Service.objects.filter(pk=self.service.pk).exists())
        self.assertTrue(Container.objects.filter(server=self.server).exists())
        self.assertTrue(ServerHeartbeat.objects.filter(server=self.server).exists())


class RollupRetentionTests(_Base):
    """Daily roll-ups are kept for 365d (long-range trends), independent of the raw window."""

    def test_daily_rollup_kept_within_365d_even_beyond_raw_window(self):
        _set_retention(30)
        now = timezone.now()
        daily = AggregatedMetric.objects.create(server=self.server, aggregation_type="daily",
                                                timestamp=now - timedelta(days=100))
        hourly = AggregatedMetric.objects.create(server=self.server, aggregation_type="hourly",
                                                 timestamp=now - timedelta(days=100))
        call_command("prune_old_data")
        self.assertTrue(AggregatedMetric.objects.filter(pk=daily.pk).exists())    # kept
        self.assertFalse(AggregatedMetric.objects.filter(pk=hourly.pk).exists())  # pruned

    def test_daily_rollup_pruned_beyond_365d(self):
        _set_retention(30)
        old_daily = AggregatedMetric.objects.create(
            server=self.server, aggregation_type="daily",
            timestamp=timezone.now() - timedelta(days=400))
        call_command("prune_old_data")
        self.assertFalse(AggregatedMetric.objects.filter(pk=old_daily.pk).exists())


class OperationalSafetyTests(_Base):
    def test_dry_run_deletes_nothing(self):
        _set_retention(30)
        self._metric(timezone.now() - timedelta(days=100))
        call_command("prune_old_data", dry_run=True)
        self.assertEqual(SystemMetric.objects.count(), 1)        # nothing deleted

    def test_idempotent(self):
        _set_retention(30)
        self._metric(timezone.now() - timedelta(days=100))
        self._metric(timezone.now() - timedelta(days=1))
        call_command("prune_old_data")
        call_command("prune_old_data")                          # second run: clean, no-op
        self.assertEqual(SystemMetric.objects.count(), 1)       # the recent one

    def test_empty_db_runs_clean(self):
        _set_retention(30)
        call_command("prune_old_data")
        self.assertEqual(SystemMetric.objects.count(), 0)


class StaleServiceOrphanTests(_Base):
    """Stale auto-detected service rows (unmonitored + stopped + >24h unseen) are orphans left
    after the systemd<->port merge and must be pruned so each service shows as ONE row -- but a
    running, monitored, manual, or recently-seen service must never be deleted."""

    def _svc(self, name, *, status="stopped", monitored=False, auto=True, age_hours=48):
        return Service.objects.create(
            server=self.server, name=name, service_type="port",
            status=status, monitoring_enabled=monitored, auto_detected=auto,
            last_checked=timezone.now() - timedelta(hours=age_hours),
        )

    def test_stale_stopped_orphan_is_pruned(self):
        self._svc("port-22", status="stopped", monitored=False, auto=True, age_hours=48)
        call_command("prune_old_data")
        self.assertFalse(Service.objects.filter(name="port-22").exists())

    def test_running_service_is_kept(self):
        self._svc("apache2", status="running", monitored=False, auto=True, age_hours=48)
        call_command("prune_old_data")
        self.assertTrue(Service.objects.filter(name="apache2").exists())

    def test_monitored_service_is_kept(self):
        self._svc("mariadb", status="stopped", monitored=True, auto=True, age_hours=48)
        call_command("prune_old_data")
        self.assertTrue(Service.objects.filter(name="mariadb").exists())

    def test_manual_service_is_kept(self):
        self._svc("my-app", status="stopped", monitored=False, auto=False, age_hours=48)
        call_command("prune_old_data")
        self.assertTrue(Service.objects.filter(name="my-app").exists())

    def test_recently_seen_stopped_service_is_kept(self):
        # Stopped only an hour ago -> not yet an orphan (could be a brief restart).
        self._svc("port-9999", status="stopped", monitored=False, auto=True, age_hours=1)
        call_command("prune_old_data")
        self.assertTrue(Service.objects.filter(name="port-9999").exists())

    def test_dry_run_keeps_orphan(self):
        self._svc("port-53", status="stopped", monitored=False, auto=True, age_hours=48)
        call_command("prune_old_data", dry_run=True)
        self.assertTrue(Service.objects.filter(name="port-53").exists())
