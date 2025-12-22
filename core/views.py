from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.contrib.auth import logout as auth_logout
from django.utils import timezone
from datetime import timedelta
from .models import Server, SystemMetric, Anomaly, MonitoringConfig, Service, EmailAlertConfig, AlertHistory, UserACL, ServerHeartbeat, MonitoredLog, LogEvent, AnalysisRule, AgentVersion, LoginActivity
from django.http import JsonResponse, HttpResponseRedirect
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_exempt
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


def _calculate_server_status(server):
    """
    Calculate server status based on heartbeat and active alerts.
    
    Status logic:
    - OFFLINE: Monitoring suspended OR no heartbeat OR heartbeat older than threshold
    - ONLINE: Heartbeat within threshold AND no active alerts/anomalies AND monitoring not suspended
    - WARNING: Heartbeat within threshold BUT has active alerts/anomalies AND monitoring not suspended
    
    IMPORTANT: Uses adaptive threshold to handle app downtime gracefully.
    If the monitoring app itself was down, we use a longer grace period to avoid
    false offline statuses when servers are actually still online.
    
    Returns: "offline", "online", or "warning"
    """
    from .models import Anomaly, AlertHistory, MonitoringConfig
    from django.core.cache import cache
    from datetime import datetime, timedelta
    import os
    
    # Check if monitoring is suspended - if so, server appears offline
    try:
        config = server.monitoring_config
        if config.monitoring_suspended:
            return "offline"
    except (MonitoringConfig.DoesNotExist, AttributeError):
        pass  # Continue with normal status calculation
    
    # Determine adaptive threshold based on app downtime
    base_threshold = 60  # Normal threshold: 60 seconds
    app_heartbeat_key = "monitoring_app_heartbeat"
    app_heartbeat_file = "/tmp/monitoring_app_heartbeat.txt"
    
    # Check if monitoring app was recently down
    app_was_down = False
    try:
        from .utils import parse_app_heartbeat
        
        # Check cache first (fast, expires after 5 min)
        app_last_heartbeat_str = cache.get(app_heartbeat_key)
        if app_last_heartbeat_str:
            app_last_heartbeat = parse_app_heartbeat(app_last_heartbeat_str)
            if app_last_heartbeat:
                app_downtime = (timezone.now() - app_last_heartbeat).total_seconds()
                # If app heartbeat is missing or very old, app was likely down
                if app_downtime > 300:  # 5 minutes
                    app_was_down = True
        else:
            # Check file as fallback (persists across restarts)
            if os.path.exists(app_heartbeat_file):
                with open(app_heartbeat_file, 'r') as f:
                    app_last_heartbeat_str = f.read().strip()
                app_last_heartbeat = parse_app_heartbeat(app_last_heartbeat_str)
                if app_last_heartbeat:
                    app_downtime = (timezone.now() - app_last_heartbeat).total_seconds()
                    if app_downtime > 300:  # 5 minutes
                        app_was_down = True
                else:
                    app_was_down = True
            else:
                app_was_down = True
    except Exception:
        # If we can't determine, assume app wasn't down (conservative approach)
        app_was_down = False
    
    # Use longer threshold if app was down (grace period after app restart)
    if app_was_down:
        threshold = 600  # 10 minutes grace period after app restart
    else:
        threshold = base_threshold
    
    # Check heartbeat
    try:
        heartbeat = ServerHeartbeat.objects.get(server=server)
        time_diff = timezone.now() - heartbeat.last_heartbeat
        time_diff_seconds = time_diff.total_seconds()
        
        # Use adaptive threshold
        if time_diff_seconds > threshold:
            return "offline"
        
        # Server is online (heartbeat OK) - check for alerts
        active_anomalies = Anomaly.objects.filter(server=server, resolved=False).exists()
        active_alerts = AlertHistory.objects.filter(server=server, status="triggered").exists()
        
        if active_anomalies or active_alerts:
            return "warning"
        else:
            return "online"
            
    except ServerHeartbeat.DoesNotExist:
        # No heartbeat record - server is offline
        return "offline"


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
    from .models import Anomaly, AlertHistory
    
    # Don't use .only() with select_related on same field - causes Django error
    servers = Server.objects.filter(monitoring_config__enabled=True).select_related("monitoring_config")
    metrics_data = []
    
    for server in servers:
        # Try Redis first (fast)
        redis_key = f"metrics:{server.id}:latest"
        cached_metric = cache.get(redis_key)
        
        latest_metric = None
        if cached_metric:
            try:
                metric = json.loads(cached_metric) if isinstance(cached_metric, str) else cached_metric
                # Ensure all required fields are present
                if "disk_io_read" not in metric:
                    metric["disk_io_read"] = 0
                if "net_io_sent" not in metric:
                    metric["net_io_sent"] = 0
                # Get timestamp from cache if available
                if "timestamp" in metric:
                    try:
                        from datetime import datetime
                        if isinstance(metric["timestamp"], str):
                            latest_metric_timestamp = datetime.fromisoformat(metric["timestamp"].replace('Z', '+00:00'))
                        else:
                            latest_metric_timestamp = metric["timestamp"]
                    except:
                        latest_metric_timestamp = None
                else:
                    latest_metric_timestamp = None
                metrics_data.append(metric)
                # Calculate status based on heartbeat
                metric["status"] = _calculate_server_status(server)
                continue
            except:
                pass
        
        # Fallback to PostgreSQL if Redis miss
        latest_metric = SystemMetric.objects.filter(server=server).only(
            "cpu_percent", "memory_percent", "disk_usage", "disk_io_read", "disk_io_write",
            "net_io_sent", "net_io_recv", "timestamp"
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
            
            # Convert disk I/O from bytes/sec to KB/s
            disk_io_read_kb = 0
            if latest_metric.disk_io_read:
                try:
                    disk_io_read_kb = round(float(latest_metric.disk_io_read) / 1024, 0)
                except:
                    pass
            
            # Convert network I/O from bytes/sec to KB/s
            net_io_sent_kb = 0
            if latest_metric.net_io_sent:
                try:
                    net_io_sent_kb = round(float(latest_metric.net_io_sent) / 1024, 0)
                except:
                    pass
            
            # Calculate status based on heartbeat
            status = _calculate_server_status(server)
            
            metrics_data.append({
                "server_id": server.id,
                "server_name": server.name,
                "cpu_percent": latest_metric.cpu_percent or 0,
                "memory_percent": latest_metric.memory_percent or 0,
                "disk_percent": disk_percent,
                "disk_io_read": disk_io_read_kb,
                "net_io_sent": net_io_sent_kb,
                "timestamp": latest_metric.timestamp.isoformat(),
                "status": status,
            })
    
    return JsonResponse({"metrics": metrics_data})


@require_http_methods(["POST"])
def heartbeat_api(request, server_id):
    """
    API endpoint for agent heartbeat signals.
    Agents send POST requests every 30 seconds to indicate server is online.
    """
    try:
        server = Server.objects.get(id=server_id)
    except Server.DoesNotExist:
        return JsonResponse({"error": "Server not found"}, status=404)
    
    # Get optional agent version from request
    agent_version = None
    if request.content_type == 'application/json':
        try:
            import json
            data = json.loads(request.body)
            agent_version = data.get('agent_version', None)
        except:
            pass
    
    # Update or create heartbeat record
    heartbeat, created = ServerHeartbeat.objects.update_or_create(
        server=server,
        defaults={
            'last_heartbeat': timezone.now(),
            'agent_version': agent_version,
        }
    )
    
    return JsonResponse({
        "status": "ok",
        "server_id": server.id,
        "server_name": server.name,
        "heartbeat_received": True,
        "timestamp": heartbeat.last_heartbeat.isoformat(),
    })


@require_http_methods(["GET"])
def metric_history_api(request, server_id):
    """
    API endpoint for retrieving metric history with anomaly overlays.
    
    Returns time-series data for CPU, memory, disk, and network metrics
    along with anomaly events for visualization in charts.
    
    Args:
        request: Django HTTP request
        server_id: Integer server ID
    
    Query Parameters:
        hours: Number of hours of history to retrieve (default: 6, max: 24)
    
    Returns:
        JsonResponse: Metric history data with anomalies
        
    Example Response:
        {
            "timestamps": ["2024-01-01T12:00:00Z", ...],
            "cpu": [45.2, 47.8, ...],
            "memory": [62.1, 63.5, ...],
            "disk": [78.3, 78.5, ...],
            "anomalies": [
                {
                    "timestamp": "2024-01-01T12:05:00Z",
                    "metric_name": "cpu_percent",
                    "metric_type": "cpu",
                    "severity": "HIGH",
                    "metric_value": 85.3
                },
                ...
            ]
        }
    """
    try:
        # Get server or return 404
        try:
            server = Server.objects.get(id=server_id)
        except Server.DoesNotExist:
            return JsonResponse(
                {"error": "Server not found"},
                status=404
            )
        
        # Parse time range from query parameter
        # Accept either 'range' (1h, 7d, 1m, 3m) or 'hours' (legacy support)
        range_param = request.GET.get('range', '1h')
        hours_param = request.GET.get('hours', None)
        
        # Legacy support: if hours is provided, use it
        if hours_param:
            try:
                hours = int(hours_param)
                hours = min(max(hours, 1), 24)
                since = timezone.now() - timedelta(hours=hours)
            except (ValueError, TypeError):
                range_param = '1h'  # Fallback to default
        
        # Parse range parameter
        if not hours_param:
            range_param = range_param.lower()
            if range_param == '1h':
                hours = 1
            elif range_param == '7d':
                hours = 7 * 24  # 7 days
            elif range_param == '1m':
                hours = 30 * 24  # 30 days (1 month)
            elif range_param == '3m':
                hours = 90 * 24  # 90 days (3 months)
            else:
                # Default to 1 hour if invalid range
                hours = 1
            
            # Calculate time range
            since = timezone.now() - timedelta(hours=hours)
        
        # Query SystemMetric for this server in the time range
        # Use select_related and ensure we get all fields needed
        metrics_qs = (
            SystemMetric.objects
            .filter(server=server, timestamp__gte=since)
            .order_by("timestamp")
            .only("timestamp", "cpu_percent", "memory_percent", "disk_usage")
        )
        
        # Log query for debugging (can be removed in production)
        metrics_count = metrics_qs.count()
        if metrics_count == 0:
            app_logger.warning(f"No metrics found for server {server.name} (ID: {server_id}) in range {range_param} (since: {since})")
        
        # Convert to list for processing
        metrics_list = list(metrics_qs)
        
        # Down-sample if too many points (cap at ~500)
        # Use smarter downsampling that preserves peaks and boundaries
        if len(metrics_list) > 500:
            # Calculate step size
            step = max(1, len(metrics_list) // 500)
            
            # Smart downsampling: keep first, last, and evenly sample middle
            # Also preserve points with high CPU/memory values (potential spikes)
            downsampled = []
            
            # Always keep first point
            if metrics_list:
                downsampled.append(metrics_list[0])
            
            # Sample middle points, but prioritize high values
            high_value_indices = set()
            for i, metric in enumerate(metrics_list[1:-1], start=1):
                # Mark indices with high CPU or memory (>80%)
                if (metric.cpu_percent and metric.cpu_percent > 80) or \
                   (metric.memory_percent and metric.memory_percent > 80):
                    high_value_indices.add(i)
            
            # Add evenly sampled points and high-value points
            for i in range(1, len(metrics_list) - 1):
                if i % step == 0 or i in high_value_indices:
                    if metrics_list[i] not in downsampled:
                        downsampled.append(metrics_list[i])
            
            # Always keep last point
            if len(metrics_list) > 1:
                if metrics_list[-1] not in downsampled:
                    downsampled.append(metrics_list[-1])
            
            # Sort by timestamp to maintain order
            metrics_list = sorted(downsampled, key=lambda m: m.timestamp)
        
        # Extract arrays
        timestamps = []
        cpu_values = []
        memory_values = []
        disk_values = []
        
        for metric in metrics_list:
            timestamps.append(metric.timestamp.isoformat())
            # Ensure we're using the actual values, not None
            cpu_val = float(metric.cpu_percent) if metric.cpu_percent is not None else 0.0
            memory_val = float(metric.memory_percent) if metric.memory_percent is not None else 0.0
            cpu_values.append(cpu_val)
            memory_values.append(memory_val)
            
            # Extract max disk percent from disk_usage JSONField
            max_disk = None
            if metric.disk_usage:
                try:
                    if isinstance(metric.disk_usage, str):
                        disk_data = json.loads(metric.disk_usage)
                    else:
                        disk_data = metric.disk_usage
                    
                    max_disk = 0.0
                    for mount, usage in disk_data.items():
                        if isinstance(usage, dict):
                            percent = usage.get("percent", 0.0)
                        else:
                            percent = float(usage) if isinstance(usage, (int, float)) else 0.0
                        max_disk = max(max_disk, float(percent))
                except (json.JSONDecodeError, TypeError, ValueError):
                    max_disk = None
            
            disk_values.append(float(max_disk) if max_disk is not None else None)
        
        # Query anomalies for this server in the same time range
        anomalies_qs = Anomaly.objects.filter(
            server=server,
            timestamp__gte=since
        ).order_by("timestamp").only(
            "timestamp", "metric_name", "metric_type", "severity", "metric_value"
        )
        
        # Extract anomaly points
        anomaly_points = []
        for anomaly in anomalies_qs:
            anomaly_points.append({
                "timestamp": anomaly.timestamp.isoformat(),
                "metric_name": anomaly.metric_name,
                "metric_type": anomaly.metric_type,
                "severity": anomaly.severity,
                "metric_value": float(anomaly.metric_value) if anomaly.metric_value is not None else 0.0,
            })
        
        # Build response
        response_data = {
            "timestamps": timestamps,
            "cpu": cpu_values,
            "memory": memory_values,
            "disk": disk_values,
            "anomalies": anomaly_points
        }
        
        return JsonResponse(response_data, safe=False)
        
    except Exception as e:
        # Log error and return error response
        app_logger.error(f"Error in metric_history_api for server {server_id}: {e}")
        return JsonResponse(
            {"error": "Internal server error", "message": str(e)},
            status=500
        )


@require_http_methods(["GET"])
def anomaly_status_api(request, server_id):
    """
    API endpoint for retrieving anomaly status for a server.
    
    This endpoint:
    1. Tries to load cached status from Redis
    2. Validates cache against database (if cache shows active anomalies, verify they still exist)
    3. If cache miss or invalid, computes fresh summary and caches it
    4. Returns JSON response with anomaly status
    
    Args:
        request: Django HTTP request
        server_id: Integer server ID
    
    Returns:
        JsonResponse: Anomaly status summary or error response
    
    Example Response:
        {
            "active": 2,
            "highest_severity": "HIGH",
            "timestamp": "2024-01-01T12:00:00Z",
            "details": {
                "cpu": "anomaly",
                "memory": "normal",
                "disk": "anomaly",
                "network": "normal"
            }
        }
    """
    try:
        # Import here to avoid circular imports
        from .anomaly_cache import AnomalyCache
        from .anomaly_status_service import AnomalyStatusService
        from .models import Anomaly
        
        # Get server or return 404
        try:
            server = Server.objects.get(id=server_id)
        except Server.DoesNotExist:
            return JsonResponse(
                {"error": "Server not found"},
                status=404
            )
        
        # ALWAYS check database first to ensure accuracy
        # This ensures we catch any resolved anomalies immediately
        actual_active_count = Anomaly.objects.filter(
            server=server,
            resolved=False
        ).count()
        
        # Try to load from Redis cache
        cached_summary = AnomalyCache.load_status(server_id)
        
        if cached_summary is not None:
            # Cache hit - validate cache against database
            cached_active = cached_summary.get('active', 0)
            cached_severity = cached_summary.get('highest_severity', 'OK')
            
            # If DB shows different count, cache is stale - clear and recompute
            if actual_active_count != cached_active:
                # Cache is stale - clear it and recompute
                AnomalyCache.clear(server_id)
                summary = AnomalyStatusService.refresh_and_cache(server)
                return JsonResponse(summary)
            
            # Additional validation: if DB shows 0 but cache shows non-OK severity, invalidate
            if actual_active_count == 0 and cached_severity != 'OK':
                AnomalyCache.clear(server_id)
                summary = AnomalyStatusService.refresh_and_cache(server)
                return JsonResponse(summary)
            
            # Cache matches DB - return it
            return JsonResponse(cached_summary)
        
        # Cache miss - compute fresh summary
        summary = AnomalyStatusService.refresh_and_cache(server)
        
        # Return the summary
        return JsonResponse(summary)
        
    except Exception as e:
        # Log error and return fallback OK response (never return error to avoid loading state)
        app_logger.error(f"Error in anomaly_status_api for server {server_id}: {e}")
        # Return a valid OK response to prevent UI from getting stuck at "Loading..."
        return JsonResponse({
            "active": 0,
            "highest_severity": "OK",
            "timestamp": timezone.now().isoformat(),
            "details": {
                "cpu": "normal",
                "memory": "normal",
                "disk": "normal",
                "network": "normal"
            }
        })


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
    
    # Get all services (running and stopped) - ensure monitoring_enabled defaults to False
    all_services = Service.objects.filter(server=server).order_by("name")
    # Ensure all services have monitoring_enabled set (default to False if None)
    for service in all_services:
        if service.monitoring_enabled is None:
            service.monitoring_enabled = False
            service.save()
    
    # Get recent anomalies (last 50)
    recent_anomalies = Anomaly.objects.filter(server=server).select_related("metric").order_by("-timestamp")[:50]
    
    # Parse disk usage
    disk_data = {}
    disk_percent = 0
    if latest_metric and latest_metric.disk_usage:
        import json
        disk_data = (
            json.loads(latest_metric.disk_usage)
            if isinstance(latest_metric.disk_usage, str)
            else latest_metric.disk_usage
        )
        # Get root partition disk percent
        root_partition = disk_data.get("/", None)
        if root_partition and isinstance(root_partition, dict):
            disk_percent = root_partition.get("percent", 0) or 0
    
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
    

    # Calculate server status based on heartbeat
    server_status = _calculate_server_status(server)
    # Get recent alerts for this server (last 50, only triggered ones)
    # Use the string value to match what's stored in the database
    recent_alerts = AlertHistory.objects.filter(
        server=server,
        status="triggered"  # Using string value to match database storage
    ).order_by("-sent_at")[:50]
    
    # Get all alerts for this server only (both triggered and resolved)
    all_alerts = AlertHistory.objects.filter(
        server=server
    ).select_related('server').order_by("-sent_at")[:100]
    
    # Get all alert types
    alert_types = AlertHistory.AlertType.choices
    
    # Get all alert statuses
    alert_statuses = AlertHistory.AlertStatus.choices
    
    # Get monitored disks from config (default to ["/"] if not set)
    monitored_disks = server.monitoring_config.monitored_disks if hasattr(server, 'monitoring_config') and server.monitoring_config.monitored_disks else ["/"]
    
    context = {
        "server": server,
        "server_status": server_status,
        "latest_metric": latest_metric,
        "recent_metrics": recent_metrics,
        "all_services": all_services,
        "recent_anomalies": recent_anomalies,
        "disk_summary": disk_summary,
        "disk_data": disk_data,
        "disk_percent": disk_percent,
        "monitored_disks": monitored_disks,
        "chart_data": chart_data_json,
        "recent_alerts": recent_alerts,
        "all_alerts": all_alerts,
        "alert_types": alert_types,
        "alert_statuses": alert_statuses,
        "show_sidebar": True,
    }
    
    return render(request, "core/server_details.html", context)

@staff_member_required
def server_list(request):
    """Server list view with CRUD actions"""
    from .utils import has_privilege

    if not has_privilege(request.user, 'add_server'):
        messages.error(request, "You don't have permission to view servers.")
        return redirect('monitoring_dashboard')

    servers = Server.objects.all().select_related("monitoring_config").order_by("name")

    # Calculate server status based on heartbeat
    servers_with_status = []
    for server in servers:
        server.status = _calculate_server_status(server)
        # Get last heartbeat timestamp if available
        try:
            from .models import ServerHeartbeat
            heartbeat = ServerHeartbeat.objects.filter(server=server).first()
            server.last_checkin = heartbeat.last_heartbeat if heartbeat else None
        except:
            server.last_checkin = None
        servers_with_status.append(server)

    context = {
        'servers': servers_with_status,
        'show_sidebar': True,
    }
    return render(request, "core/server_list.html", context)

@staff_member_required
def edit_server(request, server_id):
    """Edit server configuration"""
    from .utils import has_privilege

    if not has_privilege(request.user, 'add_server'):
        messages.error(request, "You don't have permission to edit servers.")
        return redirect('server_list')

    server = get_object_or_404(Server, id=server_id)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        ip_address = request.POST.get('ip_address', '').strip()
        port = request.POST.get('port', '').strip()

        if not name or not ip_address:
            messages.error(request, "Server name and IP address are required.")
            return redirect('edit_server', server_id=server_id)

        server.name = name
        server.ip_address = ip_address
        if port:
            server.port = int(port)
        server.save()

        messages.success(request, f"Server '{name}' updated successfully.")
        return redirect('server_list')

    context = {
        'server': server,
        'show_sidebar': True,
    }
    return render(request, "core/edit_server.html", context)

@staff_member_required
def delete_server(request, server_id):
    """Delete server"""
    from .utils import has_privilege

    if not has_privilege(request.user, 'add_server'):
        messages.error(request, "You don't have permission to delete servers.")
        return redirect('server_list')

    server = get_object_or_404(Server, id=server_id)

    if request.method == 'POST':
        server_name = server.name
        server.delete()
        messages.success(request, f"Server '{server_name}' deleted successfully.")
        return redirect('server_list')

    context = {
        'server': server,
        'show_sidebar': True,
    }
    return render(request, "core/delete_server.html", context)

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
        active_alerts = AlertHistory.objects.filter(server=server, status="triggered").count()
        has_active_issues = active_anomalies.exists() or active_alerts > 0
        alert_count += active_anomalies.count()
        
        # Get monitoring config values (for display only, NOT for status calculation)
        from .models import MonitoringConfig
        try:
            config = server.monitoring_config
            monitoring_enabled = config.enabled
            alert_suppressed = config.alert_suppressed
            monitoring_suspended = config.monitoring_suspended
        except MonitoringConfig.DoesNotExist:
            # Create MonitoringConfig if it doesn't exist
            config = MonitoringConfig.objects.create(server=server)
            monitoring_enabled = config.enabled
            alert_suppressed = config.alert_suppressed
            monitoring_suspended = config.monitoring_suspended
        except AttributeError:
            # Handle case where monitoring_config is None
            monitoring_enabled = False
            alert_suppressed = False
            monitoring_suspended = False
        
        # Calculate server status based on heartbeat
        status = _calculate_server_status(server)
        
        # Update counts
        if status == "online":
            online_count += 1
        elif status == "warning":
            warning_count += 1
        else:
            offline_count += 1
        
        # Calculate uptime (actual system uptime from server)
        uptime_days = 0
        uptime_hours = 0
        uptime_minutes = 0
        uptime_formatted = "—"
        
        if latest_metric:
            # Try to get actual system uptime from the latest metric
            system_uptime_seconds = getattr(latest_metric, 'system_uptime_seconds', None)
            
            if system_uptime_seconds is not None and system_uptime_seconds > 0:
                # Use actual system uptime
                total_seconds = system_uptime_seconds
                uptime_days = total_seconds // 86400
                uptime_hours = (total_seconds % 86400) // 3600
                uptime_minutes = (total_seconds % 3600) // 60
                
                # Format uptime string
                if uptime_days > 0:
                    uptime_formatted = f"{uptime_days}d {uptime_hours}h {uptime_minutes}m"
                elif uptime_hours > 0:
                    uptime_formatted = f"{uptime_hours}h {uptime_minutes}m"
                elif uptime_minutes > 0:
                    uptime_formatted = f"{uptime_minutes}m"
                else:
                    uptime_formatted = f"{total_seconds}s"
            else:
                # Fallback: Use time since first metric (for old data or if uptime not collected)
                first_metric = SystemMetric.objects.filter(server=server).order_by("timestamp").first()
                if first_metric:
                    uptime_delta = latest_metric.timestamp - first_metric.timestamp
                    total_seconds = int(uptime_delta.total_seconds())
                    uptime_days = total_seconds // 86400
                    uptime_hours = (total_seconds % 86400) // 3600
                    uptime_minutes = (total_seconds % 3600) // 60
                    
                    # Format uptime string
                    if uptime_days > 0:
                        uptime_formatted = f"{uptime_days}d {uptime_hours}h {uptime_minutes}m"
                    elif uptime_hours > 0:
                        uptime_formatted = f"{uptime_hours}h {uptime_minutes}m"
                    elif uptime_minutes > 0:
                        uptime_formatted = f"{uptime_minutes}m"
                    else:
                        uptime_formatted = f"{total_seconds}s"
        
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

        # Calculate server status based on heartbeat
        calculated_status = _calculate_server_status(server)

        # Create a wrapper object for latest_metric with uptime_formatted
        class MetricWrapper:
            def __init__(self, metric, uptime_str):
                self.metric = metric
                self.uptime_formatted = uptime_str
                # Proxy other attributes to the original metric
                if metric:
                    self.cpu_percent = metric.cpu_percent
                    self.memory_percent = metric.memory_percent
                    self.disk_io_read = metric.disk_io_read
                    self.net_io_sent = metric.net_io_sent
                else:
                    self.cpu_percent = None
                    self.memory_percent = None
                    self.disk_io_read = None
                    self.net_io_sent = None
        
        latest_metric_wrapper = MetricWrapper(latest_metric, uptime_formatted)
        
        servers_data.append({
            "server": server,
            "server_status": calculated_status,
            "latest_metric": latest_metric_wrapper,
            "status": status,
            "uptime_days": uptime_days,
            "uptime_hours": uptime_hours,
            "uptime_minutes": uptime_minutes,
            "uptime_formatted": uptime_formatted,
            "network_download": network_download,
            "network_upload": network_upload,
            "active_anomalies": active_anomalies.count(),
            "active_anomalies_list": list(active_anomalies[:10]),
            "monitoring_enabled": monitoring_enabled,
            "disk_percent": disk_percent,
            "alert_suppressed": alert_suppressed,
            "monitoring_suspended": monitoring_suspended,
        })

    context = {
        "servers_data": servers_data,
        "total_servers": len(servers_data),
        "online_count": online_count,
        "warning_count": warning_count,
        "offline_count": offline_count,
        "alert_count": alert_count,
        "show_sidebar": True,
    }
    context.update(admin.site.each_context(request))
    
    return render(request, "core/monitoring_dashboard.html", context)


@staff_member_required
def add_server(request):
    """Add a new server with SSH key deployment"""
    from .utils import has_privilege

    if not has_privilege(request.user, 'add_server'):
        messages.error(request, "You don't have permission to add servers.")
        return redirect('monitoring_dashboard')

    if request.method == "GET":
        # Render the add server page
        context = {
            "show_sidebar": True,
        }
        context.update(admin.site.each_context(request))
        return render(request, "core/add_server.html", context)
    
    # Handle POST request
    _log_user_action(request, "ADD_SERVER", f"Attempting to add server")
    try:
        name = request.POST.get('name', '').strip()
        ip_address = request.POST.get('ip_address', '').strip()
        port = int(request.POST.get('port', 22))
        username = request.POST.get('username', 'root').strip()
        password = request.POST.get('password', '').strip()
        
        # Validate required fields
        if not all([name, ip_address, username, password]):
            messages.error(request, 'All fields are required.')
            return render(request, "core/add_server.html", {"show_sidebar": True})

        # Validate port range
        if port < 1 or port > 65535:
            messages.error(request, 'Port must be between 1 and 65535.')
            return render(request, "core/add_server.html", {"show_sidebar": True})
        
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
            
            # Trigger immediate metrics collection (only if psutil is installed)
            if psutil_success:
                # Collect metrics immediately (not in background) to ensure it happens
                try:
                    collection_success = _collect_metrics_for_server(server)
                    if collection_success:
                        messages.success(request, f'Server "{name}" added successfully! SSH key deployed. {psutil_message}. Initial metrics collected.')
                    else:
                        messages.success(request, f'Server "{name}" added successfully! SSH key deployed. {psutil_message}. Metrics collection will start automatically.')
                except Exception as e:
                    # Log error but don't fail the request - scheduler will pick it up
                    error_logger.error(f"Failed to collect initial metrics for {server.name}: {e}")
                    messages.success(request, f'Server "{name}" added successfully! SSH key deployed. {psutil_message}. Metrics collection will start automatically.')
                
                return redirect('server_list')
            else:
                messages.success(request, f'Server "{name}" added successfully! SSH key deployed. However, {psutil_message}. Please install psutil manually to enable metrics collection.')
                return redirect('server_list')
        except Exception as e:
            # Server was created but SSH key deployment failed
            server.delete()  # Clean up if SSH key deployment fails
            _log_user_action(request, "ADD_SERVER", f"Failed: SSH key deployment error - {str(e)}")
            error_logger.error(f"ADD_SERVER failed: {str(e)}")
            messages.error(request, f'SSH key deployment failed: {str(e)}')
            return render(request, "core/add_server.html", {"show_sidebar": True})
            
    except ValueError as e:
        _log_user_action(request, "ADD_SERVER", f"Failed: Invalid input - {str(e)}")
        error_logger.error(f"ADD_SERVER validation error: {str(e)}")
        messages.error(request, f'Invalid input: {str(e)}')
        return render(request, "core/add_server.html", {"show_sidebar": True})
    except Exception as e:
        _log_user_action(request, "ADD_SERVER", f"Failed: {str(e)}")
        error_logger.error(f"ADD_SERVER error: {str(e)}")
        messages.error(request, f'Failed to add server: {str(e)}')
        return render(request, "core/add_server.html", {"show_sidebar": True})


@staff_member_required
@require_http_methods(["POST"])
def add_server_api(request):
    """API endpoint for adding server with progress updates"""
    from .utils import has_privilege
    from django.http import JsonResponse
    import json as json_module
    
    if not has_privilege(request.user, 'add_server'):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    
    try:
        data = json_module.loads(request.body)
        name = data.get('name', '').strip()
        ip_address = data.get('ip_address', '').strip()
        port = int(data.get('port', 22))
        username = data.get('username', 'root').strip()
        password = data.get('password', '').strip()
        
        progress = []
        
        # Validate required fields
        if not all([name, ip_address, username, password]):
            return JsonResponse({"success": False, "error": "All fields are required"})
        
        # Validate port range
        if port < 1 or port > 65535:
            return JsonResponse({"success": False, "error": "Port must be between 1 and 65535"})
        
        # Step 1: Create server object
        server = Server(
            name=name,
            ip_address=ip_address,
            port=port,
            username=username
        )
        server.save()
        progress.append({"step": "Creating server record", "status": "completed", "message": "Server record created"})
        
        # Step 2: Create monitoring configuration
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
        progress.append({"step": "Creating monitoring configuration", "status": "completed", "message": "Monitoring config created"})
        
        # Step 3: Deploy SSH key
        try:
            progress.append({"step": "Connecting via SSH", "status": "in_progress", "message": "Establishing SSH connection..."})
            _deploy_ssh_key(server, password)
            server.ssh_key_deployed = True
            server.ssh_key_deployed_at = timezone.now()
            server.save(update_fields=["ssh_key_deployed", "ssh_key_deployed_at"])
            progress.append({"step": "Connecting via SSH", "status": "completed", "message": "SSH connection successful"})
            progress.append({"step": "Deploying SSH key", "status": "completed", "message": "SSH key deployed successfully"})
            
            # Step 4: Check requirements / Install psutil
            progress.append({"step": "Checking requirements", "status": "in_progress", "message": "Checking Python and dependencies..."})
            psutil_success, psutil_message, psutil_details = _install_psutil_on_server(server)
            if psutil_success:
                progress.append({"step": "Checking requirements", "status": "completed", "message": psutil_message})
            else:
                progress.append({"step": "Checking requirements", "status": "warning", "message": psutil_message})
            
            # Step 5: Collect initial metrics
            if psutil_success:
                progress.append({"step": "Collecting initial metrics", "status": "in_progress", "message": "Collecting system metrics..."})
                try:
                    collection_success = _collect_metrics_for_server(server)
                    if collection_success:
                        progress.append({"step": "Collecting initial metrics", "status": "completed", "message": "Initial metrics collected successfully"})
                    else:
                        progress.append({"step": "Collecting initial metrics", "status": "warning", "message": "Metrics collection will start automatically"})
                except Exception as e:
                    error_logger.error(f"Failed to collect initial metrics for {server.name}: {e}")
                    progress.append({"step": "Collecting initial metrics", "status": "warning", "message": "Metrics collection will start automatically"})
            
            progress.append({"step": "Setup complete", "status": "completed", "message": f'Server "{name}" added successfully!'})
            
            return JsonResponse({
                "success": True,
                "server_id": server.id,
                "server_name": server.name,
                "progress": progress,
                "message": f'Server "{name}" added successfully!'
            })
        except Exception as e:
            # Clean up if SSH deployment fails
            server.delete()
            progress.append({"step": "Connecting via SSH", "status": "failed", "message": str(e)})
            _log_user_action(request, "ADD_SERVER", f"Failed: SSH key deployment error - {str(e)}")
            error_logger.error(f"ADD_SERVER failed: {str(e)}")
            return JsonResponse({
                "success": False,
                "error": f'SSH key deployment failed: {str(e)}',
                "progress": progress
            }, status=500)
            
    except ValueError as e:
        _log_user_action(request, "ADD_SERVER", f"Failed: Invalid input - {str(e)}")
        error_logger.error(f"ADD_SERVER validation error: {str(e)}")
        return JsonResponse({"success": False, "error": f'Invalid input: {str(e)}'}, status=400)
    except Exception as e:
        _log_user_action(request, "ADD_SERVER", f"Failed: {str(e)}")
        error_logger.error(f"ADD_SERVER error: {str(e)}")
        return JsonResponse({"success": False, "error": f'Failed to add server: {str(e)}'}, status=500)


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
    
    # CRITICAL: Check if monitoring is suspended or disabled before collecting
    try:
        monitoring_config = server.monitoring_config
        if not monitoring_config.enabled:
            error_logger.warning(f"[METRICS] Skipping metric collection for {server.name} - monitoring disabled")
            return False
        if monitoring_config.monitoring_suspended:
            error_logger.warning(f"[METRICS] Skipping metric collection for {server.name} - monitoring suspended")
            return False
    except Exception as e:
        # If no config exists, allow collection (for new servers)
        error_logger.warning(f"[METRICS] No monitoring config for {server.name}, attempting collection anyway: {e}")
    
    # Use the management command to collect metrics
    # This ensures we use the same logic as the scheduled collection
    try:
        # Directly call the collection logic
        from core.management.commands.collect_metrics import Command
        cmd = Command()
        metrics = cmd._collect_metrics(server)
        
        if metrics:
            # Filter out fields that don't exist in SystemMetric model
            # Remove internal/temporary fields (starting with _) and other non-model fields
            from core.models import SystemMetric
            valid_fields = {f.name for f in SystemMetric._meta.get_fields() if hasattr(f, 'name')}
            # Filter: must be in valid_fields AND not start with underscore
            metric_fields = {}
            for k, v in metrics.items():
                if k in valid_fields and not k.startswith('_'):
                    metric_fields[k] = v
            
            # Create the metric record
            SystemMetric.objects.create(server=server, **metric_fields)
            
            # Also cache in Redis
            redis_key = f"metrics:{server.id}:latest"
            cache.set(redis_key, json.dumps(metrics), timeout=300)  # 5 min TTL
            
            app_logger.info(f"[METRICS] Successfully collected initial metrics for {server.name}")
            return True
        else:
            error_logger.warning(f"[METRICS] Collection returned no metrics for {server.name}")
            return False
    except Exception as e:
        error_logger.error(f"[METRICS] Failed to collect metrics for {server.name}: {e}")
        import traceback
        error_logger.error(traceback.format_exc())
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
@staff_member_required
def alert_config(request):
    """Email alert configuration page"""
    if request.method == "GET":
        config = EmailAlertConfig.objects.first()
        context = {
            'config': config,
            'show_sidebar': True,  # Match add_server page layout with sidebar
        }
        return render(request, "core/alert_config.html", context)
    else:
        messages.error(request, "Method not allowed")
        return redirect('alert_config')


import logging
logger = logging.getLogger(__name__)

def _smtp_login_if_supported(server, username, password):
    """
    Attempt SMTP login only if AUTH is supported by the server.
    Returns True if login succeeded, False if AUTH not supported, raises exception on other errors.
    """
    if not username or not password:
        return False  # No credentials provided, skip authentication
    
    # Remove spaces from password (Gmail App Passwords have no spaces)
    password_clean = password.strip().replace(' ', '')
    
    try:
        # Check if server supports AUTH extension
        if hasattr(server, 'ehlo_resp') and server.ehlo_resp:
            # Check if AUTH is in the EHLO response
            if 'AUTH' in str(server.ehlo_resp):
                server.login(username, password_clean)
                return True
        # Try to use has_extn if available
        elif hasattr(server, 'has_extn') and server.has_extn('AUTH'):
            server.login(username, password_clean)
            return True
        else:
            # Try login anyway - some servers support AUTH but don't advertise it properly
            try:
                server.login(username, password_clean)
                return True
            except smtplib.SMTPNotSupportedError:
                # AUTH not supported, skip authentication (common for port 25)
                return False
    except smtplib.SMTPNotSupportedError:
        # AUTH extension not supported by server (e.g., port 25)
        return False
    except smtplib.SMTPAuthenticationError:
        # Authentication failed - re-raise to show proper error
        raise
    except Exception:
        # Other errors - re-raise
        raise

@staff_member_required
@require_http_methods(["POST"])
def save_alert_config(request):
    """Save email alert configuration"""
    try:
        provider = request.POST.get('provider', 'custom')
        smtp_host = request.POST.get('smtp_host', '').strip()
        smtp_port = request.POST.get('smtp_port', '').strip()
        use_tls = request.POST.get('use_tls') == 'on'
        use_ssl = request.POST.get('use_ssl') == 'on'
        username = request.POST.get('username', '').strip()
        # Strip and remove spaces from password (Gmail App Passwords have no spaces)
        password = request.POST.get('password', '').strip().replace(' ', '') if request.POST.get('password') else ''
        from_email = request.POST.get('from_email', '').strip()
        to_email = request.POST.get('to_email', '').strip()

        # Check if this is an update (config exists) or new config
        existing_config = EmailAlertConfig.objects.first()

        # Validate required fields based on provider
        if provider == 'custom':
            if not smtp_host or not smtp_port or not username or not from_email:
                try:
                    messages.error(request, 'SMTP host, port, username, and from email are required for custom SMTP configuration')
                except:
                    pass  # Messages middleware not available in test environment
                return redirect('alert_config')
            # Password is required for new custom configs, optional for updates
            if not existing_config and not password:
                try:
                    messages.error(request, 'Password is required for new custom SMTP configuration')
                except:
                    pass  # Messages middleware not available in test environment
                return redirect('alert_config')
        else:
            if not username or not from_email:
                try:
                    messages.error(request, 'Username and from email are required')
                except:
                    pass  # Messages middleware not available in test environment
                return redirect('alert_config')
            # Password is required for new configs, optional for updates
            if not existing_config and not password:
                try:
                    messages.error(request, 'Password is required for new email configuration')
                except:
                    pass  # Messages middleware not available in test environment
                return redirect('alert_config')

        # Get or create config (only one config allowed)
        config, created = EmailAlertConfig.objects.get_or_create(
            id=1,  # Single instance
            defaults={
                'provider': provider,
                'smtp_host': smtp_host,
                'smtp_port': int(smtp_port) if smtp_port else 587,
                'use_tls': use_tls,
                'use_ssl': use_ssl,
                'username': username,
                'password': password,  # In production, encrypt this
                'from_email': from_email,
                'to_email': to_email,
                'enabled': True
            }
        )

        if not created:
            # Update existing config
            config.provider = provider
            config.smtp_host = smtp_host
            config.smtp_port = int(smtp_port) if smtp_port else 587
            config.use_tls = use_tls
            config.use_ssl = use_ssl
            config.username = username
            if password:  # Only update password if provided
                config.password = password
            config.from_email = from_email
            config.to_email = to_email
            config.enabled = True
            config.save()

        try:
            messages.success(request, 'Email alert configuration saved successfully!')
        except:
            pass  # Messages middleware not available in test environment
        return redirect('alert_config')

    except Exception as e:
        logger.error(f"Failed to save alert config: {str(e)}")
        try:
            messages.error(request, f'Failed to save configuration: {str(e)}')
        except:
            pass  # Messages middleware not available in test environment
        return redirect('alert_config')


@staff_member_required
@require_http_methods(["POST"])
def clear_alert_config(request):
    """Clear/delete email alert configuration"""
    try:
        config = EmailAlertConfig.objects.filter(id=1).first()
        if config:
            config.delete()
            try:
                messages.success(request, 'Email alert configuration cleared successfully!')
            except:
                pass  # Messages middleware not available in test environment
        else:
            try:
                messages.info(request, 'No email alert configuration found to clear.')
            except:
                pass  # Messages middleware not available in test environment
        return redirect('alert_config')

    except Exception as e:
        logger.error(f"Failed to clear alert config: {str(e)}")
        try:
            messages.error(request, f'Failed to clear configuration: {str(e)}')
        except:
            pass  # Messages middleware not available in test environment
        return redirect('alert_config')


@staff_member_required
@require_http_methods(["POST"])
def test_alert_config(request):
    """Test email alert configuration - uses saved config values from database"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    
    try:
        # Get saved configuration from database
        saved_config = EmailAlertConfig.objects.first()
        
        if not saved_config:
            messages.error(request, 'No saved configuration found. Please save your email configuration first.')
            return redirect('alert_config')
        
        # Use saved configuration values
        smtp_host = saved_config.smtp_host or ''
        smtp_port = str(saved_config.smtp_port) if saved_config.smtp_port else ''
        use_tls = saved_config.use_tls
        use_ssl = saved_config.use_ssl
        username = saved_config.username or ''
        # Strip and remove all spaces from password (Gmail App Passwords are 16 chars, no spaces)
        password = (saved_config.password or '').strip().replace(' ', '')
        from_email = saved_config.from_email or ''
        test_recipient = saved_config.to_email or ''

        # Validate required fields
        if not all([smtp_host, smtp_port, username, from_email]):
            messages.error(request, 'SMTP host, port, username, and from email are required in saved configuration.')
            return redirect('alert_config')
        
        if not password:
            messages.error(request, 'Password is required for testing. Please save your configuration with a password first.')
            return redirect('alert_config')
        
        if not test_recipient:
            messages.error(request, 'To email address is required for testing. Please save configuration with a valid "To Email" address first.')
            return redirect('alert_config')

        # Create test email
        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = test_recipient
        msg['Subject'] = 'Test Alert - Server Monitoring System'
        body = 'This is a test email from the Server Monitoring System. If you receive this, your email configuration is working correctly.'
        msg.attach(MIMEText(body, 'plain'))
        
        # Connect and send
        server = None
        try:
            if use_ssl:
                # SSL connection (port 465)
                server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=30)
                server.set_debuglevel(0)
                server.ehlo()  # Identify ourselves
                # Attempt login only if AUTH is supported
                _smtp_login_if_supported(server, username, password)
            elif use_tls:
                # TLS connection (port 587)
                server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=30)
                server.set_debuglevel(0)
                server.ehlo()  # Identify ourselves to the SMTP server
                server.starttls()  # Secure the connection
                server.ehlo()  # Re-identify ourselves after TLS
                # Attempt login only if AUTH is supported
                _smtp_login_if_supported(server, username, password)
            else:
                # Plain connection (port 25 or other unencrypted ports)
                server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=30)
                server.set_debuglevel(0)
                server.ehlo()  # Identify ourselves
                # Attempt login only if AUTH is supported (many port 25 servers don't support AUTH)
                _smtp_login_if_supported(server, username, password)
            
            server.send_message(msg)
            server.quit()
            server = None  # Mark as closed
            
            messages.success(request, f'Test email sent successfully to {test_recipient}!')
            
        except smtplib.SMTPNotSupportedError as e:
            if server:
                try:
                    server.quit()
                except:
                    pass
            error_str = str(e)
            error_msg = f'SMTP AUTH extension not supported by server: {error_str}. This is normal for port 25 servers. Email will be sent without authentication.'
            messages.warning(request, error_msg)
            # Continue to send email even without authentication
            try:
                server.send_message(msg)
                server.quit()
                messages.success(request, f'Test email sent successfully to {test_recipient} (without authentication, as AUTH is not supported by this server)!')
            except Exception as send_error:
                messages.error(request, f'Failed to send email: {str(send_error)}')
            return redirect('alert_config')
        except smtplib.SMTPAuthenticationError as e:
            if server:
                try:
                    server.quit()
                except:
                    pass
            error_str = str(e)
            # Check for rate limiting
            if 'Too many login attempts' in error_str or '4.7.0' in error_str:
                error_msg = f'Gmail has temporarily blocked login attempts due to too many failed attempts. Please wait 10-30 minutes before trying again. Error: {error_str}'
            else:
                error_msg = f'Authentication failed: {error_str}. Please check your username and password (use App Password for Gmail/Outlook).'
            messages.error(request, error_msg)
        except smtplib.SMTPServerDisconnected as e:
            if server:
                try:
                    server.quit()
                except:
                    pass
            error_str = str(e)
            # This often happens after a rate limit error or wrong credentials
            error_msg = f'Connection was closed by the server: {error_str}. This may be due to too many login attempts (wait 10-30 minutes) or incorrect App Password. Please verify your App Password at https://myaccount.google.com/apppasswords.'
            messages.error(request, error_msg)
        except smtplib.SMTPException as e:
            if server:
                try:
                    server.quit()
                except:
                    pass
            error_str = str(e)
            # Provide helpful guidance for common errors
            if 'Connection unexpectedly closed' in error_str or 'Connection closed' in error_str:
                error_msg = f'SMTP error: {error_str}. This often occurs with Gmail when not using an App Password. Please generate an App Password at https://myaccount.google.com/apppasswords (requires 2-Step Verification) and use it instead of your regular password.'
            else:
                error_msg = f'SMTP error: {error_str}'
            messages.error(request, error_msg)
        except (ConnectionError, OSError) as e:
            if server:
                try:
                    server.quit()
                except:
                    pass
            messages.error(request, f'Connection error: {str(e)}. Please check your SMTP settings and network connection.')
        except Exception as e:
            if server:
                try:
                    server.quit()
                except:
                    pass
            import traceback
            error_msg = str(e)
            logger.error(f"Test email error: {error_msg}\n{traceback.format_exc()}")
            messages.error(request, f'Error sending test email: {error_msg}')

        return redirect('alert_config')

    except Exception as e:
        logger.error(f"Failed to test alert config: {str(e)}")
        messages.error(request, f'Failed to send test email: {str(e)}')
        return redirect('alert_config')


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
        required_fields = ['smtp_host', 'smtp_port', 'smtp_username', 'smtp_password', 'from_email', 'to_email']
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
        recipients = [r.strip() for r in data.get('to_email', '').split(',')] if data.get('to_email') else []
        
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
        
        # Check if monitoring is suspended - if so, skip all alerts
        if config.monitoring_suspended:
            print(f"[ALERT] Monitoring suspended for {server.name}, skipping alert checks")
            return
        
        # Check if alerts are suppressed for this server
        if config.alert_suppressed:
            print(f"[ALERT] Alerts suppressed for {server.name}, skipping alert checks")
            return
        
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
            'Disk': {},
            'DiskIO': False,
            'NetworkIO': False
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
        
        # Check Disk I/O thresholds
        if hasattr(config, 'disk_io_threshold') and config.disk_io_threshold:
            disk_io_read_mb = 0
            disk_io_write_mb = 0
            if metric.disk_io_read:
                disk_io_read_mb = float(metric.disk_io_read) / (1024 * 1024)  # Convert bytes/sec to MB/s
            if metric.disk_io_write:
                disk_io_write_mb = float(metric.disk_io_write) / (1024 * 1024)  # Convert bytes/sec to MB/s
            
            threshold_mb = float(config.disk_io_threshold)  # Threshold in MB/s
            disk_io_read_above = disk_io_read_mb >= threshold_mb
            disk_io_write_above = disk_io_write_mb >= threshold_mb
            disk_io_above = disk_io_read_above or disk_io_write_above
            current_state['DiskIO'] = disk_io_above
            
            if disk_io_above:
                io_details = []
                if disk_io_read_above:
                    io_details.append(f"read: {disk_io_read_mb:.2f} MB/s")
                if disk_io_write_above:
                    io_details.append(f"write: {disk_io_write_mb:.2f} MB/s")
                
                alerts.append({
                    'type': 'DiskIO',
                    'value': max(disk_io_read_mb, disk_io_write_mb),
                    'threshold': threshold_mb,
                    'message': f"Disk I/O exceeded threshold: {', '.join(io_details)} (threshold: {threshold_mb} MB/s)"
                })
                print(f"[ALERT] Disk I/O threshold exceeded: {', '.join(io_details)} >= {threshold_mb} MB/s")
            elif previous_state.get('DiskIO', False):
                resolved_alerts.append({
                    'type': 'DiskIO',
                    'value': max(disk_io_read_mb, disk_io_write_mb),
                    'threshold': threshold_mb,
                    'message': f"Disk I/O returned to normal: read {disk_io_read_mb:.2f} MB/s, write {disk_io_write_mb:.2f} MB/s (threshold: {threshold_mb} MB/s)"
                })
                print(f"[ALERT] Disk I/O threshold resolved: < {threshold_mb} MB/s")
        
        # Check Network I/O thresholds
        if hasattr(config, 'network_io_threshold') and config.network_io_threshold:
            net_io_sent_mb = 0
            net_io_recv_mb = 0
            if metric.net_io_sent:
                net_io_sent_mb = float(metric.net_io_sent) / (1024 * 1024)  # Convert bytes/sec to MB/s
            if metric.net_io_recv:
                net_io_recv_mb = float(metric.net_io_recv) / (1024 * 1024)  # Convert bytes/sec to MB/s
            
            threshold_mb = float(config.network_io_threshold)  # Threshold in MB/s
            net_io_sent_above = net_io_sent_mb >= threshold_mb
            net_io_recv_above = net_io_recv_mb >= threshold_mb
            net_io_above = net_io_sent_above or net_io_recv_above
            current_state['NetworkIO'] = net_io_above
            
            if net_io_above:
                io_details = []
                if net_io_sent_above:
                    io_details.append(f"sent: {net_io_sent_mb:.2f} MB/s")
                if net_io_recv_above:
                    io_details.append(f"received: {net_io_recv_mb:.2f} MB/s")
                
                alerts.append({
                    'type': 'NetworkIO',
                    'value': max(net_io_sent_mb, net_io_recv_mb),
                    'threshold': threshold_mb,
                    'message': f"Network I/O exceeded threshold: {', '.join(io_details)} (threshold: {threshold_mb} MB/s)"
                })
                print(f"[ALERT] Network I/O threshold exceeded: {', '.join(io_details)} >= {threshold_mb} MB/s")
            elif previous_state.get('NetworkIO', False):
                resolved_alerts.append({
                    'type': 'NetworkIO',
                    'value': max(net_io_sent_mb, net_io_recv_mb),
                    'threshold': threshold_mb,
                    'message': f"Network I/O returned to normal: sent {net_io_sent_mb:.2f} MB/s, received {net_io_recv_mb:.2f} MB/s (threshold: {threshold_mb} MB/s)"
                })
                print(f"[ALERT] Network I/O threshold resolved: < {threshold_mb} MB/s")
        
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
                    recipients=email_config.to_email or ''
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
                    recipients=email_config.to_email or '',
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
        recipients = [email.strip() for email in email_config.to_email.split(',')] if email_config.to_email else []
        
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
            server_smtp.ehlo()
            server_smtp.starttls()
            server_smtp.ehlo()
            # Attempt login only if AUTH is supported
            _smtp_login_if_supported(server_smtp, email_config.username, email_config.password)
        else:
            # SSL (port 465) or plain (port 25)
            if email_config.use_ssl:
                print(f"[ALERT] Using SSL on {email_config.smtp_host}:{email_config.smtp_port}")
                server_smtp = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
            else:
                print(f"[ALERT] Using plain SMTP on {email_config.smtp_host}:{email_config.smtp_port}")
                server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
            server_smtp.ehlo()
            # Attempt login only if AUTH is supported
            _smtp_login_if_supported(server_smtp, email_config.username, email_config.password)
        
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


def _send_connection_alert(server, state):
    """Send alert when server connection state changes (online/offline)"""
    try:
        # Refresh server to get latest monitoring_config
        server.refresh_from_db()
        config = server.monitoring_config
        
        # CRITICAL: Don't send alerts if monitoring is disabled or suspended
        if not config.enabled:
            print(f"[CONNECTION_ALERT] Monitoring disabled for {server.name}, skipping connection alert")
            return
        
        if config.monitoring_suspended:
            print(f"[CONNECTION_ALERT] Monitoring suspended for {server.name}, skipping connection alert")
            return
        
        # Check if alerts are suppressed
        if config.alert_suppressed:
            print(f"[CONNECTION_ALERT] Alerts suppressed for {server.name}, skipping connection alert")
            return
        
        email_config = EmailAlertConfig.objects.filter(enabled=True).first()
        
        if not email_config:
            print(f"[CONNECTION_ALERT] No email config found for {server.name}")
            return
        
        recipients = [email.strip() for email in email_config.to_email.split(',')] if email_config.to_email else []
        
        if state == "offline":
            subject = f"🔴 Server Offline: {server.name}"
            body = f"""
Server Connection Alert

⚠️ ALERT: Server is now OFFLINE

Server: {server.name}
IP Address: {server.ip_address}
Status: OFFLINE
Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}

The server is not responding to metric collection requests.
This may indicate:
- Network connectivity issues
- Server is down or unreachable
- SSH/exporter connection failure

Please investigate immediately.
            """
        else:  # online
            subject = f"✅ Server Online: {server.name}"
            body = f"""
Server Connection Alert - RESOLVED

✅ RESOLVED: Server is now ONLINE

Server: {server.name}
IP Address: {server.ip_address}
Status: ONLINE
Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}

