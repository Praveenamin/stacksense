"""Precompute the heavy dashboard panels (trend insights + AI recommendations) into
the cache, so dashboard refreshes never trigger the 30-day, all-server analyses on the
web workers. Run periodically by the scheduler (metrics_scheduler.py)."""
from django.core.management.base import BaseCommand

from core.dashboard_panels import refresh_panels


class Command(BaseCommand):
    help = "Precompute dashboard trend-insights + AI-recommendations panels into the cache"

    def handle(self, *args, **options):
        result = refresh_panels()
        self.stdout.write(self.style.SUCCESS(
            f"Precomputed dashboard panels: {result}"))
