"""
Django management command to discover services on a server.

This command connects to a server via SSH and discovers all systemd services,
creating Service records in the database.

Usage:
    python manage.py discover_services <server_id>
    python manage.py discover_services --all  # Discover for all servers
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import Server, Service
import paramiko
from django.conf import settings


class Command(BaseCommand):
    help = "Discover services on a server by connecting via SSH"

    def add_arguments(self, parser):
        parser.add_argument(
            'server_id',
            nargs='?',
            type=int,
            help='Server ID to discover services for',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Discover services for all servers',
        )

    def handle(self, *args, **options):
        server_id = options.get('server_id')
        discover_all = options.get('all', False)
        
        if not discover_all and not server_id:
            self.stdout.write(self.style.ERROR('Please provide a server_id or use --all flag'))
            return
        
        if discover_all:
            servers = Server.objects.all()
            self.stdout.write(f"Discovering services for {servers.count()} server(s)...")
        else:
            try:
                server = Server.objects.get(id=server_id)
                servers = [server]
            except Server.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Server with ID {server_id} not found'))
                return
        
        for server in servers:
            self.stdout.write(f"\nDiscovering services for {server.name} (ID: {server.id})...")
            try:
                services_discovered = self.discover_services(server)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"✓ Discovered {services_discovered} services for {server.name}"
                    )
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"✗ Error discovering services for {server.name}: {str(e)}")
                )
    
    def discover_services(self, server):
        """Discover services on a server via SSH"""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            ssh.connect(
                server.ip_address,
                port=server.port,
                username=server.username,
                key_filename=getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa"),
                timeout=10
            )
            
            # Get all systemd services
            cmd = "systemctl list-units --type=service --all --no-pager --no-legend"
            stdin, stdout, stderr = ssh.exec_command(cmd)
            output = stdout.read().decode('utf-8').strip()
            error_output = stderr.read().decode('utf-8').strip()
            
            if error_output:
                raise Exception(f"SSH command failed: {error_output}")
            
            # Parse systemctl output
            all_services = []
            for line in output.split('\n'):
                if line.strip():
                    # Remove any special characters at the start (like ●)
                    line = line.strip()
                    # Skip lines that don't look like service entries
                    if not line or line.startswith('UNIT') or '●' in line[:5]:
                        # Remove bullet point if present
                        line = line.replace('●', '').strip()
                    
                    parts = line.split()
                    if len(parts) >= 4:
                        unit_name = parts[0]
                        load_state = parts[1]
                        active_state = parts[2]
                        sub_state = parts[3]
                        
                        # Clean unit name (remove .service suffix)
                        if unit_name.endswith('.service'):
                            service_name = unit_name[:-8]
                        else:
                            service_name = unit_name
                        
                        # Skip invalid service names
                        if not service_name or len(service_name.strip()) == 0:
                            continue
                        
                        # Clean service name
                        service_name = service_name.strip()
                        
                        # Determine status
                        if active_state == 'active' and sub_state == 'running':
                            status = 'running'
                        elif active_state == 'failed':
                            status = 'failed'
                        elif active_state == 'inactive':
                            status = 'stopped'
                        else:
                            status = 'unknown'
                        
                        all_services.append({
                            'name': service_name,
                            'status': status,
                        })
            
            # Get existing Service records
            existing_services = {s.name: s for s in Service.objects.filter(server=server)}
            current_time = timezone.now()
            services_created = 0
            
            # Create or update Service records
            for service_data in all_services:
                service_name = service_data['name']
                if service_name in existing_services:
                    # Update existing service
                    db_service = existing_services[service_name]
                    db_service.status = service_data['status']
                    db_service.last_checked = current_time
                    db_service.save()
                else:
                    # Create new service record
                    Service.objects.create(
                        server=server,
                        name=service_name,
                        status=service_data['status'],
                        service_type='systemd',
                        last_checked=current_time,
                        monitoring_enabled=False
                    )
                    services_created += 1
            
            return len(all_services)
            
        finally:
            try:
                ssh.close()
            except:
                pass

