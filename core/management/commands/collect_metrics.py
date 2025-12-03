import json
import paramiko
import os
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from core.models import Server, SystemMetric, MonitoringConfig, Anomaly, Service
from datetime import timedelta


class Command(BaseCommand):
    help = "Collects system metrics from all monitored servers with enhanced details"

    def handle(self, *args, **options):
        servers = Server.objects.filter(monitoring_config__enabled=True).select_related("monitoring_config")
        
        if not servers.exists():
            self.stdout.write(self.style.WARNING("No servers with monitoring enabled found."))
            return
        
        for server in servers:
            try:
                config = server.monitoring_config
                
                # CRITICAL: Skip collection if monitoring is suspended
                if config.monitoring_suspended:
                    self.stdout.write(f"Skipping {server.name} - monitoring is suspended")
                    continue
                
                # Adaptive collection logic
                collection_interval = self._get_collection_interval(server, config)
                
                # Check if we should collect now (based on last collection time)
                last_metric = SystemMetric.objects.filter(server=server).order_by("-timestamp").first()
                if last_metric:
                    time_since_last = (timezone.now() - last_metric.timestamp).total_seconds()
                    if time_since_last < collection_interval:
                        self.stdout.write(f"Skipping {server.name} - collected {time_since_last:.0f}s ago (interval: {collection_interval}s)")
                        continue
                
                self.stdout.write(f"Collecting metrics from {server.name}...")
                metrics = self._collect_metrics(server)
                if metrics:
                    metric_obj = SystemMetric.objects.create(server=server, **metrics)
                    self.stdout.write(self.style.SUCCESS(f"✓ Collected metrics from {server.name}"))
                    
                    # Check and send alerts
                    try:
                        from core.views import _check_and_send_alerts
                        _check_and_send_alerts(server, metric_obj)
                    except Exception as alert_error:
                        self.stderr.write(self.style.WARNING(f"Alert check failed for {server.name}: {alert_error}"))
                
                # Check monitored services (every minute)
                # IMPORTANT: Services are server-specific. Only checks services for THIS server.
                # Enabling a service on one server does NOT affect other servers.
                # Check ALL services with monitoring enabled, regardless of current status
                # This allows detection of failed services and recovery of previously failed services
                try:
                    from core.views import _check_service_status
                    # Only get services for THIS specific server with monitoring enabled
                    # Don't filter by status - we need to check failed/stopped services too
                    monitored_services = Service.objects.filter(
                        server=server,  # Server-specific filter
                        monitoring_enabled=True
                        # Removed status="running" filter to check ALL monitored services
                        # This allows detection of failed services and recovery detection
                    )
                    for service in monitored_services:
                        try:
                            _check_service_status(server, service)
                        except Exception as service_error:
                            self.stderr.write(self.style.WARNING(f"Service check failed for {service.name} on {server.name}: {service_error}"))
                except Exception as service_check_error:
                    self.stderr.write(self.style.WARNING(f"Service monitoring check failed for {server.name}: {service_check_error}"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"✗ Failed to collect from {server.name}: {e}"))

    def _get_collection_interval(self, server, config):
        """Get collection interval based on adaptive settings"""
        base_interval = config.collection_interval_seconds
        
        if not config.adaptive_collection_enabled:
            return base_interval
        
        # Check for recent anomalies
        recent_anomalies = Anomaly.objects.filter(
            server=server,
            resolved=False,
            timestamp__gte=timezone.now() - timedelta(hours=1)
        ).exists()
        
        if recent_anomalies:
            # Anomaly detected - collect more frequently
            return config.anomaly_detection_interval
        else:
            # No anomalies - use normal interval
            return base_interval

    def _collect_metrics(self, server):
        """Collect enhanced metrics via SSH"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Load SSH key
        private_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
        pkey = None
        if os.path.exists(private_key_path):
            try:
                pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
            except:
                pass
        
        try:
            if pkey:
                client.connect(
                    hostname=server.ip_address,
                    port=server.port,
                    username=server.username,
                    pkey=pkey,
                    timeout=30,
                )
            else:
                client.connect(
                    hostname=server.ip_address,
                    port=server.port,
                    username=server.username,
                    timeout=30,
                    look_for_keys=True,
                    allow_agent=True,
                )
            
            # Enhanced remote script with all required metrics
            script = """
import psutil
import json
import sys
import subprocess
import os

def get_physical_cpu_count():
    \"\"\"Get physical CPU core count\"\"\"
    try:
        result = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.split("\\n"):
            if "Socket(s)" in line or "Core(s) per socket" in line:
                if "Socket(s)" in line:
                    sockets = int(line.split(":")[1].strip())
                if "Core(s) per socket" in line:
                    cores_per_socket = int(line.split(":")[1].strip())
                    return sockets * cores_per_socket
    except:
        pass
    return None