The server connection has been restored and is responding normally.
            """
        
        print(f"[CONNECTION_ALERT] Attempting to send {state} alert email to {recipients}")
        
        # Send email
        try:
            # Remove spaces from password (Gmail App Passwords have no spaces)
            password_clean = (email_config.password or '').strip().replace(' ', '')
            
            if email_config.use_tls:
                server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
                server_smtp.ehlo()
                server_smtp.starttls()
                server_smtp.ehlo()
                # Attempt login only if AUTH is supported
                _smtp_login_if_supported(server_smtp, email_config.username, email_config.password)
            else:
                if email_config.use_ssl:
                    server_smtp = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
                else:
                    server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
                server_smtp.ehlo()
                # Attempt login only if AUTH is supported
                _smtp_login_if_supported(server_smtp, email_config.username, email_config.password)
            
            msg = MIMEMultipart()
            msg['From'] = email_config.from_email
            msg['To'] = ', '.join(recipients)
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))
            
            server_smtp.send_message(msg)
            server_smtp.quit()
            
            print(f"[CONNECTION_ALERT] ✓ {state.upper()} alert email sent successfully for {server.name}")
            
            # Log to alert history
            AlertHistory.objects.create(
                server=server,
                alert_type="CONNECTION",
                status="triggered" if state == "offline" else "resolved",
                value=0.0,
                threshold=30.0,
                message=f"Server is {state.upper()}" if state == "offline" else f"Server connection restored",
                recipients=', '.join(recipients)
            )
            
        except Exception as e:
            error_msg = f"[CONNECTION_ALERT] ✗ Failed to send {state} alert for {server.name}: {e}"
            print(error_msg)
            error_logger.error(error_msg)
            
    except Exception as e:
        error_logger.error(f"CONNECTION_ALERT error for {server.name}: {str(e)}")


def _check_service_status(server, service):
    """
    Check if a monitored service is running and track consecutive failures.
    
    IMPORTANT: This function checks the service on the specific server only.
    Services are server-specific - each server has its own Service records.
    """
    try:
        # SSH connection to check service status
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        ssh_key_path = os.path.join(settings.BASE_DIR, 'ssh_keys', 'id_rsa')
        private_key = paramiko.RSAKey.from_private_key_file(ssh_key_path)
        
        ssh.connect(
            hostname=server.ip_address,
            port=server.port,
            username=server.username,
            pkey=private_key,
            timeout=10
        )
        
        # Check service status using systemctl
        command = f"systemctl is-active {service.name} 2>/dev/null"
        stdin, stdout, stderr = ssh.exec_command(command)
        output = stdout.read().decode('utf-8').strip()
        errors = stderr.read().decode('utf-8').strip()
        
        ssh.close()
        
        # Service is active if output is "active"
        is_active = output == "active"
        is_failed = False  # Initialize variable
        
        # Cache key for tracking consecutive failures
        failure_count_key = f"service_failures:{server.id}:{service.id}"
        last_status_key = f"service_last_status:{server.id}:{service.id}"
        alert_sent_key = f"service_alert_sent:{server.id}:{service.id}"
        
        if is_active:
            # Service is running - reset failure count
            cache.delete(failure_count_key)
            last_status = cache.get(last_status_key)
            cache.set(last_status_key, "active", 300)  # 5 min TTL
            
            # Check if we need to send a resolved alert
            if last_status == "inactive" or last_status == "failed":
                # Service came back online - send resolved alert
                _send_service_alert(server, service, "resolved")
                cache.delete(alert_sent_key)
        else:
            # Service is down - check if it's in failed state immediately
            # First, check if service is in failed state
            try:
                ssh_failed = paramiko.SSHClient()
                ssh_failed.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh_key_path = os.path.join(settings.BASE_DIR, 'ssh_keys', 'id_rsa')
                private_key = paramiko.RSAKey.from_private_key_file(ssh_key_path)
                ssh_failed.connect(
                    hostname=server.ip_address,
                    port=server.port,
                    username=server.username,
                    pkey=private_key,
                    timeout=10
                )
                # Check if service is failed
                command_failed = f"systemctl is-failed {service.name} 2>/dev/null"
                stdin_failed, stdout_failed, stderr_failed = ssh_failed.exec_command(command_failed)
                output_failed = stdout_failed.read().decode('utf-8').strip()
                ssh_failed.close()
                
                is_failed = output_failed == "failed"
            except:
                is_failed = False
            
            # Increment failure count
            failure_count = cache.get(failure_count_key, 0)
            failure_count += 1
            cache.set(failure_count_key, failure_count, 300)  # 5 min TTL
            cache.set(last_status_key, "failed" if is_failed else "inactive", 300)
            
            # Send alert after 2 consecutive failures (60 seconds: 2 checks * 30 seconds)
            should_alert = False
            if is_failed:
                # Service is in failed state - send alert immediately
                should_alert = True
                print(f"[SERVICE_ALERT] Service {service.name} on {server.name} is in FAILED state")
            elif failure_count >= 2:
                # Service has been down for 2 consecutive checks (60 seconds)
                should_alert = True
                print(f"[SERVICE_ALERT] Service {service.name} on {server.name} is down (2 consecutive failures = 60 seconds)")
            
            if should_alert:
                # Check if we already sent an alert for this failure
                alert_sent = cache.get(alert_sent_key, False)
                if not alert_sent:
                    # Send alert
                    _send_service_alert(server, service, "triggered")
                    cache.set(alert_sent_key, True, 3600)  # Prevent duplicate alerts for 1 hour
        
        # Update service status in database
        # Use the is_failed status we already checked above
        if is_active:
            service.status = "running"
        else:
            # Use the is_failed status from the check above
            if is_failed:
                service.status = "failed"
            else:
                service.status = "stopped"
        
        service.last_checked = timezone.now()
        service.save()
        
        return is_active
        
    except Exception as e:
        error_logger.error(f"SERVICE_CHECK error for {service.name} on {server.name}: {str(e)}")
        return None


def _send_service_alert(server, service, status):
    """Send alert when service status changes (down/up)"""
    try:
        # Refresh server to get latest monitoring_config
        server.refresh_from_db()
        config = server.monitoring_config
        
        # Don't send alerts if monitoring is disabled or suspended
        if not config.enabled or config.monitoring_suspended:
            return
        
        # Get email config
        email_config = EmailAlertConfig.objects.filter(enabled=True).first()
        if not email_config:
            print(f"[SERVICE_ALERT] No email config found, skipping alert for {service.name} on {server.name}")
            return
        
        # Use to_email as recipient
        if not email_config.to_email:
            print(f"[SERVICE_ALERT] No to_email configured, skipping alert for {service.name} on {server.name}")
            return
        recipients = [email.strip() for email in email_config.to_email.split(',')] if email_config.to_email else []
        
        if status == "triggered":
            subject = f"🚨 Service Alert: {service.name} is DOWN on {server.name}"
            body = f"""
