from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, get_object_or_404
from django.contrib import admin
from django.utils import timezone
from datetime import timedelta
from .models import Server, SystemMetric, Anomaly, MonitoringConfig, Service, EmailAlertConfig, AlertHistory
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
import paramiko
import os
from django.conf import settings
from django.core.cache import cache
import json
import subprocess
import threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

# Get logger instances
app_logger = logging.getLogger('core')
error_logger = logging.getLogger('django.request')


def _get_client_ip(request):
    """Get client IP address from request"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def _log_user_action(request, action, details=""):
    """Log user actions to app.log"""
    user = request.user if hasattr(request, 'user') and request.user.is_authenticated else "Anonymous"
    ip = _get_client_ip(request)
    timestamp = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
    log_message = f"User: {user} | IP: {ip} | Action: {action} | {details} | Time: {timestamp}"
    app_logger.info(log_message)


@staff_member_required
@require_http_methods(["GET"])
def get_live_metrics(request):
    """API endpoint for live metrics updates - reads from Redis with PostgreSQL fallback"""
    servers = Server.objects.filter(monitoring_config__enabled=True).only("id", "name")
    metrics_data = []
    
    for server in servers:
        # Try Redis first (fast)
        redis_key = f"metrics:{server.id}:latest"
        cached_metric = cache.get(redis_key)
        
        if cached_metric:
            try:
                metric = json.loads(cached_metric) if isinstance(cached_metric, str) else cached_metric
                metrics_data.append(metric)
                continue
            except:
                pass
        
        # Fallback to PostgreSQL if Redis miss
        latest_metric = SystemMetric.objects.filter(server=server).only(
            "cpu_percent", "memory_percent", "disk_usage", "timestamp"
        ).order_by("-timestamp").first()
        
        if latest_metric:
            # Calculate disk percent from root partition
            disk_percent = 0
            if latest_metric.disk_usage:
                try:
                    disk_data = (
                        json.loads(latest_metric.disk_usage)
                        if isinstance(latest_metric.disk_usage, str)
                        else latest_metric.disk_usage
                    )
                    root_partition = disk_data.get("/", None)
                    if root_partition:
                        disk_percent = root_partition.get("percent", 0) or 0
                except:
                    pass
            
            metrics_data.append({
                "server_id": server.id,
                "server_name": server.name,
                "cpu_percent": latest_metric.cpu_percent or 0,
                "memory_percent": latest_metric.memory_percent or 0,
                "disk_percent": disk_percent,
                "timestamp": latest_metric.timestamp.isoformat(),
            })
    
    return JsonResponse({"metrics": metrics_data})



@staff_member_required
def server_details(request, server_id):
    """Detailed server metrics view"""
    try:
        server = Server.objects.select_related("monitoring_config").get(id=server_id)
    except Server.DoesNotExist:
        from django.http import Http404
        raise Http404("Server not found")
    
    # Get latest metric
    latest_metric = SystemMetric.objects.filter(server=server).order_by("-timestamp").first()
    
    # Get recent metrics for graphs (last 100)
    recent_metrics = SystemMetric.objects.filter(server=server).order_by("-timestamp")[:100]
    
    # Get active services
    active_services = Service.objects.filter(server=server, status="running").order_by("name")
    
    # Get recent anomalies (last 50)
    recent_anomalies = Anomaly.objects.filter(server=server).select_related("metric").order_by("-timestamp")[:50]
    
    # Parse disk usage
    disk_data = {}
    if latest_metric and latest_metric.disk_usage:
        import json
        disk_data = (
            json.loads(latest_metric.disk_usage)
            if isinstance(latest_metric.disk_usage, str)
            else latest_metric.disk_usage
        )
    
    # Calculate disk summary
    disk_summary = {
        "total_disks": 0,
        "ssd_count": 0,
        "hdd_count": 0,
        "nvme_count": 0,
        "raid_count": 0,
        "partitions": []
    }
    
    physical_disks = {}
    for mount_point, info in disk_data.items():
        if isinstance(info, dict):
            disk_type = info.get("disk_type", "Unknown")
            raid_status = info.get("raid", "none")
            physical_disk = info.get("physical_disk", "unknown")
            
            # Track physical disks
            if physical_disk not in physical_disks:
                physical_disks[physical_disk] = {
                    "type": disk_type,
                    "raid": raid_status,
                    "partitions": []
                }
                disk_summary["total_disks"] += 1
                if disk_type == "SSD":
                    disk_summary["ssd_count"] += 1
                elif disk_type == "HDD":
                    disk_summary["hdd_count"] += 1
                elif disk_type == "NVMe":
                    disk_summary["nvme_count"] += 1
                if raid_status != "none":
                    disk_summary["raid_count"] += 1
            
            physical_disks[physical_disk]["partitions"].append({
                "mount": mount_point,
                "total": info.get("total", 0),
                "used": info.get("used", 0),
                "free": info.get("free", 0),
                "percent": info.get("percent", 0)
            })
    
    disk_summary["physical_disks"] = physical_disks
    
    # Prepare chart data
    chart_data = {
        "timestamps": [],
        "cpu": [],
        "memory": [],
        "disk": []
    }
    
    for metric in reversed(recent_metrics):  # Reverse to get chronological order
        chart_data["timestamps"].append(metric.timestamp.isoformat())
        chart_data["cpu"].append(float(metric.cpu_percent or 0))
        chart_data["memory"].append(float(metric.memory_percent or 0))
        
        # Get disk percent from root partition
        disk_percent = 0
        if metric.disk_usage:
            try:
                disk_info = (
                    json.loads(metric.disk_usage)
                    if isinstance(metric.disk_usage, str)
                    else metric.disk_usage
                )
                root_part = disk_info.get("/", {})
                if root_part:
                    disk_percent = float(root_part.get("percent", 0) or 0)
            except:
                pass
        chart_data["disk"].append(disk_percent)
    
    # Convert to JSON string for template
    import json as json_module
    # Ensure chart_data has all required keys
    if not chart_data.get("timestamps"):
        chart_data = {"timestamps": [], "cpu": [], "memory": [], "disk": []}
    chart_data_json = json_module.dumps(chart_data)
    

    # Calculate server status
    # Consider server online if metrics are within last 15 minutes (3x typical collection interval)
    server_status = "offline"
    if latest_metric:
        time_diff = timezone.now() - latest_metric.timestamp
        if time_diff < timedelta(minutes=15):
            cpu = latest_metric.cpu_percent or 0
            memory = latest_metric.memory_percent or 0
            if cpu > 80 or memory > 85:
                server_status = "warning"
            else:
                server_status = "online"
        else:
            server_status = "offline"
    context = {
        "server": server,
        "server_status": server_status,
        "latest_metric": latest_metric,
        "recent_metrics": recent_metrics,
        "active_services": active_services,
        "recent_anomalies": recent_anomalies,
        "disk_summary": disk_summary,
        "chart_data": chart_data_json,
    }
    
    return render(request, "core/server_details.html", context)

@staff_member_required
def monitoring_dashboard(request):
    """Server Monitor Dashboard - Real-time infrastructure monitoring"""
    servers = Server.objects.all().select_related("monitoring_config").order_by("name")
    
    servers_data = []
    online_count = 0
    warning_count = 0
    offline_count = 0
    alert_count = 0
    
    for server in servers:
        latest_metric = SystemMetric.objects.filter(server=server).only("cpu_percent", "memory_percent", "disk_usage", "timestamp").order_by("-timestamp").first()
        active_anomalies = Anomaly.objects.filter(server=server, resolved=False).only("id")
        alert_count += active_anomalies.count()
        
        try:
            monitoring_enabled = server.monitoring_config.enabled
        except:
            monitoring_enabled = False
        
        # Calculate status
        # Consider server online if metrics are within last 15 minutes (3x typical collection interval)
        status = "offline"
        if latest_metric:
            # Check if metric is recent (within last 15 minutes)
            time_diff = timezone.now() - latest_metric.timestamp
            if time_diff < timedelta(minutes=15):
                # Determine status based on metrics
                cpu = latest_metric.cpu_percent or 0
                memory = latest_metric.memory_percent or 0
                
                if cpu > 80 or memory > 85:
                    status = "warning"
                    warning_count += 1
                else:
                    status = "online"
                    online_count += 1
            else:
                offline_count += 1
        else:
            offline_count += 1
        
        # Calculate uptime (time since first metric or server creation)
        uptime_days = 0
        uptime_hours = 0
        uptime_minutes = 0
        
        if latest_metric:
            first_metric = SystemMetric.objects.filter(server=server).order_by("timestamp").first()
            if first_metric:
                uptime_delta = latest_metric.timestamp - first_metric.timestamp
                total_seconds = int(uptime_delta.total_seconds())
                uptime_days = total_seconds // 86400
                uptime_hours = (total_seconds % 86400) // 3600
                uptime_minutes = (total_seconds % 3600) // 60
        
        # Derived metrics from latest sample
        network_download = 0
        network_upload = 0
        disk_percent = 0
        if latest_metric:
            if latest_metric.network_io:
                try:
                    import json
                    network_data = (
                        json.loads(latest_metric.network_io)
                        if isinstance(latest_metric.network_io, str)
                        else latest_metric.network_io
                    )
                    # Sum up all interfaces
                    total_bytes_sent = 0
                    total_bytes_recv = 0
                    if isinstance(network_data, dict):
                        for interface, data in network_data.items():
                            if isinstance(data, dict):
                                total_bytes_sent += data.get("bytes_sent", 0)
                                total_bytes_recv += data.get("bytes_recv", 0)
                    network_download = round(total_bytes_recv / 1024 / 1024, 1) if total_bytes_recv else 0
                    network_upload = round(total_bytes_sent / 1024 / 1024, 1) if total_bytes_sent else 0
                except Exception:
                    pass
            if latest_metric.disk_usage:
                try:
                    import json
                    disk_data = (
                        json.loads(latest_metric.disk_usage)
                        if isinstance(latest_metric.disk_usage, str)
                        else latest_metric.disk_usage
                    )
                    # Use root partition ("/") if available, otherwise first partition
                    root_partition = disk_data.get("/", None)
                    if root_partition:
                        disk_percent = root_partition.get("percent", 0) or 0
                    else:
                        first_partition = next(iter(disk_data.values()), None)
                        if first_partition:
                            disk_percent = first_partition.get("percent", 0) or 0
                except Exception:
                    disk_percent = 0
        
        servers_data.append({
            "server": server,
        "server_status": status,
            "latest_metric": latest_metric,
            "status": status,
            "uptime_days": uptime_days,
            "uptime_hours": uptime_hours,
            "uptime_minutes": uptime_minutes,
            "network_download": network_download,
            "network_upload": network_upload,
            "active_anomalies": active_anomalies.count(),
            "active_anomalies_list": list(active_anomalies[:10]),
            "monitoring_enabled": monitoring_enabled,
            "disk_percent": disk_percent,
        })
    

    # Calculate server status
    # Consider server online if metrics are within last 15 minutes (3x typical collection interval)
    server_status = "offline"
    if latest_metric:
        time_diff = timezone.now() - latest_metric.timestamp
        if time_diff < timedelta(minutes=15):
            cpu = latest_metric.cpu_percent or 0
            memory = latest_metric.memory_percent or 0
            if cpu > 80 or memory > 85:
                server_status = "warning"
            else:
                server_status = "online"
        else:
            server_status = "offline"
    context = {
        "servers_data": servers_data,
        "total_servers": len(servers_data),
        "online_count": online_count,
        "warning_count": warning_count,
        "offline_count": offline_count,
        "alert_count": alert_count,
    }
    context.update(admin.site.each_context(request))
    
    return render(request, "core/monitoring_dashboard.html", context)


@staff_member_required
@require_http_methods(["POST"])
def add_server(request):
    """Add a new server with SSH key deployment"""
    _log_user_action(request, "ADD_SERVER", f"Attempting to add server")
    try:
        name = request.POST.get('name', '').strip()
        ip_address = request.POST.get('ip_address', '').strip()
        port = int(request.POST.get('port', 22))
        username = request.POST.get('username', 'root').strip()
        password = request.POST.get('password', '').strip()
        
        # Validate required fields
        if not all([name, ip_address, username, password]):
            return JsonResponse({
                'success': False,
                'error': 'All fields are required.'
            }, status=400)
        
        # Validate port range
        if port < 1 or port > 65535:
            return JsonResponse({
                'success': False,
                'error': 'Port must be between 1 and 65535.'
            }, status=400)
        
        # Create server object
        server = Server(
            name=name,
            ip_address=ip_address,
            port=port,
            username=username
        )
        server.save()
        
        # Create monitoring configuration
        MonitoringConfig.objects.get_or_create(
            server=server,
            defaults={
                "enabled": True,
                "collection_interval_seconds": 60,
                "adaptive_collection_enabled": False,
                "use_adtk": True,
                "use_isolation_forest": False,
                "use_llm_explanation": True,
                "retention_period_days": 30,
                "aggregation_enabled": True,
            }
        )
        
        # Deploy SSH key (password is used here but NOT stored)
        try:
            _deploy_ssh_key(server, password)
            server.ssh_key_deployed = True
            server.ssh_key_deployed_at = timezone.now()
            server.save(update_fields=["ssh_key_deployed", "ssh_key_deployed_at"])
            
            # Install psutil on the server
            psutil_success, psutil_message, psutil_details = _install_psutil_on_server(server)
            
            # Trigger immediate metrics collection in background (only if psutil is installed)
            if psutil_success:
                def collect_metrics_async():
                    try:
                        _collect_metrics_for_server(server)
                    except Exception as e:
                        # Log error but don't fail the request
                        print(f"Failed to collect initial metrics for {server.name}: {e}")
                
                thread = threading.Thread(target=collect_metrics_async)
                thread.daemon = True
                thread.start()
                
                return JsonResponse({
                    'success': True,
                    'message': f'Server "{name}" added successfully! SSH key deployed. {psutil_message}. Metrics collection started.',
                    'psutil_status': {
                        'installed': True,
                        'message': psutil_message,
                        'details': psutil_details[:500]  # Limit details length
                    }
                })
            else:
                return JsonResponse({
                    'success': True,
                    'message': f'Server "{name}" added successfully! SSH key deployed. However, {psutil_message}. Please install psutil manually to enable metrics collection.',
                    'psutil_status': {
                        'installed': False,
                        'message': psutil_message,
                        'details': psutil_details[:500],
                        'manual_install': 'Run: pip3 install --user psutil (or sudo apt-get install python3-psutil)'
                    }
                })
        except Exception as e:
            # Server was created but SSH key deployment failed
            server.delete()  # Clean up if SSH key deployment fails
            _log_user_action(request, "ADD_SERVER", f"Failed: SSH key deployment error - {str(e)}")
            error_logger.error(f"ADD_SERVER failed: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': f'SSH key deployment failed: {str(e)}'
            }, status=400)
            
    except ValueError as e:
        _log_user_action(request, "ADD_SERVER", f"Failed: Invalid input - {str(e)}")
        error_logger.error(f"ADD_SERVER validation error: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': f'Invalid input: {str(e)}'
        }, status=400)
    except Exception as e:
        _log_user_action(request, "ADD_SERVER", f"Failed: {str(e)}")
        error_logger.error(f"ADD_SERVER error: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': f'Failed to add server: {str(e)}'
        }, status=500)


def _deploy_ssh_key(server, password):
    """Deploy SSH public key to server using password authentication"""
    private_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
    public_key_path = getattr(settings, "SSH_PUBLIC_KEY_PATH", "/app/ssh_keys/id_rsa.pub")
    
    if not os.path.exists(public_key_path):
        raise FileNotFoundError(f"SSH public key not found at {public_key_path}")
    
    with open(public_key_path, "r") as f:
        public_key = f.read().strip()
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(
            hostname=server.ip_address,
            port=server.port,
            username=server.username,
            password=password,
            timeout=30
        )
        
        check_cmd = f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -F \"{public_key}\" ~/.ssh/authorized_keys || echo NOT_FOUND"
        stdin, stdout, stderr = client.exec_command(check_cmd)
        key_exists = stdout.read().decode().strip()
        
        if key_exists == "NOT_FOUND":
            add_cmd = f'echo \"{public_key}\" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
            stdin, stdout, stderr = client.exec_command(add_cmd)
            exit_status = stdout.channel.recv_exit_status()
            
            if exit_status != 0:
                error = stderr.read().decode()
                raise RuntimeError(f"Failed to add SSH key: {error}")
        
        client.close()
        
        # Test connection with key
        if os.path.exists(private_key_path):
            pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
            test_client = paramiko.SSHClient()
            test_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            test_client.connect(
                hostname=server.ip_address,
                port=server.port,
                username=server.username,
                pkey=pkey,
                timeout=10
            )
            test_client.close()
            
    except paramiko.AuthenticationException:
        raise Exception("Authentication failed. Check username and password.")
    except paramiko.SSHException as e:
        raise Exception(f"SSH error: {str(e)}")
    except Exception as e:
        raise Exception(f"Connection error: {str(e)}")
    finally:
        try:
            client.close()
        except:
            pass


def _install_psutil_on_server(server):
    """
    Install psutil on remote server via SSH using --user flag (safe for root and sudo users).
    Returns: (success: bool, message: str, details: str)
    """
    private_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
    pkey = None
    if os.path.exists(private_key_path):
        try:
            pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
        except:
            pass
    
    if not pkey:
        return False, "SSH key not found", "Cannot connect without SSH key"
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(
            hostname=server.ip_address,
            port=server.port,
            username=server.username,
            pkey=pkey,
            timeout=30
        )
        
        # Check if psutil is already installed
        check_cmd = 'python3 -c "import psutil; print(psutil.__version__)" 2>&1'
        stdin, stdout, stderr = client.exec_command(check_cmd)
        check_output = stdout.read().decode() + stderr.read().decode()
        
        if 'ModuleNotFoundError' not in check_output and 'ImportError' not in check_output:
            version = check_output.strip()
            client.close()
            return True, f"psutil already installed (version: {version})", check_output
        
        # Try installation methods in order of preference (using --user flag for safety)
        install_methods = [
            {
                'name': 'pip3 (user install - safest)',
                'cmd': 'pip3 install --user --upgrade-strategy only-if-needed psutil 2>&1',
                'timeout': 120,
                'success_indicators': ['Successfully installed', 'Requirement already satisfied', 'already satisfied']
            },
            {
                'name': 'python3 -m pip (user install)',
                'cmd': 'python3 -m pip install --user --upgrade-strategy only-if-needed psutil 2>&1',
                'timeout': 120,
                'success_indicators': ['Successfully installed', 'Requirement already satisfied', 'already satisfied']
            },
            {
                'name': 'system package manager (apt, no upgrade)',
                'cmd': 'sudo apt-get update -qq && sudo apt-get install -y --no-upgrade python3-psutil 2>&1',
                'timeout': 180,
                'success_indicators': ['Setting up', 'is already the newest', '0 upgraded', 'newly installed']
            },
            {
                'name': 'system package manager (yum/dnf)',
                'cmd': 'sudo yum install -y --setopt=upgrade_requirements_only=1 python3-psutil 2>&1 || sudo dnf install -y --setopt=upgrade_requirements_only=1 python3-psutil 2>&1',
                'timeout': 180,
                'success_indicators': ['Installed:', 'already installed', 'Nothing to do', 'Complete!']
            },
            {
                'name': 'pip3 (fallback, user install)',
                'cmd': 'pip install --user psutil 2>&1',
                'timeout': 120,
                'success_indicators': ['Successfully installed', 'Requirement already satisfied']
            }
        ]
        
        for method in install_methods:
            try:
                stdin, stdout, stderr = client.exec_command(method['cmd'], timeout=method['timeout'])
                
                # Read output with timeout handling
                import time
                start_time = time.time()
                output_parts = []
                error_parts = []
                
                # Read output in chunks to avoid blocking
                while True:
                    if time.time() - start_time > method['timeout']:
                        break
                    
                    if stdout.channel.recv_ready():
                        output_parts.append(stdout.channel.recv(4096).decode('utf-8', errors='ignore'))
                    
                    if stderr.channel.recv_stderr_ready():
                        error_parts.append(stderr.channel.recv_stderr(4096).decode('utf-8', errors='ignore'))
                    
                    if stdout.channel.exit_status_ready():
                        break
                    
                    time.sleep(0.1)
                
                # Get remaining output
                remaining_output = stdout.read().decode('utf-8', errors='ignore')
                remaining_error = stderr.read().decode('utf-8', errors='ignore')
                
                install_output = ''.join(output_parts) + remaining_output + ''.join(error_parts) + remaining_error
                
                # Check if installation was successful
                if any(indicator.lower() in install_output.lower() for indicator in method['success_indicators']):
                    # Verify installation
                    verify_cmd = 'python3 -c "import psutil; print(psutil.__version__)" 2>&1'
                    stdin, stdout, stderr = client.exec_command(verify_cmd, timeout=10)
                    verify_output = stdout.read().decode() + stderr.read().decode()
                    
                    if 'ModuleNotFoundError' not in verify_output and 'ImportError' not in verify_output:
                        version = verify_output.strip()
                        client.close()
                        return True, f"psutil installed successfully via {method['name']} (version: {version})", install_output[:1000]
                
            except Exception as e:
                # Continue to next method
                continue
        
        client.close()
        return False, "Failed to install psutil", "All installation methods failed. The system may require manual intervention. Try: pip3 install --user psutil (or sudo apt-get install python3-psutil)"
        
    except paramiko.AuthenticationException:
        return False, "Authentication failed", "Cannot authenticate with SSH key"
    except paramiko.SSHException as e:
        return False, f"SSH error: {str(e)}", str(e)
    except Exception as e:
        return False, f"Connection error: {str(e)}", str(e)
    finally:
        try:
            client.close()
        except:
            pass


def _collect_metrics_for_server(server):
    """Collect metrics for a specific server - called immediately after adding server"""
    from django.core.management import call_command
    import sys
    from io import StringIO
    
    # Use the management command to collect metrics
    # This ensures we use the same logic as the scheduled collection
    try:
        # Call the collect_metrics command programmatically
        # We'll use subprocess to call it for the specific server
        # But first, let's try a simpler approach - directly call the collection logic
        from core.management.commands.collect_metrics import Command
        cmd = Command()
        metrics = cmd._collect_metrics(server)
        
        if metrics:
            # Filter out fields that don't exist in SystemMetric model
            metric_fields = {k: v for k, v in metrics.items() 
                           if k not in ['disk_count', 'raid_info']}
            SystemMetric.objects.create(server=server, **metric_fields)
            
            # Also cache in Redis
            redis_key = f"metrics:{server.id}:latest"
            cache.set(redis_key, json.dumps(metrics), timeout=300)  # 5 min TTL
            
            return True
    except Exception as e:
        # If direct call fails, try using subprocess to call the management command
        try:
            result = subprocess.run(
                ['python', '/app/manage.py', 'collect_metrics'],
                capture_output=True,
                text=True,
                timeout=60,
                cwd='/app'
            )
            if result.returncode == 0:
                return True
        except Exception as subprocess_error:
            print(f"Failed to collect metrics via subprocess: {subprocess_error}")
    
    return False


@staff_member_required
@require_http_methods(["POST"])
def remove_server(request, server_id):
    """Remove a server and all its associated data"""
    try:
        server = get_object_or_404(Server, id=server_id)
        server_name = server.name
        
        # Delete all associated data
        SystemMetric.objects.filter(server=server).delete()
        Anomaly.objects.filter(server=server).delete()
        Service.objects.filter(server=server).delete()
        MonitoringConfig.objects.filter(server=server).delete()
        
        # Delete the server
        server.delete()
        
        return JsonResponse({
            'success': True,
            'message': f'Server "{server_name}" and all associated data have been removed successfully.'
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'Failed to remove server: {str(e)}'
        }, status=500)


@staff_member_required
@require_http_methods(["GET", "POST"])
def alert_config(request):
    """Get or save email alert configuration"""
    if request.method == "GET":
        try:
            config = EmailAlertConfig.objects.first()
            if config:
                return JsonResponse({
                    'success': True,
                    'config': {
                        'provider': config.provider,
                        'smtp_host': config.smtp_host,
                        'smtp_port': config.smtp_port,
                        'use_tls': config.use_tls,
                        'smtp_username': config.smtp_username,
                        'from_email': config.from_email,
                        'alert_recipients': config.alert_recipients,
                        'enabled': config.enabled
                    }
                })
            else:
                return JsonResponse({
                    'success': True,
                    'config': None
                })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'Failed to load configuration: {str(e)}'
            }, status=500)
    
    elif request.method == "POST":
        try:
            data = json.loads(request.body)
            
            # Validate required fields
            required_fields = ['smtp_host', 'smtp_port', 'smtp_username', 'smtp_password', 'from_email', 'alert_recipients']
            for field in required_fields:
                if not data.get(field):
                    return JsonResponse({
                        'success': False,
                        'error': f'Field {field} is required'
                    }, status=400)
            
            # Get or create config (only one config allowed)
            config, created = EmailAlertConfig.objects.get_or_create(
                id=1,  # Single instance
                defaults={
                    'provider': data.get('provider', 'custom'),
                    'smtp_host': data['smtp_host'],
                    'smtp_port': int(data['smtp_port']),
                    'use_tls': data.get('use_tls', False),
                    'smtp_username': data['smtp_username'],
                    'smtp_password': data['smtp_password'],  # In production, encrypt this
                    'from_email': data['from_email'],
                    'alert_recipients': data['alert_recipients'],
                    'enabled': True
                }
            )
            
            if not created:
                # Update existing config
                config.provider = data.get('provider', 'custom')
                config.smtp_host = data['smtp_host']
                config.smtp_port = int(data['smtp_port'])
                config.use_tls = data.get('use_tls', False)
                config.smtp_username = data['smtp_username']
                config.smtp_password = data['smtp_password']  # In production, encrypt this
                config.from_email = data['from_email']
                config.alert_recipients = data['alert_recipients']
                config.enabled = True
                config.save()
            
            return JsonResponse({
                'success': True,
                'message': 'Email alert configuration saved successfully!'
            })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'Failed to save configuration: {str(e)}'
            }, status=500)


@staff_member_required
@require_http_methods(["POST"])
def test_email_connection(request):
    """Test email connection by sending a test email"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        data = json.loads(request.body)
        
        # Validate required fields
        required_fields = ['smtp_host', 'smtp_port', 'smtp_username', 'smtp_password', 'from_email', 'alert_recipients']
        for field in required_fields:
            if not data.get(field):
                return JsonResponse({
                    'success': False,
                    'error': f'Field {field} is required'
                }, status=400)
        
        smtp_host = data['smtp_host']
        smtp_port = int(data['smtp_port'])
        use_tls = data.get('use_tls', False)
        smtp_username = data['smtp_username']
        smtp_password = data['smtp_password']
        from_email = data['from_email']
        recipients = [r.strip() for r in data['alert_recipients'].split(',')]
        
        # Create test email
        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = 'Test Alert - Server Monitoring System'
        body = 'This is a test email from the Server Monitoring System. If you receive this, your email configuration is working correctly.'
        msg.attach(MIMEText(body, 'plain'))
        
        # Connect and send
        try:
            if use_tls:
                server = smtplib.SMTP(smtp_host, smtp_port)
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port)
            
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
            server.quit()
            
            return JsonResponse({
                'success': True,
                'message': f'Test email sent successfully to {", ".join(recipients)}!'
            })
        except smtplib.SMTPAuthenticationError:
            return JsonResponse({
                'success': False,
                'error': 'Authentication failed. Please check your username and password (use App Password for Gmail/Outlook).'
            }, status=400)
        except smtplib.SMTPException as e:
            return JsonResponse({
                'success': False,
                'error': f'SMTP error: {str(e)}'
            }, status=400)
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'Connection error: {str(e)}'
            }, status=400)
            
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'Failed to test email: {str(e)}'
        }, status=500)


