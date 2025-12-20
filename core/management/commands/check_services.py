"""
Django management command to check service status on monitored servers.

This command runs every 30 seconds and checks all services with monitoring enabled.
Alerts are triggered after 2 consecutive failures (60 seconds total).

Usage:
    python manage.py check_services
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import Server, Service, MonitoringConfig
from core.views import _check_service_status


class Command(BaseCommand):
    help = "Check service status on all monitored servers (runs every 30 seconds)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed service check information',
        )

    def handle(self, *args, **options):
        verbose = options['verbose']
        now = timezone.now()
        
        # Get all servers with monitoring enabled
        servers = Server.objects.filter(monitoring_config__enabled=True).select_related('monitoring_config')
        
        if not servers.exists():
            self.stdout.write(self.style.WARNING("No servers with monitoring enabled found."))
            return
        
        checked_count = 0
        error_count = 0
        skipped_count = 0
        
        self.stdout.write(f"Checking services on {servers.count()} server(s)...")
        self.stdout.write("")
        
        for server in servers:
            try:
                config = server.monitoring_config
                
                # Skip if monitoring is suspended
                if config.monitoring_suspended:
                    if verbose:
                        self.stdout.write(
                            f"{self.style.WARNING('SKIPPED')} {server.name} - Monitoring suspended"
                        )
                    skipped_count += 1
                    continue
                
                # Get all services with monitoring enabled for this server
                monitored_services = Service.objects.filter(
                    server=server,
                    monitoring_enabled=True
                )
                
                if not monitored_services.exists():
                    if verbose:
                        self.stdout.write(f"No monitored services found for {server.name}")
                    continue
                
                # Check each service
                for service in monitored_services:
                    try:
                        _check_service_status(server, service)
                        checked_count += 1
                        if verbose:
                            self.stdout.write(
                                f"{self.style.SUCCESS('CHECKED')} {server.name} - {service.name}"
                            )
                    except Exception as service_error:
                        error_count += 1
                        self.stderr.write(
                            self.style.ERROR(
                                f"Service check failed for {service.name} on {server.name}: {service_error}"
                            )
                        )
            except Exception as e:
                error_count += 1
                self.stderr.write(
                    self.style.ERROR(f"Error checking services for {server.name}: {e}")
                )
        
        # Summary
        if verbose or checked_count > 0:
            self.stdout.write("")
            self.stdout.write("=" * 60)
            self.stdout.write(f"Summary:")
            self.stdout.write(f"  {self.style.SUCCESS('Checked')}: {checked_count}")
            self.stdout.write(f"  {self.style.ERROR('Errors')}: {error_count}")
            self.stdout.write(f"  {self.style.WARNING('Skipped')}: {skipped_count}")
            self.stdout.write("=" * 60)
