"""
Enforce the global data-retention window.

Sliding window: keep only the last N days of collected data (N =
AppConfig.data_retention_days, default 60, clamped 7..365) and delete everything
older. Run daily by the in-container scheduler. Irreversible.

Pruned (everything older than the window):
  Anomaly, SystemMetric, ServiceLatencyMeasurement, SSHAuthEvent, AlertHistory,
  SecurityEvent, LoginActivity, and HOURLY AggregatedMetric.
Kept longer (handled separately, Component 2): DAILY AggregatedMetric roll-ups.
Also pruned (separate, fixed 24h staleness): stale auto-detected service rows that are
  unmonitored + stopped + not seen in STALE_SERVICE_HOURS -- orphans (e.g. a listening-port
  row left behind after its systemd unit absorbed the port), so each service shows as ONE row.
Not pruned: Server / Container / heartbeat, and any running / monitored / manual Service.
"""
from datetime import timedelta
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import (
    AppConfig, Service, SystemMetric, Anomaly, ServiceLatencyMeasurement, ServiceAvailabilitySample,
    SSHAuthEvent, AlertHistory, SecurityEvent, LoginActivity, AggregatedMetric,
)

logger = logging.getLogger(__name__)

BATCH = 5000

# How long an auto-detected service must be unmonitored + stopped (unreported) before it's
# treated as an orphan and removed. Independent of the data_retention_days window: this is
# about de-duplicating the CURRENT service list, not aging out history.
STALE_SERVICE_HOURS = 24


class Command(BaseCommand):
    help = "Prune collected data older than the global retention window (AppConfig.data_retention_days)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be deleted, without deleting.")

    def _prune(self, model, field, cutoff, dry_run, extra=None):
        flt = {f"{field}__lt": cutoff}
        if extra:
            flt.update(extra)
        qs = model.objects.filter(**flt)
        if dry_run:
            return qs.count()
        total = 0
        while True:
            ids = list(qs.values_list("pk", flat=True)[:BATCH])
            if not ids:
                break
            model.objects.filter(pk__in=ids).delete()
            total += len(ids)
        return total

    def handle(self, *args, **options):
        dry = options["dry_run"]
        days = max(7, min(365, int(AppConfig.get_config().data_retention_days or 60)))
        cutoff = timezone.now() - timedelta(days=days)
        self.stdout.write(
            f"{'[DRY RUN] ' if dry else ''}Retention {days}d -> pruning data older than "
            f"{cutoff.strftime('%Y-%m-%d %H:%M')} UTC"
        )

        # Anomaly before SystemMetric so the per-table count is meaningful (SystemMetric
        # has an on_delete=CASCADE to its anomalies; pruning Anomaly first avoids that
        # cascade swallowing the count).
        targets = [
            ("Anomaly",                   Anomaly,                   "timestamp", None),
            ("SystemMetric",              SystemMetric,              "timestamp", None),
            ("ServiceLatencyMeasurement", ServiceLatencyMeasurement, "timestamp", None),
            ("ServiceAvailabilitySample", ServiceAvailabilitySample, "timestamp", None),
            ("SSHAuthEvent",              SSHAuthEvent,              "timestamp", None),
            ("AlertHistory",              AlertHistory,              "sent_at",   None),
            ("SecurityEvent",            SecurityEvent,             "last_seen", None),
            ("LoginActivity",            LoginActivity,             "timestamp", None),
            ("AggregatedMetric(hourly)", AggregatedMetric,          "timestamp", {"aggregation_type": "hourly"}),
        ]

        grand = 0
        for label, model, field, extra in targets:
            try:
                n = self._prune(model, field, cutoff, dry, extra)
            except Exception as e:
                logger.error("prune_old_data: %s failed: %s", label, e)
                self.stderr.write(self.style.ERROR(f"  {label}: error - {e}"))
                continue
            grand += n
            if n:
                self.stdout.write(f"  {'would prune' if dry else 'pruned'} {n:>8} {label}")

        # Daily roll-ups are kept much longer than the raw window (for long-range
        # trends/forecasts), capped at 365 days.
        daily_cutoff = timezone.now() - timedelta(days=365)
        try:
            nd = self._prune(AggregatedMetric, "timestamp", daily_cutoff, dry,
                             {"aggregation_type": "daily"})
            grand += nd
            if nd:
                self.stdout.write(f"  {'would prune' if dry else 'pruned'} {nd:>8} AggregatedMetric(daily >365d)")
        except Exception as e:
            logger.error("prune_old_data: daily-aggregate prune failed: %s", e)
            self.stderr.write(self.style.ERROR(f"  AggregatedMetric(daily): error - {e}"))

        # Stale auto-detected service ORPHANS (not part of the time window). A service that is
        # auto-detected, unmonitored, currently 'stopped', and hasn't been re-reported in
        # STALE_SERVICE_HOURS is a leftover -- e.g. a `port-22` row left behind after the `ssh`
        # systemd unit absorbed :22. Delete it so each running service is a single row. The
        # gates make this safe: a running, monitored, or manually-added service is never touched.
        svc_cutoff = timezone.now() - timedelta(hours=STALE_SERVICE_HOURS)
        try:
            ns = self._prune(
                Service, "last_checked", svc_cutoff, dry,
                {"auto_detected": True, "monitoring_enabled": False, "status": "stopped"},
            )
            grand += ns
            if ns:
                self.stdout.write(
                    f"  {'would prune' if dry else 'pruned'} {ns:>8} Service(stale stopped orphan)"
                )
        except Exception as e:
            logger.error("prune_old_data: stale-service prune failed: %s", e)
            self.stderr.write(self.style.ERROR(f"  Service(stale): error - {e}"))

        self.stdout.write(
            self.style.SUCCESS(f"{'Would prune' if dry else 'Pruned'} {grand} row(s) total.")
        )