def _check_and_send_alerts(server, metric):
    """Check if metrics exceed thresholds and send alerts. Also sends resolved alerts when metrics drop below threshold."""
    try:
        # Refresh server to get latest monitoring_config
        server.refresh_from_db()
        config = server.monitoring_config
        
        email_config = EmailAlertConfig.objects.filter(enabled=True).first()
        
        if not email_config:
            print(f"[ALERT] No email config found or disabled for {server.name}")
            return
        
        # Get previous alert state from cache
        cache_key = f"alert_state:{server.id}"
        previous_state = cache.get(cache_key, {})
        
        # Initialize current state
        current_state = {
            'CPU': False,
            'Memory': False,
            'Disk': {}
        }
        
        alerts = []
        resolved_alerts = []
        
        # Check CPU threshold
        cpu_above_threshold = metric.cpu_percent is not None and metric.cpu_percent >= config.cpu_threshold
        current_state['CPU'] = cpu_above_threshold
        
        if cpu_above_threshold:
            alerts.append({
                'type': 'CPU',
                'value': metric.cpu_percent,
                'threshold': config.cpu_threshold,
                'message': f"CPU usage is {metric.cpu_percent:.1f}% (threshold: {config.cpu_threshold}%)"
            })
            print(f"[ALERT] CPU threshold exceeded: {metric.cpu_percent:.1f}% >= {config.cpu_threshold}%")
        elif previous_state.get('CPU', False):
            # CPU was above threshold before, but now it's below - resolved!
            resolved_alerts.append({
                'type': 'CPU',
                'value': metric.cpu_percent,
                'threshold': config.cpu_threshold,
                'message': f"CPU usage has returned to normal: {metric.cpu_percent:.1f}% (threshold: {config.cpu_threshold}%)"
            })
            print(f"[ALERT] CPU threshold resolved: {metric.cpu_percent:.1f}% < {config.cpu_threshold}%")
        
        # Check Memory threshold
        memory_above_threshold = metric.memory_percent is not None and metric.memory_percent >= config.memory_threshold
        current_state['Memory'] = memory_above_threshold
        
        if memory_above_threshold:
            alerts.append({
                'type': 'Memory',
                'value': metric.memory_percent,
                'threshold': config.memory_threshold,
                'message': f"Memory usage is {metric.memory_percent:.1f}% (threshold: {config.memory_threshold}%)"
            })
            print(f"[ALERT] Memory threshold exceeded: {metric.memory_percent:.1f}% >= {config.memory_threshold}%")
        elif previous_state.get('Memory', False):
            # Memory was above threshold before, but now it's below - resolved!
            resolved_alerts.append({
                'type': 'Memory',
                'value': metric.memory_percent,
                'threshold': config.memory_threshold,
                'message': f"Memory usage has returned to normal: {metric.memory_percent:.1f}% (threshold: {config.memory_threshold}%)"
            })
            print(f"[ALERT] Memory threshold resolved: {metric.memory_percent:.1f}% < {config.memory_threshold}%")
        
        # Check Disk thresholds (check all partitions)
        if metric.disk_usage:
            try:
                disk_data = json.loads(metric.disk_usage) if isinstance(metric.disk_usage, str) else metric.disk_usage
                previous_disk_state = previous_state.get('Disk', {})
                
                for mountpoint, usage in disk_data.items():
                    if isinstance(usage, dict):
                        disk_percent = usage.get('percent', 0)
                        disk_above_threshold = disk_percent >= config.disk_threshold
                        current_state['Disk'][mountpoint] = disk_above_threshold
                        
                        if disk_above_threshold:
                            alerts.append({
                                'type': 'Disk',
                                'value': disk_percent,
                                'threshold': config.disk_threshold,
                                'message': f"Disk usage on {mountpoint} is {disk_percent:.1f}% (threshold: {config.disk_threshold}%)"
                            })
                            print(f"[ALERT] Disk threshold exceeded on {mountpoint}: {disk_percent:.1f}% >= {config.disk_threshold}%")
                        elif previous_disk_state.get(mountpoint, False):
                            # Disk was above threshold before, but now it's below - resolved!
                            resolved_alerts.append({
                                'type': 'Disk',
                                'value': disk_percent,
                                'threshold': config.disk_threshold,
                                'message': f"Disk usage on {mountpoint} has returned to normal: {disk_percent:.1f}% (threshold: {config.disk_threshold}%)"
                            })
                            print(f"[ALERT] Disk threshold resolved on {mountpoint}: {disk_percent:.1f}% < {config.disk_threshold}%")
            except Exception as disk_error:
                print(f"[ALERT] Error parsing disk data: {disk_error}")
        
        # Send email if new alerts exist
        if alerts:
            print(f"[ALERT] Sending {len(alerts)} alert(s) for {server.name}")
            _send_alert_email(email_config, server, alerts)
            # Log to AlertHistory
            for alert in alerts:
                AlertHistory.objects.create(
                    server=server,
                    alert_type=alert['type'],
                    status=AlertHistory.AlertStatus.TRIGGERED,
                    value=alert['value'],
                    threshold=alert['threshold'],
                    message=alert['message'],
                    recipients=email_config.alert_recipients
                )
                app_logger.info(f"Alert sent: {server.name} - {alert['type']} - {alert['message']}")
        
        # Send email if resolved alerts exist
        if resolved_alerts:
            print(f"[ALERT] Sending {len(resolved_alerts)} resolved alert(s) for {server.name}")
            _send_resolved_alert_email(email_config, server, resolved_alerts)
            # Log to AlertHistory
            for alert in resolved_alerts:
                AlertHistory.objects.create(
                    server=server,
                    alert_type=alert['type'],
                    status=AlertHistory.AlertStatus.RESOLVED,
                    value=alert['value'],
                    threshold=alert['threshold'],
                    message=alert['message'],
                    recipients=email_config.alert_recipients,
                    resolved_at=timezone.now()
                )
                app_logger.info(f"Alert resolved: {server.name} - {alert['type']} - {alert['message']}")
        
        # Update cache with current state (store for 24 hours)
        cache.set(cache_key, current_state, 86400)
        
        if not alerts and not resolved_alerts:
            print(f"[ALERT] No alerts triggered for {server.name} (CPU: {metric.cpu_percent}, Memory: {metric.memory_percent})")
            
    except Exception as e:
        import traceback
        print(f"[ALERT] Error checking alerts for {server.name}: {e}")
        print(f"[ALERT] Traceback: {traceback.format_exc()}")


