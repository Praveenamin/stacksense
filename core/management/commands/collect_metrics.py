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
                    # Calculate I/O rates by comparing with previous metric
                    previous_metric = SystemMetric.objects.filter(server=server).order_by("-timestamp").first()

                    if previous_metric and previous_metric.timestamp:
                        time_diff = (timezone.now() - previous_metric.timestamp).total_seconds()
                        if time_diff > 0 and time_diff < 3600:  # Only calculate if within last hour and positive time diff
                            # Calculate disk I/O rates using actual I/O counters (consolidated across all disks)
                            try:
                                # Get previous I/O counters from previous metric
                                prev_read_bytes = getattr(previous_metric, 'disk_read_bytes_total', None)
                                prev_write_bytes = getattr(previous_metric, 'disk_write_bytes_total', None)
                                
                                # Get current I/O counters from collected metrics
                                curr_read_bytes = metrics.get("disk_read_bytes_total", 0)
                                curr_write_bytes = metrics.get("disk_write_bytes_total", 0)
                                
                                if prev_read_bytes is not None and prev_write_bytes is not None and time_diff > 0:
                                    # Calculate rates: (current - previous) / time_diff
                                    # This gives bytes per second, consolidated across ALL physical disks
                                    if curr_read_bytes >= prev_read_bytes:
                                        metrics["disk_io_read"] = int((curr_read_bytes - prev_read_bytes) / time_diff)
                                    else:
                                        metrics["disk_io_read"] = 0  # Counter reset or overflow
                                    
                                    if curr_write_bytes >= prev_write_bytes:
                                        metrics["disk_io_write"] = int((curr_write_bytes - prev_write_bytes) / time_diff)
                                    else:
                                        metrics["disk_io_write"] = 0  # Counter reset or overflow
                                else:
                                    # First collection or no previous data
                                    metrics["disk_io_read"] = 0
                                    metrics["disk_io_write"] = 0
                            except (AttributeError, KeyError, TypeError, ZeroDivisionError):
                                metrics["disk_io_read"] = 0
                                metrics["disk_io_write"] = 0

                            # Calculate network I/O rates and utilization
                            if previous_metric.network_io and metrics.get("network_io"):
                                try:
                                    prev_net = previous_metric.network_io if isinstance(previous_metric.network_io, dict) else json.loads(previous_metric.network_io)
                                    curr_net = metrics.get("network_io", {})

                                    # Sum all network interfaces (excluding loopback/virtual)
                                    prev_sent = sum(
                                        iface.get("bytes_sent", 0) 
                                        for iface_name, iface in prev_net.items() 
                                        if isinstance(iface, dict) and not iface_name.startswith("lo") and not iface_name.startswith("docker")
                                    )
                                    prev_recv = sum(
                                        iface.get("bytes_recv", 0) 
                                        for iface_name, iface in prev_net.items() 
                                        if isinstance(iface, dict) and not iface_name.startswith("lo") and not iface_name.startswith("docker")
                                    )
                                    curr_sent = sum(
                                        iface.get("bytes_sent", 0) 
                                        for iface_name, iface in curr_net.items() 
                                        if isinstance(iface, dict) and not iface_name.startswith("lo") and not iface_name.startswith("docker")
                                    )
                                    curr_recv = sum(
                                        iface.get("bytes_recv", 0) 
                                        for iface_name, iface in curr_net.items() 
                                        if isinstance(iface, dict) and not iface_name.startswith("lo") and not iface_name.startswith("docker")
                                    )

                                    # Calculate rates in bytes per second
                                    if curr_sent > prev_sent and time_diff > 0:
                                        metrics["net_io_sent"] = int((curr_sent - prev_sent) / time_diff)
                                    else:
                                        metrics["net_io_sent"] = 0
                                    
                                    if curr_recv > prev_recv and time_diff > 0:
                                        metrics["net_io_recv"] = int((curr_recv - prev_recv) / time_diff)
                                    else:
                                        metrics["net_io_recv"] = 0
                                    
                                    # Calculate utilization percentage based on NIC max speed
                                    total_nic_speed_bits = metrics.get("_total_nic_speed_bits", 0)
                                    if total_nic_speed_bits > 0:
                                        # Convert bytes/sec to bits/sec and calculate utilization
                                        sent_bits_per_sec = metrics["net_io_sent"] * 8
                                        recv_bits_per_sec = metrics["net_io_recv"] * 8
                                        
                                        metrics["net_utilization_sent"] = min(100.0, (sent_bits_per_sec / total_nic_speed_bits) * 100.0)
                                        metrics["net_utilization_recv"] = min(100.0, (recv_bits_per_sec / total_nic_speed_bits) * 100.0)
                                        metrics["nic_max_speed_bits"] = total_nic_speed_bits
                                    else:
                                        metrics["net_utilization_sent"] = None
                                        metrics["net_utilization_recv"] = None
                                        metrics["nic_max_speed_bits"] = None
                                        
                                except (json.JSONDecodeError, KeyError, TypeError, ZeroDivisionError):
                                    metrics["net_io_sent"] = 0
                                    metrics["net_io_recv"] = 0
                                    metrics["net_utilization_sent"] = None
                                    metrics["net_utilization_recv"] = None
                                    metrics["nic_max_speed_bits"] = None
                            else:
                                metrics["net_io_sent"] = 0
                                metrics["net_io_recv"] = 0
                                metrics["net_utilization_sent"] = None
                                metrics["net_utilization_recv"] = None
                                metrics["nic_max_speed_bits"] = None
                        else:
                            metrics["disk_io_read"] = 0
                            metrics["disk_io_write"] = 0
                            metrics["net_io_sent"] = 0
                            metrics["net_io_recv"] = 0
                    else:
                        # First metric or no previous data
                        metrics["disk_io_read"] = 0
                        metrics["disk_io_write"] = 0
                        metrics["net_io_sent"] = 0
                        metrics["net_io_recv"] = 0

                    # Remove temporary field that doesn't exist in SystemMetric model
                    metrics.pop("_total_nic_speed_bits", None)
                    
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

