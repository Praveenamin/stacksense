"""
Detect servers that have stopped pushing (DOWN) or have recovered (RESOLVED)
in the push-agent model, and send connection alerts on state changes only.

The agent updates ServerHeartbeat.last_heartbeat on every push (~30s). A server
is considered DOWN when its last heartbeat is older than --down-seconds. Alerts
fire once on the transition to down and once on recovery (using the most recent
CONNECTION AlertHistory record as the state marker, so there is no repeat spam).

Intended to run frequently (e.g. every 60s from the scheduler).

Usage:
    python manage.py check_server_connectivity [--down-seconds 90]
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Server, ServerHeartbeat, AlertHistory


class Command(BaseCommand):
    help = "Send DOWN/RESOLVED connection alerts based on agent heartbeat freshness."

    def add_arguments(self, parser):
        parser.add_argument(
            "--down-seconds", type=int, default=90,
            help="Seconds without a heartbeat before a server is considered down (default: 90)",
        )

    def handle(self, *args, **options):
        # Imported here to avoid import side effects at module load.
        from core.views import _send_connection_alert

        threshold = options["down_seconds"]
        now = timezone.now()
        down_alerts = resolved_alerts = 0

        servers = Server.objects.select_related("monitoring_config").all()
        for server in servers:
            config = getattr(server, "monitoring_config", None)
            # Skip servers that aren't actively monitored.
            if not config or not config.enabled or config.monitoring_suspended:
                continue

            hb = ServerHeartbeat.objects.filter(server=server).first()
            if hb is None:
                # Never reported a heartbeat -> agent not installed yet; don't alert.
                continue

            age = (now - hb.last_heartbeat).total_seconds()
            is_down = age > threshold

            # State marker: the most recent CONNECTION event. If it's 'triggered',
            # the server is currently in an alerted-down state.
            last_conn = (AlertHistory.objects
                         .filter(server=server, alert_type="CONNECTION")
                         .order_by("-sent_at")
                         .first())
            currently_alerted_down = bool(last_conn and last_conn.status == "triggered")

            if is_down and not currently_alerted_down:
                _send_connection_alert(server, "offline")
                down_alerts += 1
                self.stdout.write(self.style.WARNING(
                    f"DOWN: {server.name} (no heartbeat for {int(age)}s)"))
            elif not is_down and currently_alerted_down:
                _send_connection_alert(server, "online")
                resolved_alerts += 1
                self.stdout.write(self.style.SUCCESS(f"RESOLVED: {server.name}"))

        if down_alerts or resolved_alerts:
            self.stdout.write(f"Connectivity: {down_alerts} down, {resolved_alerts} resolved.")