def _send_resolved_alert_email(email_config, server, resolved_alerts):
    """Send resolved alert email when metrics return to normal"""
    try:
        recipients = [email.strip() for email in email_config.alert_recipients.split(',')]
        
        # Create email content
        subject = f"✅ Resolved: {server.name} - Threshold Returned to Normal"
        alert_list = "\n".join([f"• {alert['message']}" for alert in resolved_alerts])
        body = f"""
Server Alert Resolved Notification

Server: {server.name}
IP Address: {server.ip_address}
Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}

Resolved Alerts:
{alert_list}

The resource usage has returned to normal levels.
        """
        
        print(f"[ALERT] Attempting to send resolved alert email to {recipients}")
        
        # Send email
        if email_config.use_tls:
            # STARTTLS (port 587)
            print(f"[ALERT] Using STARTTLS on {email_config.smtp_host}:{email_config.smtp_port}")
            server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
            server_smtp.starttls()
            server_smtp.login(email_config.smtp_username, email_config.smtp_password)
        else:
            # SSL (port 465)
            print(f"[ALERT] Using SSL on {email_config.smtp_host}:{email_config.smtp_port}")
            server_smtp = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
            server_smtp.login(email_config.smtp_username, email_config.smtp_password)
        
        msg = MIMEMultipart()
        msg['From'] = email_config.from_email
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        server_smtp.send_message(msg)
        server_smtp.quit()
        
        print(f"[ALERT] ✓ Resolved alert email sent successfully for {server.name} to {recipients}")
        
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"[ALERT] ✗ SMTP Authentication failed for {server.name}: {e}"
        print(error_msg)
        raise Exception(error_msg)
    except smtplib.SMTPException as e:
        error_msg = f"[ALERT] ✗ SMTP error for {server.name}: {e}"
        print(error_msg)
        raise Exception(error_msg)
    except Exception as e:
        error_msg = f"[ALERT] ✗ Error sending resolved alert email for {server.name}: {e}"
        print(error_msg)
        raise Exception(error_msg)


