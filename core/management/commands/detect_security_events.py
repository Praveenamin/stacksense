"""
Run one pass of the security detection engine.

Safe to run frequently (e.g. every 60s from the scheduler); it correlates
recent login activity and de-duplicates ongoing incidents.

Usage:
    python manage.py detect_security_events
"""

from django.core.management.base import BaseCommand

from core.security_monitor import detect_security_events


class Command(BaseCommand):
    help = "Detect authentication-based security events (brute force, failure spikes, takeovers)."

    def handle(self, *args, **options):
        new_events = detect_security_events()
        if not new_events:
            self.stdout.write("No new security events.")
            return
        for ev in new_events:
            self.stdout.write(self.style.WARNING(f"NEW [{ev.severity}] {ev.title}"))
        self.stdout.write(f"{len(new_events)} new security event(s).")
