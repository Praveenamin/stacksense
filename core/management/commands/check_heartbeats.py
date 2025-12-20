"""
Django management command to check and report on server heartbeat status.

This command can be run periodically (e.g., via cron) to verify heartbeat
status and log any servers that haven't sent heartbeats recently.

Usage:
    python manage.py check_heartbeats
    python manage.py check_heartbeats --warn-seconds 90
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from core.models import Server, ServerHeartbeat


class Command(BaseCommand):
    help = "Check server heartbeat status and report any issues"

    def add_arguments(self, parser):
        parser.add_argument(
            '--warn-seconds',
            type=int,
            default=60,
            help='Number of seconds since last heartbeat to trigger warning (default: 60)',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed information for all servers',
        )

    def handle(self, *args, **options):
        warn_seconds = options['warn_seconds']
        verbose = options['verbose']
        
        servers = Server.objects.all().select_related('monitoring_config')
        now = timezone.now()
        
        online_count = 0
        warning_count = 0
        offline_count = 0
        no_heartbeat_count = 0
        
        self.stdout.write(f"Checking heartbeats (warn threshold: {warn_seconds}s)...")
        self.stdout.write("")
        
        for server in servers:
            try:
                heartbeat = ServerHeartbeat.objects.get(server=server)
                time_diff = now - heartbeat.last_heartbeat
                time_diff_seconds = time_diff.total_seconds()
                
                if time_diff_seconds <= warn_seconds:
                    status = "ONLINE"
                    status_style = self.style.SUCCESS
                    online_count += 1
                else:
                    status = "OFFLINE"
                    status_style = self.style.ERROR
                    offline_count += 1
                
                if verbose or time_diff_seconds > warn_seconds:
                    self.stdout.write(
                        f"{status_style(status)} {server.name} (ID: {server.id}) - "
                        f"Last heartbeat: {heartbeat.last_heartbeat.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"({int(time_diff_seconds)}s ago)"
                    )
                    
            except ServerHeartbeat.DoesNotExist:
                status = "NO HEARTBEAT"
                status_style = self.style.WARNING
                no_heartbeat_count += 1
                offline_count += 1
                
                if verbose:
                    self.stdout.write(
                        f"{status_style(status)} {server.name} (ID: {server.id}) - "
                        "No heartbeat record found"
                    )
        
        # Summary
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write(f"Summary:")
        self.stdout.write(f"  {self.style.SUCCESS('Online')}: {online_count}")
        self.stdout.write(f"  {self.style.ERROR('Offline')}: {offline_count}")
        self.stdout.write(f"  {self.style.WARNING('No Heartbeat')}: {no_heartbeat_count}")
        self.stdout.write(f"  Total Servers: {servers.count()}")
        self.stdout.write("=" * 60)
        
        # Exit with error code if any servers are offline
        if offline_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"\nWarning: {offline_count} server(s) are offline or have no heartbeat"
                )
            )
            import sys
            sys.exit(1)