def _send_alert_email(email_config, server, alerts):
    """Send alert email using configured SMTP settings"""
    try:
        recipients = [email.strip() for email in email_config.alert_recipients.split(',')]
        
        # Create email content
        subject = f"🚨 Alert: {server.name} - Threshold Exceeded"
        alert_list = "\n".join([f"• {alert['message']}" for alert in alerts])
        body = f"""
Server Alert Notification

Server: {server.name}
IP Address: {server.ip_address}
Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}

Alerts:
{alert_list}

Please check the server immediately.
        """
        
        print(f"[ALERT] Attempting to send email to {recipients}")
        
        # Send email
        if email_config.use_tls:
            # STARTTLS (port 587)
            print(f"[ALERT] Using STARTTLS on {email_config.smtp_host}:{email_config.smtp_port}")
            server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
            server_smtp.starttls()
            server_smtp.login(email_config.smtp_username, email_config.smtp_password)
        else:
            # SSL (port 465)
            print(f"[ALERT] Using SSL on {email_config.smtp_host}:{email_config.smtp_port}")
            server_smtp = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
            server_smtp.login(email_config.smtp_username, email_config.smtp_password)
        
        msg = MIMEMultipart()
        msg['From'] = email_config.from_email
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        server_smtp.send_message(msg)
        server_smtp.quit()
        
        print(f"[ALERT] ✓ Alert email sent successfully for {server.name} to {recipients}")
        
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"[ALERT] ✗ SMTP Authentication failed for {server.name}: {e}"
        print(error_msg)
        raise Exception(error_msg)
    except smtplib.SMTPException as e:
        error_msg = f"[ALERT] ✗ SMTP error for {server.name}: {e}"
        print(error_msg)
        raise Exception(error_msg)
    except Exception as e:
        import traceback
        error_msg = f"[ALERT] ✗ Error sending alert email for {server.name}: {e}"
        print(error_msg)
        print(f"[ALERT] Traceback: {traceback.format_exc()}")
        raise Exception(error_msg)


