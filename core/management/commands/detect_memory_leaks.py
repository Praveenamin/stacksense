"""
Detect memory leaks (system RAM growth, per-process RSS growth, SysV IPC / shared
memory) for each monitored server and record them as Anomaly rows.

Runs on a slow cadence (hourly is plenty -- leaks evolve over hours/days). All trend
math is server-side; the agent only reports raw stats. Findings are deduplicated over
a 24h window so a persistent leak doesn't spam a new anomaly every run.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Server, SystemMetric, Anomaly
from core.utils.leak_detection import detect_leaks

DEDUP_HOURS = 24


class Command(BaseCommand):
    help = "Detect memory / shared-memory / semaphore leaks and record them as anomalies."

    def handle(self, *args, **options):
        servers = Server.objects.select_related("monitoring_config").all()
        created = 0

        for server in servers:
            config = getattr(server, "monitoring_config", None)
            if not config or not config.enabled:
                continue
            if getattr(config, "anomaly_sensitivity", "BALANCED") == "OFF":
                continue

            try:
                findings = detect_leaks(server)
            except Exception as e:
                self.stderr.write(f"Leak detection failed for {server.name}: {e}")
                continue
            if not findings:
                continue

            latest = (
                SystemMetric.objects.filter(server=server).order_by("-timestamp").first()
            )
            if latest is None:
                continue

            recent = timezone.now() - timedelta(hours=DEDUP_HOURS)
            for f in findings:
                # Dedup: one unresolved finding per (server, metric_name) per 24h.
                if Anomaly.objects.filter(
                    server=server,
                    metric_name=f["metric_name"],
                    resolved=False,
                    timestamp__gte=recent,
                ).exists():
                    self.stdout.write(
                        f"⚠ Skipping duplicate leak: {server.name} - {f['metric_name']}"
                    )
                    continue

                Anomaly.objects.create(server=server, metric=latest, **f)
                created += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"⚑ Leak: {server.name} [{f['severity']}] {f['explanation']}"
                    )
                )

        if created:
            self.stdout.write(self.style.SUCCESS(f"Recorded {created} leak anomaly/anomalies."))
        else:
            self.stdout.write("No memory leaks detected.")
