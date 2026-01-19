"""
Management command to collect service latency measurements for all monitored servers.
Run this command periodically (e.g., every minute) via cron or scheduler.

Usage:
    python manage.py collect_service_latency
    python manage.py collect_service_latency --server=1  # Specific server
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import Server, Service
from core.service_latency import collect_all_service_latencies
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Collect service latency measurements for monitored services"

    def add_arguments(self, parser):
        parser.add_argument(
            '--server',
            type=int,
            help='Server ID to collect latency for (optional, defaults to all)',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Print detailed output',
        )

    def handle(self, *args, **options):
        server_id = options.get('server')
        verbose = options.get('verbose', False)
        
        # Get servers with monitoring enabled
        if server_id:
            servers = Server.objects.filter(
                id=server_id,
                monitoring_config__enabled=True
            ).select_related('monitoring_config')
        else:
            servers = Server.objects.filter(
                monitoring_config__enabled=True
            ).select_related('monitoring_config')
        
        if not servers.exists():
            self.stdout.write(self.style.WARNING("No servers with monitoring enabled found."))
            return
        
        total_measurements = 0
        successful_measurements = 0
        failed_measurements = 0
        
        for server in servers:
            try:
                # Skip if monitoring is suspended
                if server.monitoring_config.monitoring_suspended:
                    if verbose:
                        self.stdout.write(f"Skipping {server.name} - monitoring suspended")
                    continue
                
                # Check if server has any monitored services
                monitored_count = Service.objects.filter(
                    server=server,
                    monitoring_enabled=True,
                    port__isnull=False
                ).exclude(port=0).count()
                
                if monitored_count == 0:
                    if verbose:
                        self.stdout.write(f"Skipping {server.name} - no monitored services")
                    continue
                
                if verbose:
                    self.stdout.write(f"Collecting latency for {server.name} ({monitored_count} services)...")
                
                # Collect latencies
                results = collect_all_service_latencies(server)
                
                for result in results:
                    total_measurements += 1
                    if result.get('result', {}).get('success'):
                        successful_measurements += 1
                        latency = result['result'].get('latency_ms', 0)
                        if verbose:
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"  ✓ {result['service_name']} (:{result['port']}) - {latency:.2f}ms"
                                )
                            )
                    else:
                        failed_measurements += 1
                        error = result.get('result', {}).get('error_message', 'Unknown error')
                        if verbose:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"  ✗ {result['service_name']} (:{result['port']}) - {error}"
                                )
                            )
                
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f"Error collecting latency for {server.name}: {e}")
                )
        
        # Summary
        if total_measurements > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nLatency collection complete: {successful_measurements}/{total_measurements} successful"
                )
            )
            if failed_measurements > 0:
                self.stdout.write(
                    self.style.WARNING(f"  Failed measurements: {failed_measurements}")
                )
        else:
            self.stdout.write(
                self.style.WARNING("No latency measurements collected (no monitored services with ports)")
            )