@staff_member_required
@require_http_methods(["POST"])
def update_thresholds(request, server_id):
    """Update alert thresholds for a server"""
    try:
        server = Server.objects.get(id=server_id)
        config = server.monitoring_config
        
        cpu_threshold = request.POST.get('cpu_threshold')
        memory_threshold = request.POST.get('memory_threshold')
        disk_threshold = request.POST.get('disk_threshold')
        
        if cpu_threshold:
            config.cpu_threshold = float(cpu_threshold)
        if memory_threshold:
            config.memory_threshold = float(memory_threshold)
        if disk_threshold:
            config.disk_threshold = float(disk_threshold)
        
        config.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Thresholds updated successfully'
        })
    except Server.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Server not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@staff_member_required
def admin_users(request):
    users = User.objects.filter(is_staff=True).order_by("username")
    return render(request, "core/admin_users.html", {"users": users})

@staff_member_required
def create_admin_user(request):
    if request.method == "POST":
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")
        is_superuser = request.POST.get("is_superuser") == "on"
        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists.")
            return redirect("admin_users")
        user = User.objects.create_user(username=username, email=email, password=password, is_staff=True, is_superuser=is_superuser)
        messages.success(request, f"Admin user {username} created successfully.")
        return redirect("admin_users")
    return redirect("admin_users")