Service Monitoring Alert

Service: {service.name}
Server: {server.name} ({server.ip_address})
Status: DOWN
Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}

The service has been down for 60 seconds (2 consecutive checks at 30-second intervals).

Please investigate and restore the service.
"""
        else:  # resolved
            subject = f"✅ Service Resolved: {service.name} is UP on {server.name}"
            body = f"""
Service Monitoring Alert - Resolved

Service: {service.name}
Server: {server.name} ({server.ip_address})
Status: UP
Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}

The service has been restored and is now running.
"""
        
        # Create email
        msg = MIMEMultipart()
        msg['From'] = email_config.from_email
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        # Send email
        try:
            if email_config.use_tls:
                server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
                server_smtp.ehlo()
                server_smtp.starttls()
                server_smtp.ehlo()
            else:
                if email_config.use_ssl:
                    server_smtp = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
                else:
                    server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
                server_smtp.ehlo()
            
            # Attempt login only if AUTH is supported
            _smtp_login_if_supported(server_smtp, email_config.username, email_config.password)
            server_smtp.send_message(msg)
            server_smtp.quit()
            
            print(f"[SERVICE_ALERT] ✓ Sent {status} alert for {service.name} on {server.name} to {recipients}")
            
            # Log to AlertHistory
            AlertHistory.objects.create(
                server=server,
                alert_type="SERVICE",
                status=status,
                value=0.0,
                threshold=2.0,
                message=f"Service {service.name} is {'DOWN' if status == 'triggered' else 'UP'}",
                recipients=email_config.to_email or ''
            )
            
        except Exception as e:
            error_msg = f"[SERVICE_ALERT] ✗ Failed to send {status} alert for {service.name} on {server.name}: {e}"
            print(error_msg)
            error_logger.error(error_msg)
            
    except Exception as e:
        error_logger.error(f"SERVICE_ALERT error for {service.name} on {server.name}: {str(e)}")
        return


def _send_alert_email(email_config, server, alerts):
    """Send alert email using configured SMTP settings"""
    try:
        recipients = [email.strip() for email in email_config.to_email.split(',')] if email_config.to_email else []
        
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
            server_smtp.ehlo()
            server_smtp.starttls()
            server_smtp.ehlo()
            # Attempt login only if AUTH is supported
            _smtp_login_if_supported(server_smtp, email_config.username, email_config.password)
        else:
            # SSL (port 465) or plain (port 25)
            if email_config.use_ssl:
                print(f"[ALERT] Using SSL on {email_config.smtp_host}:{email_config.smtp_port}")
                server_smtp = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
            else:
                print(f"[ALERT] Using plain SMTP on {email_config.smtp_host}:{email_config.smtp_port}")
                server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
            server_smtp.ehlo()
            # Attempt login only if AUTH is supported
            _smtp_login_if_supported(server_smtp, email_config.username, email_config.password)
        
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
        import json
        server = Server.objects.get(id=server_id)
        config = server.monitoring_config
        
        # Handle JSON POST data
        if request.content_type == 'application/json':
            data = json.loads(request.body)
        else:
            data = request.POST
        
        if 'cpu_threshold' in data:
            config.cpu_threshold = float(data['cpu_threshold'])
        if 'memory_threshold' in data:
            config.memory_threshold = float(data['memory_threshold'])
        if 'disk_threshold' in data:
            config.disk_threshold = float(data['disk_threshold'])
        if 'disk_io_threshold' in data:
            config.disk_io_threshold = float(data['disk_io_threshold'])
        if 'network_io_threshold' in data:
            config.network_io_threshold = float(data['network_io_threshold'])
        
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
@require_http_methods(["POST"])
def update_monitored_disks(request, server_id):
    """Update monitored disk partitions for a server"""
    try:
        import json
        server = Server.objects.get(id=server_id)
        config = server.monitoring_config
        
        data = json.loads(request.body)
        disks = data.get('disks', [])
        
        # Ensure / is always included
        if '/' not in disks:
            disks.insert(0, '/')
        
        config.monitored_disks = disks
        config.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Monitored disks updated successfully',
            'disks': config.monitored_disks
        })
    except Server.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Server not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@staff_member_required
def admin_users(request):
    from .models import UserACL
    from .utils import has_privilege

    # RBAC check: Only admin users (role.is_admin == True) can access this page
    if not request.user.is_superuser:
        messages.error(request, "You don't have permission to manage users.")
        return redirect('monitoring_dashboard')

    # Query all Django staff users and ensure they have UserACL entries
    django_users = User.objects.filter(is_staff=True).order_by("username")

    # Ensure all users have ACL entries and collect them
    users = []
    for django_user in django_users:
        # This will create ACL if it doesn't exist
        acl = UserACL.get_or_create_for_user(django_user)
        users.append(acl)

    user_count = len(users)

    return render(request, "core/admin_users.html", {
        "users": users,
        "user_count": user_count,
        "show_sidebar": True,
    })

@staff_member_required
def create_admin_user(request):
    from .models import UserACL, Role
    from .utils import has_privilege

    # Check permissions - only users with manage_users privilege can create users
    if not has_privilege(request.user, 'manage_users'):
        messages.error(request, "You do not have permission to create users.")
        return redirect("admin_users")

    if request.method == "POST":
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")
        is_superuser = request.POST.get("is_superuser") == "on"
        role_id = request.POST.get("role")

        if not username or not password:
            messages.error(request, "Username and password are required.")
            return redirect("create_admin_user")

        if not is_superuser and not role_id:
            messages.error(request, "Please select a role for non-superuser accounts.")
            return redirect("create_admin_user")

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists.")
            return redirect("create_admin_user")

        try:
            user = User.objects.create_user(username=username, email=email, password=password, is_staff=True, is_superuser=is_superuser)

            # Assign role for non-superuser staff
            if not is_superuser:
                try:
                    role = Role.objects.get(id=role_id)
                    acl = UserACL.get_or_create_for_user(user)
                    acl.role = role
                    acl.save()
                except Role.DoesNotExist:
                    messages.error(request, "Selected role does not exist.")
                    user.delete()  # Clean up the created user
                    return redirect("create_admin_user")

            messages.success(request, f"User {username} created successfully.")
            return redirect("admin_users")
        except Exception as e:
            messages.error(request, f"Error creating user: {str(e)}")
            return redirect("create_admin_user")

    # GET request - render the form with available roles
    available_roles = Role.objects.all().order_by('name')
    return render(request, "core/create_admin_user.html", {
        'available_roles': available_roles,
        'show_sidebar': True
    })

@staff_member_required
def edit_admin_user(request, user_id):
    from .models import UserACL, Role
    from .utils import has_privilege

    # Check permissions - only users with manage_users privilege can edit users
    if not has_privilege(request.user, 'manage_users'):
        messages.error(request, "You do not have permission to edit users.")
        return redirect("admin_users")

    user = get_object_or_404(User, id=user_id, is_staff=True)

    # Prevent non-admin users from editing admin accounts
    if user.username == "admin" and request.user.username != "admin":
        messages.error(request, "Only the admin user can edit the admin account.")
        return redirect("admin_users")

    acl = UserACL.get_or_create_for_user(user)

    if request.method == "POST":
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")
        role_id = request.POST.get("role")
        is_superuser = request.POST.get("is_superuser") == "on"
        is_active = request.POST.get("is_active") == "on"

        # Prevent changing admin username
        if user.username == "admin" and username != "admin":
            messages.error(request, "Admin username cannot be changed.")
            return redirect("edit_admin_user", user_id=user_id)

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

        # Update role for non-superuser accounts
        if not is_superuser and role_id:
            try:
                role = Role.objects.get(id=role_id)
                acl.role = role
                acl.save()
            except Role.DoesNotExist:
                messages.warning(request, "Selected role does not exist.")
        elif is_superuser:
            # Clear role for superuser accounts
            acl.role = None
            acl.save()

        messages.success(request, f"User {username} updated successfully.")
        return redirect("admin_users")

    roles = Role.objects.all().order_by("name")

    return render(request, "core/edit_admin_user.html", {
        "user_obj": user,
        "user_id": user_id,
        "acl": acl,
        "roles": roles,
        "show_sidebar": True,
        "page_title": "Edit User"
    })

@staff_member_required
def delete_admin_user(request, user_id):
    from .utils import has_privilege

    # Check permissions - only users with manage_users privilege can delete users
    if not has_privilege(request.user, 'manage_users'):
        messages.error(request, "You do not have permission to delete users.")
        return redirect("admin_users")

    user = get_object_or_404(User, id=user_id, is_staff=True)

    # Prevent deleting the admin user
    if user.username == "admin":
        messages.error(request, "Admin account cannot be deleted.")
        return redirect("admin_users")

    if user == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("admin_users")

    username = user.username
    user.delete()
    messages.success(request, f"User {username} deleted successfully.")
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
        _log_user_action(request, "CREATE_ADMIN_USER", f"Created user: {username} (superuser: {is_superuser})")
        return JsonResponse({"success": True, "message": f"User {username} created successfully."})
    except Exception as e:
        _log_user_action(request, "CREATE_ADMIN_USER", f"Failed: {str(e)}")
        error_logger.error(f"CREATE_ADMIN_USER error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
def alert_history(request):
    """View page to display and manage alert history (including anomalies)"""
    # Get query parameters for filtering
    server_id = request.GET.get('server_id', '').strip()
    alert_type = request.GET.get('alert_type', '').strip()
    status = request.GET.get('status', '').strip()  # Default to empty (all alerts)
    
    # Build AlertHistory query
    alert_history_query = AlertHistory.objects.all().select_related('server')
    
    # Build Anomaly query
    anomaly_query = Anomaly.objects.all().select_related('server', 'metric')
    
    if server_id:
        try:
            server_id_int = int(server_id)
            alert_history_query = alert_history_query.filter(server_id=server_id_int)
            anomaly_query = anomaly_query.filter(server_id=server_id_int)
        except (ValueError, TypeError):
            pass  # Invalid server_id, ignore filter
    
    if alert_type:
        # Validate alert_type - can be from AlertHistory or Anomaly metric_type
        valid_alert_types = [choice[0] for choice in AlertHistory.AlertType.choices]
        valid_anomaly_types = ['cpu', 'memory', 'disk', 'network']
        
        if alert_type in valid_alert_types:
            alert_history_query = alert_history_query.filter(alert_type=alert_type)
        if alert_type.upper() in [t.upper() for t in valid_anomaly_types]:
            # Map alert type to anomaly metric_type (CPU -> cpu, MEMORY -> memory, etc.)
            type_map = {'CPU': 'cpu', 'MEMORY': 'memory', 'DISK': 'disk', 'NETWORK': 'network'}
            anomaly_type = type_map.get(alert_type.upper(), alert_type.lower())
            anomaly_query = anomaly_query.filter(metric_type=anomaly_type)
    
    if status:
        # Validate status is in choices
        valid_statuses = [choice[0] for choice in AlertHistory.AlertStatus.choices]
        if status in valid_statuses:
            if status == 'triggered':
                alert_history_query = alert_history_query.filter(status='triggered')
                anomaly_query = anomaly_query.filter(resolved=False)
            elif status == 'resolved':
                alert_history_query = alert_history_query.filter(status='resolved')
                anomaly_query = anomaly_query.filter(resolved=True)
    
    # Get alerts ordered by most recent first
    alerts_list = list(alert_history_query.order_by('-sent_at')[:500])
    
    # Get anomalies ordered by most recent first
    anomalies_list = list(anomaly_query.order_by('-timestamp')[:500])
    
    # Combine alerts and anomalies into unified list with type indicators
    # Use a list of dicts with 'type' field to distinguish
    unified_items = []
    
    # Add alerts with type='alert'
    for alert in alerts_list:
        unified_items.append({
            'type': 'alert',
            'object': alert,
            'timestamp': alert.sent_at,
        })
    
    # Add anomalies with type='anomaly'
    for anomaly in anomalies_list:
        unified_items.append({
            'type': 'anomaly',
            'object': anomaly,
            'timestamp': anomaly.timestamp,
        })
    
    # Sort unified list by timestamp (most recent first)
    unified_items.sort(key=lambda x: x['timestamp'], reverse=True)
    
    # Limit to 500 most recent items
    unified_items = unified_items[:500]
    
    # Get filter options
    servers = Server.objects.all().order_by('name')
    alert_types = AlertHistory.AlertType.choices
    alert_statuses = AlertHistory.AlertStatus.choices
    
    # Counts for summary cards (all alerts and anomalies, not filtered)
    total_alerts = AlertHistory.objects.count()
    triggered_alerts = AlertHistory.objects.filter(status='triggered').count()
    resolved_alerts = AlertHistory.objects.filter(status='resolved').count()
    
    total_anomalies = Anomaly.objects.count()
    unresolved_anomalies = Anomaly.objects.filter(resolved=False).count()
    resolved_anomalies = Anomaly.objects.filter(resolved=True).count()
    
    # Combined counts
    total_items = total_alerts + total_anomalies
    active_items = triggered_alerts + unresolved_anomalies
    resolved_items = resolved_alerts + resolved_anomalies
    
    # Filtered count
    filtered_count = len(unified_items)
    
    context = {
        'unified_items': unified_items,  # Combined list with type indicators
        'alerts': alerts_list,  # Keep for backward compatibility if needed
        'anomalies': anomalies_list,  # Keep for reference
        'servers': servers,
        'alert_types': alert_types,
        'alert_statuses': alert_statuses,
        'selected_server_id': server_id,
        'selected_alert_type': alert_type,
        'selected_status': status,
        'total_alerts': total_items,  # Combined total
        'triggered_count': active_items,  # Combined active
        'resolved_count': resolved_items,  # Combined resolved
        'filtered_count': filtered_count,
        'total_anomalies': total_anomalies,
        'unresolved_anomalies': unresolved_anomalies,
        'show_sidebar': True,
    }
    
    return render(request, 'core/alert_history.html', context)


@staff_member_required
@require_http_methods(["GET"])
def alert_history_api(request):
    """API endpoint to fetch alert history"""
    try:
        # Get query parameters
        limit = int(request.GET.get('limit', 100))
        server_id = request.GET.get('server_id')
        alert_type = request.GET.get('alert_type')
        status = request.GET.get('status')
        
        # Build query
        query = AlertHistory.objects.all()
        
        if server_id:
            query = query.filter(server_id=server_id)
        if alert_type:
            query = query.filter(alert_type=alert_type)
        if status:
            query = query.filter(status=status)
        
        # Get alerts ordered by most recent first
        alerts = query.order_by('-sent_at')[:limit]
        
        # Serialize alerts
        alerts_data = []
        for alert in alerts:
            alerts_data.append({
                'id': alert.id,
                'server_name': alert.server.name,
                'server_ip': alert.server.ip_address,
                'alert_type': alert.alert_type,
                'status': alert.status,
                'value': alert.value,
                'threshold': alert.threshold,
                'message': alert.message,
                'recipients': alert.recipients,
                'sent_at': alert.sent_at.isoformat(),
                'resolved_at': alert.resolved_at.isoformat() if alert.resolved_at else None,
            })
        
        return JsonResponse({
            'success': True,
            'alerts': alerts_data,
            'count': len(alerts_data)
        })
    except Exception as e:
        error_logger.error(f"ALERT_HISTORY_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["POST"])
def resolve_alert(request, alert_id):
    """Mark an alert as resolved"""
    try:
        alert = get_object_or_404(AlertHistory, id=alert_id)
        
        # Only resolve if currently triggered
        if alert.status != 'triggered':
            return JsonResponse({
                'success': False,
                'error': f'Alert is already {alert.status}'
            }, status=400)
        
        # Mark as resolved
        alert.status = AlertHistory.AlertStatus.RESOLVED
        alert.resolved_at = timezone.now()
        alert.save()
        
        _log_user_action(request, "RESOLVE_ALERT", 
                        f"Alert ID: {alert_id} on server {alert.server.name}")
        
        return JsonResponse({
            'success': True,
            'message': 'Alert marked as resolved',
            'alert': {
                'id': alert.id,
                'status': alert.status,
                'resolved_at': alert.resolved_at.isoformat(),
            }
        })
    except Exception as e:
        error_logger.error(f"RESOLVE_ALERT error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["POST"])
def bulk_resolve_alerts(request):
    """Bulk resolve multiple alerts"""
    try:
        import json
        data = json.loads(request.body)
        alert_ids = data.get('alert_ids', [])
        
        if not alert_ids:
            return JsonResponse({
                'success': False,
                'error': 'No alert IDs provided'
            }, status=400)
        
        if not isinstance(alert_ids, list):
            return JsonResponse({
                'success': False,
                'error': 'alert_ids must be a list'
            }, status=400)
        
        # Get alerts and resolve them
        alerts = AlertHistory.objects.filter(id__in=alert_ids, status='triggered')
        resolved_count = 0
        
        for alert in alerts:
            alert.status = AlertHistory.AlertStatus.RESOLVED
            alert.resolved_at = timezone.now()
            alert.save()
            resolved_count += 1
        
        _log_user_action(request, "BULK_RESOLVE_ALERTS", 
                        f"Resolved {resolved_count} alerts: {alert_ids}")
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully resolved {resolved_count} alert(s)',
            'resolved_count': resolved_count,
            'requested_count': len(alert_ids)
        })
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON in request body'
        }, status=400)
    except Exception as e:
        error_logger.error(f"BULK_RESOLVE_ALERTS error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@ensure_csrf_cookie
@require_http_methods(["POST"])
def toggle_alert_suppression(request, server_id, action):
    """API endpoint to suppress or resume alerts for a server"""
    try:
        server = get_object_or_404(Server, id=server_id)

        # Validate action
        if action not in ["suppress", "resume"]:
            return JsonResponse({"success": False, "error": "Invalid action. Use 'suppress' or 'resume'."}, status=400)

        # Get or create monitoring config
        from .models import MonitoringConfig
        config, created = MonitoringConfig.objects.get_or_create(server=server)

        # Update the state based on action
        if action == "suppress":
            config.alert_suppressed = True
            action_text = "suppressed"
        else:  # resume
            config.alert_suppressed = False
            action_text = "resumed"

        config.save(update_fields=["alert_suppressed"])
        _log_user_action(request, f"ALERT_{action.upper()}", f"Server: {server.name} (ID: {server_id})")

        return JsonResponse({
            "success": True,
            "message": f"Alert suppression {action_text} for {server.name}",
            "new_state": config.alert_suppressed
        })
    except Exception as e:
        error_logger.error(f"TOGGLE_ALERT_SUPPRESSION error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@ensure_csrf_cookie
@require_http_methods(["POST"])
def toggle_monitoring(request, server_id, action):
    """API endpoint to suspend or resume monitoring for a server"""
    try:
        server = get_object_or_404(Server, id=server_id)

        # Validate action
        if action not in ["suspend", "resume"]:
            return JsonResponse({"success": False, "error": "Invalid action. Use 'suspend' or 'resume'."}, status=400)

        # Get or create monitoring config
        from .models import MonitoringConfig
        config, created = MonitoringConfig.objects.get_or_create(server=server)

        # Update the state based on action
        if action == "suspend":
            config.monitoring_suspended = True
            action_text = "suspended"
            # CRITICAL: Clear connection state cache when suspending to prevent false offline alerts
            connection_state_key = f"connection_state:{server_id}"
            cache.delete(connection_state_key)
            # Set a flag to prevent alerts for the next 60 seconds after suspend
            suspend_timestamp_key = f"suspend_timestamp:{server_id}"
            cache.set(suspend_timestamp_key, timezone.now().isoformat(), timeout=60)
        else:  # resume
            config.monitoring_suspended = False
            action_text = "resumed"
            # CRITICAL: When resuming, clear cache to prevent false alerts on first check after resume
            # This ensures we don't send alerts based on stale state from before suspension
            connection_state_key = f"connection_state:{server_id}"
            cache.delete(connection_state_key)
            # Set a flag to prevent alerts for the next 60 seconds after resume
            resume_timestamp_key = f"resume_timestamp:{server_id}"
            cache.set(resume_timestamp_key, timezone.now().isoformat(), timeout=60)

        config.save(update_fields=["monitoring_suspended"])
        _log_user_action(request, f"MONITORING_{action.upper()}", f"Server: {server.name} (ID: {server_id})")

        return JsonResponse({
            "success": True,
            "message": f"Monitoring {action_text} for {server.name}",
            "new_state": config.monitoring_suspended
        })
    except Exception as e:
        error_logger.error(f"TOGGLE_MONITORING error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@require_http_methods(["GET"])
def server_metrics_api(request, server_id):
    """
    API endpoint for retrieving server metrics data for charts.

    Returns time-series data for CPU, memory, disk, and network metrics
    for visualization in the server details page.

    Args:
        request: Django HTTP request
        server_id: Integer server ID

    Query Parameters:
        range: Time range (1h, 24h, 7d, 30d, 90d) - default: 1h

    Returns:
        JsonResponse: Metrics data or error response

    Example Response:
        {
            "cpu": [{"timestamp": "2024-01-01T12:00:00Z", "value": 45.2}, ...],
            "memory": [{"timestamp": "2024-01-01T12:00:00Z", "value": 62.1}, ...],
            "disk": [{"timestamp": "2024-01-01T12:00:00Z", "value": 78.3}, ...],
            "network": [
                {"timestamp": "2024-01-01T12:00:00Z", "rx": 103200, "tx": 93200},
                ...
            ],
            "suspended": false
        }
    """
    try:
        # Get server or return 404
        try:
            server = Server.objects.get(id=server_id)
        except Server.DoesNotExist:
            return JsonResponse(
                {"error": "Server not found"},
                status=404
            )

        # Check if monitoring is suspended
        from .models import MonitoringConfig
        try:
            is_suspended = server.monitoring_config.monitoring_suspended
        except (AttributeError, MonitoringConfig.DoesNotExist):
            is_suspended = False

        # Parse range parameter
        range_param = request.GET.get('range', '1h').lower()

        # Calculate time range
        now = timezone.now()
        if range_param == '1h':
            since = now - timedelta(hours=1)
            max_points = 60  # ~1 point per minute
        elif range_param == '24h':
            since = now - timedelta(hours=24)
            max_points = 144  # ~1 point per 10 minutes
        elif range_param == '7d':
            since = now - timedelta(days=7)
            max_points = 168  # ~1 point per hour
        elif range_param == '30d':
            since = now - timedelta(days=30)
            max_points = 120  # ~1 point per 6 hours
        elif range_param == '90d':
            since = now - timedelta(days=90)
            max_points = 180  # ~1 point per 12 hours
        else:
            # Default to 1h
            since = now - timedelta(hours=1)
            max_points = 60

        # If suspended, return empty data
        if is_suspended:
            return JsonResponse({
                "cpu": [],
                "memory": [],
                "disk": [],
                "network": [],
                "suspended": True
            })

        # Query SystemMetric for this server in the time range
        metrics_qs = (
            SystemMetric.objects
            .filter(server=server, timestamp__gte=since)
            .order_by("timestamp")
            .only("timestamp", "cpu_percent", "memory_percent", "disk_usage", "network_io")
        )

        metrics_list = list(metrics_qs)

        # Down-sample if too many points
        if len(metrics_list) > max_points:
            step = max(1, len(metrics_list) // max_points)
            metrics_list = metrics_list[::step]

        # Initialize data structures
        cpu_data = []
        memory_data = []
        disk_data = []
        network_data = []

        # Process metrics
        prev_network = None
        for i, metric in enumerate(metrics_list):
            timestamp_str = metric.timestamp.isoformat()

            # CPU data
            cpu_val = float(metric.cpu_percent) if metric.cpu_percent is not None else None
            if cpu_val is not None:
                cpu_data.append({
                    "timestamp": timestamp_str,
                    "value": cpu_val
                })

            # Memory data
            memory_val = float(metric.memory_percent) if metric.memory_percent is not None else None
            if memory_val is not None:
                memory_data.append({
                    "timestamp": timestamp_str,
                    "value": memory_val
                })

            # Disk data - use highest partition percentage
            disk_val = None
            if metric.disk_usage:
                try:
                    if isinstance(metric.disk_usage, str):
                        disk_data_dict = json.loads(metric.disk_usage)
                    else:
                        disk_data_dict = metric.disk_usage

                    max_percent = 0.0
                    for mount, usage in disk_data_dict.items():
                        if isinstance(usage, dict):
                            percent = usage.get("percent", 0.0)
                        else:
                            percent = float(usage) if isinstance(usage, (int, float)) else 0.0
                        max_percent = max(max_percent, float(percent))

                    if max_percent > 0:
                        disk_val = max_percent
                except (json.JSONDecodeError, TypeError, ValueError):
                    disk_val = None

            if disk_val is not None:
                disk_data.append({
                    "timestamp": timestamp_str,
                    "value": disk_val
                })

            # Network data - calculate throughput (bytes/sec)
            network_entry = None
            if metric.network_io:
                try:
                    if isinstance(metric.network_io, str):
                        network_data_dict = json.loads(metric.network_io)
                    else:
                        network_data_dict = metric.network_io

                    # Sum all interfaces
                    total_rx = 0
                    total_tx = 0
                    for interface, io_data in network_data_dict.items():
                        if isinstance(io_data, dict):
                            total_rx += io_data.get("bytes_recv", 0) or 0
                            total_tx += io_data.get("bytes_sent", 0) or 0

                    # Calculate throughput if we have previous data
                    if prev_network and i > 0:
                        time_diff = (metric.timestamp - metrics_list[i-1].timestamp).total_seconds()
                        if time_diff > 0:
                            rx_throughput = (total_rx - prev_network['rx']) / time_diff
                            tx_throughput = (total_tx - prev_network['tx']) / time_diff
                            network_entry = {
                                "timestamp": timestamp_str,
                                "rx": rx_throughput,
                                "tx": tx_throughput
                            }

                    prev_network = {'rx': total_rx, 'tx': total_tx}

                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            if network_entry:
                network_data.append(network_entry)

        # Calculate server status
        server_status = _calculate_server_status(server)
        
        # Build response
        response_data = {
            "cpu": cpu_data,
            "memory": memory_data,
            "disk": disk_data,
            "network": network_data,
            "suspended": False,
            "status": server_status
        }

        return JsonResponse(response_data, safe=False)

    except Exception as e:
        # Log error and return error response
        app_logger.error(f"Error in server_metrics_api for server {server_id}: {e}")
        return JsonResponse(
            {"error": "Internal server error", "message": str(e)},
            status=500
        )


@require_http_methods(["GET"])
def disk_io_api(request, server_id):
    """
    API endpoint for retrieving disk I/O metrics for charts.

    Returns time-series data for disk read/write rates in bytes/second
    for visualization in the server details page.

    Args:
        request: Django HTTP request
        server_id: Integer server ID

    Query Parameters:
        range: Time range (1h, 24h, 7d) - default: 1h

    Returns:
        JsonResponse: Disk I/O data or error response

    Example Response:
        {
            "read": [{"timestamp": "2024-01-01T12:00:00Z", "value": 1024000}, ...],
            "write": [{"timestamp": "2024-01-01T12:00:00Z", "value": 512000}, ...],
            "suspended": false
        }
    """
    try:
        # Get server or return 404
        try:
            server = Server.objects.get(id=server_id)
        except Server.DoesNotExist:
            return JsonResponse(
                {"error": "Server not found"},
                status=404
            )

        # Check if monitoring is suspended
        from .models import MonitoringConfig
        try:
            is_suspended = server.monitoring_config.monitoring_suspended
        except (AttributeError, MonitoringConfig.DoesNotExist):
            is_suspended = False

        # Parse range parameter
        range_param = request.GET.get('range', '1h').lower()

        # Calculate time range
        now = timezone.now()
        if range_param == '1h':
            since = now - timedelta(hours=1)
            max_points = 60
        elif range_param == '24h':
            since = now - timedelta(hours=24)
            max_points = 144
        elif range_param == '7d':
            since = now - timedelta(days=7)
            max_points = 168
        else:
            # Default to 1h
            since = now - timedelta(hours=1)
            max_points = 60

        # If suspended, return empty data
        if is_suspended:
            return JsonResponse({
                "read": [],
                "write": [],
                "suspended": True
            })

        # Query SystemMetric for this server in the time range
        metrics_qs = (
            SystemMetric.objects
            .filter(server=server, timestamp__gte=since)
            .order_by("timestamp")
            .only("timestamp", "disk_io_read", "disk_io_write")
        )

        metrics_list = list(metrics_qs)

        # Down-sample if too many points
        if len(metrics_list) > max_points:
            step = max(1, len(metrics_list) // max_points)
            metrics_list = metrics_list[::step]

        # Initialize data structures
        read_data = []
        write_data = []

        # Process metrics
        for metric in metrics_list:
            timestamp_str = metric.timestamp.isoformat()

            # Disk read data
            read_val = float(metric.disk_io_read) if metric.disk_io_read is not None else None
            if read_val is not None and read_val >= 0:
                read_data.append({
                    "timestamp": timestamp_str,
                    "value": read_val
                })

            # Disk write data
            write_val = float(metric.disk_io_write) if metric.disk_io_write is not None else None
            if write_val is not None and write_val >= 0:
                write_data.append({
                    "timestamp": timestamp_str,
                    "value": write_val
                })

        # Build response
        response_data = {
            "read": read_data,
            "write": write_data,
            "suspended": False
        }

        return JsonResponse(response_data, safe=False)

    except Exception as e:
        # Log error and return error response
        app_logger.error(f"Error in disk_io_api for server {server_id}: {e}")
        return JsonResponse(
            {"error": "Internal server error", "message": str(e)},
            status=500
        )


@require_http_methods(["GET"])
def network_io_api(request, server_id):
    """
    API endpoint for retrieving network I/O metrics for charts.

    Returns time-series data for network sent/received rates in bytes/second
    for visualization in the server details page.

    Args:
        request: Django HTTP request
        server_id: Integer server ID

    Query Parameters:
        range: Time range (1h, 24h, 7d) - default: 1h

    Returns:
        JsonResponse: Network I/O data or error response

    Example Response:
        {
            "sent": [{"timestamp": "2024-01-01T12:00:00Z", "value": 512000}, ...],
            "recv": [{"timestamp": "2024-01-01T12:00:00Z", "value": 1024000}, ...],
            "suspended": false
        }
    """
    try:
        # Get server or return 404
        try:
            server = Server.objects.get(id=server_id)
        except Server.DoesNotExist:
            return JsonResponse(
                {"error": "Server not found"},
                status=404
            )

        # Check if monitoring is suspended
        from .models import MonitoringConfig
        try:
            is_suspended = server.monitoring_config.monitoring_suspended
        except (AttributeError, MonitoringConfig.DoesNotExist):
            is_suspended = False

        # Parse range parameter
        range_param = request.GET.get('range', '1h').lower()

        # Calculate time range
        now = timezone.now()
        if range_param == '1h':
            since = now - timedelta(hours=1)
            max_points = 60
        elif range_param == '24h':
            since = now - timedelta(hours=24)
            max_points = 144
        elif range_param == '7d':
            since = now - timedelta(days=7)
            max_points = 168
        else:
            # Default to 1h
            since = now - timedelta(hours=1)
            max_points = 60

        # If suspended, return empty data
        if is_suspended:
            return JsonResponse({
                "sent": [],
                "recv": [],
                "suspended": True
            })

        # Query SystemMetric for this server in the time range
        metrics_qs = (
            SystemMetric.objects
            .filter(server=server, timestamp__gte=since)
            .order_by("timestamp")
            .only("timestamp", "net_io_sent", "net_io_recv")
        )

        metrics_list = list(metrics_qs)

        # Down-sample if too many points
        if len(metrics_list) > max_points:
            step = max(1, len(metrics_list) // max_points)
            metrics_list = metrics_list[::step]

        # Initialize data structures
        sent_data = []
        recv_data = []

        # Process metrics
        for metric in metrics_list:
            timestamp_str = metric.timestamp.isoformat()

            # Network sent data
            sent_val = float(metric.net_io_sent) if metric.net_io_sent is not None else None
            if sent_val is not None and sent_val >= 0:
                sent_data.append({
                    "timestamp": timestamp_str,
                    "value": sent_val
                })

            # Network received data
            recv_val = float(metric.net_io_recv) if metric.net_io_recv is not None else None
            if recv_val is not None and recv_val >= 0:
                recv_data.append({
                    "timestamp": timestamp_str,
                    "value": recv_val
                })

        # Build response
        response_data = {
            "sent": sent_data,
            "recv": recv_data,
            "suspended": False
        }

        return JsonResponse(response_data, safe=False)

    except Exception as e:
        # Log error and return error response
        app_logger.error(f"Error in network_io_api for server {server_id}: {e}")
        return JsonResponse(
            {"error": "Internal server error", "message": str(e)},
            status=500
        )


@staff_member_required
@require_http_methods(["GET"])
def get_top_cpu_processes(request, server_id):
    """API endpoint to get top 3 CPU consuming processes from a server"""
    try:
        server = get_object_or_404(Server, id=server_id)
        
        # SSH connection to get top processes
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Connect using SSH key
            ssh_key_path = os.path.join(settings.BASE_DIR, 'ssh_keys', 'id_rsa')
            private_key = paramiko.RSAKey.from_private_key_file(ssh_key_path)
            
            ssh.connect(
                hostname=server.ip_address,
                port=server.port,
                username=server.username,
                pkey=private_key,
                timeout=10
            )
            
            # Get top 3 CPU processes using ps command
            # Using ps with custom format: pid, cpu%, command
            command = "ps aux --sort=-%cpu | head -4 | tail -3 | awk '{print $2\"|\"$3\"|\"$11\" \"$12\" \"$13\" \"$14\" \"$15\" \"$16\" \"$17\" \"$18\" \"$19\" \"$20}'"
            stdin, stdout, stderr = ssh.exec_command(command)
            
            processes = []
            output = stdout.read().decode('utf-8').strip()
            errors = stderr.read().decode('utf-8').strip()
            
            if errors:
                return JsonResponse({
                    "success": False,
                    "error": f"Error executing command: {errors}"
                }, status=500)
            
            # Parse output: PID|CPU%|COMMAND
            for line in output.split('\n'):
                if line.strip():
                    parts = line.split('|')
                    if len(parts) >= 3:
                        try:
                            pid = parts[0].strip()
                            cpu_percent = float(parts[1].strip())
                            command = '|'.join(parts[2:]).strip()[:50]  # Limit command length
                            
                            processes.append({
                                'pid': pid,
                                'cpu_percent': round(cpu_percent, 1),
                                'command': command
                            })
                        except (ValueError, IndexError):
                            continue
            
            ssh.close()
            
            # Sort by CPU and take top 3
            processes.sort(key=lambda x: x['cpu_percent'], reverse=True)
            top_processes = processes[:3]
            
            return JsonResponse({
                "success": True,
                "processes": top_processes,
                "server_id": server_id
            })
            
        except paramiko.AuthenticationException:
            return JsonResponse({
                "success": False,
                "error": "SSH authentication failed"
            }, status=401)
        except paramiko.SSHException as e:
            return JsonResponse({
                "success": False,
                "error": f"SSH connection error: {str(e)}"
            }, status=500)
        except Exception as e:
            return JsonResponse({
                "success": False,
                "error": f"Error connecting to server: {str(e)}"
            }, status=500)
            
    except Exception as e:
        error_logger.error(f"GET_TOP_CPU_PROCESSES error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def get_top_ram_processes(request, server_id):
    """API endpoint to get top 3 RAM consuming processes from a server"""
    try:
        server = get_object_or_404(Server, id=server_id)
        
        # SSH connection to get top processes
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Connect using SSH key
            ssh_key_path = os.path.join(settings.BASE_DIR, 'ssh_keys', 'id_rsa')
            private_key = paramiko.RSAKey.from_private_key_file(ssh_key_path)
            
            ssh.connect(
                hostname=server.ip_address,
                port=server.port,
                username=server.username,
                pkey=private_key,
                timeout=10
            )
            
            # Get top 3 RAM processes using ps command
            # Using ps with custom format: pid, mem%, command
            # %mem is column 4 in ps aux output
            command = "ps aux --sort=-%mem | head -4 | tail -3 | awk '{print $2\"|\"$4\"|\"$11\" \"$12\" \"$13\" \"$14\" \"$15\" \"$16\" \"$17\" \"$18\" \"$19\" \"$20}'"
            stdin, stdout, stderr = ssh.exec_command(command)
            
            processes = []
            output = stdout.read().decode('utf-8').strip()
            errors = stderr.read().decode('utf-8').strip()
            
            if errors:
                return JsonResponse({
                    "success": False,
                    "error": f"Error executing command: {errors}"
                }, status=500)
            
            # Parse output: PID|MEM%|COMMAND
            for line in output.split('\n'):
                if line.strip():
                    parts = line.split('|')
                    if len(parts) >= 3:
                        try:
                            pid = parts[0].strip()
                            mem_percent = float(parts[1].strip())
                            command = '|'.join(parts[2:]).strip()[:50]  # Limit command length
                            
                            processes.append({
                                'pid': pid,
                                'mem_percent': round(mem_percent, 1),
                                'command': command
                            })
                        except (ValueError, IndexError):
                            continue
            
            ssh.close()
            
            # Sort by RAM and take top 3
            processes.sort(key=lambda x: x['mem_percent'], reverse=True)
            top_processes = processes[:3]
            
            return JsonResponse({
                "success": True,
                "processes": top_processes,
                "server_id": server_id
            })
            
        except paramiko.AuthenticationException:
            return JsonResponse({
                "success": False,
                "error": "SSH authentication failed"
            }, status=401)
        except paramiko.SSHException as e:
            return JsonResponse({
                "success": False,
                "error": f"SSH connection error: {str(e)}"
            }, status=500)
        except Exception as e:
            return JsonResponse({
                "success": False,
                "error": f"Error connecting to server: {str(e)}"
            }, status=500)
            
    except Exception as e:
        error_logger.error(f"GET_TOP_RAM_PROCESSES error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def get_active_services(request, server_id):
    """API endpoint to get all active services from systemctl"""
    try:
        server = get_object_or_404(Server, id=server_id)
        
        # SSH connection to get active services
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Connect using SSH key
            ssh_key_path = os.path.join(settings.BASE_DIR, 'ssh_keys', 'id_rsa')
            private_key = paramiko.RSAKey.from_private_key_file(ssh_key_path)
            
            ssh.connect(
                hostname=server.ip_address,
                port=server.port,
                username=server.username,
                pkey=private_key,
                timeout=10
            )
            
            # Get ALL services (running, stopped, failed, etc.) using systemctl
            # Format: UNIT LOAD ACTIVE SUB DESCRIPTION
            command = "systemctl list-units --type=service --all --no-pager --no-legend 2>/dev/null"
            stdin, stdout, stderr = ssh.exec_command(command)
            
            services = []
            output = stdout.read().decode('utf-8').strip()
            errors = stderr.read().decode('utf-8').strip()
            
            if errors and "No such file" not in errors and "command not found" not in errors.lower():
                return JsonResponse({
                    "success": False,
                    "error": f"Error executing command: {errors}"
                }, status=500)
            
            # Parse output: Format is typically:
            # service-name.service loaded active running Description
            for line in output.split('\n'):
                if line.strip() and '.service' in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        try:
                            service_name = parts[0].replace('.service', '').strip()
                            loaded = parts[1].strip() if len(parts) > 1 else 'unknown'
                            active = parts[2].strip() if len(parts) > 2 else 'unknown'
                            sub = parts[3].strip() if len(parts) > 3 else 'unknown'
                            
                            # Include all services regardless of status
                            # Determine status based on active and sub states
                            if active == 'active' and sub == 'running':
                                service_status = 'running'
                            elif active == 'active' and sub in ['exited', 'dead']:
                                service_status = 'stopped'
                            elif active == 'inactive':
                                service_status = 'stopped'
                            elif active == 'failed':
                                service_status = 'failed'
                            else:
                                service_status = 'unknown'
                                
                            # Get service uptime if running
                            uptime_hours = None
                            if service_status == 'running':
                                try:
                                    # Use systemctl show to get active timestamp
                                    uptime_cmd = f"systemctl show {service_name} --property=ActiveEnterTimestamp --value 2>/dev/null"
                                    stdin_uptime, stdout_uptime, stderr_uptime = ssh.exec_command(uptime_cmd)
                                    uptime_output = stdout_uptime.read().decode('utf-8').strip()
                                    if uptime_output:
                                        # Parse timestamp and calculate uptime
                                        from datetime import datetime
                                        try:
                                            # systemctl returns format: Mon 2024-01-01 12:00:00 UTC or similar
                                            # Try to parse the timestamp
                                            uptime_str = uptime_output.split('.')[0] if '.' in uptime_output else uptime_output
                                            # Remove day name if present (e.g., "Mon ")
                                            if len(uptime_str.split()) > 3:
                                                uptime_str = ' '.join(uptime_str.split()[1:])
                                            
                                            # Try different formats
                                            try:
                                                uptime_dt = datetime.strptime(uptime_str, '%Y-%m-%d %H:%M:%S')
                                            except:
                                                try:
                                                    uptime_dt = datetime.strptime(uptime_str, '%Y-%m-%d %H:%M:%S %Z')
                                                except:
                                                    uptime_dt = None
                                            
                                            if uptime_dt:
                                                # Make timezone-aware (assume UTC)
                                                from django.utils import timezone as tz_utils
                                                if uptime_dt.tzinfo is None:
                                                    import pytz
                                                    uptime_dt = pytz.UTC.localize(uptime_dt)
                                                
                                                uptime_delta = timezone.now() - uptime_dt
                                                uptime_hours = round(uptime_delta.total_seconds() / 3600, 1)
                                        except Exception as e:
                                            # If parsing fails, skip uptime
                                            pass
                                except:
                                    pass
                            
                            # Categorize service as critical or other
                            is_critical = False
                            service_name_lower = service_name.lower()
                            
                            # Critical service patterns
                            critical_patterns = [
                                'apache', 'httpd', 'nginx', 'web', 'www',
                                'mysql', 'mariadb', 'postgresql', 'postgres', 'mongodb', 'redis', 'db',
                                'mail', 'postfix', 'sendmail', 'dovecot', 'exim', 'smtp', 'imap', 'pop',
                                'php', 'python', 'node', 'java', 'tomcat', 'jetty', 'gunicorn',
                                'cpanel', 'whm', 'lsws', 'openlitespeed',
                                'docker', 'containerd', 'kube',
                                'elasticsearch', 'kibana', 'logstash',
                                'rabbitmq', 'activemq', 'kafka'
                            ]
                            
                            for pattern in critical_patterns:
                                if pattern in service_name_lower:
                                    is_critical = True
                                    break
                            
                            services.append({
                                'name': service_name,
                                'status': service_status,
                                'loaded': loaded,
                                'active': active,
                                'sub': sub,
                                'uptime_hours': uptime_hours,
                                'is_critical': is_critical
                            })
                        except (ValueError, IndexError):
                            continue
            
            ssh.close()
            
            # Sort services by name
            services.sort(key=lambda x: x['name'].lower())
            
            # Update or create Service records in database
            from .models import Service
            current_time = timezone.now()
            existing_services = {s.name: s for s in Service.objects.filter(server=server)}
            service_names_in_response = {s['name'] for s in services}
            
            # Update existing services or create new ones (monitoring disabled by default)
            for service_data in services:
                service_name = service_data['name']
                if service_name in existing_services:
                    # Update existing service
                    service = existing_services[service_name]
                    service.status = service_data['status']  # Use the status from systemctl
                    service.last_checked = current_time
                    # Ensure monitoring_enabled defaults to False if not set
                    if service.monitoring_enabled is None:
                        service.monitoring_enabled = False
                    service.save()
                    # Add monitoring_enabled to response (default to False if None)
                    service_data['monitoring_enabled'] = service.monitoring_enabled if service.monitoring_enabled is not None else False
                    service_data['id'] = service.id
                else:
                    # Create new service with monitoring disabled by default
                    new_service = Service.objects.create(
                        server=server,
                        name=service_name,
                        status=service_data['status'],  # Use the status from systemctl
                        service_type='systemd',
                        last_checked=current_time,
                        monitoring_enabled=False  # Disabled by default
                    )
                    service_data['monitoring_enabled'] = False
                    service_data['id'] = new_service.id
            
            # Mark services that are no longer running as stopped
            for service_name, service in existing_services.items():
                if service_name not in service_names_in_response:
                    service.status = 'stopped'
                    service.last_checked = current_time
                    service.save()
            
            # Add monitoring_enabled and id to all services in response (ensure False by default)
            for service_data in services:
                if 'monitoring_enabled' not in service_data:
                    # Get from database if not already set
                    service = existing_services.get(service_data['name'])
                    if service:
                        # Default to False if None
                        service_data['monitoring_enabled'] = service.monitoring_enabled if service.monitoring_enabled is not None else False
                        service_data['id'] = service.id
                    else:
                        service_data['monitoring_enabled'] = False
                # Ensure it's explicitly False, not None
                if service_data.get('monitoring_enabled') is None:
                    service_data['monitoring_enabled'] = False
            
            return JsonResponse({
                "success": True,
                "services": services,
                "count": len(services),
                "server_id": server_id,
                "timestamp": current_time.isoformat()
            })

        finally:
            try:
                ssh.close()
            except:
                pass

    except Exception as e:
        logger.error(f"Failed to get active services: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
def get_server_services(request, server_id):
    """API endpoint to get categorized systemd services for a server"""
    try:
        server = get_object_or_404(Server, id=server_id)
        monitoring_config, created = MonitoringConfig.objects.get_or_create(server=server)

        # SSH connection to get all services
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
                return JsonResponse({
                    "success": False,
                    "error": f"SSH command failed: {error_output}"
                }, status=500)

            # Parse systemctl output
            all_services = []
            for line in output.split('\n'):
                if line.strip():
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

                        # Determine status
                        if active_state == 'active' and sub_state == 'running':
                            status = 'running'
                        elif active_state == 'failed':
                            status = 'failed'
                        elif active_state == 'inactive':
                            status = 'stopped'
                        else:
                            status = 'unknown'

                        # Get description if available
                        description = ' '.join(parts[4:]) if len(parts) > 4 else ''

                        all_services.append({
                            'name': service_name,
                            'status': status,
                            'description': description,
                            'unit_name': unit_name
                        })

            # Get existing Service records from database to include IDs and monitoring status
            from .models import Service
            existing_services = {s.name: s for s in Service.objects.filter(server=server)}
            current_time = timezone.now()
            
            # Update or create Service records and add IDs/monitoring status to response
            for service in all_services:
                service_name = service['name']
                if service_name in existing_services:
                    # Update existing service status
                    db_service = existing_services[service_name]
                    db_service.status = service['status']
                    db_service.last_checked = current_time
                    db_service.save()
                    # Add ID and monitoring status
                    service['id'] = db_service.id
                    service['monitoring_enabled'] = db_service.monitoring_enabled if db_service.monitoring_enabled is not None else False
                else:
                    # Create new service record
                    new_service = Service.objects.create(
                        server=server,
                        name=service_name,
                        status=service['status'],
                        service_type='systemd',
                        last_checked=current_time,
                        monitoring_enabled=False
                    )
                    service['id'] = new_service.id
                    service['monitoring_enabled'] = False
            
            # Categorize services
            monitored_services = set(monitoring_config.monitored_services or [])
            critical = []
            running = []
            stopped = []
            failed = []
            
            # Critical service patterns (same as in get_active_services)
            critical_patterns = [
                'apache', 'httpd', 'nginx', 'web', 'www',
                'mysql', 'mariadb', 'postgresql', 'postgres', 'mongodb', 'redis', 'db',
                'mail', 'postfix', 'sendmail', 'dovecot', 'exim', 'smtp', 'imap', 'pop',
                'php', 'python', 'node', 'java', 'tomcat', 'jetty', 'gunicorn',
                'cpanel', 'whm', 'lsws', 'openlitespeed',
                'docker', 'containerd', 'kube',
                'elasticsearch', 'kibana', 'logstash',
                'rabbitmq', 'activemq', 'kafka'
            ]

            for service in all_services:
                service_name_lower = service['name'].lower()
                is_critical_service = False
                
                # Check if it's in monitored services
                if service['name'] in monitored_services:
                    is_critical_service = True
                else:
                    # Check if it matches critical patterns
                    for pattern in critical_patterns:
                        if pattern in service_name_lower:
                            is_critical_service = True
                            break
                
                # Add is_critical flag to service
                service['is_critical'] = is_critical_service
                
                if is_critical_service:
                    critical.append(service)
                
                # Also categorize by status
                if service['status'] == 'running':
                    running.append(service)
                elif service['status'] == 'stopped':
                    stopped.append(service)
                elif service['status'] == 'failed':
                    failed.append(service)

            # Separate monitored services (enabled) from all critical services
            monitored = [s for s in all_services if s.get('monitoring_enabled', False)]
            
            return JsonResponse({
                "success": True,
                "critical": critical,
                "monitored": monitored,  # Services with monitoring enabled
                "running": running,
                "stopped": stopped,
                "failed": failed,
                "all": all_services,
                "monitored_count": len(monitored),
                "total_count": len(all_services)
            })

        finally:
            ssh.close()

    except Exception as e:
        logger.error(f"Failed to get server services: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["POST"])
def update_monitored_services(request, server_id):
    """Update the list of monitored services for a server"""
    try:
        server = get_object_or_404(Server, id=server_id)
        monitoring_config, created = MonitoringConfig.objects.get_or_create(server=server)

        data = json.loads(request.body)
        services = data.get('services', [])

        # Validate services is a list
        if not isinstance(services, list):
            return JsonResponse({"success": False, "error": "Services must be a list"}, status=400)

        # Update monitored services
        monitoring_config.monitored_services = services
        monitoring_config.save()

        _log_user_action(request, "UPDATE_MONITORED_SERVICES", f"Server: {server.name} (ID: {server_id}) - Services: {', '.join(services)}")

        return JsonResponse({
            "success": True,
            "message": f"Updated monitored services for {server.name}",
            "monitored_services": services
        })

    except Exception as e:
        logger.error(f"Failed to update monitored services: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["POST"])
def update_service_thresholds(request, server_id):
    """Update service monitoring thresholds for a server"""
    try:
        server = get_object_or_404(Server, id=server_id)
        monitoring_config, created = MonitoringConfig.objects.get_or_create(server=server)

        data = json.loads(request.body)

        # Update thresholds
        if 'failure_alert' in data:
            monitoring_config.service_failure_alert = data['failure_alert']
        if 'restart_threshold' in data:
            monitoring_config.service_restart_threshold = data['restart_threshold']
        if 'down_duration_threshold' in data:
            monitoring_config.service_down_duration_threshold = data['down_duration_threshold']

        monitoring_config.save()

        _log_user_action(request, "UPDATE_SERVICE_THRESHOLDS", f"Server: {server.name} (ID: {server_id})")

        return JsonResponse({
            "success": True,
            "message": f"Updated service thresholds for {server.name}",
            "thresholds": {
                "failure_alert": monitoring_config.service_failure_alert,
                "restart_threshold": monitoring_config.service_restart_threshold,
                "down_duration_threshold": monitoring_config.service_down_duration_threshold
            }
        })

    except Exception as e:
        logger.error(f"Failed to update service thresholds: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def server_anomalies_log(request, server_id):
    """View page to display and manage anomalies for a specific server"""
    try:
        server = get_object_or_404(Server, id=server_id)
    except Server.DoesNotExist:
        from django.http import Http404
        raise Http404("Server not found")
    
    # Get query parameters for filtering
    status_filter = request.GET.get('status', '').strip()
    severity_filter = request.GET.get('severity', '').strip()
    
    # Build query for anomalies
    anomalies_query = Anomaly.objects.filter(server=server).select_related('metric').order_by('-timestamp')
    
    # Apply filters
    if status_filter == 'resolved':
        anomalies_query = anomalies_query.filter(resolved=True)
    elif status_filter == 'unresolved':
        anomalies_query = anomalies_query.filter(resolved=False)
    
    if severity_filter:
        valid_severities = [choice[0] for choice in Anomaly.Severity.choices]
        if severity_filter in valid_severities:
            anomalies_query = anomalies_query.filter(severity=severity_filter)
    
    anomalies = list(anomalies_query[:500])  # Limit to 500 most recent
    
    # Counts for summary
    total_anomalies = Anomaly.objects.filter(server=server).count()
    unresolved_count = Anomaly.objects.filter(server=server, resolved=False).count()
    resolved_count = Anomaly.objects.filter(server=server, resolved=True).count()
    
    # Severity counts
    severity_counts = {}
    for severity_code, severity_label in Anomaly.Severity.choices:
        severity_counts[severity_code] = Anomaly.objects.filter(server=server, severity=severity_code).count()
    
    context = {
        'server': server,
        'anomalies': anomalies,
        'total_anomalies': total_anomalies,
        'unresolved_count': unresolved_count,
        'resolved_count': resolved_count,
        'severity_counts': severity_counts,
        'selected_status': status_filter,
        'selected_severity': severity_filter,
        'severity_choices': Anomaly.Severity.choices,
        'show_sidebar': True,
    }
    
    return render(request, 'core/server_anomalies_log.html', context)


@staff_member_required
@require_http_methods(["GET"])
def anomaly_detail_api(request, anomaly_id):
    """
    API endpoint for retrieving detailed anomaly information.
    
    If explanation is empty and LLM is enabled, generates explanation on-demand.
    
    Args:
        request: Django HTTP request
        anomaly_id: Integer anomaly ID
    
    Returns:
        JsonResponse: Anomaly details with explanation
    """
    try:
        anomaly = get_object_or_404(Anomaly, id=anomaly_id)
        
        # Check if user has permission to view this anomaly
        # (Anomalies are server-specific, so check server access)
        if not request.user.is_superuser:
            # Check ACL if needed - for now, allow all staff members
            pass
        
        # Prepare response data
        response_data = {
            "id": anomaly.id,
            "server": {
                "id": anomaly.server.id,
                "name": anomaly.server.name
            },
            "timestamp": anomaly.timestamp.isoformat(),
            "metric_type": anomaly.metric_type,
            "metric_name": anomaly.metric_name,
            "metric_value": anomaly.metric_value,
            "severity": anomaly.severity,
            "anomaly_score": anomaly.anomaly_score,
            "explanation": anomaly.explanation or "",
            "llm_generated": anomaly.llm_generated,
            "acknowledged": anomaly.acknowledged,
            "resolved": anomaly.resolved,
            "resolved_at": anomaly.resolved_at.isoformat() if anomaly.resolved_at else None,
        }
        
        # If explanation is empty and LLM is enabled, generate it
        if not anomaly.explanation:
            try:
                config = anomaly.server.monitoring_config
                if config and getattr(config, 'use_llm_explanation', True):
                    from .llm_analyzer import OllamaAnalyzer
                    analyzer = OllamaAnalyzer()
                    
                    if analyzer.enabled:
                        explanation = analyzer.explain_anomaly(
                            metric_type=anomaly.metric_type,
                            metric_name=anomaly.metric_name,
                            metric_value=anomaly.metric_value,
                            server_name=anomaly.server.name
                        )
                        
                        if explanation:
                            anomaly.explanation = explanation
                            anomaly.llm_generated = True
                            anomaly.save()
                            
                            response_data["explanation"] = explanation
                            response_data["llm_generated"] = True
            except Exception as e:
                app_logger.warning(f"Failed to generate LLM explanation for anomaly {anomaly_id}: {e}")
        
        # If still no explanation, generate fallback
        if not response_data["explanation"]:
            response_data["explanation"] = _generate_fallback_explanation(anomaly)
        
        return JsonResponse(response_data)
        
    except Exception as e:
        error_logger.error(f"Error in anomaly_detail_api for anomaly {anomaly_id}: {e}")
        return JsonResponse(
            {"error": "Internal server error", "message": str(e)},
            status=500
        )


def _generate_fallback_explanation(anomaly):
    """Generate a fallback explanation when LLM is not available."""
    metric_type = anomaly.metric_type.lower()
    metric_value = anomaly.metric_value
    severity = anomaly.severity
    
    explanations = {
        "cpu": f"CPU usage reached {metric_value:.1f}%, which is {severity.lower()} severity. This may indicate high computational load, background processes, or resource-intensive applications running on the server.",
        "memory": f"Memory usage reached {metric_value:.1f}%, which is {severity.lower()} severity. This suggests the system is using a significant portion of available RAM, potentially causing performance degradation or swap usage.",
        "disk": f"Disk usage reached {metric_value:.1f}%, which is {severity.lower()} severity. The storage partition is nearly full, which may prevent new files from being written and could cause application failures.",
        "network": f"Network throughput reached {metric_value:.2f} GB, which is {severity.lower()} severity. This indicates high network activity, which may be due to legitimate traffic, data transfers, or potential network issues."
    }
    
    return explanations.get(metric_type, f"Anomaly detected in {metric_type} metric with value {metric_value:.2f} ({severity} severity). Please investigate the system state and recent changes.")


@staff_member_required
@require_http_methods(["POST"])
def anomaly_resolve_api(request, anomaly_id):
    """
    API endpoint for manually resolving an anomaly.
    
    Sets acknowledged=True, resolved=True, and resolved_at=now().
    Does NOT delete the anomaly record.
    
    Args:
        request: Django HTTP request
        anomaly_id: Integer anomaly ID
    
    Returns:
        JsonResponse: Success status and updated anomaly data
    """
    try:
        anomaly = get_object_or_404(Anomaly, id=anomaly_id)
        
        # Check if user has permission to resolve anomalies
        if not request.user.is_superuser:
            # Check ACL if needed - for now, allow all staff members
            pass
        
        # Mark as resolved
        anomaly.acknowledged = True
        anomaly.resolved = True
        anomaly.resolved_at = timezone.now()
        anomaly.save()
        
        # Clear anomaly cache for this server to force refresh
        try:
            from .anomaly_cache import AnomalyCache
            AnomalyCache.clear(anomaly.server.id)
        except Exception as e:
            app_logger.warning(f"Failed to clear anomaly cache for server {anomaly.server.id}: {e}")
        
        _log_user_action(request, "RESOLVE_ANOMALY", 
                        f"Anomaly ID: {anomaly_id} on server {anomaly.server.name}")
        
        return JsonResponse({
            "success": True,
            "message": "Anomaly marked as resolved",
            "anomaly": {
                "id": anomaly.id,
                "resolved": anomaly.resolved,
                "resolved_at": anomaly.resolved_at.isoformat(),
                "acknowledged": anomaly.acknowledged
            }
        })
        
    except Exception as e:
        error_logger.error(f"Error in anomaly_resolve_api for anomaly {anomaly_id}: {e}")
        return JsonResponse(
            {"success": False, "error": "Internal server error", "message": str(e)},
            status=500
        )


@staff_member_required
@require_http_methods(["POST"])
def anomaly_bulk_resolve_api(request):
    """
    API endpoint for bulk resolving multiple anomalies.
    
    Accepts a list of anomaly IDs in the request body and marks them all as resolved.
    
    Request Body (JSON):
        {
            "anomaly_ids": [1, 2, 3, ...]
        }
    
    Returns:
        JsonResponse: Success status and count of resolved anomalies
    """
    try:
        import json
        data = json.loads(request.body)
        anomaly_ids = data.get('anomaly_ids', [])
        
        if not anomaly_ids:
            return JsonResponse(
                {"success": False, "error": "No anomaly IDs provided"},
                status=400
            )
        
        if not isinstance(anomaly_ids, list):
            return JsonResponse(
                {"success": False, "error": "anomaly_ids must be a list"},
                status=400
            )
        
        # Get anomalies and verify they exist
        anomalies = Anomaly.objects.filter(id__in=anomaly_ids)
        resolved_count = 0
        server_ids = set()
        
        for anomaly in anomalies:
            # Mark as resolved
            anomaly.acknowledged = True
            anomaly.resolved = True
            anomaly.resolved_at = timezone.now()
            anomaly.save()
            resolved_count += 1
            server_ids.add(anomaly.server.id)
        
        # Clear anomaly cache for affected servers
        try:
            from .anomaly_cache import AnomalyCache
            for server_id in server_ids:
                AnomalyCache.clear(server_id)
        except Exception as e:
            app_logger.warning(f"Failed to clear anomaly cache: {e}")
        
        _log_user_action(request, "BULK_RESOLVE_ANOMALIES", 
                        f"Resolved {resolved_count} anomalies: {anomaly_ids}")
        
        return JsonResponse({
            "success": True,
            "message": f"Successfully resolved {resolved_count} anomaly/anomalies",
            "resolved_count": resolved_count,
            "requested_count": len(anomaly_ids)
        })
        
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON in request body"},
            status=400
        )
    except Exception as e:
        error_logger.error(f"Error in anomaly_bulk_resolve_api: {e}")
        return JsonResponse(
            {"success": False, "error": "Internal server error", "message": str(e)},
            status=500
        )


@staff_member_required
@require_http_methods(["POST"])
def toggle_service_monitoring(request, server_id, service_id):
    """
    API endpoint to toggle monitoring for a service.
    
    IMPORTANT: Services are server-specific. Enabling monitoring for a service
    on one server (e.g., cpanel-test) will ONLY affect that service on that server.
    It will NOT enable monitoring for the same service name on other servers.
    Each server has its own separate Service records.
    """
    try:
        server = get_object_or_404(Server, id=server_id)
        from .models import Service
        # CRITICAL: This ensures the service belongs to the specific server
        # Services are server-specific - enabling on one server does NOT affect others
        service = get_object_or_404(Service, id=service_id, server=server)
        
        # Toggle monitoring_enabled
        service.monitoring_enabled = not service.monitoring_enabled
        service.save()
        
        _log_user_action(request, "TOGGLE_SERVICE_MONITORING", 
                        f"Service: {service.name} on {server.name} - Monitoring {'enabled' if service.monitoring_enabled else 'disabled'}")
        
        return JsonResponse({
            "success": True,
            "message": f"Monitoring {'enabled' if service.monitoring_enabled else 'disabled'} for {service.name}",
            "monitoring_enabled": service.monitoring_enabled
        })
        
    except Exception as e:
        error_logger.error(f"TOGGLE_SERVICE_MONITORING error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_news_feed_api(request):
    """
    API endpoint for dashboard news ticker feed.
    Returns recent events (alerts, anomalies, status changes) in a news-like format.
    """
    try:
        from django.utils import timezone
        from datetime import timedelta
        
        # Get events from the last 24 hours
        since = timezone.now() - timedelta(hours=24)
        news_items = []
        
        # 1. Recent alerts (triggered and resolved)
        recent_alerts = AlertHistory.objects.filter(
            sent_at__gte=since
        ).select_related('server').order_by('-sent_at')[:50]
        
        for alert in recent_alerts:
            if alert.status == 'triggered':
                time_ago = timezone.now() - alert.sent_at
                if time_ago < timedelta(minutes=1):
                    time_str = "just now"
                elif time_ago < timedelta(hours=1):
                    minutes = int(time_ago.total_seconds() / 60)
                    time_str = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
                elif time_ago < timedelta(hours=12):
                    hours = int(time_ago.total_seconds() / 3600)
                    time_str = f"{hours} hour{'s' if hours != 1 else ''} ago"
                else:
                    time_str = alert.sent_at.strftime("%I:%M %p")
                
                news_items.append({
                    'type': 'alert',
                    'icon': '🔴',
                    'severity': 'high',
                    'text': f"{alert.server.name}: {alert.message} ({time_str})",
                    'timestamp': alert.sent_at.isoformat(),
                    'server_name': alert.server.name,
                })
            elif alert.status == 'resolved' and alert.resolved_at:
                time_ago = timezone.now() - alert.resolved_at
                if time_ago < timedelta(minutes=1):
                    time_str = "just now"
                elif time_ago < timedelta(hours=1):
                    minutes = int(time_ago.total_seconds() / 60)
                    time_str = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
                else:
                    time_str = alert.resolved_at.strftime("%I:%M %p")
                
                news_items.append({
                    'type': 'resolved',
                    'icon': '✅',
                    'severity': 'info',
                    'text': f"{alert.server.name}: {alert.alert_type} alert resolved ({time_str})",
                    'timestamp': alert.resolved_at.isoformat(),
                    'server_name': alert.server.name,
                })
        
        # 2. Recent anomalies (unresolved)
        recent_anomalies = Anomaly.objects.filter(
            timestamp__gte=since,
            resolved=False
        ).select_related('server', 'metric').order_by('-timestamp')[:30]
        
        for anomaly in recent_anomalies:
            time_ago = timezone.now() - anomaly.timestamp
            if time_ago < timedelta(minutes=1):
                time_str = "just now"
            elif time_ago < timedelta(hours=1):
                minutes = int(time_ago.total_seconds() / 60)
                time_str = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            elif time_ago < timedelta(hours=12):
                hours = int(time_ago.total_seconds() / 3600)
                time_str = f"{hours} hour{'s' if hours != 1 else ''} ago"
            else:
                time_str = anomaly.timestamp.strftime("%I:%M %p")
            
            severity_icon = {
                'LOW': '🟡',
                'MEDIUM': '🟠',
                'HIGH': '🔴',
                'CRITICAL': '🚨'
            }.get(anomaly.severity, '⚠️')
            
            news_items.append({
                'type': 'anomaly',
                'icon': severity_icon,
                'severity': anomaly.severity.lower(),
                'text': f"{anomaly.server.name}: {anomaly.metric_type.upper()} anomaly detected - {anomaly.metric_name} = {anomaly.metric_value:.1f}% ({time_str})",
                'timestamp': anomaly.timestamp.isoformat(),
                'server_name': anomaly.server.name,
            })
        
        # 3. Add current system status summary
        total_servers = Server.objects.count()
        online_servers = sum(1 for s in Server.objects.all() if _calculate_server_status(s) == 'online')
        warning_servers = sum(1 for s in Server.objects.all() if _calculate_server_status(s) == 'warning')
        offline_servers = total_servers - online_servers - warning_servers
        
        active_alerts = AlertHistory.objects.filter(status='triggered').count()
        active_anomalies = Anomaly.objects.filter(resolved=False).count()
        
        # Add status summary as first item (if no recent events, show current status)
        if len(news_items) == 0 or online_servers == total_servers:
            news_items.insert(0, {
                'type': 'status',
                'icon': '📊',
                'severity': 'info',
                'text': f"All systems operational: {online_servers}/{total_servers} servers online, {active_alerts} active alerts, {active_anomalies} active anomalies",
                'timestamp': timezone.now().isoformat(),
                'server_name': 'System',
            })
        else:
            # Add current status as context
            if warning_servers > 0 or offline_servers > 0:
                news_items.insert(0, {
                    'type': 'status',
                    'icon': '⚠️',
                    'severity': 'warning',
                    'text': f"System Status: {online_servers} online, {warning_servers} warning, {offline_servers} offline",
                    'timestamp': timezone.now().isoformat(),
                    'server_name': 'System',
                })
        
        # Sort by timestamp (most recent first)
        news_items.sort(key=lambda x: x['timestamp'], reverse=True)
        
        # Limit to 100 items
        news_items = news_items[:100]
        
        return JsonResponse({
            'success': True,
            'news_items': news_items,
            'count': len(news_items),
            'generated_at': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_NEWS_FEED_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_summary_stats_api(request):
    """API endpoint for dashboard summary statistics"""
    try:
        from datetime import datetime, timedelta
        
        total_servers = Server.objects.count()
        active_alerts = AlertHistory.objects.filter(status='triggered').count()
        active_anomalies = Anomaly.objects.filter(resolved=False).count()
        
        # Count critical servers (warning or offline status)
        critical_count = 0
        online_count = 0
        for server in Server.objects.all():
            status = _calculate_server_status(server)
            if status in ['warning', 'offline']:
                critical_count += 1
            elif status == 'online':
                online_count += 1
        
        # Calculate SLA compliance (online servers / total servers * 100)
        sla_compliance = (online_count / total_servers * 100) if total_servers > 0 else 100.0
        
        # Calculate trends (compare with yesterday)
        yesterday = timezone.now() - timedelta(days=1)
        yesterday_servers = Server.objects.filter(created_at__lt=yesterday).count() if hasattr(Server, 'created_at') else total_servers
        server_trend = total_servers - yesterday_servers if yesterday_servers > 0 else 0
        
        yesterday_alerts = AlertHistory.objects.filter(status='triggered', sent_at__lt=yesterday).count()
        alert_trend = active_alerts - yesterday_alerts
        
        return JsonResponse({
            'success': True,
            'data': {
                'total_vms': total_servers,
                'server_trend': server_trend,
                'active_alerts': active_alerts + active_anomalies,
                'alert_trend': alert_trend,
                'critical_vms': critical_count,
                'sla_compliance': round(sla_compliance, 1),
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_SUMMARY_STATS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_cpu_trend_api(request):
    """API endpoint for 24-hour CPU trend data"""
    try:
        from datetime import timedelta
        
        hours = int(request.GET.get('hours', 24))
        server_id = request.GET.get('server_id', 'all')  # 'all' for average, or server ID
        since = timezone.now() - timedelta(hours=hours)
        
        data_points = []
        
        # Filter by server if specified
        if server_id and server_id != 'all':
            try:
                server = Server.objects.get(id=int(server_id))
                metrics_filter = SystemMetric.objects.filter(
                    server=server,
                    timestamp__gte=since
                )
            except (Server.DoesNotExist, ValueError):
                return JsonResponse({"success": False, "error": "Invalid server ID"}, status=400)
        else:
            # Get average of all servers
            metrics_filter = SystemMetric.objects.filter(timestamp__gte=since)
        
        # Aggregate CPU metrics by hour
        metrics = metrics_filter.order_by('timestamp').values('timestamp', 'cpu_percent', 'server_id')
        
        # Group by hour and calculate average
        hourly_data = {}
        for metric in metrics:
            hour_key = metric['timestamp'].replace(minute=0, second=0, microsecond=0)
            if hour_key not in hourly_data:
                hourly_data[hour_key] = []
            hourly_data[hour_key].append(metric['cpu_percent'] or 0)
        
        # Calculate averages and prepare data
        for hour, values in sorted(hourly_data.items()):
            avg_cpu = sum(values) / len(values) if values else 0
            data_points.append({
                'timestamp': hour.isoformat(),
                'value': round(avg_cpu, 2)
            })
        
        # Calculate current, peak, and average
        current_cpu = data_points[-1]['value'] if data_points else 0
        peak_cpu = max([d['value'] for d in data_points]) if data_points else 0
        avg_cpu = sum([d['value'] for d in data_points]) / len(data_points) if data_points else 0
        
        return JsonResponse({
            'success': True,
            'data': {
                'points': data_points,
                'current': round(current_cpu, 1),
                'peak': round(peak_cpu, 1),
                'average': round(avg_cpu, 1)
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_CPU_TREND_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_memory_trend_api(request):
    """API endpoint for 24-hour memory trend data"""
    try:
        from datetime import timedelta
        
        hours = int(request.GET.get('hours', 24))
        server_id = request.GET.get('server_id', 'all')  # 'all' for average, or server ID
        since = timezone.now() - timedelta(hours=hours)
        
        # Filter by server if specified
        if server_id and server_id != 'all':
            try:
                server = Server.objects.get(id=int(server_id))
                metrics_filter = SystemMetric.objects.filter(
                    server=server,
                    timestamp__gte=since
                )
            except (Server.DoesNotExist, ValueError):
                return JsonResponse({"success": False, "error": "Invalid server ID"}, status=400)
        else:
            # Get average of all servers
            metrics_filter = SystemMetric.objects.filter(timestamp__gte=since)
        
        metrics = metrics_filter.order_by('timestamp').values('timestamp', 'memory_percent', 'server_id')
        
        hourly_data = {}
        for metric in metrics:
            hour_key = metric['timestamp'].replace(minute=0, second=0, microsecond=0)
            if hour_key not in hourly_data:
                hourly_data[hour_key] = []
            hourly_data[hour_key].append(metric['memory_percent'] or 0)
        
        data_points = []
        for hour, values in sorted(hourly_data.items()):
            avg_memory = sum(values) / len(values) if values else 0
            data_points.append({
                'timestamp': hour.isoformat(),
                'value': round(avg_memory, 2)
            })
        
        current_memory = data_points[-1]['value'] if data_points else 0
        peak_memory = max([d['value'] for d in data_points]) if data_points else 0
        avg_memory = sum([d['value'] for d in data_points]) / len(data_points) if data_points else 0
        
        return JsonResponse({
            'success': True,
            'data': {
                'points': data_points,
                'current': round(current_memory, 1),
                'peak': round(peak_memory, 1),
                'average': round(avg_memory, 1)
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_MEMORY_TREND_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_network_trend_api(request):
    """API endpoint for 24-hour network trend data (inbound/outbound)"""
    try:
        from datetime import timedelta
        import json
        
        hours = int(request.GET.get('hours', 24))
        server_id = request.GET.get('server_id', 'all')  # 'all' for average, or server ID
        since = timezone.now() - timedelta(hours=hours)
        
        # Filter by server if specified
        if server_id and server_id != 'all':
            try:
                server = Server.objects.get(id=int(server_id))
                metrics_filter = SystemMetric.objects.filter(
                    server=server,
                    timestamp__gte=since
                )
            except (Server.DoesNotExist, ValueError):
                return JsonResponse({"success": False, "error": "Invalid server ID"}, status=400)
        else:
            # Get average of all servers
            metrics_filter = SystemMetric.objects.filter(timestamp__gte=since)
        
        metrics = metrics_filter.order_by('timestamp').values('timestamp', 'network_io', 'server_id')
        
        hourly_inbound = {}
        hourly_outbound = {}
        
        for metric in metrics:
            hour_key = metric['timestamp'].replace(minute=0, second=0, microsecond=0)
            network_data = metric.get('network_io')
            
            if network_data:
                try:
                    if isinstance(network_data, str):
                        network_data = json.loads(network_data)
                    
                    total_recv = 0
                    total_sent = 0
                    if isinstance(network_data, dict):
                        for interface, data in network_data.items():
                            if isinstance(data, dict):
                                total_recv += data.get('bytes_recv', 0)
                                total_sent += data.get('bytes_sent', 0)
                    
                    # Convert to MB/s (assuming 1 hour intervals, divide by 3600 and 1024*1024)
                    recv_mb = total_recv / (1024 * 1024) if total_recv > 0 else 0
                    sent_mb = total_sent / (1024 * 1024) if total_sent > 0 else 0
                    
                    if hour_key not in hourly_inbound:
                        hourly_inbound[hour_key] = []
                        hourly_outbound[hour_key] = []
                    hourly_inbound[hour_key].append(recv_mb)
                    hourly_outbound[hour_key].append(sent_mb)
                except:
                    pass
        
        data_points = []
        for hour in sorted(set(list(hourly_inbound.keys()) + list(hourly_outbound.keys()))):
            avg_inbound = sum(hourly_inbound.get(hour, [0])) / len(hourly_inbound.get(hour, [1]))
            avg_outbound = sum(hourly_outbound.get(hour, [0])) / len(hourly_outbound.get(hour, [1]))
            data_points.append({
                'timestamp': hour.isoformat(),
                'inbound': round(avg_inbound, 2),
                'outbound': round(avg_outbound, 2)
            })
        
        return JsonResponse({
            'success': True,
            'data': {
                'points': data_points
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_NETWORK_TREND_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_disk_io_summary_api(request):
    """API endpoint for disk I/O summary statistics"""
    try:
        server_id = request.GET.get('server_id', 'all')  # 'all' for average, or server ID
        
        # Filter by server if specified
        if server_id and server_id != 'all':
            try:
                servers = [Server.objects.get(id=int(server_id))]
            except (Server.DoesNotExist, ValueError):
                return JsonResponse({"success": False, "error": "Invalid server ID"}, status=400)
        else:
            # Get all servers for average
            servers = Server.objects.all()
        
        total_read_iops = 0
        total_write_iops = 0
        
        for server in servers:
            latest_metric = SystemMetric.objects.filter(server=server).order_by('-timestamp').first()
            if latest_metric:
                # Convert KB/s to IOPS (approximate: assume 4KB average I/O size)
                read_iops = (latest_metric.disk_io_read or 0) / 4 if latest_metric.disk_io_read else 0
                write_iops = (latest_metric.disk_io_write or 0) / 4 if latest_metric.disk_io_write else 0
                total_read_iops += read_iops
                total_write_iops += write_iops
        
        total_iops = total_read_iops + total_write_iops
        read_write_ratio = f"{int(total_read_iops / total_write_iops)}:1" if total_write_iops > 0 else "N/A"
        read_percentage = (total_read_iops / total_iops * 100) if total_iops > 0 else 0
        
        # Determine status message
        if total_iops < 1000:
            status_message = "Low I/O Load - Optimal performance"
            status_class = "success"
        elif total_iops < 5000:
            status_message = "Normal I/O Load"
            status_class = "info"
        else:
            status_message = "High I/O Load - Monitor closely"
            status_class = "warning"
        
        return JsonResponse({
            'success': True,
            'data': {
                'read_iops': round(total_read_iops),
                'write_iops': round(total_write_iops),
                'total_iops': round(total_iops),
                'read_write_ratio': read_write_ratio,
                'read_percentage': round(read_percentage, 1),
                'status_message': status_message,
                'status_class': status_class
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_DISK_IO_SUMMARY_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_top_cpu_consumers_api(request):
    """API endpoint for top 5 CPU consumers"""
    try:
        servers = Server.objects.all()
        server_cpu_data = []
        
        for server in servers:
            latest_metric = SystemMetric.objects.filter(server=server).order_by('-timestamp').first()
            if latest_metric and latest_metric.cpu_percent is not None:
                status = _calculate_server_status(server)
                cpu_percent = latest_metric.cpu_percent
                
                # Determine status tag
                if cpu_percent >= 85 or status == 'warning':
                    status_tag = 'critical'
                elif cpu_percent >= 70:
                    status_tag = 'warning'
                else:
                    status_tag = 'normal'
                
                server_cpu_data.append({
                    'server_name': server.name,
                    'server_id': server.id,
                    'cpu_percent': round(cpu_percent, 1),
                    'status_tag': status_tag
                })
        
        # Sort by CPU and take top 5
        top_5 = sorted(server_cpu_data, key=lambda x: x['cpu_percent'], reverse=True)[:5]
        
        return JsonResponse({
            'success': True,
            'data': top_5,
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_TOP_CPU_CONSUMERS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_top_memory_consumers_api(request):
    """API endpoint for top 5 memory consumers"""
    try:
        servers = Server.objects.all()
        server_memory_data = []
        
        for server in servers:
            latest_metric = SystemMetric.objects.filter(server=server).order_by('-timestamp').first()
            if latest_metric and latest_metric.memory_percent is not None:
                status = _calculate_server_status(server)
                memory_percent = latest_metric.memory_percent
                
                # Determine status tag
                if memory_percent >= 90 or status == 'warning':
                    status_tag = 'critical'
                elif memory_percent >= 80:
                    status_tag = 'warning'
                else:
                    status_tag = 'normal'
                
                server_memory_data.append({
                    'server_name': server.name,
                    'server_id': server.id,
                    'memory_percent': round(memory_percent, 1),
                    'status_tag': status_tag
                })
        
        # Sort by memory and take top 5
        top_5 = sorted(server_memory_data, key=lambda x: x['memory_percent'], reverse=True)[:5]
        
        return JsonResponse({
            'success': True,
            'data': top_5,
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_TOP_MEMORY_CONSUMERS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_health_status_api(request):
    """API endpoint for health status distribution"""
    try:
        servers = Server.objects.all()
        healthy_count = 0
        warning_count = 0
        critical_count = 0
        offline_count = 0
        
        for server in servers:
            status = _calculate_server_status(server)
            if status == 'online':
                healthy_count += 1
            elif status == 'warning':
                warning_count += 1
            elif status == 'offline':
                offline_count += 1
            else:
                critical_count += 1
        
        return JsonResponse({
            'success': True,
            'data': {
                'healthy': healthy_count,
                'warning': warning_count,
                'critical': critical_count,
                'offline': offline_count,
                'total': servers.count()
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_HEALTH_STATUS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_recent_alerts_api(request):
    """API endpoint for recent alert timeline (last 20 alerts)"""
    try:
        limit = int(request.GET.get('limit', 20))
        
        # Get recent alerts and anomalies
        alerts = AlertHistory.objects.select_related('server').order_by('-sent_at')[:limit]
        
        alert_list = []
        for alert in alerts:
            time_ago = timezone.now() - alert.sent_at
            if time_ago < timedelta(minutes=1):
                time_str = "just now"
            elif time_ago < timedelta(hours=1):
                minutes = int(time_ago.total_seconds() / 60)
                time_str = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            elif time_ago < timedelta(days=1):
                hours = int(time_ago.total_seconds() / 3600)
                time_str = f"{hours} hour{'s' if hours != 1 else ''} ago"
            else:
                days = int(time_ago.total_seconds() / 86400)
                time_str = f"{days} day{'s' if days != 1 else ''} ago"
            
            # Determine severity
            severity = 'critical' if alert.status == 'triggered' else 'resolved'
            
            alert_list.append({
                'id': alert.id,
                'title': f"{alert.alert_type} Alert",
                'host': alert.server.name,
                'description': alert.message,
                'timestamp': alert.sent_at.isoformat(),
                'time_ago': time_str,
                'severity': severity,
                'status': alert.status
            })
        
        return JsonResponse({
            'success': True,
            'data': alert_list,
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_RECENT_ALERTS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_ai_recommendations_api(request):
    """API endpoint for AI recommendations (rule-based)"""
    try:
        from .utils.recommendation_engine import generate_recommendations
        
        recommendations = generate_recommendations()
        
        return JsonResponse({
            'success': True,
            'data': recommendations,
            'timestamp': timezone.now().isoformat()
        })
    except ImportError:
        # If recommendation engine doesn't exist yet, return empty list
        return JsonResponse({
            'success': True,
            'data': [],
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_AI_RECOMMENDATIONS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_disk_forecast_api(request, server_id, mount_point):
    """API endpoint for 30-day disk space forecast"""
    try:
        from .utils.forecast_engine import forecast_disk_usage
        
        server = get_object_or_404(Server, id=server_id)
        forecast_data = forecast_disk_usage(server, mount_point)
        
        return JsonResponse({
            'success': True,
            'data': forecast_data,
            'timestamp': timezone.now().isoformat()
        })
    except ImportError:
        # If forecast engine doesn't exist yet, return placeholder
        return JsonResponse({
            'success': True,
            'data': {
                'current_usage': 0,
                'forecast': [],
                'warning': None
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_DISK_FORECAST_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_agent_versions_api(request):
    """API endpoint for agent version distribution"""
    try:
        # Get all agent versions
        from django.db.models import Count, Max
        versions = AgentVersion.objects.values('version').annotate(
            count=Count('id'),
            latest_seen=Max('last_seen')
        ).order_by('-latest_seen')
        
        total_agents = AgentVersion.objects.values('server').distinct().count()
        version_list = []
        
        for v in versions:
            version_list.append({
                'version': v['version'],
                'count': v['count'],
                'percentage': round((v['count'] / total_agents * 100) if total_agents > 0 else 0, 1)
            })
        
        # Determine latest version (most recent)
        latest_version = version_list[0]['version'] if version_list else None
        latest_count = version_list[0]['count'] if version_list else 0
        latest_percentage = (latest_count / total_agents * 100) if total_agents > 0 else 0
        
        return JsonResponse({
            'success': True,
            'data': {
                'versions': version_list,
                'total_agents': total_agents,
                'latest_version': latest_version,
                'latest_percentage': round(latest_percentage, 1)
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_AGENT_VERSIONS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_servers_list_api(request):
    """API endpoint for getting list of all servers (for dropdowns)"""
    try:
        servers = Server.objects.all().order_by('name')
        server_list = [{'id': s.id, 'name': s.name} for s in servers]
        
        return JsonResponse({
            'success': True,
            'data': {
                'servers': server_list
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_SERVERS_LIST_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_disk_mount_points_api(request, server_id):
    """API endpoint for getting disk mount points for a server"""
    try:
        import json
        server = get_object_or_404(Server, id=server_id)
        
        # Get latest metric with disk usage data
        latest_metric = SystemMetric.objects.filter(server=server).order_by('-timestamp').first()
        
        mount_points = []
        if latest_metric and latest_metric.disk_usage:
            try:
                disk_data = json.loads(latest_metric.disk_usage) if isinstance(latest_metric.disk_usage, str) else latest_metric.disk_usage
                if isinstance(disk_data, dict):
                    mount_points = list(disk_data.keys())
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        
        # If no mount points found, return common defaults
        if not mount_points:
            mount_points = ['/']
        
        return JsonResponse({
            'success': True,
            'data': {
                'mount_points': mount_points
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_DISK_MOUNT_POINTS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_login_activity_api(request):
    """API endpoint for recent login activity"""
    try:
        limit = int(request.GET.get('limit', 10))
        
        login_activities = LoginActivity.objects.select_related('user').order_by('-timestamp')[:limit]
        
        activity_list = []
        for activity in login_activities:
            time_ago = timezone.now() - activity.timestamp
            if time_ago < timedelta(minutes=1):
                time_str = "just now"
            elif time_ago < timedelta(hours=1):
                minutes = int(time_ago.total_seconds() / 60)
                time_str = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            elif time_ago < timedelta(days=1):
                hours = int(time_ago.total_seconds() / 3600)
                time_str = f"{hours} hour{'s' if hours != 1 else ''} ago"
            else:
                days = int(time_ago.total_seconds() / 86400)
                time_str = f"{days} day{'s' if days != 1 else ''} ago"
            
            activity_list.append({
                'id': activity.id,
                'email': activity.email,
                'location': activity.location or 'Unknown',
                'status': activity.status,
                'timestamp': activity.timestamp.isoformat(),
                'time_ago': time_str,
                'ip_address': str(activity.ip_address)
            })
        
        return JsonResponse({
            'success': True,
            'data': activity_list,
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_LOGIN_ACTIVITY_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
def log_troubleshooting_config(request):
    """
    Log troubleshooting configuration page.
    Allows users to configure log monitoring for servers and services.
    """
    servers = Server.objects.all().order_by('name')
    monitored_logs = MonitoredLog.objects.all().select_related('server').order_by('-id')
    
    # Get service choices
    service_choices = MonitoredLog.ServiceChoices.choices
    
    context = {
        'servers': servers,
        'monitored_logs': monitored_logs,
        'service_choices': service_choices,
        'show_sidebar': True,
    }
    context.update(admin.site.each_context(request))
    
    return render(request, 'core/log_troubleshooting_config.html', context)


@staff_member_required
@require_http_methods(["POST"])
def log_troubleshooting_add(request):
    """API endpoint to add/configure log troubleshooting"""
    try:
        import json
        data = json.loads(request.body)
        
        server_id = data.get('server_id')
        service_type = data.get('service_type')
        log_path = data.get('log_path', '').strip()
        application_name = data.get('application_name', '').strip()
        enabled = data.get('enabled', True)
        
        if not server_id:
            return JsonResponse({'success': False, 'error': 'Server is required'}, status=400)
        
        server = get_object_or_404(Server, id=server_id)
        
        # Determine log path based on service type
        if not log_path:
            defaults = {
                'apache': '/var/log/apache2/error.log',
                'nginx': '/var/log/nginx/error.log',
                'exim': '/var/log/exim4/mainlog',
                'postfix': '/var/log/mail.log',
                'mysql': '/var/log/mysql/error.log',
                'mariadb': '/var/log/mysql/error.log',
            }
            log_path = defaults.get(service_type, '')
        
        if not log_path:
            return JsonResponse({'success': False, 'error': 'Log path is required'}, status=400)
        
        # Determine parser type based on service
        parser_map = {
            'apache': MonitoredLog.ParserChoices.APACHE_ERROR,
            'nginx': MonitoredLog.ParserChoices.NGINX_ERROR,
            'exim': MonitoredLog.ParserChoices.EXIM_ERROR,
            'postfix': MonitoredLog.ParserChoices.POSTFIX_ERROR,
            'mysql': MonitoredLog.ParserChoices.MYSQL_ERROR,
            'mariadb': MonitoredLog.ParserChoices.MARIADB_ERROR,
            'custom': MonitoredLog.ParserChoices.CUSTOM_APP,
        }
        parser_type = parser_map.get(service_type, MonitoredLog.ParserChoices.GENERIC_ERROR)
        
        # Default application name
        if not application_name:
            application_name = service_type.upper() if service_type != 'custom' else 'Custom App'
        
        # Create or update MonitoredLog
        monitored_log, created = MonitoredLog.objects.update_or_create(
            server=server,
            log_path=log_path,
            defaults={
                'application_name': application_name,
                'service_type': service_type,
                'parser_type': parser_type,
                'enabled': enabled,
            }
        )
        
        _log_user_action(request, "LOG_TROUBLESHOOTING_ADD", 
                        f"{'Created' if created else 'Updated'} log monitoring: {server.name} - {log_path}")
        
        return JsonResponse({
            'success': True,
            'message': f"Log troubleshooting {'configured' if created else 'updated'} successfully",
            'monitored_log_id': monitored_log.id
        })
    except Exception as e:
        error_logger.error(f"LOG_TROUBLESHOOTING_ADD error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def log_troubleshooting_results(request):
    """
    Display log troubleshooting results - detected errors and issues
    """
    # Get all log events grouped by monitored log
    log_events = LogEvent.objects.select_related(
        'monitored_log', 
        'monitored_log__server'
    ).order_by('-last_seen', '-event_count')
    
    # Filter by server if provided
    server_id = request.GET.get('server_id')
    if server_id:
        log_events = log_events.filter(monitored_log__server_id=server_id)
    
    # Get events with solutions from AnalysisRule
    events_with_solutions = []
    for event in log_events[:100]:  # Limit to 100 most recent
        # Check if there's an AnalysisRule that matches this log message
        matching_rule = AnalysisRule.objects.filter(
            pattern_to_match__icontains=event.message[:100]  # Match on first 100 chars
        ).first()
        
        events_with_solutions.append({
            'event': event,
            'solution': matching_rule.solution if matching_rule else None,
            'rule': matching_rule,
        })
    
    context = {
        'events_with_solutions': events_with_solutions,
        'total_events': log_events.count(),
        'show_sidebar': True,
    }
    context.update(admin.site.each_context(request))
    
    return render(request, 'core/log_troubleshooting_results.html', context)


@staff_member_required
@require_http_methods(["POST"])
def log_troubleshooting_create_solution(request):
    """API endpoint to create AnalysisRule solution from log error"""
    try:
        import json
        data = json.loads(request.body)
        
        event_id = data.get('event_id')
        pattern = data.get('pattern', '').strip()
        name = data.get('name', '').strip()
        explanation = data.get('explanation', '').strip()
        solution = data.get('solution', '').strip()
        
        if not all([event_id, pattern, name, explanation, solution]):
            return JsonResponse({'success': False, 'error': 'All fields are required'}, status=400)
        
        # Get the log event
        try:
            log_event = LogEvent.objects.get(id=event_id)
        except LogEvent.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Log event not found'}, status=404)
        
        # Create AnalysisRule
        analysis_rule = AnalysisRule.objects.create(
            name=name,
            pattern_to_match=pattern,
            explanation=explanation,
            solution=solution,
            recommendation=AnalysisRule.ActionType.INVESTIGATE,
            llm_generated=False
        )
        
        _log_user_action(request, "LOG_TROUBLESHOOTING_CREATE_SOLUTION", 
                        f"Created solution for log event {event_id}: {name}")
        
        return JsonResponse({
            'success': True,
            'message': 'Solution created successfully',
            'rule_id': analysis_rule.id
        })
    except Exception as e:
        error_logger.error(f"LOG_TROUBLESHOOTING_CREATE_SOLUTION error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
def help_docs(request):
    """
    Help documentation page.
    Provides non-technical feature descriptions and usage guides.
    """
    return render(request, 'core/help_docs.html', {
        'show_sidebar': True
    })


def custom_logout(request):
    """
    Custom logout view that redirects to login page.
    """
    auth_logout(request)
    return HttpResponseRedirect('/admin/login/')


# RBAC Views
@staff_member_required
def role_management(request):
    """
    Role management page for Root Admin - list and manage roles
    """
    from .utils import has_privilege

    if not has_privilege(request.user, 'manage_roles'):
        messages.error(request, "You don't have permission to manage roles.")
        return redirect('monitoring_dashboard')

    from .models import Role, Privilege, UserACL

    roles = Role.objects.all().prefetch_related('role_privileges__privilege')

    # Add user count for each role
    roles_with_counts = []
    for role in roles:
        user_count = UserACL.objects.filter(role=role).count()
        role.user_count = user_count
        roles_with_counts.append(role)

    privileges = Privilege.objects.all().order_by('key')

    context = {
        'roles': roles_with_counts,
        'privileges': privileges,
        'can_manage_roles': has_privilege(request.user, 'manage_roles'),
        'show_sidebar': True,
    }
    return render(request, 'core/role_management.html', context)


@staff_member_required
def create_role(request):
    """
    Create a new role
    """
    from .utils import has_privilege

    if not has_privilege(request.user, 'manage_roles'):
        messages.error(request, "You don't have permission to manage roles.")
        return redirect('monitoring_dashboard')

    from .models import Role, Privilege, RolePrivilege

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        privilege_ids = request.POST.getlist('privileges')

        if not name:
            messages.error(request, "Role name is required.")
            return redirect('create_role')

        if Role.objects.filter(name=name).exists():
            messages.error(request, "A role with this name already exists.")
            return redirect('create_role')

        # Create role
        role = Role.objects.create(
            name=name,
            description=description
        )

        # Assign privileges
        for priv_id in privilege_ids:
            try:
                privilege = Privilege.objects.get(id=priv_id)
                RolePrivilege.objects.create(role=role, privilege=privilege)
            except Privilege.DoesNotExist:
                continue

        messages.success(request, f"Role '{name}' created successfully.")
        return redirect('role_management')

    privileges = Privilege.objects.all().order_by('key')
    return render(request, 'core/create_role.html', {
        'privileges': privileges,
        'show_sidebar': True
    })


@staff_member_required
def edit_role(request, role_id):
    """
    Edit an existing role
    """
    from .utils import has_privilege

    if not has_privilege(request.user, 'manage_roles'):
        messages.error(request, "You don't have permission to manage roles.")
        return redirect('monitoring_dashboard')

    from .models import Role, Privilege, RolePrivilege

    role = get_object_or_404(Role, id=role_id)

    # Don't allow editing protected roles
    if role.is_protected:
        messages.error(request, "Protected roles cannot be edited.")
        return redirect('role_management')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        privilege_ids = request.POST.getlist('privileges')

        if not name:
            messages.error(request, "Role name is required.")
            return redirect('edit_role', role_id=role_id)

        # Check if name conflicts with another role
        if Role.objects.filter(name=name).exclude(id=role_id).exists():
            messages.error(request, "A role with this name already exists.")
            return redirect('edit_role', role_id=role_id)

        # Update role
        role.name = name
        role.description = description
        role.save()

        # Clear existing privileges
        RolePrivilege.objects.filter(role=role).delete()

        # Assign new privileges
        for priv_id in privilege_ids:
            try:
                privilege = Privilege.objects.get(id=priv_id)
                RolePrivilege.objects.create(role=role, privilege=privilege)
            except Privilege.DoesNotExist:
                continue

        messages.success(request, f"Role '{name}' updated successfully.")
        return redirect('role_management')

    privileges = Privilege.objects.all().order_by('key')
    role_privilege_ids = set(role.role_privileges.values_list('privilege_id', flat=True))

    context = {
        'role': role,
        'privileges': privileges,
        'role_privilege_ids': role_privilege_ids,
        'show_sidebar': True,
    }
    return render(request, 'core/edit_role.html', context)


@staff_member_required
def delete_role(request, role_id):
    """
    Delete a role
    """
    from .utils import has_privilege

    if not has_privilege(request.user, 'manage_roles'):
        messages.error(request, "You don't have permission to manage roles.")
        return redirect('monitoring_dashboard')

    from .models import Role, UserACL

    role = get_object_or_404(Role, id=role_id)

    # Don't allow deleting protected roles
    if role.is_protected:
        messages.error(request, "Protected roles cannot be deleted.")
        return redirect('role_management')

    # Check if role is assigned to any users
    if UserACL.objects.filter(role=role).exists():
        messages.error(request, f"Cannot delete role '{role.name}' because it is assigned to users. Please reassign users to other roles first.")
        return redirect('role_management')

    role_name = role.name
    role.delete()
    messages.success(request, f"Role '{role_name}' deleted successfully.")
    return redirect('role_management')


def demo_dashboard(request):
    """
    Demo dashboard showcasing IBM Carbon Design System components
    """
    # Return the IBM Carbon demo page content directly
    from django.http import HttpResponse

    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IBM Carbon Design System Demo - StackSense</title>

    <!-- IBM Carbon Design System -->
    <link rel="stylesheet" href="https://unpkg.com/carbon-components@10.58.0/css/carbon-components.min.css">
    <link rel="stylesheet" href="https://unpkg.com/@carbon/icons@10.49.2/css/carbon-icons.min.css">
    <script src="https://unpkg.com/carbon-components@10.58.0/scripts/carbon-components.min.js"></script>

    <style>
        /* IBM Carbon Design System CSS Variables from design.json */
        :root {
            --cds-color-blue-50: #f0f9ff;
            --cds-color-blue-60: #0ea5e9;
            --cds-color-blue-70: #0369a1;
            --cds-color-gray-10: #f8fafc;
            --cds-color-gray-20: #e2e8f0;
            --cds-color-gray-50: #64748b;
            --cds-color-gray-70: #334155;
            --cds-color-gray-100: #0f172a;
            --cds-color-green-50: #f0fdf4;
            --cds-color-green-60: #22c55e;
            --cds-color-yellow-50: #fffbeb;
            --cds-color-yellow-60: #f59e0b;
            --cds-color-red-50: #fef2f2;
            --cds-color-red-60: #ef4444;
            --cds-color-white: #ffffff;
            --cds-font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }

        body {
            font-family: var(--cds-font-family);
            margin: 0;
            padding: 0;
            background-color: var(--cds-color-gray-10);
        }

        .demo-header {
            background: linear-gradient(135deg, var(--cds-color-blue-50) 0%, var(--cds-color-white) 100%);
            border-bottom: 1px solid var(--cds-color-gray-20);
            padding: 3rem 0;
            text-align: center;
        }

        .demo-container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 0 1rem;
        }

        .demo-title {
            font-size: 2.25rem;
            font-weight: 700;
            color: var(--cds-color-gray-100);
            margin-bottom: 1rem;
        }

        .demo-subtitle {
            font-size: 1.125rem;
            color: var(--cds-color-gray-70);
            margin-bottom: 2rem;
            max-width: 600px;
            margin-left: auto;
            margin-right: auto;
        }

        .demo-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1.5rem;
            margin-bottom: 3rem;
        }

        .demo-section {
            margin-bottom: 3rem;
        }

        .demo-section-title {
            font-size: 1.5rem;
            font-weight: 600;
            color: var(--cds-color-gray-100);
            margin-bottom: 1.5rem;
        }

        .demo-actions {
            display: flex;
            gap: 1rem;
            justify-content: center;
            margin-top: 2rem;
        }

        .demo-footer {
            background: var(--cds-color-gray-10);
            border-top: 1px solid var(--cds-color-gray-20);
            padding: 2rem 0;
            margin-top: 3rem;
            text-align: center;
        }

        @media (max-width: 768px) {
            .demo-stats {
                grid-template-columns: 1fr;
            }
            .demo-actions {
                flex-direction: column;
                align-items: center;
            }
        }
    </style>
</head>
<body>
    <!-- Header Section -->
    <header class="demo-header">
        <div class="demo-container">
            <h1 class="demo-title">✅ IBM Carbon Design System Demo</h1>
            <p class="demo-subtitle">
                Professional monitoring interface built with IBM Carbon components. Clean, accessible, and enterprise-grade design system.
            </p>
            <div class="demo-actions">
                <button class="bx--btn bx--btn--primary">Explore Components</button>
                <button class="bx--btn bx--btn--secondary">View Guidelines</button>
            </div>
        </div>
    </header>

    <div class="demo-container">
        <!-- Statistics Cards -->
        <div class="demo-stats">
            <div class="bx--tile">
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <div>
                        <h4 style="margin: 0; font-size: 2rem; font-weight: 600; color: #0ea5e9;">8</h4>
                        <p style="margin: 0.5rem 0 0 0; color: #64748b;">Active Servers</p>
                    </div>
                    <svg width="32" height="32" fill="#0ea5e9" viewBox="0 0 24 24">
                        <path d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01"/>
                    </svg>
                </div>
            </div>

            <div class="bx--tile">
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <div>
                        <h4 style="margin: 0; font-size: 2rem; font-weight: 600; color: #22c55e;">156</h4>
                        <p style="margin: 0.5rem 0 0 0; color: #64748b;">Total Metrics</p>
                    </div>
                    <svg width="32" height="32" fill="#22c55e" viewBox="0 0 24 24">
                        <path d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>
                    </svg>
                </div>
            </div>

            <div class="bx--tile">
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <div>
                        <h4 style="margin: 0; font-size: 2rem; font-weight: 600; color: #ef4444;">3</h4>
                        <p style="margin: 0.5rem 0 0 0; color: #64748b;">Active Alerts</p>
                    </div>
                    <svg width="32" height="32" fill="#ef4444" viewBox="0 0 24 24">
                        <path d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/>
                    </svg>
                </div>
            </div>

            <div class="bx--tile">
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <div>
                        <h4 style="margin: 0; font-size: 2rem; font-weight: 600; color: #f59e0b;">98.5%</h4>
                        <p style="margin: 0.5rem 0 0 0; color: #64748b;">System Uptime</p>
                    </div>
                    <svg width="32" height="32" fill="#f59e0b" viewBox="0 0 24 24">
                        <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
                    </svg>
                </div>
            </div>
        </div>

        <!-- Color Palette Section -->
        <div class="demo-section">
            <h2 class="demo-section-title">🎨 IBM Carbon Color Palette</h2>
            <p>Enterprise-grade color system with semantic meanings and accessibility compliance.</p>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1rem;">
                <div class="bx--tile" style="background: #0ea5e9; color: white; border: none; text-align: center; padding: 1rem;">
                    <strong>Primary Blue</strong><br>
                    <small>#0ea5e9</small>
                </div>
                <div class="bx--tile" style="background: #22c55e; color: white; border: none; text-align: center; padding: 1rem;">
                    <strong>Success Green</strong><br>
                    <small>#22c55e</small>
                </div>
                <div class="bx--tile" style="background: #f59e0b; color: white; border: none; text-align: center; padding: 1rem;">
                    <strong>Warning Amber</strong><br>
                    <small>#f59e0b</small>
                </div>
                <div class="bx--tile" style="background: #ef4444; color: white; border: none; text-align: center; padding: 1rem;">
                    <strong>Danger Red</strong><br>
                    <small>#ef4444</small>
                </div>
                <div class="bx--tile" style="background: #64748b; color: white; border: none; text-align: center; padding: 1rem;">
                    <strong>Secondary Gray</strong><br>
                    <small>#64748b</small>
                </div>
                <div class="bx--tile" style="background: #ffffff; color: #000; border: 1px solid #e2e8f0; text-align: center; padding: 1rem;">
                    <strong>White</strong><br>
                    <small>#ffffff</small>
                </div>
            </div>
        </div>

        <!-- Components Section -->
        <div class="demo-section">
            <h2 class="demo-section-title">🧩 IBM Carbon Components</h2>
            <p>Professional UI components designed for enterprise applications.</p>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem;">
                <!-- Buttons -->
                <div class="bx--tile">
                    <h4>Action Buttons</h4>
                    <p>Primary actions and secondary options</p>
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                        <button class="bx--btn bx--btn--primary bx--btn--sm">Primary Action</button>
                        <button class="bx--btn bx--btn--secondary bx--btn--sm">Secondary</button>
                        <button class="bx--btn bx--btn--ghost bx--btn--sm">Ghost Button</button>
                        <button class="bx--btn bx--btn--danger bx--btn--sm">Danger</button>
                    </div>
                </div>

                <!-- Form Elements -->
                <div class="bx--tile">
                    <h4>Form Controls</h4>
                    <p>Accessible input components</p>
                    <div class="bx--form-item" style="margin-bottom: 1rem;">
                        <label class="bx--label">Server Name</label>
                        <input type="text" class="bx--text-input" placeholder="web-server-01">
                    </div>
                    <div class="bx--form-item">
                        <div class="bx--checkbox-wrapper">
                            <input type="checkbox" class="bx--checkbox" id="monitoring-enabled" checked>
                            <label class="bx--checkbox-label" for="monitoring-enabled">Enable monitoring</label>
                        </div>
                    </div>
                </div>

                <!-- Status Indicators -->
                <div class="bx--tile">
                    <h4>Status Tags</h4>
                    <p>Semantic status indicators</p>
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                        <span class="bx--tag bx--tag--green">Operational</span>
                        <span class="bx--tag bx--tag--yellow">Warning</span>
                        <span class="bx--tag bx--tag--red">Critical</span>
                        <span class="bx--tag bx--tag--gray">Maintenance</span>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Footer -->
    <footer class="demo-footer">
        <div class="demo-container">
            <h3 style="margin-bottom: 1rem; color: #0f172a;">🎨 IBM Carbon Design System</h3>
            <p style="color: #64748b; margin-bottom: 2rem;">
                Enterprise-grade design system powering StackSense monitoring platform.
                Built with accessibility, scalability, and professional aesthetics in mind.
            </p>
            <div class="demo-actions">
                <a href="/monitoring/" class="bx--btn bx--btn--secondary">← Back to Dashboard</a>
                <button class="bx--btn bx--btn--primary">🔄 Refresh Demo</button>
            </div>
        </div>
    </footer>

    <script>
        // Initialize Carbon Components
        document.addEventListener('DOMContentLoaded', function() {
            console.log('✅ IBM Carbon Design System Demo Loaded Successfully!');
        });
    </script>
</body>
</html>"""

    return HttpResponse(html_content, content_type='text/html')
    context = {
        'page_title': 'Design System Demo',
        'demo_data': {
            'server_count': 8,
            'active_alerts': 3,
            'total_metrics': 156,
            'uptime_percentage': 98.5,
        },
        'sample_servers': [
            {'name': 'Web Server 01', 'status': 'online', 'cpu': 45, 'memory': 67},
            {'name': 'Database 01', 'status': 'warning', 'cpu': 78, 'memory': 82},
            {'name': 'API Gateway', 'status': 'online', 'cpu': 23, 'memory': 34},
            {'name': 'Cache Server', 'status': 'offline', 'cpu': 0, 'memory': 0},
        ],
        'alerts': [
            {'type': 'warning', 'message': 'High CPU usage on Database 01', 'time': '2 min ago'},
            {'type': 'danger', 'message': 'API Gateway connection timeout', 'time': '5 min ago'},
            {'type': 'info', 'message': 'Scheduled maintenance completed', 'time': '1 hour ago'},
        ],
        'show_sidebar': True,
    }
    return render(request, 'core/demo_dashboard.html', context)