def get_disk_info():
    \"\"\"Get detailed disk information including type, RAID, count\"\"\"
    disk_info = {
        "physical_disks": [],
        "disk_count": 0,
        "raid_info": {}
    }
    
    try:
        # Get physical disks
        result = subprocess.run(["lsblk", "-d", "-o", "NAME,TYPE,SIZE"], capture_output=True, text=True, timeout=5)
        physical_disks = []
        for line in result.stdout.split("\\n")[1:]:
            if line.strip() and "disk" in line.lower():
                parts = line.split()
                if len(parts) >= 2:
                    disk_name = parts[0]
                    physical_disks.append(disk_name)
        
        disk_info["physical_disks"] = physical_disks
        disk_info["disk_count"] = len(physical_disks)
        
        # Detect disk types (SSD/HDD/NVMe)
        for disk in physical_disks:
            disk_path = f"/sys/block/{disk}/queue/rotational"
            if os.path.exists(disk_path):
                with open(disk_path, "r") as f:
                    rotational = f.read().strip()
                    if rotational == "0":
                        # Check if NVMe
                        if "nvme" in disk.lower():
                            disk_type = "NVMe"
                        else:
                            disk_type = "SSD"
                    else:
                        disk_type = "HDD"
            else:
                disk_type = "Unknown"
            
            disk_info["physical_disks"].append({
                "name": disk,
                "type": disk_type
            })
        
        # Detect RAID
        if os.path.exists("/proc/mdstat"):
            with open("/proc/mdstat", "r") as f:
                mdstat = f.read()
                if "md" in mdstat:
                    # Parse RAID info
                    for line in mdstat.split("\\n"):
                        if line.startswith("md"):
                            parts = line.split()
                            if len(parts) > 0:
                                raid_name = parts[0]
                                disk_info["raid_info"][raid_name] = {
                                    "status": "active" if "active" in line else "inactive"
                                }
        
        # Check mdadm
        try:
            result = subprocess.run(["mdadm", "--detail", "--scan"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout:
                for line in result.stdout.split("\\n"):
                    if "ARRAY" in line:
                        disk_info["raid_info"]["mdadm"] = "configured"
        except:
            pass
            
    except Exception as e:
        pass
    
    return disk_info

metrics = {
    "cpu_percent": psutil.cpu_percent(interval=1),
    "cpu_count": psutil.cpu_count(),
    "physical_cpu_count": get_physical_cpu_count(),
}

try:
    load_avg = psutil.getloadavg()
    metrics["cpu_load_avg_1m"] = load_avg[0]
    metrics["cpu_load_avg_5m"] = load_avg[1]
    metrics["cpu_load_avg_15m"] = load_avg[2]
except (AttributeError, OSError):
    metrics["cpu_load_avg_1m"] = None
    metrics["cpu_load_avg_5m"] = None
    metrics["cpu_load_avg_15m"] = None

mem = psutil.virtual_memory()
metrics.update({
    "memory_total": mem.total,
    "memory_available": mem.available,
    "memory_percent": mem.percent,
    "memory_used": mem.used,
    "memory_buffers": getattr(mem, "buffers", 0),
    "memory_cached": getattr(mem, "cached", 0),
    "memory_shared": getattr(mem, "shared", 0),
})

swap = psutil.swap_memory()
metrics.update({
    "swap_total": swap.total if swap.total > 0 else None,
    "swap_used": swap.used if swap.used > 0 else None,
    "swap_percent": swap.percent if swap.total > 0 else None,
})

# Enhanced disk usage with type and RAID info
disk_info = get_disk_info()
disk_usage = {}

# Filesystem types to ignore (virtual filesystems)
IGNORED_FSTYPES = {
    'squashfs', 'tmpfs', 'devtmpfs', 'proc', 'sysfs',
    'cgroup', 'cgroup2', 'ramfs', 'overlay', 'udev'
}

for partition in psutil.disk_partitions():
    # Skip virtual filesystems
    if partition.fstype.lower() in IGNORED_FSTYPES:
        continue
    
    try:
        usage = psutil.disk_usage(partition.mountpoint)
        
        # Find associated physical disk
        disk_type = "Unknown"
        raid_status = "none"
        physical_disk = "unknown"
        
        # Disk matching disabled - using defaults
        disk_type = "Unknown"
        physical_disk = "unknown"
        
        # Check RAID
        if disk_info.get("raid_info"):
            raid_status = "configured"
        
        disk_usage[partition.mountpoint] = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
            "device": partition.device,
            "fstype": partition.fstype,
            "disk_type": disk_type,
            "raid": raid_status,
            "physical_disk": physical_disk,
        }
    except (PermissionError, OSError):
        pass

metrics["disk_usage"] = disk_usage

# Network I/O
network_io = {}
try:
    net_io = psutil.net_io_counters(pernic=True)
    for interface, counters in net_io.items():
        network_io[interface] = {
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "packets_sent": counters.packets_sent,
            "packets_recv": counters.packets_recv,
        }
except Exception:
    pass
metrics["network_io"] = network_io

# Network connections count
try:
    metrics["network_connections"] = len(psutil.net_connections())
except (psutil.AccessDenied, AttributeError):
    metrics["network_connections"] = None

print(json.dumps(metrics))
"""
            
            # Create remote script file
            remote_script = "/tmp/collect_metrics.py"
            
            # Write script to remote server
            sftp = client.open_sftp()
            with sftp.file(remote_script, "w") as f:
                f.write(script)
            sftp.chmod(remote_script, 0o755)
            sftp.close()
            
            # Execute script
            # Execute script
            stdin, stdout, stderr = client.exec_command(f"python3 {remote_script}", timeout=90)
            
            # Read output and error
            output = stdout.read().decode("utf-8")
            error = stderr.read().decode("utf-8")
            
            # Get exit status (this blocks until command completes)
            exit_status = stdout.channel.recv_exit_status()
            
            # Debug output
            if not output:
                # Try to get more info
                if error:
                    raise Exception(f"Remote script failed with exit status {exit_status}. Error: {error[:500]}")
                else:
                    raise Exception(f"Remote script produced no output (exit status: {exit_status})")
            
            if error and "Traceback" in error:
                raise Exception(f"Remote script error (exit {exit_status}): {error[:1000]}")
            
            if not output:
                if error:
                    raise Exception(f"No output from remote script (exit {exit_status}). Error: {error[:1000]}")
                else:
                    raise Exception(f"No output from remote script (exit status: {exit_status})")
            
            return json.loads(output)
            
        finally:
            client.close()