@staff_member_required
def edit_admin_user(request, user_id):
    user = get_object_or_404(User, id=user_id, is_staff=True)
    # Prevent staff users from editing superusers
    if not request.user.is_superuser and user.is_superuser:
        messages.error(request, "Staff users cannot edit superuser accounts.")
        return redirect("admin_users")
    if request.method == "POST":
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")
        is_superuser = request.POST.get("is_superuser") == "on"
        is_active = request.POST.get("is_active") == "on"
        if username != user.username and User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists.")
            return redirect("edit_admin_user", user_id=user_id)
        user.username = username
        user.email = email
        if password:
            user.set_password(password)
        user.is_superuser = is_superuser
        user.is_active = is_active
        user.save()
        messages.success(request, f"Admin user {username} updated successfully.")
        return redirect("admin_users")
    return render(request, "core/edit_admin_user.html", {"user": user, "user_id": user_id})

@staff_member_required
def delete_admin_user(request, user_id):
    user = get_object_or_404(User, id=user_id, is_staff=True)
    if user == request.user:
        messages.error(request, "You cannot delete your own account.")
    else:
        username = user.username
        user.delete()
        messages.success(request, f"Admin user {username} deleted successfully.")
    return redirect("admin_users")

@staff_member_required
@require_http_methods(["GET"])
def admin_users_api(request):
    try:
        users = User.objects.filter(is_staff=True).order_by("username")
        users_data = [{"id": u.id, "username": u.username, "email": u.email or "", "is_superuser": u.is_superuser, "is_active": u.is_active} for u in users]
        return JsonResponse({"success": True, "users": users_data})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

