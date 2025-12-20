"""
Django management command to check server heartbeats via SSH connection.

This command runs on the monitoring server and SSH connects to each client server
every 30 seconds to verify connectivity and update heartbeat records.

Usage:
    python manage.py check_heartbeats_ssh
"""

import paramiko
import os
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from core.models import Server, ServerHeartbeat
from core.views import _send_connection_alert
from django.core.cache import cache


class Command(BaseCommand):
    help = "Check server heartbeats by SSH connecting to each server"

    def add_arguments(self, parser):
        parser.add_argument(
            '--timeout',
            type=int,
            default=5,
            help='SSH connection timeout in seconds (default: 5)',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed connection information',
        )

    def handle(self, *args, **options):
        timeout = options['timeout']
        verbose = options['verbose']
        
        # Track that monitoring app is running
        from django.core.management import call_command
        try:
            call_command("track_app_heartbeat", verbosity=0)
        except Exception:
            pass  # Don't fail heartbeat check if app heartbeat tracking fails
        
        # Don't use select_related to avoid conflicts with deferred fields elsewhere
        servers = Server.objects.all()
        now = timezone.now()
        
        success_count = 0
        failure_count = 0
        skipped_count = 0
        
        self.stdout.write(f"Checking server heartbeats via SSH (timeout: {timeout}s)...")
        self.stdout.write("")
        
        # Load SSH key (reuse from collect_metrics logic)
        private_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
        pkey = None
        if os.path.exists(private_key_path):
            try:
                pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
            except Exception as e:
                if verbose:
                    self.stdout.write(
                        self.style.WARNING(f"Could not load SSH key from {private_key_path}: {e}")
                    )
        
        for server in servers:
            # Skip if monitoring is suspended
            try:
                if server.monitoring_config.monitoring_suspended:
                    if verbose:
                        self.stdout.write(
                            f"{self.style.WARNING('SKIPPED')} {server.name} (ID: {server.id}) - Monitoring suspended"
                        )
                    skipped_count += 1
                    continue
            except:
                pass  # No monitoring config, continue
            
            # Attempt SSH connection
            client = None
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                
                # Attempt connection
                if pkey:
                    client.connect(
                        hostname=server.ip_address,
                        port=server.port,
                        username=server.username,
                        pkey=pkey,
                        timeout=timeout,
                    )
                else:
                    client.connect(
                        hostname=server.ip_address,
                        port=server.port,
                        username=server.username,
                        timeout=timeout,
                        look_for_keys=True,
                        allow_agent=True,
                    )
                
                # Connection successful - update heartbeat
                heartbeat, created = ServerHeartbeat.objects.update_or_create(
                    server=server,
                    defaults={
                        'last_heartbeat': now,
                    }
                )
                
                # Check if server was previously offline and send online alert
                offline_key = f"server_offline_state:{server.id}"
                was_offline = cache.get(offline_key, False)
                if was_offline:
                    # Server came back online - send alert
                    try:
                        _send_connection_alert(server, "online")
                        cache.delete(offline_key)
                    except Exception as alert_error:
                        if verbose:
                            self.stdout.write(f"Warning: Could not send online alert for {server.name}: {alert_error}")
                
                status_msg = "CREATED" if created else "UPDATED"
                success_count += 1
                
                if verbose:
                    self.stdout.write(
                        f"{self.style.SUCCESS('SUCCESS')} {server.name} (ID: {server.id}) - "
                        f"Heartbeat {status_msg} at {now.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                
            except (paramiko.AuthenticationException, paramiko.SSHException, Exception) as e:
                failure_count += 1
                error_type = type(e).__name__
                if verbose:
                    self.stdout.write(
                        f"{self.style.ERROR('FAILED')} {server.name} (ID: {server.id}) - "
                        f"{error_type}: {str(e)[:100]}"
                    )
                
                # Check if server was previously online and send offline alert
                offline_key = f"server_offline_state:{server.id}"
                was_offline = cache.get(offline_key, False)
                if not was_offline:
                    # Server just went offline - send alert
                    try:
                        _send_connection_alert(server, "offline")
                        cache.set(offline_key, True, 3600)  # Mark as offline for 1 hour
                    except Exception as alert_error:
                        if verbose:
                            self.stdout.write(f"Warning: Could not send offline alert for {server.name}: {alert_error}")
            finally:
                if client:
                    try:
                        client.close()
                    except:
                        pass
        
        # Summary
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write(f"Summary:")
        self.stdout.write(f"  {self.style.SUCCESS('Successful')}: {success_count}")
        self.stdout.write(f"  {self.style.ERROR('Failed')}: {failure_count}")
        self.stdout.write(f"  {self.style.WARNING('Skipped')}: {skipped_count}")
        self.stdout.write(f"  Total Servers: {servers.count()}")
        self.stdout.write("=" * 60)
        
        if failure_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{failure_count} server(s) could not be reached via SSH"
                )
            )