def get_nic_speed(interface_name):
    \"\"\"Get NIC link speed in bits per second using ethtool or /sys/class/net\"\"\"
    try:
        # Try ethtool first (most accurate)
        result = subprocess.run(
            ["ethtool", interface_name],
            capture_output=True,
            text=True,
            timeout=3
        )
        if result.returncode == 0:
            for line in result.stdout.split("\\n"):
                if "Speed:" in line:
                    speed_str = line.split("Speed:")[1].strip()
                    if "Mb/s" in speed_str:
                        speed_mbps = int(speed_str.replace("Mb/s", "").strip())
                        return speed_mbps * 1000000  # Convert to bits per second
                    elif "Gb/s" in speed_str:
                        speed_gbps = float(speed_str.replace("Gb/s", "").strip())
                        return int(speed_gbps * 1000000000)  # Convert to bits per second
    except:
        pass
    
    # Fallback: Try /sys/class/net (Linux)
    try:
        speed_path = f"/sys/class/net/{interface_name}/speed"
        if os.path.exists(speed_path):
            with open(speed_path, "r") as f:
                speed_mbps = int(f.read().strip())
                if speed_mbps > 0:
                    return speed_mbps * 1000000  # Convert to bits per second
    except:
        pass
    
    return None  # Speed not detectable

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

# Disk I/O counters (consolidated across all physical disks)
# perdisk=False gives system-wide totals across ALL disks
try:
    disk_io = psutil.disk_io_counters(perdisk=False)
    if disk_io:
        metrics["disk_read_bytes_total"] = disk_io.read_bytes
        metrics["disk_write_bytes_total"] = disk_io.write_bytes
    else:
        metrics["disk_read_bytes_total"] = 0
        metrics["disk_write_bytes_total"] = 0
except Exception:
    metrics["disk_read_bytes_total"] = 0
    metrics["disk_write_bytes_total"] = 0

# Network I/O with NIC speed detection
network_io = {}
total_nic_speed_bits = 0  # Total max speed across all active interfaces
try:
    net_io = psutil.net_io_counters(pernic=True)
    for interface, counters in net_io.items():
        # Skip loopback and virtual interfaces
        if interface.startswith("lo") or interface.startswith("docker") or interface.startswith("veth"):
            continue
            
        # Get NIC speed for this interface
        nic_speed_bits = get_nic_speed(interface)
        if nic_speed_bits:
            total_nic_speed_bits += nic_speed_bits
        
        network_io[interface] = {
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "packets_sent": counters.packets_sent,
            "packets_recv": counters.packets_recv,
            "speed_bits_per_sec": nic_speed_bits,  # Store speed per interface
        }
except Exception:
    pass

metrics["network_io"] = network_io
metrics["_total_nic_speed_bits"] = total_nic_speed_bits  # Total max speed for utilization calculation

# Network connections count
try:
    metrics["network_connections"] = len(psutil.net_connections())
except (psutil.AccessDenied, AttributeError):
    metrics["network_connections"] = None

# System uptime (time since last boot in seconds)
try:
    import time
    boot_time = psutil.boot_time()
    current_time = time.time()
    system_uptime_seconds = int(current_time - boot_time)
    metrics["system_uptime_seconds"] = system_uptime_seconds
except Exception:
    metrics["system_uptime_seconds"] = None

# Collect top processes (CPU and Memory)
top_processes = {"cpu": [], "memory": []}
try:
    # Get all processes
    processes = []
    for proc in psutil.process_iter(['pid', 'cpu_percent', 'memory_percent', 'name', 'cmdline']):
        try:
            proc_info = proc.info
            # Get CPU percent (non-blocking, uses previous interval)
            if proc_info['cpu_percent'] is None:
                proc_info['cpu_percent'] = 0.0
            # Get memory percent
            if proc_info['memory_percent'] is None:
                proc_info['memory_percent'] = 0.0
            # Build command string (limit length)
            cmdline = proc_info.get('cmdline', [])
            if cmdline:
                command = ' '.join(cmdline)[:100]  # Limit to 100 chars
            else:
                command = proc_info.get('name', 'unknown')
            processes.append({
                'pid': str(proc_info['pid']),
                'cpu_percent': round(proc_info['cpu_percent'], 1),
                'memory_percent': round(proc_info['memory_percent'], 1),
                'command': command
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    # Sort and get top 3 CPU processes
    cpu_processes = sorted(processes, key=lambda x: x['cpu_percent'], reverse=True)[:3]
    top_processes["cpu"] = [{"pid": p["pid"], "cpu_percent": p["cpu_percent"], "command": p["command"]} for p in cpu_processes]
    
    # Sort and get top 3 memory processes
    memory_processes = sorted(processes, key=lambda x: x['memory_percent'], reverse=True)[:3]
    top_processes["memory"] = [{"pid": p["pid"], "memory_percent": p["memory_percent"], "command": p["command"]} for p in memory_processes]
except Exception:
    # If process collection fails, continue without it
    pass

metrics["top_processes"] = top_processes

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
