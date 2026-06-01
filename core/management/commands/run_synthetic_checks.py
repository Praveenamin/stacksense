"""
Run all due synthetic (uptime) checks.

Each check has its own interval; this command runs only the ones that are due,
so it is safe to invoke frequently (e.g. every 30s from the scheduler).

Usage:
    python manage.py run_synthetic_checks
    python manage.py run_synthetic_checks --all     # ignore intervals, run every enabled check now
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import SyntheticCheck
from core.synthetic import run_check


class Command(BaseCommand):
    help = "Run all due synthetic uptime checks (HTTP/TCP probes)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Run every enabled check now, ignoring intervals")

    def handle(self, *args, **options):
        now = timezone.now()
        run_all = options.get("all")
        checks = SyntheticCheck.objects.filter(enabled=True)

        ran = 0
        for check in checks:
            if not run_all and not check.is_due(now):
                continue
            try:
                result, transition = run_check(check)
                ran += 1
                state = "OK" if result.success else "FAIL"
                extra = f" -> {transition}" if transition else ""
                self.stdout.write(f"{check.name}: {state}{extra}")
            except Exception as e:  # never let one bad check stop the rest
                self.stderr.write(f"{check.name}: error - {e}")

        if ran == 0:
            self.stdout.write("No synthetic checks were due.")