@staff_member_required
@require_http_methods(["GET", "PUT", "DELETE"])
def admin_user_api(request, user_id):
    try:
        user = get_object_or_404(User, id=user_id, is_staff=True)
        if request.method == "GET":
            # Prevent staff users from viewing superuser details
            if not request.user.is_superuser and user.is_superuser:
                return JsonResponse({"success": False, "error": "Staff users cannot view superuser accounts."}, status=403)
            return JsonResponse({"success": True, "user": {"id": user.id, "username": user.username, "email": user.email or "", "is_superuser": user.is_superuser, "is_active": user.is_active}})
        elif request.method == "PUT":
            # Prevent staff users from editing superusers
            if not request.user.is_superuser and user.is_superuser:
                return JsonResponse({"success": False, "error": "Staff users cannot edit superuser accounts."}, status=403)
            import json
            data = json.loads(request.body)
            if data.get("username") != user.username and User.objects.filter(username=data.get("username")).exists():
                return JsonResponse({"success": False, "error": "Username already exists."}, status=400)
            user.username = data.get("username", user.username)
            user.email = data.get("email", user.email)
            if data.get("password"):
                user.set_password(data["password"])
            user.is_superuser = data.get("is_superuser", user.is_superuser)
            user.is_active = data.get("is_active", user.is_active)
            user.save()
            return JsonResponse({"success": True, "message": f"User {user.username} updated successfully."})
        elif request.method == "DELETE":
            # Prevent deletion of default admin user
            if user.username.lower() == "admin":
                return JsonResponse({"success": False, "error": "Cannot delete the default admin user."}, status=400)
            if user == request.user:
                return JsonResponse({"success": False, "error": "You cannot delete your own account."}, status=400)
            username = user.username
            user.delete()
            return JsonResponse({"success": True, "message": f"User {username} deleted successfully."})
    except User.DoesNotExist:
        return JsonResponse({"success": False, "error": "User not found."}, status=404)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

@staff_member_required
@require_http_methods(["POST"])
def create_admin_user_api(request):
    try:
        import json
        data = json.loads(request.body)
        username = data.get("username")
        email = data.get("email", "")
        password = data.get("password")
        is_superuser = data.get("is_superuser", False)
        if not username or not password:
            return JsonResponse({"success": False, "error": "Username and password are required."}, status=400)
        if User.objects.filter(username=username).exists():
            return JsonResponse({"success": False, "error": "Username already exists."}, status=400)
        user = User.objects.create_user(username=username, email=email, password=password, is_staff=True, is_superuser=is_superuser)
        return JsonResponse({"success": True, "message": f"User {username} created successfully."})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)
