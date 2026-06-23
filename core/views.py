from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.contrib.auth import logout as auth_logout
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from .models import Server, SystemMetric, Anomaly, MonitoringConfig, Service, EmailAlertConfig, SlackAlertConfig, AlertHistory, UserACL, ServerHeartbeat, AgentVersion, LoginActivity, AgentCredential, SyntheticCheck, SyntheticCheckResult, SecurityEvent, SecurityMonitorConfig, BusinessKPI, BusinessKPIValue, BusinessMonitorConfig, Container
from .service_latency import measure_service_latency
from . import alert_categories
from . import alert_routing
from .mount_filters import is_ephemeral_mount, primary_mount, primary_disk_percent
from .port_roles import role_for_port
from django.http import JsonResponse, HttpResponseRedirect
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_exempt
from django.views.decorators.http import require_http_methods
from core.utils import convert_to_display_timezone
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
    - ONLINE: Heartbeat within threshold AND no active alerts AND monitoring not suspended
    - WARNING: Heartbeat within threshold BUT has active alerts AND monitoring not suspended
    (Anomalies are notifications, not health -- they do NOT affect this status.)
    
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
    
    # Determine adaptive threshold based on app downtime.
    # The agent pushes ~every 30s, but a single FAILED push cycle (3 retries x 15s timeout
    # + delays) takes ~55s with no heartbeat -- so a tight 60s threshold flapped a healthy
    # server "offline" on one transient blip (server load spike / cross-region network
    # hiccup). Default to 180s so a few consecutive missed pushes are tolerated while a
    # real outage still surfaces within ~3 min. Operator-tunable via settings.
    base_threshold = int(getattr(settings, "OFFLINE_THRESHOLD_SECONDS", 180))
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
        
        # Server is online (heartbeat OK) - check for alerts.
        # Status reflects the server's own health only. Anomalies are NOT health/alerts
        # -- they're notifications surfaced on the dashboard anomalies icon -- so they no
        # longer make a server "warning". SERVICE/CONTAINER alerts show under Services.
        active_alerts = (AlertHistory.objects
                         .filter(server=server, status="triggered")
                         .exclude(alert_type__in=[AlertHistory.AlertType.SERVICE, AlertHistory.AlertType.CONTAINER])
                         .exists())

        if active_alerts:
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
                    disk_percent = primary_disk_percent(disk_data)
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
                "timestamp": convert_to_display_timezone(latest_metric.timestamp).isoformat() if latest_metric.timestamp else None,
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
        "timestamp": convert_to_display_timezone(heartbeat.last_heartbeat).isoformat() if heartbeat.last_heartbeat else None,
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
            # Convert UTC timestamp to display timezone
            if metric.timestamp:
                display_timestamp = convert_to_display_timezone(metric.timestamp)
                timestamps.append(display_timestamp.isoformat() if display_timestamp else metric.timestamp.isoformat())
            else:
                timestamps.append(None)
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
            # Convert UTC timestamp to display timezone
            if anomaly.timestamp:
                display_timestamp = convert_to_display_timezone(anomaly.timestamp)
                timestamp_str = display_timestamp.isoformat() if display_timestamp else anomaly.timestamp.isoformat()
            else:
                timestamp_str = None
            anomaly_points.append({
                "timestamp": timestamp_str,
                "metric_name": anomaly.metric_name,
                "metric_type": anomaly.metric_type,
                "severity": anomaly.severity,
                "metric_value": float(anomaly.metric_value) if anomaly.metric_value is not None else 0.0,
            })
        
        # Get timezone info for client-side conversion
        from core.utils import get_display_timezone
        display_tz = get_display_timezone()
        
        # Build response
        response_data = {
            "timestamps": timestamps,
            "cpu": cpu_values,
            "memory": memory_values,
            "disk": disk_values,
            "anomalies": anomaly_points,
            "timezone": display_tz,  # Include timezone info for client
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
    # Unresolved anomalies drive the active-issues banner (same notion the dashboard
    # counts as "active alerts": triggered AlertHistory + unresolved Anomaly).
    active_anomalies = Anomaly.objects.filter(server=server, resolved=False).order_by("-timestamp")
    active_anomaly_count = active_anomalies.count()

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
        # Primary partition disk percent (root "/" on Linux, "C:\\" on Windows, ...).
        disk_percent = primary_disk_percent(disk_data)

    # Physical disk inventory (SSD/HDD/NVMe, RAID, disk count) pushed by the agent
    disk_hardware = {}
    if latest_metric and getattr(latest_metric, "disk_hardware", None):
        import json
        disk_hardware = (
            json.loads(latest_metric.disk_hardware)
            if isinstance(latest_metric.disk_hardware, str)
            else latest_metric.disk_hardware
        ) or {}

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

    # Prefer the agent's authoritative hardware inventory for the counts/badges.
    hw_disks = disk_hardware.get("disks") if isinstance(disk_hardware, dict) else None
    if hw_disks:
        disk_summary["total_disks"] = disk_hardware.get("physical_disk_count", len(hw_disks))
        disk_summary["ssd_count"] = sum(1 for d in hw_disks if d.get("type") == "SSD")
        disk_summary["hdd_count"] = sum(1 for d in hw_disks if d.get("type") == "HDD")
        disk_summary["nvme_count"] = sum(1 for d in hw_disks if d.get("type") == "NVMe")
        disk_summary["raid_count"] = len(disk_hardware.get("raid_arrays") or [])

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
        
        # Disk percent of the server's primary partition (root "/" on Linux, "C:\\" on
        # Windows, ...). Loop-local var so we don't clobber the `disk_percent` context
        # value computed from the latest metric above.
        point_disk = 0
        if metric.disk_usage:
            try:
                disk_info = (
                    json.loads(metric.disk_usage)
                    if isinstance(metric.disk_usage, str)
                    else metric.disk_usage
                )
                point_disk = primary_disk_percent(disk_info)
            except Exception:
                pass
        chart_data["disk"].append(point_disk)
    
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
    
    # Monitored disks from config; default to the server's primary drive (root "/" on
    # Linux, "C:\\" on Windows) so a fresh server's disk card isn't blank.
    if hasattr(server, 'monitoring_config') and server.monitoring_config.monitored_disks:
        monitored_disks = server.monitoring_config.monitored_disks
    else:
        monitored_disks = [primary_mount(disk_data) or "/"]
    
    # Get monitored services for latency display (services with monitoring_enabled=True)
    monitored_services = all_services.filter(monitoring_enabled=True)

    # Service Status panel: same data + notable/background split as the Services
    # page (services_overview), but scoped to this one server. Reads the agent-
    # pushed Service rows straight from the DB — no SSH — so the two pages can
    # never disagree, and the monitoring toggle hits the same shared endpoint.
    panel_services = list(all_services)
    notable_services = [s for s in panel_services if not _is_background_service(s)]
    background_services = [s for s in panel_services if _is_background_service(s)]
    services_monitored_count = sum(1 for s in panel_services if s.monitoring_enabled)

    # Containers panel: same data + toggle as the Containers page, this server
    # only. Reads the agent-pushed Container rows from the DB; shared endpoint.
    server_containers = list(server.containers.all())
    containers_monitored_count = sum(1 for c in server_containers if c.monitoring_enabled)

    context = {
        "server": server,
        "server_status": server_status,
        "latest_metric": latest_metric,
        "recent_metrics": recent_metrics,
        "all_services": all_services,
        "monitored_services": monitored_services,
        "notable_services": notable_services,
        "background_services": background_services,
        "services_monitored_count": services_monitored_count,
        "server_containers": server_containers,
        "containers_monitored_count": containers_monitored_count,
        "recent_anomalies": recent_anomalies,
        "active_anomalies": active_anomalies,
        "active_anomaly_count": active_anomaly_count,
        "disk_summary": disk_summary,
        "disk_hardware": disk_hardware,
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
    """Server list view. Viewing is read-only (VIEW_OPERATIONS, allowed for the support
    Operator); the per-row edit/suspend/delete actions require manage_monitoring and are
    shown disabled for users without it (and enforced server-side by the RBAC middleware)."""
    servers = Server.objects.all().select_related("monitoring_config").order_by("name")
    import json
    from django.core.cache import cache
    from datetime import datetime, timedelta

    # Calculate server status based on heartbeat and get metrics
    servers_with_data = []
    for server in servers:
        server.status = _calculate_server_status(server)
        
        # Get last heartbeat timestamp if available
        try:
            from .models import ServerHeartbeat
            heartbeat = ServerHeartbeat.objects.filter(server=server).first()
            server.last_checkin = heartbeat.last_heartbeat if heartbeat else None
        except:
            server.last_checkin = None
        
        # Get latest metrics
        server.cpu_percent = None
        server.memory_percent = None
        server.memory_used = None
        server.memory_total = None
        server.disk_percent = None
        server.network_sent = None
        server.network_recv = None
        server.uptime_seconds = None
        server.agent_installed = False
        
        # Try Redis first
        redis_key = f"metrics:{server.id}:latest"
        cached_metric = cache.get(redis_key)
        
        if cached_metric:
            try:
                metric = json.loads(cached_metric) if isinstance(cached_metric, str) else cached_metric
                server.cpu_percent = metric.get("cpu_percent")
                server.memory_percent = metric.get("memory_percent")
                # Convert bytes to GB
                memory_used_bytes = metric.get("memory_used", 0)
                memory_total_bytes = metric.get("memory_total", 0)
                server.memory_used = memory_used_bytes / (1024 ** 3) if memory_used_bytes else None
                server.memory_total = memory_total_bytes / (1024 ** 3) if memory_total_bytes else None
                server.disk_percent = metric.get("disk_percent")
                server.network_sent = metric.get("net_io_sent", 0) / (1024 * 1024)  # Convert to MB/s
                server.network_recv = metric.get("net_io_recv", 0) / (1024 * 1024)  # Convert to MB/s
                server.uptime_seconds = metric.get("system_uptime_seconds")
            except:
                pass
        
        # Fallback to PostgreSQL
        if server.cpu_percent is None:
            latest_metric = SystemMetric.objects.filter(server=server).order_by("-timestamp").first()
            if latest_metric:
                server.cpu_percent = latest_metric.cpu_percent
                server.memory_percent = latest_metric.memory_percent
                # Convert bytes to GB
                memory_used_bytes = latest_metric.memory_used or 0
                memory_total_bytes = latest_metric.memory_total or 0
                server.memory_used = memory_used_bytes / (1024 ** 3) if memory_used_bytes else None
                server.memory_total = memory_total_bytes / (1024 ** 3) if memory_total_bytes else None
                server.uptime_seconds = latest_metric.system_uptime_seconds
                
                # Calculate disk percent from root partition
                if latest_metric.disk_usage:
                    try:
                        disk_data = json.loads(latest_metric.disk_usage) if isinstance(latest_metric.disk_usage, str) else latest_metric.disk_usage
                        server.disk_percent = primary_disk_percent(disk_data)
                    except:
                        pass
                
                # Network I/O
                if latest_metric.net_io_sent:
                    server.network_sent = latest_metric.net_io_sent / (1024 * 1024)  # MB/s
                if latest_metric.net_io_recv:
                    server.network_recv = latest_metric.net_io_recv / (1024 * 1024)  # MB/s
        
        # Check if agent is installed (has recent metrics or heartbeat)
        server.agent_installed = server.last_checkin is not None or server.cpu_percent is not None
        
        # Get alert and monitoring suppression status
        try:
            from .models import MonitoringConfig
            config = MonitoringConfig.objects.filter(server=server).first()
            server.alert_suppressed = config.alert_suppressed if config else False
            server.monitoring_suspended = config.monitoring_suspended if config else False
        except:
            server.alert_suppressed = False
            server.monitoring_suspended = False

        # Why is it in warning? Count active alerts + unresolved anomalies (drives the card tags).
        # Exclude SERVICE alerts here too -- service health is shown under Services, not the server.
        try:
            server.active_alerts = (AlertHistory.objects
                                    .filter(server=server, status="triggered")
                                    .exclude(alert_type__in=[AlertHistory.AlertType.SERVICE, AlertHistory.AlertType.CONTAINER])
                                    .count())
            server.active_anomalies = Anomaly.objects.filter(server=server, resolved=False).count()
        except Exception:
            server.active_alerts = 0
            server.active_anomalies = 0
        
        # Format uptime
        if server.uptime_seconds:
            days = int(server.uptime_seconds // 86400)
            hours = int((server.uptime_seconds % 86400) // 3600)
            minutes = int((server.uptime_seconds % 3600) // 60)
            if days > 0:
                server.uptime_display = f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                server.uptime_display = f"{hours}h {minutes}m"
            else:
                server.uptime_display = f"{minutes}m"
        else:
            server.uptime_display = "0m"
        
        servers_with_data.append(server)

    context = {
        'servers': servers_with_data,
        'show_sidebar': True,
    }
    return render(request, "core/server_list.html", context)

@staff_member_required
def edit_server(request, server_id):
    """Edit server configuration"""
    from .utils import has_privilege

    if not has_privilege(request.user, 'manage_monitoring'):
        messages.error(request, "You don't have permission to edit servers.")
        return redirect('server_list')

    server = get_object_or_404(Server, id=server_id)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        ip_address = request.POST.get('ip_address', '').strip()

        if not name or not ip_address:
            messages.error(request, "Server name and IP address are required.")
            return redirect('edit_server', server_id=server_id)

        server.name = name
        server.ip_address = ip_address
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

    if not has_privilege(request.user, 'manage_monitoring'):
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
                    # Primary partition ("/" on Linux, "C:\\" on Windows), else first.
                    disk_percent = primary_disk_percent(disk_data)
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

    # Top disk-usage partitions across all servers (for the Disk Usage widget)
    import json as _json
    disk_parts = []
    for srv in servers:
        lm = (SystemMetric.objects.filter(server=srv)
              .only("disk_usage", "timestamp").order_by("-timestamp").first())
        if not lm or not lm.disk_usage:
            continue
        du = lm.disk_usage
        if isinstance(du, str):
            try:
                du = _json.loads(du)
            except (ValueError, TypeError):
                continue
        if not isinstance(du, dict):
            continue
        for mount, info in du.items():
            if not isinstance(info, dict):
                continue
            # Skip boot and temp partitions
            if mount in ("/boot", "/boot/efi", "/tmp", "/var/tmp") or mount.startswith("/boot"):
                continue
            pct = info.get("percent")
            if pct is None:
                continue
            used = info.get("used") or 0
            total = info.get("total") or 0
            disk_parts.append({
                "server": srv.name,
                "server_id": srv.id,
                "mount": mount,
                "percent": round(pct, 1),
                "used_gb": round(used / (1024 ** 3), 1) if used else 0,
                "total_gb": round(total / (1024 ** 3), 1) if total else 0,
            })
    disk_parts.sort(key=lambda x: x["percent"], reverse=True)
    top_disk_partitions = disk_parts[:3]

    # Per-user dashboard perspective (Operations vs Executive)
    acl = UserACL.get_or_create_for_user(request.user)
    dashboard_view = acl.dashboard_view

    # Server-side guard: only users with the Executive capability may see the
    # Executive persona; everyone else is forced to Operations (defense in depth
    # alongside the route-level middleware).
    from .permissions import user_can, VIEW_EXECUTIVE
    if dashboard_view == UserACL.DashboardView.EXECUTIVE and not user_can(request.user, VIEW_EXECUTIVE):
        dashboard_view = UserACL.DashboardView.OPERATIONS

    # Dashboard shows only services/containers the user has chosen to monitor
    services_qs = Service.objects.filter(monitoring_enabled=True)
    services_total = services_qs.count()
    services_running = services_qs.filter(status="running").count()
    containers_qs = Container.objects.filter(monitoring_enabled=True)
    containers_total = containers_qs.count()
    containers_running = containers_qs.filter(state="running").count()

    # Overview health roll-up (servers + monitored services + monitored containers)
    _healthy = online_count + services_running + containers_running
    _targets = len(servers_data) + services_total + containers_total
    _latest_hb = ServerHeartbeat.objects.order_by("-last_heartbeat").first()
    overview = {
        "healthy": _healthy,
        "total_targets": _targets,
        "attention": max(0, _targets - _healthy),
        "all_ok": (_targets > 0 and _healthy >= _targets),
        "last_updated": _latest_hb.last_heartbeat if _latest_hb else None,
    }

    context = {
        "servers_data": servers_data,
        "total_servers": len(servers_data),
        "online_count": online_count,
        "warning_count": warning_count,
        "offline_count": offline_count,
        "alert_count": alert_count,
        "show_sidebar": True,
        "dashboard_view": dashboard_view,
        "overview": overview,
        "services_total": services_total,
        "services_running": services_running,
        "services_stopped": services_total - services_running,
        "containers_total": containers_total,
        "containers_running": containers_running,
        "containers_stopped": containers_total - containers_running,
        "top_disk_partitions": top_disk_partitions,
    }

    # Executive view = VM right-sizing recommendations (business KPIs linked).
    if dashboard_view == UserACL.DashboardView.EXECUTIVE:
        context["kpis"] = list(BusinessKPI.objects.filter(enabled=True).order_by("name"))
        try:
            # Early (0-7 day) VMs are shown by default as a directional category;
            # pass ?early=0 to hide them.
            context.update(build_executive_context(
                allow_early=request.GET.get("early") != "0"))
        except Exception as e:
            error_logger.error(f"EXECUTIVE_DASHBOARD error: {e}")
            context["exec_error"] = True

    context.update(admin.site.each_context(request))
    return render(request, "core/monitoring_dashboard.html", context)


@staff_member_required
@require_http_methods(["POST"])
def set_dashboard_view(request):
    """Persist the user's preferred dashboard perspective, then return to it."""
    from .permissions import user_can, VIEW_EXECUTIVE
    view = request.POST.get("view")
    # Operators (no Executive capability) cannot switch to the Executive persona.
    if view == UserACL.DashboardView.EXECUTIVE and not user_can(request.user, VIEW_EXECUTIVE):
        return redirect("monitoring_dashboard")
    if view in (UserACL.DashboardView.OPERATIONS, UserACL.DashboardView.EXECUTIVE):
        acl = UserACL.get_or_create_for_user(request.user)
        acl.dashboard_view = view
        acl.save(update_fields=["dashboard_view", "updated_at"])
    return redirect("monitoring_dashboard")


@staff_member_required
def account_password(request):
    """Self-service password change for the logged-in user (any role)."""
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm
    from django.contrib import messages

    # Always change the REAL account, even if currently impersonating.
    target = getattr(request, "real_user", None) or request.user
    if request.method == "POST":
        form = PasswordChangeForm(target, request.POST)
        if form.is_valid():
            form.save()
            if target == request.user:
                update_session_auth_hash(request, target)  # stay logged in
            messages.success(request, "Your password has been updated.")
            return redirect("account_password")
    else:
        form = PasswordChangeForm(target)
    return render(request, "core/account_password.html",
                  {"form": form, "show_sidebar": True})


@staff_member_required
def home_redirect(request):
    """Post-login dispatcher: send each role to its default landing page.
    Admin/Operator -> Operations, CEO -> Executive."""
    from .permissions import default_landing_for, LANDING_EXECUTIVE
    acl = UserACL.get_or_create_for_user(request.user)
    desired = (UserACL.DashboardView.EXECUTIVE
               if default_landing_for(request.user) == LANDING_EXECUTIVE
               else UserACL.DashboardView.OPERATIONS)
    if acl.dashboard_view != desired:
        acl.dashboard_view = desired
        acl.save(update_fields=["dashboard_view", "updated_at"])
    return redirect("monitoring_dashboard")


@staff_member_required
@require_http_methods(["POST"])
def impersonate_start(request, user_id):
    """Begin impersonating a lower-privilege user. Admin/CEO only; never a peer
    (anyone with the impersonate capability) and never yourself. The real actor
    is preserved server-side and the impersonator gains only the target's
    (lesser) permissions — no escalation."""
    from .permissions import user_can, IMPERSONATE, can_be_impersonated, default_landing_for, LANDING_EXECUTIVE
    from . import audit
    from django.contrib import messages
    from django.http import HttpResponseForbidden

    # Real actor (defensive: block starting a nested impersonation).
    actor = getattr(request, "real_user", None) or request.user
    if request.session.get("impersonate_user_id"):
        return redirect("monitoring_dashboard")

    ip = audit.client_ip(request)
    if not user_can(actor, IMPERSONATE):
        audit.record(actor, "impersonate_denied", resource=f"user:{user_id}",
                     method="POST", result=audit.DENIED, ip=ip)
        return HttpResponseForbidden("403 — not permitted to switch accounts")

    target = get_object_or_404(User, id=user_id, is_staff=True, is_active=True)
    if target.id == actor.id or not can_be_impersonated(target):
        # Cannot impersonate yourself or another privileged (Admin/CEO) account.
        audit.record(actor, "impersonate_denied", resource=f"user:{target.username}",
                     method="POST", result=audit.DENIED, ip=ip, target=target)
        messages.error(request, "You can't switch into that account.")
        return redirect("admin_users")

    request.session["impersonate_user_id"] = target.id
    audit.record(actor, "impersonate_start", resource=f"user:{target.username}",
                 method="POST", result=audit.ALLOWED, ip=ip, target=target)
    # Land on the target's default page.
    if default_landing_for(target) == LANDING_EXECUTIVE:
        return redirect("monitoring_dashboard")
    return redirect("monitoring_dashboard")


@staff_member_required
@require_http_methods(["POST"])
def impersonate_exit(request):
    """Stop impersonating and return to the real account (one click)."""
    from . import audit
    actor = getattr(request, "real_user", None) or request.user
    target_id = request.session.pop("impersonate_user_id", None)
    if target_id:
        target = User.objects.filter(id=target_id).first()
        audit.record(actor, "impersonate_exit",
                     resource=f"user:{getattr(target, 'username', target_id)}",
                     method="POST", result=audit.ALLOWED, ip=audit.client_ip(request),
                     target=target)
    return redirect("monitoring_dashboard")


@staff_member_required
def executive_dashboard_preview(request):
    """Preview of the Executive right-sizing dashboard rendered against demo
    data (handy when the live fleet has <7 days of history). ?empty=1 forces
    the gate state. The live page is the Executive persona on the dashboard."""
    from .utils.rightsizing_demo import build_demo_context
    ctx = build_demo_context(empty=request.GET.get("empty") == "1",
                             allow_early=request.GET.get("early") != "0")
    return render(request, "core/executive_dashboard.html", ctx)


def build_executive_context(allow_early=True):
    """Real Executive right-sizing context from live metrics. Safe to call from
    the dashboard view; returns a context dict for executive_dashboard.html.

    allow_early=True is the opt-in preview: VMs with 0<days<7 are shown as
    "Early" (directional only) instead of being gated out."""
    from .utils import rightsizing_constants as C
    from .utils.rightsizing_data import (
        gather_vm_window_stats, fleet_trend, fleet_forecast,
    )
    from .utils.rightsizing_engine import assess_vm
    from .utils.rightsizing_report import build_report

    # Cost / $ savings has been removed; right-sizing is capacity-only now.
    stats = gather_vm_window_stats()
    assessments = [assess_vm(s, allow_early=allow_early) for s in stats]
    report = build_report(assessments, pricing_configured=False)
    ctx = dict(report)
    ctx.update({
        "is_demo": False,
        "trend_data": fleet_trend(),
        "forecast_data": fleet_forecast(),
        "show_gate": report["eligible_count"] == 0,
        "early_mode": any(a.confidence == "EARLY" for a in assessments),
        "early_message": C.MSG_EARLY,
        # Is there anything to preview early? (some VM with <7d but >0d of data)
        "can_preview_early": any(0 < a.data_days < C.MIN_DAYS for a in assessments),
    })
    return ctx


def _fleet_status_counts():
    """Fleet-wide counts for the dashboard status donuts (servers + monitored
    services/containers). Used by the dashboard page and the auto-refresh API."""
    online = warning = offline = 0
    for s in Server.objects.all():
        st = _calculate_server_status(s)
        if st == "online":
            online += 1
        elif st == "warning":
            warning += 1
        else:
            offline += 1
    svc = Service.objects.filter(monitoring_enabled=True)
    svc_total = svc.count()
    svc_running = svc.filter(status="running").count()
    ctr = Container.objects.filter(monitoring_enabled=True)
    ctr_total = ctr.count()
    ctr_running = ctr.filter(state="running").count()
    return {
        "servers": {"online": online, "warning": warning, "offline": offline,
                    "total": online + warning + offline},
        "services": {"running": svc_running, "stopped": svc_total - svc_running, "total": svc_total},
        "containers": {"running": ctr_running, "stopped": ctr_total - ctr_running, "total": ctr_total},
    }


@staff_member_required
@require_http_methods(["GET"])
def dashboard_fleet_status_api(request):
    """Live fleet-status counts for the dashboard donuts (auto-refresh)."""
    return JsonResponse({"success": True, "data": _fleet_status_counts()})


@staff_member_required
def add_server(request):
    """Legacy add entry point — redirects to the push-agent add flow.

    SSH onboarding was removed; the agent flow is the only way to add a server.
    """
    return redirect('add_server_agent')


def _public_base_url(request):
    """Public base URL for agent commands / links. Prefer the operator-configured base
    URL (set in /setup -- it carries the true external scheme+host+PORT, even behind a
    forward / non-standard port where the request Host can't be trusted to). Fall back
    to the request host only if it isn't configured."""
    try:
        from .models import AppConfig
        configured = (AppConfig.get_config().base_url or "").strip().rstrip("/")
        if configured:
            return configured
    except Exception:
        pass
    return request.build_absolute_uri('/').rstrip('/')


def _render_agent_install_command(request, server, raw_token, created=False, rotated=False):
    """Render the one-time page showing a server's agent install command.

    The raw token is only available here (it is stored hashed), so this page is
    the single place the operator can copy it from.
    """
    base_url = _public_base_url(request)
    if server.os_type == "windows":
        # PowerShell one-liner: download install.ps1 and run it elevated. The agent ships
        # as a standalone .exe registered as a Windows service (see agent/install.ps1).
        install_cmd = (
            f"powershell -ExecutionPolicy Bypass -Command \""
            f"iwr {base_url}/agent/install.ps1 -OutFile $env:TEMP\\install.ps1; "
            f"& $env:TEMP\\install.ps1 -Url {base_url} -Token {raw_token}\""
        )
    else:
        install_cmd = (
            f"curl -fsSL {base_url}/agent/install.sh | sudo bash -s -- "
            f"--url {base_url} --token {raw_token}"
        )
    context = {
        "show_sidebar": True,
        "server": server,
        "raw_token": raw_token,
        "install_cmd": install_cmd,
        "base_url": base_url,
        "created": created,
        "rotated": rotated,
    }
    context.update(admin.site.each_context(request))
    return render(request, "core/agent_install_command.html", context)


def add_server_agent(request):
    """Add a server using the push agent (no SSH).

    Creates the server record + monitoring config, generates a per-server agent
    token, and shows the one-line install command to run on the VM. The
    monitoring server never connects to the VM -- the VM dials out to us.
    """
    from .utils import has_privilege
    from django.core.validators import validate_ipv46_address
    from django.core.exceptions import ValidationError as DjangoValidationError

    if not has_privilege(request.user, 'manage_monitoring'):
        messages.error(request, "You don't have permission to add servers.")
        return redirect('monitoring_dashboard')

    if request.method == "GET":
        context = {"show_sidebar": True}
        context.update(admin.site.each_context(request))
        return render(request, "core/add_server_agent.html", context)

    # POST
    _log_user_action(request, "ADD_SERVER_AGENT", "Attempting to add server (agent)")
    name = request.POST.get('name', '').strip()
    ip_address = request.POST.get('ip_address', '').strip()
    os_type = request.POST.get('os_type', 'linux').strip().lower()
    if os_type not in ('linux', 'windows'):
        os_type = 'linux'

    def _form_error(msg):
        messages.error(request, msg)
        context = {"show_sidebar": True, "name": name, "ip_address": ip_address, "os_type": os_type}
        context.update(admin.site.each_context(request))
        return render(request, "core/add_server_agent.html", context)

    if not name or not ip_address:
        return _form_error('Server name and IP address are required.')
    try:
        validate_ipv46_address(ip_address)
    except DjangoValidationError:
        return _form_error('Please enter a valid IPv4 or IPv6 address.')
    if Server.objects.filter(name=name).exists():
        return _form_error(f'A server named "{name}" already exists.')

    server = Server.objects.create(
        name=name,
        ip_address=ip_address,
        username='agent',  # push model does not use SSH login; placeholder
        os_type=os_type,
    )
    MonitoringConfig.objects.get_or_create(
        server=server,
        defaults={
            "enabled": True,
            "collection_interval_seconds": 60,
            "use_adtk": True,
            "use_isolation_forest": False,
            "use_llm_explanation": True,
            "retention_period_days": 30,
            "aggregation_enabled": True,
        },
    )
    _, raw_token = AgentCredential.generate_for_server(server)
    _log_user_action(request, "ADD_SERVER_AGENT", f"Added server {name} (id={server.id})")
    return _render_agent_install_command(request, server, raw_token, created=True)


@require_http_methods(["POST"])
def regenerate_agent_token(request, server_id):
    """Rotate a server's agent token and show the new install command.

    The previous token stops working immediately (instant revocation).
    """
    from .utils import has_privilege

    if not has_privilege(request.user, 'manage_monitoring'):
        messages.error(request, "You don't have permission to manage server tokens.")
        return redirect('monitoring_dashboard')

    server = get_object_or_404(Server, id=server_id)
    _, raw_token = AgentCredential.generate_for_server(server)
    _log_user_action(request, "ROTATE_AGENT_TOKEN", f"Rotated token for {server.name} (id={server.id})")
    return _render_agent_install_command(request, server, raw_token, rotated=True)


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
def app_config(request):
    """Application configuration page (timezone and language settings)"""
    from .models import AppConfig
    import pytz
    
    if request.method == "GET":
        config = AppConfig.get_config()
        
        # Get list of common timezones
        common_timezones = [
            ('UTC', 'UTC (Coordinated Universal Time)'),
            ('Asia/Kolkata', 'Asia/Kolkata (IST - Indian Standard Time)'),
            ('America/New_York', 'America/New_York (EST/EDT - Eastern Time)'),
            ('America/Chicago', 'America/Chicago (CST/CDT - Central Time)'),
            ('America/Denver', 'America/Denver (MST/MDT - Mountain Time)'),
            ('America/Los_Angeles', 'America/Los_Angeles (PST/PDT - Pacific Time)'),
            ('Europe/London', 'Europe/London (GMT/BST - British Time)'),
            ('Europe/Paris', 'Europe/Paris (CET/CEST - Central European Time)'),
            ('Europe/Berlin', 'Europe/Berlin (CET/CEST - Central European Time)'),
            ('Asia/Tokyo', 'Asia/Tokyo (JST - Japan Standard Time)'),
            ('Asia/Shanghai', 'Asia/Shanghai (CST - China Standard Time)'),
            ('Australia/Sydney', 'Australia/Sydney (AEST/AEDT - Australian Eastern Time)'),
            ('America/Sao_Paulo', 'America/Sao_Paulo (BRT - Brazil Time)'),
        ]
        
        # Get available languages
        available_languages = AppConfig.LanguageChoices.choices
        
        # Get current timezone info
        current_tz_name = config.display_timezone
        try:
            tz = pytz.timezone(current_tz_name)
            now = timezone.now()
            tz_now = now.astimezone(tz)
            offset = tz_now.strftime('%z')
            offset_formatted = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
            abbrev = tz_now.strftime('%Z') or current_tz_name.split('/')[-1][:3].upper()
        except Exception:
            offset_formatted = "+00:00"
            abbrev = "UTC"
        
        context = {
            'config': config,
            'current_timezone': current_tz_name,
            'current_language': config.language,
            'current_data_retention_days': getattr(config, 'data_retention_days', 60),
            'data_retention_presets': [30, 45, 60, 90],
            'timezone_offset': offset_formatted,
            'timezone_abbrev': abbrev,
            'common_timezones': common_timezones,
            'available_languages': available_languages,
            'show_sidebar': True,
        }
        return render(request, "core/app_config.html", context)
    elif request.method == "POST":
        try:
            new_timezone = request.POST.get('timezone', '').strip()
            new_language = request.POST.get('language', '').strip()
            
            # Validate timezone
            if new_timezone:
                try:
                    pytz.timezone(new_timezone)
                except pytz.UnknownTimeZoneError:
                    messages.error(request, f"Invalid timezone: {new_timezone}")
                    return redirect('app_config')
            
            # Validate language
            if new_language:
                valid_languages = [choice[0] for choice in AppConfig.LanguageChoices.choices]
                if new_language not in valid_languages:
                    messages.error(request, f"Invalid language: {new_language}")
                    return redirect('app_config')

            # Validate data_retention_days (custom value, clamped to 7..365)
            raw_data_retention = request.POST.get('data_retention_days', '').strip()
            new_data_retention = None
            if raw_data_retention:
                try:
                    new_data_retention = max(7, min(365, int(float(raw_data_retention))))
                except ValueError:
                    pass

            # Update config
            config = AppConfig.get_config()
            if new_timezone:
                config.display_timezone = new_timezone
            if new_language:
                config.language = new_language
            if new_data_retention is not None:
                config.data_retention_days = new_data_retention
            config.save()
            
            # Invalidate cache
            try:
                from django.core.cache import cache
                cache.delete('app_display_timezone')
            except Exception:
                pass
            
            parts = []
            if new_timezone:
                parts.append(f"Timezone={new_timezone}")
            if new_language:
                parts.append(f"Language={new_language}")
            if new_data_retention is not None:
                parts.append(f"Data retention={new_data_retention} days")
            if parts:
                messages.success(request, "Settings updated: " + ", ".join(parts))
            else:
                messages.info(request, "No changes made")
            
            return redirect('app_config')
        except Exception as e:
            error_logger.error(f"APP_CONFIG save error: {str(e)}")
            messages.error(request, f"Failed to update settings: {str(e)}")
            return redirect('app_config')
    else:
        messages.error(request, "Method not allowed")
        return redirect('app_config')


def _build_routing_context():
    """Build the Role x Category routing matrix for the alert-config page.

    Returns (roles, rows, min_severity_choices) where `rows` is one entry per alert
    category, each carrying a `cells` list aligned to `roles` with the current
    min-severity for that (role, category)."""
    from .models import Role, AlertRoutingRule
    from .permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_CEO

    alert_routing.ensure_default_rules()  # make sure built-in roles have a full matrix

    # Order columns: the three built-in roles first (Admin, Operator, CEO), then any
    # custom roles alphabetically.
    preferred = [ROLE_ADMIN, ROLE_OPERATOR, ROLE_CEO]
    all_roles = list(Role.objects.all())
    roles = ([r for name in preferred for r in all_roles if r.name == name]
             + sorted([r for r in all_roles if r.name not in preferred], key=lambda r: r.name))

    existing = {(rule.role_id, rule.category): rule.min_severity
                for rule in AlertRoutingRule.objects.all()}

    rows = []
    for cat_value, cat_label in alert_categories.AlertCategory.choices:
        cells = [{'role': role,
                  'value': existing.get((role.id, cat_value), AlertRoutingRule.OFF)}
                 for role in roles]
        rows.append({'category': cat_value, 'label': cat_label, 'cells': cells})

    return roles, rows, AlertRoutingRule.MIN_SEVERITY_CHOICES


def _is_admin(user):
    """Admin (or superuser) -- the only role allowed to change cross-role alert routing."""
    from .permissions import ROLE_ADMIN
    if getattr(user, "is_superuser", False):
        return True
    acl = getattr(user, "acl", None)
    return bool(acl and acl.role and acl.role.name == ROLE_ADMIN)


@staff_member_required
def alert_config(request):
    """Email alert configuration page"""
    if request.method == "GET":
        config = EmailAlertConfig.objects.first()
        slack_config = SlackAlertConfig.objects.first()
        roles, routing_rows, min_severity_choices = _build_routing_context()
        context = {
            'config': config,
            'slack_config': slack_config,
            'routing_roles': roles,
            'routing_rows': routing_rows,
            'min_severity_choices': min_severity_choices,
            # Routing is cross-role alerting policy -> only Admins may edit it. Others see
            # it read-only.
            'can_edit_routing': _is_admin(request.user),
            'show_sidebar': True,  # Match add_server page layout with sidebar
        }
        return render(request, "core/alert_config.html", context)
    else:
        messages.error(request, "Method not allowed")
        return redirect('alert_config')


@staff_member_required
@require_http_methods(["POST"])
def save_alert_routing(request):
    """Persist the Role x Category routing matrix from the alert-config page.

    Each editable cell posts as `route_<role_id>_<category>` = min-severity (or OFF).
    Routing decides who gets paged across ALL roles, so it is Admin-only (even though
    the alert-config page itself is reachable by anyone with MANAGE_ALERTS)."""
    from .models import Role, AlertRoutingRule

    if not _is_admin(request.user):
        try:
            messages.error(request, "Only administrators can change alert routing.")
        except Exception:
            pass
        return redirect('alert_config')

    valid_categories = {c for c, _ in alert_categories.AlertCategory.choices}
    valid_severities = {s for s, _ in AlertRoutingRule.MIN_SEVERITY_CHOICES}
    try:
        for key, value in request.POST.items():
            if not key.startswith('route_'):
                continue
            try:
                _, role_id, category = key.split('_', 2)
                role_id = int(role_id)
            except (ValueError, TypeError):
                continue
            if category not in valid_categories or value not in valid_severities:
                continue
            if not Role.objects.filter(id=role_id).exists():
                continue
            AlertRoutingRule.objects.update_or_create(
                role_id=role_id, category=category,
                defaults={'min_severity': value})
        try:
            messages.success(request, 'Alert routing saved.')
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Failed to save alert routing: {e}")
        try:
            messages.error(request, f'Failed to save alert routing: {e}')
        except Exception:
            pass
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
@require_http_methods(["GET"])
def disk_alerts_config_api(request):
    """API endpoint to get disk alert configuration"""
    try:
        # Get default thresholds from MonitoringConfig or use defaults
        from .models import MonitoringConfig, Server
        default_config = MonitoringConfig.objects.first()
        
        # Get all servers with their monitored disks
        servers = Server.objects.all().select_related('monitoring_config').order_by('name')
        servers_data = []
        for server in servers:
            config = server.monitoring_config if hasattr(server, 'monitoring_config') else None
            monitored_disks = config.monitored_disks if config and config.monitored_disks else []
            servers_data.append({
                'id': server.id,
                'name': server.name,
                'monitored_disks': monitored_disks if isinstance(monitored_disks, list) else []
            })
        
        return JsonResponse({
            'success': True,
            'disk_threshold': default_config.disk_threshold if default_config else 90.0,
            'disk_io_threshold': default_config.disk_io_threshold if default_config else 1000.0,
            'servers': servers_data,
        })
    except Exception as e:
        error_logger.error(f"DISK_ALERTS_CONFIG_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["POST"])
def save_disk_alerts_config(request):
    """Save disk alert configuration"""
    try:
        import json
        disk_threshold = float(request.POST.get('disk_threshold', 90.0))
        disk_io_threshold = float(request.POST.get('disk_io_threshold', 1000.0))
        
        # Update all monitoring configs with new thresholds
        from .models import MonitoringConfig
        MonitoringConfig.objects.all().update(
            disk_threshold=disk_threshold,
            disk_io_threshold=disk_io_threshold
        )
        
        messages.success(request, f"Disk alert thresholds updated: Usage={disk_threshold}%, I/O={disk_io_threshold}MB/s")
        return redirect('alert_config')
    except Exception as e:
        error_logger.error(f"SAVE_DISK_ALERTS_CONFIG error: {str(e)}")
        messages.error(request, f"Failed to save disk alert configuration: {str(e)}")
        return redirect('alert_config')


@staff_member_required
@require_http_methods(["POST"])
def save_monitored_disks(request, server_id):
    """Save monitored disk partitions for a server"""
    try:
        import json
        server = get_object_or_404(Server, id=server_id)
        
        # Get selected partitions from POST data
        selected_partitions = request.POST.getlist('partitions[]')
        
        # Get or create monitoring config
        from .models import MonitoringConfig
        config, created = MonitoringConfig.objects.get_or_create(server=server)
        
        # Update monitored disks
        config.monitored_disks = selected_partitions
        config.save(update_fields=['monitored_disks'])
        
        return JsonResponse({
            'success': True,
            'message': f'Monitored disks updated for {server.name}',
            'monitored_disks': selected_partitions
        })
    except Exception as e:
        error_logger.error(f"SAVE_MONITORED_DISKS error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def service_alerts_config_api(request):
    """API endpoint to get service alert configuration"""
    try:
        from .models import Service, Server
        # Get all servers with their services
        servers = Server.objects.all().order_by('name')
        servers_data = []
        
        for server in servers:
            services = Service.objects.filter(server=server).order_by('name')
            services_data = []
            for service in services:
                services_data.append({
                    'id': service.id,
                    'name': service.name,
                    'status': service.status,
                    'service_type': service.service_type,
                    'monitoring_enabled': service.monitoring_enabled,
                })
            
            servers_data.append({
                'id': server.id,
                'name': server.name,
                'services': services_data
            })
        
        return JsonResponse({
            'success': True,
            'servers': servers_data,
        })
    except Exception as e:
        error_logger.error(f"SERVICE_ALERTS_CONFIG_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def server_services_api(request, server_id):
    """API endpoint to get services for a specific server"""
    try:
        server = get_object_or_404(Server, id=server_id)
        services = Service.objects.filter(server=server).order_by('name')
        
        services_data = []
        for service in services:
            services_data.append({
                'id': service.id,
                'name': service.name,
                'status': service.status,
                'service_type': service.service_type,
                'port': service.port,
                'monitoring_enabled': service.monitoring_enabled,
            })
        
        return JsonResponse({
            'success': True,
            'services': services_data,
        })
    except Exception as e:
        error_logger.error(f"SERVER_SERVICES_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["POST"])
def save_service_monitoring(request, server_id):
    """Save service monitoring configuration for a server"""
    try:
        server = get_object_or_404(Server, id=server_id)
        
        # Get selected service IDs from POST data
        selected_service_ids = [int(id) for id in request.POST.getlist('services[]')]
        
        # Update monitoring_enabled for all services on this server
        from .models import Service
        Service.objects.filter(server=server).update(monitoring_enabled=False)
        if selected_service_ids:
            Service.objects.filter(server=server, id__in=selected_service_ids).update(monitoring_enabled=True)
        
        return JsonResponse({
            'success': True,
            'message': f'Service monitoring updated for {server.name}',
            'monitored_services': selected_service_ids
        })
    except Exception as e:
        error_logger.error(f"SAVE_SERVICE_MONITORING error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


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
        # The sender is always the authenticated SMTP account -- we never send "as"
        # an arbitrary address (anti-spoofing). No separate From field.
        # Recipients are no longer a single field -- they are resolved per alert from
        # the role-based routing matrix (see alert_routing / AlertRoutingRule).
        from_email = username

        # Check if this is an update (config exists) or new config
        existing_config = EmailAlertConfig.objects.first()

        # Validate required fields based on provider
        if provider == 'custom':
            if not smtp_host or not smtp_port or not username:
                try:
                    messages.error(request, 'SMTP host, port, and username are required for custom SMTP configuration')
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
            if not username:
                try:
                    messages.error(request, 'Username is required')
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
        # Always send as the authenticated account (no separate From / anti-spoofing).
        from_email = username
        # There is no single "To" address anymore -- the test goes to whoever is signed
        # in and clicked the button (they're verifying their own SMTP setup).
        test_recipient = (getattr(request.user, 'email', '') or '').strip()

        # Validate required fields
        if not all([smtp_host, smtp_port, username]):
            messages.error(request, 'SMTP host, port, and username are required in saved configuration.')
            return redirect('alert_config')

        if not password:
            messages.error(request, 'Password is required for testing. Please save your configuration with a password first.')
            return redirect('alert_config')

        if not test_recipient:
            messages.error(request, 'Your account has no email address set, so there is nowhere to send the test. Add an email to your user profile and try again.')
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
@require_http_methods(["GET", "POST"])
def slack_config(request):
    """Display Slack configuration page"""
    if request.method == "GET":
        slack_config_obj = SlackAlertConfig.objects.first()
        context = {
            'slack_config': slack_config_obj,
            'show_sidebar': True,
        }
        return render(request, "core/alert_config.html", context)
    else:
        messages.error(request, "Method not allowed")
        return redirect('alert_config')


@staff_member_required
@require_http_methods(["POST"])
def save_slack_config(request):
    """Save Slack alert configuration"""
    try:
        webhook_url = request.POST.get('webhook_url', '').strip()
        channel = request.POST.get('channel', '').strip()
        username = request.POST.get('username', '').strip()
        icon_emoji = request.POST.get('icon_emoji', '').strip()
        enabled = request.POST.get('enabled') == 'on'

        # Validate webhook URL
        if not webhook_url:
            messages.error(request, 'Webhook URL is required')
            return redirect('alert_config')
        
        if not webhook_url.startswith('https://hooks.slack.com/'):
            messages.error(request, 'Webhook URL must start with https://hooks.slack.com/')
            return redirect('alert_config')

        # Get or create config (only one config allowed)
        config, created = SlackAlertConfig.objects.get_or_create(
            id=1,  # Single instance
            defaults={
                'webhook_url': webhook_url,
                'channel': channel,
                'username': username,
                'icon_emoji': icon_emoji or ':warning:',
                'enabled': enabled
            }
        )

        if not created:
            # Update existing config
            config.webhook_url = webhook_url
            config.channel = channel
            config.username = username
            config.icon_emoji = icon_emoji or ':warning:'
            config.enabled = enabled
            config.save()

        messages.success(request, 'Slack alert configuration saved successfully!')
        return redirect('alert_config')

    except Exception as e:
        logger.error(f"Failed to save Slack config: {str(e)}")
        messages.error(request, f'Failed to save configuration: {str(e)}')
        return redirect('alert_config')


@staff_member_required
@require_http_methods(["POST"])
def clear_slack_config(request):
    """Clear/delete Slack alert configuration"""
    try:
        config = SlackAlertConfig.objects.filter(id=1).first()
        if config:
            config.delete()
            messages.success(request, 'Slack alert configuration cleared successfully!')
        else:
            messages.info(request, 'No Slack alert configuration found to clear.')
        return redirect('alert_config')

    except Exception as e:
        logger.error(f"Failed to clear Slack config: {str(e)}")
        messages.error(request, f'Failed to clear configuration: {str(e)}')
        return redirect('alert_config')


@staff_member_required
@require_http_methods(["POST"])
def test_slack_config(request):
    """Test Slack alert configuration - sends a test message"""
    import requests
    import json
    
    try:
        # Get saved configuration from database
        saved_config = SlackAlertConfig.objects.first()
        
        if not saved_config:
            messages.error(request, 'No saved Slack configuration found. Please save your Slack configuration first.')
            return redirect('alert_config')
        
        if not saved_config.enabled:
            messages.error(request, 'Slack notifications are disabled. Enable them first.')
            return redirect('alert_config')
        
        # Prepare test message
        payload = {
            'text': 'Test message from StackWatch',
            'blocks': [
                {
                    'type': 'section',
                    'text': {
                        'type': 'mrkdwn',
                        'text': '*Test Alert from StackWatch*\n\nThis is a test message to verify your Slack webhook configuration is working correctly.'
                    }
                }
            ]
        }
        
        # Add optional fields
        if saved_config.username:
            payload['username'] = saved_config.username
        if saved_config.icon_emoji:
            payload['icon_emoji'] = saved_config.icon_emoji
        if saved_config.channel:
            payload['channel'] = saved_config.channel
        
        # Send test message
        response = requests.post(
            saved_config.webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        
        if response.status_code == 200:
            messages.success(request, 'Test message sent successfully to Slack! Check your Slack channel.')
        else:
            error_msg = f"Slack API returned status {response.status_code}: {response.text}"
            messages.error(request, f'Failed to send test message: {error_msg}')
            logger.error(f"Slack test failed: {error_msg}")
        
        return redirect('alert_config')

    except requests.exceptions.RequestException as e:
        error_msg = f"Network error: {str(e)}"
        messages.error(request, f'Failed to send test message: {error_msg}')
        logger.error(f"Slack test network error: {error_msg}")
        return redirect('alert_config')
    except Exception as e:
        error_msg = f"Error: {str(e)}"
        messages.error(request, f'Failed to send test message: {error_msg}')
        logger.error(f"Slack test error: {error_msg}")
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
        required_fields = ['smtp_host', 'smtp_port', 'smtp_username', 'smtp_password']
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
        from_email = smtp_username  # always send as the authenticated account (anti-spoofing)
        # No single "To" address anymore -- send the test to the signed-in user.
        user_email = (getattr(request.user, 'email', '') or '').strip()
        if not user_email:
            return JsonResponse({
                'success': False,
                'error': 'Your account has no email address set, so there is nowhere to send the test email.'
            }, status=400)
        recipients = [user_email]
        
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
        
        # Get pending alert state (for sustained threshold checking)
        # Alerts only trigger after 2 consecutive readings above threshold
        pending_cache_key = f"alert_pending:{server.id}"
        pending_state = cache.get(pending_cache_key, {})
        
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
        
        # Check CPU threshold (2 consecutive readings to arm, then alert ONCE per episode).
        # current_state['CPU'] tracks whether an alert is OPEN (alerting), not the raw breach,
        # so a sustained breach fires on the rising edge only and resolves on the falling edge.
        cpu_above_threshold = metric.cpu_percent is not None and metric.cpu_percent >= config.cpu_threshold
        cpu_was_alerting = previous_state.get('CPU', False)

        if cpu_above_threshold:
            if cpu_was_alerting:
                # Already alerting -> stay open, do NOT re-fire every cycle.
                current_state['CPU'] = True
            elif pending_state.get('CPU', False):
                # Second consecutive reading and not yet alerting -> fire once (rising edge).
                current_state['CPU'] = True
                alerts.append({
                    'type': 'CPU',
                    'value': metric.cpu_percent,
                    'threshold': config.cpu_threshold,
                    'message': f"CPU usage is {metric.cpu_percent:.1f}% (threshold: {config.cpu_threshold}%)"
                })
                print(f"[ALERT] CPU threshold sustained: {metric.cpu_percent:.1f}% >= {config.cpu_threshold}% (alert raised)")
            else:
                # First reading above threshold - arm pending, not alerting yet.
                pending_state['CPU'] = True
                current_state['CPU'] = False
                print(f"[ALERT] CPU threshold exceeded (pending): {metric.cpu_percent:.1f}% >= {config.cpu_threshold}% (waiting for sustained)")
        else:
            # Below threshold - clear pending; resolve only if we were actually alerting.
            pending_state['CPU'] = False
            current_state['CPU'] = False
            if cpu_was_alerting:
                resolved_alerts.append({
                    'type': 'CPU',
                    'value': metric.cpu_percent,
                    'threshold': config.cpu_threshold,
                    'message': f"CPU usage has returned to normal: {metric.cpu_percent:.1f}% (threshold: {config.cpu_threshold}%)"
                })
                print(f"[ALERT] CPU threshold resolved: {metric.cpu_percent:.1f}% < {config.cpu_threshold}%")
        
        # Check Memory threshold (2 consecutive readings to arm, then alert ONCE per episode).
        memory_above_threshold = metric.memory_percent is not None and metric.memory_percent >= config.memory_threshold
        mem_was_alerting = previous_state.get('Memory', False)

        if memory_above_threshold:
            if mem_was_alerting:
                # Already alerting -> stay open, do NOT re-fire every cycle.
                current_state['Memory'] = True
            elif pending_state.get('Memory', False):
                # Second consecutive reading and not yet alerting -> fire once (rising edge).
                current_state['Memory'] = True
                alerts.append({
                    'type': 'Memory',
                    'value': metric.memory_percent,
                    'threshold': config.memory_threshold,
                    'message': f"Memory usage is {metric.memory_percent:.1f}% (threshold: {config.memory_threshold}%)"
                })
                print(f"[ALERT] Memory threshold sustained: {metric.memory_percent:.1f}% >= {config.memory_threshold}% (alert raised)")
            else:
                # First reading above threshold - arm pending, not alerting yet.
                pending_state['Memory'] = True
                current_state['Memory'] = False
                print(f"[ALERT] Memory threshold exceeded (pending): {metric.memory_percent:.1f}% >= {config.memory_threshold}% (waiting for sustained)")
        else:
            # Below threshold - clear pending; resolve only if we were actually alerting.
            pending_state['Memory'] = False
            current_state['Memory'] = False
            if mem_was_alerting:
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
                    if is_ephemeral_mount(mountpoint):
                        continue  # /tmp, /var/tmp, /run, ... are not capacity incidents
                    if isinstance(usage, dict):
                        disk_percent = usage.get('percent', 0)
                        disk_above_threshold = disk_percent >= config.disk_threshold
                        was_alerting = previous_disk_state.get(mountpoint, False)
                        # 'alerting' for a partition == above threshold (no sustain window for disk).
                        current_state['Disk'][mountpoint] = disk_above_threshold

                        if disk_above_threshold and not was_alerting:
                            # Rising edge -> alert once per episode for this partition.
                            alerts.append({
                                'type': 'Disk',
                                'value': disk_percent,
                                'threshold': config.disk_threshold,
                                'message': f"Disk usage on {mountpoint} is {disk_percent:.1f}% (threshold: {config.disk_threshold}%)"
                            })
                            print(f"[ALERT] Disk threshold exceeded on {mountpoint}: {disk_percent:.1f}% >= {config.disk_threshold}%")
                        elif (not disk_above_threshold) and was_alerting:
                            # Falling edge -> resolved.
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
            io_was_alerting = previous_state.get('DiskIO', False)
            current_state['DiskIO'] = disk_io_above  # alerting == above (no sustain window for I/O)

            if disk_io_above and not io_was_alerting:
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
            elif (not disk_io_above) and io_was_alerting:
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
            net_was_alerting = previous_state.get('NetworkIO', False)
            current_state['NetworkIO'] = net_io_above  # alerting == above (no sustain window for I/O)

            if net_io_above and not net_was_alerting:
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
            elif (not net_io_above) and net_was_alerting:
                resolved_alerts.append({
                    'type': 'NetworkIO',
                    'value': max(net_io_sent_mb, net_io_recv_mb),
                    'threshold': threshold_mb,
                    'message': f"Network I/O returned to normal: sent {net_io_sent_mb:.2f} MB/s, received {net_io_recv_mb:.2f} MB/s (threshold: {threshold_mb} MB/s)"
                })
                print(f"[ALERT] Network I/O threshold resolved: < {threshold_mb} MB/s")
        
        # Map alert types to valid AlertHistory types (DiskIO and NetworkIO -> Disk)
        def map_alert_type(alert_type):
            """Map alert type to valid AlertHistory.AlertType choice"""
            type_mapping = {
                'DiskIO': 'Disk',
                'NetworkIO': 'Disk',  # Map NetworkIO to Disk as closest match
            }
            return type_mapping.get(alert_type, alert_type)
        
        # Send email if new alerts exist
        if alerts:
            print(f"[ALERT] Sending {len(alerts)} alert(s) for {server.name}")
            _send_alert_email(email_config, server, alerts)
            
            # Send Slack alert if configured
            slack_config = SlackAlertConfig.objects.filter(enabled=True).first()
            if slack_config:
                _send_slack_alert(
                    slack_config.webhook_url,
                    server,
                    alerts,
                    username=slack_config.username,
                    icon_emoji=slack_config.icon_emoji,
                    channel=slack_config.channel
                )
            
            # Log to AlertHistory
            for alert in alerts:
                mapped_type = map_alert_type(alert['type'])
                
                # Capture process context for CPU and Memory alerts
                process_context = None
                if alert['type'] in ['CPU', 'Memory'] and metric and hasattr(metric, 'top_processes') and metric.top_processes:
                    process_context = metric.top_processes
                
                AlertHistory.objects.create(
                    server=server,
                    alert_type=mapped_type,
                    status=AlertHistory.AlertStatus.TRIGGERED,
                    severity=alert_categories.default_severity_for_alert_type(mapped_type, "triggered"),
                    value=alert['value'],
                    threshold=alert['threshold'],
                    message=alert['message'],
                    recipients=", ".join(alert_routing.recipients_for("resource", "HIGH")),
                    process_context=process_context
                )
                app_logger.info(f"Alert sent: {server.name} - {mapped_type} - {alert['message']}")
        
        # Send email if resolved alerts exist
        if resolved_alerts:
            print(f"[ALERT] Sending {len(resolved_alerts)} resolved alert(s) for {server.name}")
            _send_resolved_alert_email(email_config, server, resolved_alerts)
            
            # Send Slack resolved alert if configured
            slack_config = SlackAlertConfig.objects.filter(enabled=True).first()
            if slack_config:
                _send_slack_resolved_alert(
                    slack_config.webhook_url,
                    server,
                    resolved_alerts,
                    username=slack_config.username,
                    icon_emoji=slack_config.icon_emoji,
                    channel=slack_config.channel
                )
            
            # Log to AlertHistory
            for alert in resolved_alerts:
                mapped_type = map_alert_type(alert['type'])
                AlertHistory.objects.create(
                    server=server,
                    alert_type=mapped_type,
                    status=AlertHistory.AlertStatus.RESOLVED,
                    severity=alert_categories.default_severity_for_alert_type(mapped_type, "resolved"),
                    value=alert['value'],
                    threshold=alert['threshold'],
                    message=alert['message'],
                    recipients=", ".join(alert_routing.recipients_for("resource", "LOW")),
                    resolved_at=timezone.now(),
                    process_context=None  # No process context needed for resolved alerts
                )
                app_logger.info(f"Alert resolved: {server.name} - {mapped_type} - {alert['message']}")
        
        # Update cache with current state (store for 24 hours)
        cache.set(cache_key, current_state, 86400)
        
        # Update pending state cache (store for 5 minutes - enough for a few collection cycles)
        cache.set(pending_cache_key, pending_state, 300)
        
        if not alerts and not resolved_alerts:
            print(f"[ALERT] No alerts triggered for {server.name} (CPU: {metric.cpu_percent}, Memory: {metric.memory_percent})")
            
    except Exception as e:
        import traceback
        print(f"[ALERT] Error checking alerts for {server.name}: {e}")
        print(f"[ALERT] Traceback: {traceback.format_exc()}")


def _send_resolved_alert_email(email_config, server, resolved_alerts):
    """Send resolved alert email when metrics return to normal"""
    try:
        # Resource/performance thresholds, resolved -> route by (resource, LOW).
        recipients = alert_routing.recipients_for("resource", "LOW")
        if not recipients:
            print(f"[ALERT] No routed recipients for resolved resource alert on {server.name}; skipping email")
            return

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
        slack_config = SlackAlertConfig.objects.filter(enabled=True).first()
        
        if not email_config and not slack_config:
            print(f"[CONNECTION_ALERT] No email or Slack config found for {server.name}")
            return
        
        # Availability: server down -> CRITICAL, restored -> LOW. Routes by category.
        recipients = alert_routing.recipients_for(
            "availability", "CRITICAL" if state == "offline" else "LOW")

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
        
        # Send email if configured
        if email_config and recipients:
            print(f"[CONNECTION_ALERT] Attempting to send {state} alert email to {recipients}")
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
                
            except Exception as e:
                error_msg = f"[CONNECTION_ALERT] ✗ Failed to send {state} alert email for {server.name}: {e}"
                print(error_msg)
                error_logger.error(error_msg)
        
        # Send Slack alert if configured
        if slack_config:
            _send_slack_connection_alert(
                slack_config.webhook_url,
                server,
                state,
                username=slack_config.username,
                icon_emoji=slack_config.icon_emoji,
                channel=slack_config.channel
            )
        
        # Log to alert history
        if email_config or slack_config:
            _conn_status = "triggered" if state == "offline" else "resolved"
            AlertHistory.objects.create(
                server=server,
                alert_type="CONNECTION",
                status=_conn_status,
                severity=alert_categories.default_severity_for_alert_type("CONNECTION", _conn_status),
                value=0.0,
                threshold=30.0,
                message=f"Server is {state.upper()}" if state == "offline" else f"Server connection restored",
                recipients=', '.join(recipients) if email_config and recipients else 'Slack',
                process_context=None
            )
            
    except Exception as e:
        error_logger.error(f"CONNECTION_ALERT error for {server.name}: {str(e)}")


def _send_service_alert(server, service, status):
    """Send alert when service status changes (down/up)"""
    try:
        # Refresh server to get latest monitoring_config
        server.refresh_from_db()
        config = server.monitoring_config
        
        # Don't send alerts if monitoring is disabled or suspended
        if not config.enabled or config.monitoring_suspended:
            return
        
        # Get email and Slack configs
        email_config = EmailAlertConfig.objects.filter(enabled=True).first()
        slack_config = SlackAlertConfig.objects.filter(enabled=True).first()
        
        if not email_config and not slack_config:
            print(f"[SERVICE_ALERT] No email or Slack config found, skipping alert for {service.name} on {server.name}")
            return
        
        # Availability (service down/up) -> route by category + severity.
        recipients = alert_routing.recipients_for(
            "availability",
            alert_categories.default_severity_for_alert_type("service", status))
        
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
        
        # Send email if configured and someone is routed this alert
        if email_config and recipients:
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
                
                print(f"[SERVICE_ALERT] ✓ Sent {status} alert email for {service.name} on {server.name} to {recipients}")
                
            except Exception as e:
                error_msg = f"[SERVICE_ALERT] ✗ Failed to send {status} alert email for {service.name} on {server.name}: {e}"
                print(error_msg)
                error_logger.error(error_msg)
        
        # Send Slack alert if configured
        if slack_config:
            _send_slack_service_alert(
                slack_config.webhook_url,
                server,
                service,
                status,
                username=slack_config.username,
                icon_emoji=slack_config.icon_emoji,
                channel=slack_config.channel
            )
        
        # Log to AlertHistory
        if email_config or slack_config:
            AlertHistory.objects.create(
                server=server,
                alert_type="SERVICE",
                status=status,
                severity=alert_categories.default_severity_for_alert_type("service", status),
                value=0.0,
                threshold=2.0,
                message=f"Service {service.name} is {'DOWN' if status == 'triggered' else 'UP'}",
                recipients=", ".join(recipients) if recipients else 'Slack',
                process_context=None
            )
            
    except Exception as e:
        error_logger.error(f"SERVICE_ALERT error for {service.name} on {server.name}: {str(e)}")
        return


def _send_alert_email(email_config, server, alerts):
    """Send alert email using configured SMTP settings"""
    try:
        # Resource/performance thresholds, triggered -> route by (resource, HIGH).
        recipients = alert_routing.recipients_for("resource", "HIGH")
        if not recipients:
            print(f"[ALERT] No routed recipients for resource alert on {server.name}; skipping email")
            return

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


def _send_slack_alert(webhook_url, server, alerts, username=None, icon_emoji=None, channel=None):
    """Send alert to Slack via webhook"""
    import requests
    import json
    
    try:
        alert_list = "\n".join([f"• {alert['message']}" for alert in alerts])
        
        # Build Slack message with Block Kit
        blocks = [
            {
                'type': 'header',
                'text': {
                    'type': 'plain_text',
                    'text': f'🚨 Alert: {server.name}'
                }
            },
            {
                'type': 'section',
                'fields': [
                    {
                        'type': 'mrkdwn',
                        'text': f'*Server:* {server.name}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*IP Address:* {server.ip_address}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*Time:* {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}'
                    }
                ]
            },
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': f'*Alerts:*\n{alert_list}'
                }
            },
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': 'Please check the server immediately.'
                }
            }
        ]
        
        payload = {
            'text': f'Alert: {server.name} - Threshold Exceeded',
            'blocks': blocks,
            'color': 'danger'
        }
        
        if username:
            payload['username'] = username
        if icon_emoji:
            payload['icon_emoji'] = icon_emoji
        if channel:
            payload['channel'] = channel
        
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"[SLACK] ✓ Alert sent successfully for {server.name}")
        else:
            print(f"[SLACK] ✗ Failed to send alert for {server.name}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"[SLACK] ✗ Network error sending alert for {server.name}: {e}")
    except Exception as e:
        print(f"[SLACK] ✗ Error sending alert for {server.name}: {e}")


def _send_slack_resolved_alert(webhook_url, server, resolved_alerts, username=None, icon_emoji=None, channel=None):
    """Send resolved alert to Slack via webhook"""
    import requests
    import json
    
    try:
        alert_list = "\n".join([f"• {alert['message']}" for alert in resolved_alerts])
        
        blocks = [
            {
                'type': 'header',
                'text': {
                    'type': 'plain_text',
                    'text': f'✅ Resolved: {server.name}'
                }
            },
            {
                'type': 'section',
                'fields': [
                    {
                        'type': 'mrkdwn',
                        'text': f'*Server:* {server.name}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*IP Address:* {server.ip_address}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*Time:* {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}'
                    }
                ]
            },
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': f'*Resolved Alerts:*\n{alert_list}'
                }
            }
        ]
        
        payload = {
            'text': f'Resolved: {server.name} - Metrics Returned to Normal',
            'blocks': blocks,
            'color': 'good'
        }
        
        if username:
            payload['username'] = username
        if icon_emoji:
            payload['icon_emoji'] = icon_emoji
        if channel:
            payload['channel'] = channel
        
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"[SLACK] ✓ Resolved alert sent successfully for {server.name}")
        else:
            print(f"[SLACK] ✗ Failed to send resolved alert for {server.name}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"[SLACK] ✗ Network error sending resolved alert for {server.name}: {e}")
    except Exception as e:
        print(f"[SLACK] ✗ Error sending resolved alert for {server.name}: {e}")


def _send_slack_connection_alert(webhook_url, server, state, username=None, icon_emoji=None, channel=None):
    """Send connection state alert to Slack via webhook"""
    import requests
    import json
    
    try:
        if state == 'online':
            emoji = '✅'
            color = 'good'
            title = f'{emoji} Server Online: {server.name}'
            text = f'Server {server.name} is now online and responding.'
        else:
            emoji = '🔴'
            color = 'danger'
            title = f'{emoji} Server Offline: {server.name}'
            text = f'Server {server.name} is offline or not responding.'
        
        blocks = [
            {
                'type': 'header',
                'text': {
                    'type': 'plain_text',
                    'text': title
                }
            },
            {
                'type': 'section',
                'fields': [
                    {
                        'type': 'mrkdwn',
                        'text': f'*Server:* {server.name}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*IP Address:* {server.ip_address}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*Time:* {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}'
                    }
                ]
            },
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': text
                }
            }
        ]
        
        payload = {
            'text': title,
            'blocks': blocks,
            'color': color
        }
        
        if username:
            payload['username'] = username
        if icon_emoji:
            payload['icon_emoji'] = icon_emoji
        if channel:
            payload['channel'] = channel
        
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"[SLACK] ✓ Connection alert sent successfully for {server.name} ({state})")
        else:
            print(f"[SLACK] ✗ Failed to send connection alert for {server.name}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"[SLACK] ✗ Network error sending connection alert for {server.name}: {e}")
    except Exception as e:
        print(f"[SLACK] ✗ Error sending connection alert for {server.name}: {e}")


def _send_slack_service_alert(webhook_url, server, service, status, username=None, icon_emoji=None, channel=None):
    """Send service status alert to Slack via webhook"""
    import requests
    import json
    
    try:
        if status == 'resolved':
            emoji = '✅'
            color = 'good'
            title = f'{emoji} Service Restored: {service.name} on {server.name}'
            text = f'Service {service.name} on {server.name} has been restored and is now running.'
        else:
            emoji = '🚨'
            color = 'danger'
            title = f'{emoji} Service Alert: {service.name} on {server.name}'
            if service.status == 'failed':
                text = f'Service {service.name} on {server.name} is in FAILED state.'
            else:
                text = f'Service {service.name} on {server.name} is down (stopped).'
        
        blocks = [
            {
                'type': 'header',
                'text': {
                    'type': 'plain_text',
                    'text': title
                }
            },
            {
                'type': 'section',
                'fields': [
                    {
                        'type': 'mrkdwn',
                        'text': f'*Server:* {server.name}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*Service:* {service.name}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*Status:* {service.status}'
                    },
                    {
                        'type': 'mrkdwn',
                        'text': f'*Time:* {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}'
                    }
                ]
            },
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': text
                }
            }
        ]
        
        payload = {
            'text': title,
            'blocks': blocks,
            'color': color
        }
        
        if username:
            payload['username'] = username
        if icon_emoji:
            payload['icon_emoji'] = icon_emoji
        if channel:
            payload['channel'] = channel
        
        response = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"[SLACK] ✓ Service alert sent successfully for {service.name} on {server.name} ({status})")
        else:
            print(f"[SLACK] ✗ Failed to send service alert for {service.name} on {server.name}: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"[SLACK] ✗ Network error sending service alert for {service.name} on {server.name}: {e}")
    except Exception as e:
        print(f"[SLACK] ✗ Error sending service alert for {service.name} on {server.name}: {e}")


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
        if 'anomaly_sensitivity' in data:
            valid = {c[0] for c in MonitoringConfig.AnomalySensitivity.choices}
            sensitivity = str(data['anomaly_sensitivity']).upper()
            if sensitivity in valid:
                config.anomaly_sensitivity = sensitivity

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

    # RBAC check: requires the manage_users capability (Admin/CEO).
    if not has_privilege(request.user, 'manage_users'):
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
                    from .permissions import ROLE_LANDING
                    role = Role.objects.get(id=role_id)
                    acl = UserACL.get_or_create_for_user(user)
                    acl.role = role
                    # Initialise the persona to the role's default landing
                    # (e.g. CEO -> Executive) so the first login lands correctly.
                    acl.dashboard_view = ROLE_LANDING.get(
                        role.name, UserACL.DashboardView.OPERATIONS)
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
                from .permissions import ROLE_LANDING
                role = Role.objects.get(id=role_id)
                role_changed = acl.role_id != role.id
                acl.role = role
                # When the role changes, reset the persona to the new role's
                # default landing (e.g. CEO -> Executive). A same-role edit
                # leaves the user's current persona preference untouched.
                if role_changed:
                    acl.dashboard_view = ROLE_LANDING.get(
                        role.name, UserACL.DashboardView.OPERATIONS)
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


def _group_alerts_by_incident(alerts_list, time_window_minutes=5):
    """
    Group alerts by incident (same server, within time window).
    Returns a list of groups, where each group contains alerts from the same incident.
    """
    if not alerts_list:
        return []
    
    from datetime import timedelta
    
    # Sort alerts by timestamp (oldest first for grouping)
    sorted_alerts = sorted(alerts_list, key=lambda x: x.sent_at)
    
    groups = []
    current_group = []
    current_group_time = None
    
    for alert in sorted_alerts:
        # If no current group, start one
        if not current_group:
            current_group = [alert]
            current_group_time = alert.sent_at
            continue
        
        # Check if this alert belongs to the current group
        # (same server and within time window)
        time_diff = (alert.sent_at - current_group_time).total_seconds() / 60
        same_server = alert.server_id == current_group[0].server_id
        
        if same_server and time_diff <= time_window_minutes:
            # Add to current group
            current_group.append(alert)
            # Update group time to latest alert in group
            if alert.sent_at > current_group_time:
                current_group_time = alert.sent_at
        else:
            # Start a new group
            groups.append(current_group)
            current_group = [alert]
            current_group_time = alert.sent_at
    
    # Add the last group
    if current_group:
        groups.append(current_group)
    
    # Sort groups by most recent alert first
    groups.sort(key=lambda g: max(a.sent_at for a in g), reverse=True)
    
    return groups


@staff_member_required
def alert_history(request):
    """View page to display and manage alert history (including anomalies)"""
    from datetime import datetime, timedelta
    from django.utils import timezone as django_timezone
    
    # Get query parameters for filtering
    server_id = request.GET.get('server_id', '').strip()
    alert_type = request.GET.get('alert_type', '').strip()
    status = request.GET.get('status', '').strip()  # Default to empty (all alerts)
    time_range = request.GET.get('time_range', '24h').strip()
    category = request.GET.get('category', '').strip().lower()
    acknowledged = request.GET.get('acknowledged', '').strip()
    instance = request.GET.get('instance', '').strip()
    group_by_incident = request.GET.get('group', 'false').lower() == 'true' or request.GET.get('group_by_incident', 'false').lower() == 'true'
    
    # Build AlertHistory query. Anomalies are NOT shown here -- they're notifications
    # surfaced on the dashboard anomalies icon, not alerts.
    alert_history_query = AlertHistory.objects.all().select_related('server')

    # Time range filtering
    now = django_timezone.now()
    if time_range == '24h':
        alert_history_query = alert_history_query.filter(sent_at__gte=now - timedelta(hours=24))
    elif time_range == '7d':
        alert_history_query = alert_history_query.filter(sent_at__gte=now - timedelta(days=7))
    elif time_range == '30d':
        alert_history_query = alert_history_query.filter(sent_at__gte=now - timedelta(days=30))
    # 'all' means no time filter
    
    # Instance filtering (by server name or IP)
    if instance:
        alert_history_query = alert_history_query.filter(
            models.Q(server__name__icontains=instance) | models.Q(server__ip_address__icontains=instance)
        )

    if server_id:
        try:
            alert_history_query = alert_history_query.filter(server_id=int(server_id))
        except (ValueError, TypeError):
            pass  # Invalid server_id, ignore filter

    if alert_type:
        valid_alert_types = [choice[0] for choice in AlertHistory.AlertType.choices]
        if alert_type in valid_alert_types:
            alert_history_query = alert_history_query.filter(alert_type=alert_type)

    if status:
        valid_statuses = [choice[0] for choice in AlertHistory.AlertStatus.choices]
        if status in valid_statuses:
            alert_history_query = alert_history_query.filter(status=status)

    # Handle acknowledged filter (maps to resolved/triggered)
    if acknowledged == 'true':       # Acknowledged = resolved
        alert_history_query = alert_history_query.filter(status='resolved')
    elif acknowledged == 'false':    # Unacknowledged = triggered
        alert_history_query = alert_history_query.filter(status='triggered')

    # Category filter (derived from alert type): narrow by the alert_types that map to the
    # chosen category. Categories with no rows on this page yield an empty result.
    if category:
        valid_categories = {c for c, _ in alert_categories.AlertCategory.choices}
        if category in valid_categories:
            ah_types = [t for t, _ in AlertHistory.AlertType.choices
                        if alert_categories.for_alert_type(t) == category]
            alert_history_query = (alert_history_query.filter(alert_type__in=ah_types)
                                   if ah_types else alert_history_query.none())

    # Get alerts ordered by most recent first
    alerts_list = list(alert_history_query.order_by('-sent_at')[:500])

    # Group alerts by incident if requested
    alert_groups = None
    if group_by_incident and alerts_list:
        alert_groups = _group_alerts_by_incident(alerts_list, time_window_minutes=5)

    # Build the unified list (alerts only).
    unified_items = []
    for alert in alerts_list:
        if alert.status == 'triggered':
            duration_seconds = (now - alert.sent_at).total_seconds()
        elif alert.resolved_at:
            duration_seconds = (alert.resolved_at - alert.sent_at).total_seconds()
        else:
            duration_seconds = 0

        _cat = alert_categories.for_alert_type(alert.alert_type)
        unified_items.append({
            'type': 'alert',
            'object': alert,
            'timestamp': alert.sent_at,
            'duration_seconds': duration_seconds,
            'category': _cat,
            'category_label': alert_categories.label(_cat),
            'severity': getattr(alert, 'severity', '') or '',
        })

    # Sort: active (triggered) first, then most recent.
    def sort_key(item):
        is_active = item['object'].status == 'triggered'
        return (not is_active, -item['timestamp'].timestamp() if item['timestamp'] else 0)

    unified_items.sort(key=sort_key)
    unified_items = unified_items[:500]
    
    # Get filter options
    servers = Server.objects.all().order_by('name')
    alert_types = AlertHistory.AlertType.choices
    # Map status choices to Acknowledged/Unacknowledged terminology
    alert_statuses = [
        ('triggered', 'Unacknowledged'),
        ('resolved', 'Acknowledged'),
    ]
    
    # Counts for summary cards (window-scoped; alerts only -- anomalies aren't shown here).
    base_query_alerts = AlertHistory.objects.all()
    if time_range == '24h':
        base_query_alerts = base_query_alerts.filter(sent_at__gte=now - timedelta(hours=24))
    elif time_range == '7d':
        base_query_alerts = base_query_alerts.filter(sent_at__gte=now - timedelta(days=7))
    elif time_range == '30d':
        base_query_alerts = base_query_alerts.filter(sent_at__gte=now - timedelta(days=30))

    triggered_alerts = base_query_alerts.filter(status='triggered').count()
    resolved_alerts = base_query_alerts.filter(status='resolved').count()

    # Map: Triggered = Unacknowledged, Resolved = Acknowledged
    unacknowledged_items = triggered_alerts          # Unacknowledged Alerts
    acknowledged_items = resolved_alerts             # Acknowledged Alerts (resolved)
    total_critical = unacknowledged_items            # Total Critical Alerts (unacknowledged)
    # Critical Severity = alerts whose severity actually is CRITICAL (AlertHistory.severity
    # reuses the Anomaly.Severity values).
    critical_severity_count = base_query_alerts.filter(severity=Anomaly.Severity.CRITICAL).count()

    # Filtered count
    filtered_count = len(unified_items)
    
    context = {
        'unified_items': unified_items,  # Combined list with type indicators
        'alerts': alerts_list,  # Keep for backward compatibility if needed
        'alert_groups': alert_groups,  # Grouped alerts by incident
        'group_by_incident': group_by_incident,  # Whether grouping is enabled
        'servers': servers,
        'alert_types': alert_types,
        'alert_statuses': alert_statuses,
        'selected_server_id': server_id,
        'selected_alert_type': alert_type,
        'selected_status': status,
        'selected_time_range': time_range,
        'selected_category': category,
        'alert_category_choices': alert_categories.AlertCategory.choices,
        'selected_acknowledged': acknowledged,
        'selected_instance': instance,
        'triggered_count': total_critical,  # Total Critical Alerts (unacknowledged)
        'acknowledged_count': acknowledged_items,  # Acknowledged Alerts
        'unacknowledged_count': unacknowledged_items,  # Unacknowledged Alerts
        'critical_severity_count': critical_severity_count,
        'filtered_count': filtered_count,
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
                'sent_at': convert_to_display_timezone(alert.sent_at).isoformat() if alert.sent_at else None,
                'resolved_at': convert_to_display_timezone(alert.resolved_at).isoformat() if alert.resolved_at else None,
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
        
        # Mark as resolved (+ optional admin note / who resolved)
        import json as _json
        note = ""
        try:
            if request.body:
                note = (_json.loads(request.body).get("note") or "").strip()
        except (ValueError, TypeError):
            note = ""
        alert.status = AlertHistory.AlertStatus.RESOLVED
        alert.resolved_at = timezone.now()
        if note:
            alert.admin_note = note[:2000]
        if request.user.is_authenticated:
            alert.resolved_by = request.user
        alert.save()

        _log_user_action(request, "RESOLVE_ALERT",
                        f"Alert ID: {alert_id} on server {alert.server.name}")
        
        return JsonResponse({
            'success': True,
            'message': 'Alert marked as resolved',
            'alert': {
                'id': alert.id,
                'status': alert.status,
                'resolved_at': convert_to_display_timezone(alert.resolved_at).isoformat() if alert.resolved_at else None,
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
        note = (data.get('note') or '').strip()
        resolver = request.user if request.user.is_authenticated else None
        resolved_count = 0

        for alert in alerts:
            alert.status = AlertHistory.AlertStatus.RESOLVED
            alert.resolved_at = timezone.now()
            if note:
                alert.admin_note = note[:2000]
            if resolver:
                alert.resolved_by = resolver
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
def bulk_delete_alerts(request):
    """Bulk delete multiple alerts"""
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
        
        # Delete alerts
        deleted_result = AlertHistory.objects.filter(id__in=alert_ids).delete()
        deleted_count = deleted_result[0] if deleted_result else 0
        
        _log_user_action(request, "BULK_DELETE_ALERTS", 
                        f"Deleted {deleted_count} alerts: {alert_ids}")
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully deleted {deleted_count} alert(s)',
            'deleted_count': deleted_count,
            'requested_count': len(alert_ids)
        })
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON in request body'
        }, status=400)
    except Exception as e:
        error_logger.error(f"BULK_DELETE_ALERTS error: {str(e)}")
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
def server_memory_trend_api(request, server_id):
    """
    Long-window memory trend for the server page's "Memory Trend" chart.

    Plots absolute RAM used (memory_used, bytes) over 24h/7d with a fitted trend
    line and a projected time-to-full, so a slow memory leak is visible (the live
    1h percent chart can't show it). Reuses the leak-detection regression.

    GET /api/server/<id>/memory-trend/?range=24h|7d
    """
    from .models import MonitoringConfig
    from .utils.leak_detection import linear_trend, SYS_MIN_RATE_BYTES_HR, MIN_R2

    try:
        server = Server.objects.get(id=server_id)
    except Server.DoesNotExist:
        return JsonResponse({"error": "Server not found"}, status=404)

    try:
        is_suspended = server.monitoring_config.monitoring_suspended
    except (AttributeError, MonitoringConfig.DoesNotExist):
        is_suspended = False
    if is_suspended:
        return JsonResponse({"points": [], "trend_line": [], "leaking": False, "suspended": True})

    range_param = request.GET.get("range", "24h").lower()
    now = timezone.now()
    if range_param == "7d":
        since, max_points = now - timedelta(days=7), 168
    else:
        range_param = "24h"
        since, max_points = now - timedelta(hours=24), 144

    metrics = list(
        SystemMetric.objects.filter(server=server, timestamp__gte=since)
        .order_by("timestamp")
        .only("timestamp", "memory_used", "memory_total")
    )
    series = [(m, float(m.memory_used)) for m in metrics if m.memory_used]
    if len(series) < 2:
        return JsonResponse({"points": [], "trend_line": [], "leaking": False,
                             "range": range_param, "suspended": False})

    # Trend over the FULL series (before downsampling) for an accurate fit.
    pts = [(m.timestamp.timestamp(), used) for m, used in series]
    t0 = pts[0][0]
    slope, intercept, r2 = linear_trend([(t - t0, y) for t, y in pts])
    rate_bytes_hr = slope * 3600
    last_used = series[-1][1]
    total = next((float(m.memory_total) for m, _ in reversed(series) if m.memory_total), 0)
    headroom = (total - last_used) if total else 0
    days_to_full = (headroom / rate_bytes_hr / 24) if (rate_bytes_hr > 0 and headroom > 0) else None

    # Dashed projection line: y at the first and last sample from the fit.
    first_m, last_m = series[0][0], series[-1][0]
    span = pts[-1][0] - t0
    trend_line = [
        {"t": first_m.timestamp.isoformat(), "value": intercept},
        {"t": last_m.timestamp.isoformat(), "value": intercept + slope * span},
    ]

    leaking = bool(rate_bytes_hr >= SYS_MIN_RATE_BYTES_HR and r2 >= MIN_R2)
    if not leaking:
        leaking = Anomaly.objects.filter(
            server=server, resolved=False, metric_name__startswith="memory_leak"
        ).exists()

    # Down-sample the plotted line.
    if len(series) > max_points:
        step = max(1, len(series) // max_points)
        series = series[::step]
    points = [{"t": m.timestamp.isoformat(), "used": used} for m, used in series]

    return JsonResponse({
        "points": points,
        "total": total,
        "trend_line": trend_line,
        "rate_bytes_hr": rate_bytes_hr,
        "r2": round(r2, 3),
        "days_to_full": days_to_full,
        "leaking": leaking,
        "range": range_param,
        "suspended": False,
    })


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
        elif range_param == '30d':
            since = now - timedelta(days=30)
            max_points = 120
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
        elif range_param == '30d':
            since = now - timedelta(days=30)
            max_points = 120
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
    """Top 3 CPU processes from the latest pushed metric (no SSH)."""
    try:
        server = get_object_or_404(Server, id=server_id)
        metric = SystemMetric.objects.filter(server=server).order_by("-timestamp").first()
        procs = (metric.top_processes or {}).get("cpu", []) if metric else []
        top = [{
            "pid": p.get("pid"),
            "name": p.get("name") or p.get("command") or "Unknown",
            "usage": round(p.get("cpu_percent") or 0, 1),
        } for p in procs[:3]]
        return JsonResponse({"success": True, "processes": top, "server_id": server_id})
    except Exception as e:
        error_logger.error(f"GET_TOP_CPU_PROCESSES error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def get_top_ram_processes(request, server_id):
    """Top 3 memory processes from the latest pushed metric (no SSH)."""
    try:
        server = get_object_or_404(Server, id=server_id)
        metric = SystemMetric.objects.filter(server=server).order_by("-timestamp").first()
        procs = (metric.top_processes or {}).get("memory", []) if metric else []
        top = [{
            "pid": p.get("pid"),
            "name": p.get("name") or p.get("command") or "Unknown",
            "usage": round(p.get("memory_percent") or 0, 1),
        } for p in procs[:3]]
        return JsonResponse({"success": True, "processes": top, "server_id": server_id})
    except Exception as e:
        error_logger.error(f"GET_TOP_RAM_PROCESSES error: {str(e)}")
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
            "timestamp": convert_to_display_timezone(anomaly.timestamp).isoformat() if anomaly.timestamp else None,
            "metric_type": anomaly.metric_type,
            "metric_name": anomaly.metric_name,
            "metric_value": anomaly.metric_value,
            "severity": anomaly.severity,
            "anomaly_score": anomaly.anomaly_score,
            "explanation": anomaly.explanation or "",
            "llm_generated": anomaly.llm_generated,
            "acknowledged": anomaly.acknowledged,
            "resolved": anomaly.resolved,
            "resolved_at": convert_to_display_timezone(anomaly.resolved_at).isoformat() if anomaly.resolved_at else None,
        }

        # Plain-language "what to do" + when it started / how long.
        response_data["recommendation"] = _anomaly_recommendation(anomaly)
        if anomaly.timestamp:
            end = _anomaly_window_end(anomaly)
            if end:
                response_data["duration_text"] = "lasted " + _humanize_duration(
                    (end - anomaly.timestamp).total_seconds())
            else:
                response_data["duration_text"] = "ongoing for " + _humanize_duration(
                    (timezone.now() - anomaly.timestamp).total_seconds())
        else:
            response_data["duration_text"] = None

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

        # Re-word any legacy "Nσ above its recent median" text into plain English.
        response_data["explanation"] = _plainify_explanation(response_data["explanation"])
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


def _plainify_explanation(text):
    """Re-word legacy sigma/median anomaly text into plain English (display only)."""
    if not text:
        return text
    import re
    m = re.match(r"\s*(.+?) ([\d.]+)% is .*?median of ([\d.]+)% \(normal\s*[≤<=]+\s*~?([\d.]+)%\)\.?", text)
    if m:
        label, val, med, hi = m.groups()
        return f"{label} rose to {val}%, well above its usual ~{med}% (normally under ~{hi}%)."
    return text


def _anomaly_window_end(anomaly):
    """End of an anomaly's true window: when its metric returned to normal
    (recovered_at, auto-detected), else the admin resolution time, else None
    (still ongoing). Duration is measured start -> this end."""
    if getattr(anomaly, "recovered_at", None):
        return anomaly.recovered_at
    if anomaly.resolved and anomaly.resolved_at:
        return anomaly.resolved_at
    return None


def _humanize_duration(seconds):
    """Seconds -> short human string (e.g. '45s', '12 min', '3h 5m', '2d 4h')."""
    seconds = int(max(0, seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"{mins} min"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h {mins % 60}m"
    days = hours // 24
    return f"{days}d {hours % 24}h"


def _anomaly_recommendation(anomaly):
    """Plain-language 'what it likely means and what to do' for an anomaly."""
    mt = (anomaly.metric_type or "").lower()
    mn = (anomaly.metric_name or "").lower()
    if mn.startswith("memory_leak:"):
        proc = anomaly.metric_name.split(":", 1)[1]
        return (f"The process “{proc}” is steadily using more and more memory — this usually means a memory leak. "
                "Restarting that process frees the memory as a quick fix; for a real fix, update or patch the app. "
                "The server’s Memory Trend chart shows whether it climbs again.")
    if mn == "memory_leak":
        return ("The server’s overall memory keeps climbing and could eventually run out. Open the server page → "
                "Top RAM processes to find the growing one and restart or patch it. The Memory Trend chart shows "
                "how fast it’s rising and roughly when it would fill up.")
    if mn == "shm_leak":
        return ("Shared-memory segments are being left behind (no process attached) and holding RAM. On the server run "
                "“ipcs -m” to list them and “ipcrm” to remove stale ones, or restart the app that created them.")
    if mn == "semaphore_leak":
        return ("Semaphore sets are piling up (a leak). Run “ipcs -s” on the server to inspect them and clean up the "
                "leaked ones, or restart the owning application.")
    if mn == "devshm_leak":
        return ("Files under /dev/shm (shared memory) keep growing. Check what’s writing there and clear old files, "
                "or restart the owning application.")
    if mt == "cpu":
        return ("A process is using more CPU than this server normally does. Open the server page → Top CPU processes "
                "to see which one, and check whether it’s expected (a backup, a build, or a traffic spike). "
                "If it stays high, investigate or restart that process.")
    if mt == "memory":
        return ("Memory use rose above this server’s normal level. Check Top RAM processes on the server page — one may "
                "be using more than usual. If it keeps climbing it could be a leak; the Memory Trend chart will show that.")
    if mt == "disk":
        return ("A disk is filling up. Free space by clearing old logs, caches, or unused files, or expand the volume. "
                "If it reaches 100% the server can start failing.")
    if mt == "network":
        return ("Network traffic briefly spiked well above normal — often a large transfer, a backup job, or unusual "
                "traffic. Check what was running at that time. It’s only a concern if it’s unexpected or sustained.")
    return "Check the server’s recent activity and the related resource (CPU, memory, disk, or network) around this time."


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
        
        # Optional admin note / reason recorded at resolve time.
        note = ""
        try:
            if request.body:
                import json as _json
                note = (_json.loads(request.body).get("note") or "").strip()
        except (ValueError, TypeError):
            note = ""

        # Mark as resolved
        anomaly.acknowledged = True
        anomaly.resolved = True
        anomaly.resolved_at = timezone.now()
        if note:
            anomaly.admin_note = note[:2000]
        if request.user.is_authenticated:
            anomaly.resolved_by = request.user
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
        note = (data.get("note") or "").strip()
        resolver = request.user if request.user.is_authenticated else None
        resolved_count = 0
        server_ids = set()

        for anomaly in anomalies:
            # Mark as resolved
            anomaly.acknowledged = True
            anomaly.resolved = True
            anomaly.resolved_at = timezone.now()
            if note:
                anomaly.admin_note = note[:2000]
            if resolver:
                anomaly.resolved_by = resolver
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
@require_http_methods(["GET"])
def anomaly_notifications_api(request):
    """Global unresolved-anomaly feed for the dashboard anomalies icon.

    Anomalies are notifications (not alerts), surfaced only here. Returns the count of
    unresolved anomalies across all servers plus the most recent ones for the panel.
    """
    try:
        qs = (Anomaly.objects.filter(resolved=False)
              .select_related("server").order_by("-timestamp"))
        count = qs.count()
        items = [{
            "id": a.id,
            "server": a.server.name,
            "server_id": a.server_id,
            "metric_type": (a.metric_type or "").upper(),
            "metric_name": a.metric_name,
            "severity": a.severity or "LOW",
            "value": round(a.metric_value, 1) if a.metric_value is not None else None,
            "explanation": a.explanation or a.metric_name,
            "timestamp": a.timestamp.isoformat() if a.timestamp else None,
        } for a in qs[:50]]
        return JsonResponse({"success": True, "count": count, "items": items})
    except Exception as e:
        error_logger.error(f"Error in anomaly_notifications_api: {e}")
        return JsonResponse({"success": False, "count": 0, "items": []}, status=500)


@staff_member_required
@require_http_methods(["POST"])
def anomaly_clear_all_api(request):
    """Clear (mark resolved) every unresolved anomaly -- the 'Clear all' action on the
    dashboard anomalies panel. Resolved anomalies stay in the DB as history."""
    try:
        resolver = request.user if request.user.is_authenticated else None
        qs = Anomaly.objects.filter(resolved=False)
        server_ids = list(qs.values_list("server_id", flat=True).distinct())
        count = qs.update(resolved=True, acknowledged=True,
                          resolved_at=timezone.now(), resolved_by=resolver)
        try:
            from .anomaly_cache import AnomalyCache
            for sid in server_ids:
                AnomalyCache.clear(sid)
        except Exception as e:
            app_logger.warning(f"Failed to clear anomaly cache: {e}")
        _log_user_action(request, "CLEAR_ALL_ANOMALIES", f"Cleared {count} anomalies")
        return JsonResponse({"success": True, "cleared_count": count})
    except Exception as e:
        error_logger.error(f"Error in anomaly_clear_all_api: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


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
def dashboard_summary_stats_api(request):
    """API endpoint for dashboard summary statistics"""
    try:
        from datetime import datetime, timedelta
        
        total_servers = Server.objects.count()
        # Anomalies are notifications (shown only on the dashboard anomalies bell), NOT
        # alerts -- so they're excluded from the alerts banner/summary count.
        active_alerts = AlertHistory.objects.filter(status='triggered').count()

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
                'active_alerts': active_alerts,
                'alert_trend': alert_trend,
                'critical_vms': critical_count,
                'sla_compliance': round(sla_compliance, 1),
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_SUMMARY_STATS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def _parse_period_to_hours(period):
    """Convert period string (24h, 7d, 30d) to hours"""
    if period.endswith('h'):
        return int(period[:-1])
    elif period.endswith('d'):
        return int(period[:-1]) * 24
    else:
        return 24  # default to 24 hours

@staff_member_required
@require_http_methods(["GET"])
def dashboard_cpu_trend_api(request, period='24h'):
    """API endpoint for CPU trend data"""
    try:
        from datetime import timedelta
        
        hours = _parse_period_to_hours(period)
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
        
        # Group by hour and calculate average and peak
        hourly_data = {}
        for metric in metrics:
            hour_key = metric['timestamp'].replace(minute=0, second=0, microsecond=0)
            if hour_key not in hourly_data:
                hourly_data[hour_key] = []
            hourly_data[hour_key].append(metric['cpu_percent'] or 0)
        
        # Calculate averages, peaks and prepare data
        for hour, values in sorted(hourly_data.items()):
            avg_cpu = sum(values) / len(values) if values else 0
            max_cpu = max(values) if values else 0
            
            point = {
                'timestamp': hour.isoformat(),
                'value': round(avg_cpu, 2)
            }
            
            # Include peak if it's significantly higher than average (indicating a spike)
            if max_cpu > avg_cpu + 10:  # Spike threshold: 10% above average
                point['peak'] = round(max_cpu, 2)
            
            data_points.append(point)
        
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
def dashboard_memory_trend_api(request, period='24h'):
    """API endpoint for memory trend data"""
    try:
        from datetime import timedelta
        
        hours = _parse_period_to_hours(period)
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
            max_memory = max(values) if values else 0
            
            point = {
                'timestamp': hour.isoformat(),
                'value': round(avg_memory, 2)
            }
            
            # Include peak if it's significantly higher than average (indicating a spike)
            if max_memory > avg_memory + 10:  # Spike threshold: 10% above average
                point['peak'] = round(max_memory, 2)
            
            data_points.append(point)
        
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
def dashboard_network_trend_api(request, period='24h'):
    """API endpoint for network trend data (inbound/outbound)"""
    try:
        from datetime import timedelta
        import json
        
        hours = _parse_period_to_hours(period)
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
        
        # Store previous values per server to calculate differences
        prev_values = {}  # {server_id: {recv: int, sent: int, timestamp: datetime}}
        
        hourly_inbound = {}
        hourly_outbound = {}
        
        for metric in metrics:
            # Ensure timestamp is timezone-aware datetime
            timestamp = metric['timestamp']
            # Timestamps from the database should already be timezone-aware, but handle edge cases
            if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo is None:
                from django.utils import timezone as tz
                timestamp = tz.make_aware(timestamp)
            
            hour_key = timestamp.replace(minute=0, second=0, microsecond=0)
            network_data = metric.get('network_io')
            server_id = metric['server_id']
            
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
                    
                    # Calculate difference from previous measurement for this server
                    if server_id in prev_values:
                        prev = prev_values[server_id]
                        time_diff = (timestamp - prev['timestamp']).total_seconds()
                        
                        if time_diff > 0:  # Avoid division by zero
                            # Calculate bytes per second, then convert to MB/s
                            # Handle counter rollover (if new value is less than prev, assume rollover)
                            recv_diff = total_recv - prev['recv']
                            sent_diff = total_sent - prev['sent']
                            
                            # Only process if differences are positive (ignore rollovers for now)
                            if recv_diff >= 0 and sent_diff >= 0:
                                recv_bytes_per_sec = recv_diff / time_diff
                                sent_bytes_per_sec = sent_diff / time_diff
                                
                                # Convert bytes/sec to MB/s
                                recv_mb_per_sec = recv_bytes_per_sec / (1024 * 1024)
                                sent_mb_per_sec = sent_bytes_per_sec / (1024 * 1024)
                                
                                # For hourly aggregation, we want average MB/s for that hour
                                if hour_key not in hourly_inbound:
                                    hourly_inbound[hour_key] = []
                                    hourly_outbound[hour_key] = []
                                hourly_inbound[hour_key].append(recv_mb_per_sec)
                                hourly_outbound[hour_key].append(sent_mb_per_sec)
                    
                    # Update previous values for this server
                    prev_values[server_id] = {
                        'recv': total_recv,
                        'sent': total_sent,
                        'timestamp': timestamp
                    }
                except Exception as e:
                    error_logger.error(f"Network trend calculation error: {str(e)}")
                    pass
        
        data_points = []
        for hour in sorted(set(list(hourly_inbound.keys()) + list(hourly_outbound.keys()))):
            inbound_list = hourly_inbound.get(hour, [])
            outbound_list = hourly_outbound.get(hour, [])
            
            # Calculate average, avoiding division by zero
            avg_inbound = sum(inbound_list) / len(inbound_list) if inbound_list else 0.0
            avg_outbound = sum(outbound_list) / len(outbound_list) if outbound_list else 0.0
            
            # Round to 6 decimal places to preserve very small values for frontend scaling
            # The frontend will handle appropriate display precision
            data_points.append({
                'timestamp': hour.isoformat(),
                'inbound': round(avg_inbound, 6),
                'outbound': round(avg_outbound, 6)
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
                # disk_io_read and disk_io_write are in bytes/second
                # Convert bytes/sec to IOPS: assume 4KB (4096 bytes) average I/O size
                # IOPS = (bytes/sec) / (bytes per I/O) = (bytes/sec) / 4096
                read_bytes_per_sec = latest_metric.disk_io_read or 0
                write_bytes_per_sec = latest_metric.disk_io_write or 0
                read_iops = read_bytes_per_sec / 4096 if read_bytes_per_sec > 0 else 0
                write_iops = write_bytes_per_sec / 4096 if write_bytes_per_sec > 0 else 0
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

        # Recent alerts only (anomalies are notifications, surfaced on the bell, not here)
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
            
            display_timestamp = convert_to_display_timezone(alert.sent_at)
            alert_list.append({
                'id': alert.id,
                'title': f"{alert.alert_type} Alert",
                'host': alert.server.name,
                'description': alert.message,
                'timestamp': display_timestamp.isoformat() if display_timestamp else alert.sent_at.isoformat(),
                'time_ago': time_str,
                'severity': severity,
                'status': alert.status
            })
        
        current_time = convert_to_display_timezone(timezone.now())
        return JsonResponse({
            'success': True,
            'data': alert_list,
            'timestamp': current_time.isoformat() if current_time else timezone.now().isoformat()
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
        from urllib.parse import unquote
        
        # Decode URL-encoded mount point (e.g., %2Fhome -> /home)
        mount_point = unquote(mount_point)
        
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
        server_list = [{'id': s.id, 'name': s.name, 'ip_address': s.ip_address} for s in servers]
        
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
        
        # Virtual filesystem types to exclude from disk reporting
        IGNORED_FSTYPES = {
            'squashfs', 'tmpfs', 'devtmpfs', 'proc', 'sysfs',
            'cgroup', 'cgroup2', 'ramfs', 'overlay', 'udev', 'virtfs'
        }
        
        # Get latest metric with disk usage data
        latest_metric = SystemMetric.objects.filter(server=server).order_by('-timestamp').first()
        
        mount_points = []
        if latest_metric:
            if latest_metric.disk_usage:
                try:
                    # Handle both string and dict formats
                    if isinstance(latest_metric.disk_usage, str):
                        disk_data = json.loads(latest_metric.disk_usage)
                    else:
                        disk_data = latest_metric.disk_usage
                    
                    # Extract mount points from dict, filtering out virtual filesystems
                    if isinstance(disk_data, dict):
                        for mount_point, disk_info in disk_data.items():
                            if not mount_point:
                                continue
                            
                            # Skip virtfs mount points (e.g., /home/virtfs/... or named 'virtfs')
                            if '/virtfs/' in mount_point or mount_point == 'virtfs':
                                continue
                            
                            # Check if this is a virtual filesystem
                            fstype = None
                            if isinstance(disk_info, dict):
                                fstype = disk_info.get('fstype', '').lower()
                            
                            # Skip virtual filesystems
                            if fstype and fstype in IGNORED_FSTYPES:
                                continue
                            
                            mount_points.append(mount_point)
                        
                        # Sort mount points, with '/' first
                        mount_points = sorted(mount_points, key=lambda x: (x != '/', x))
                except (json.JSONDecodeError, TypeError, AttributeError) as e:
                    error_logger.warning(f"DASHBOARD_DISK_MOUNT_POINTS_API: Error parsing disk_usage for server {server_id}: {str(e)}")
            else:
                error_logger.warning(f"DASHBOARD_DISK_MOUNT_POINTS_API: No disk_usage data for server {server_id}")
        else:
            error_logger.warning(f"DASHBOARD_DISK_MOUNT_POINTS_API: No metrics found for server {server_id}")
        
        # If no mount points found, return common defaults
        if not mount_points:
            mount_points = ['/']
            error_logger.info(f"DASHBOARD_DISK_MOUNT_POINTS_API: Using default mount point '/' for server {server_id}")
        
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
                'timestamp': convert_to_display_timezone(activity.timestamp).isoformat() if activity.timestamp else None,
                'time_ago': time_str,
                'ip_address': str(activity.ip_address)
            })
        
        current_time = convert_to_display_timezone(timezone.now())
        return JsonResponse({
            'success': True,
            'data': activity_list,
            'timestamp': current_time.isoformat() if current_time else timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_LOGIN_ACTIVITY_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_sli_compliance_api(request):
    """API endpoint for overall SLI/SLO compliance summary for dashboard"""
    try:
        from core.models import SLIMeasurement
        from django.db.models import Count, Q, Avg
        
        # Get latest measurements for each server and metric type
        servers = Server.objects.all()
        metric_types = ['UPTIME', 'CPU', 'MEMORY', 'DISK', 'NETWORK', 'RESPONSE_TIME', 'ERROR_RATE']
        
        total_servers = servers.count()
        compliant_servers = 0
        by_metric = {}
        
        # Calculate overall compliance per metric type
        for metric_type in metric_types:
            # Get latest measurement for each server for this metric
            latest_measurements = SLIMeasurement.objects.filter(
                metric_type=metric_type
            ).order_by('server', '-time_window_end').distinct('server')
            
            compliant_count = latest_measurements.filter(is_compliant=True).count()
            total_count = latest_measurements.count()
            
            compliance_percentage = (compliant_count / total_count * 100) if total_count > 0 else 0.0
            
            by_metric[metric_type] = {
                'compliant_servers': compliant_count,
                'total_servers': total_count,
                'compliance_percentage': round(compliance_percentage, 2)
            }
        
        # Calculate overall compliance (servers that are compliant for all metrics)
        # This is simplified - in reality, you might want different logic
        overall_compliance = 0.0
        if by_metric:
            avg_compliance = sum(m['compliance_percentage'] for m in by_metric.values()) / len(by_metric)
            overall_compliance = round(avg_compliance, 2)
        
        return JsonResponse({
            'success': True,
            'data': {
                'total_servers': total_servers,
                'compliant_servers': compliant_servers,
                'compliance_percentage': overall_compliance,
                'by_metric': by_metric
            }
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_SLI_COMPLIANCE_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def server_sli_compliance_api(request, server_id):
    """API endpoint for per-server SLI/SLO compliance details"""
    try:
        from core.models import SLIMeasurement, ServiceLatencyMeasurement
        from core.sli_utils import get_slo_config
        
        server = get_object_or_404(Server, id=server_id)
        
        # Get latest measurements for each metric type
        metric_types = ['UPTIME', 'CPU', 'MEMORY', 'DISK', 'NETWORK', 'RESPONSE_TIME', 'ERROR_RATE']
        metrics_data = []
        
        for metric_type in metric_types:
            # Get latest measurement
            latest = SLIMeasurement.objects.filter(
                server=server,
                metric_type=metric_type
            ).order_by('-time_window_end').first()
            
            if latest:
                metrics_data.append({
                    'metric_type': metric_type,
                    'sli_value': latest.sli_value,
                    'slo_target': latest.slo_target,
                    'is_compliant': latest.is_compliant,
                    'compliance_percentage': latest.compliance_percentage,
                    'time_window_end': latest.time_window_end.isoformat() if latest.time_window_end else None
                })
            else:
                # No measurement yet, get SLO config to show target
                slo_config = get_slo_config(server, metric_type)
                if slo_config:
                    metrics_data.append({
                        'metric_type': metric_type,
                        'sli_value': None,
                        'slo_target': slo_config.target_value,
                        'is_compliant': None,
                        'compliance_percentage': None,
                        'time_window_end': None
                    })
        
        # For RESPONSE_TIME, get service latency measurements (only for services with monitoring_enabled=True)
        service_latencies = []
        if any(m['metric_type'] == 'RESPONSE_TIME' for m in metrics_data):
            monitored_services = Service.objects.filter(
                server=server,
                monitoring_enabled=True
            )
            
            for service in monitored_services:
                latest_latency = ServiceLatencyMeasurement.objects.filter(
                    service=service,
                    success=True
                ).order_by('-timestamp').first()
                
                if latest_latency:
                    service_latencies.append({
                        'service_id': service.id,
                        'service_name': service.name,
                        'latency_ms': latest_latency.latency_ms,
                        'timestamp': latest_latency.timestamp.isoformat() if latest_latency.timestamp else None
                    })
        
        # Calculate overall compliance
        compliant_metrics = sum(1 for m in metrics_data if m.get('is_compliant') is True)
        total_metrics = sum(1 for m in metrics_data if m.get('is_compliant') is not None)
        overall_compliance = (compliant_metrics / total_metrics * 100) if total_metrics > 0 else 0.0
        
        return JsonResponse({
            'success': True,
            'data': {
                'server_id': server.id,
                'server_name': server.name,
                'metrics': metrics_data,
                'service_latencies': service_latencies,
                'overall_compliance': round(overall_compliance, 2)
            }
        })
    except Exception as e:
        error_logger.error(f"SERVER_SLI_COMPLIANCE_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_response_time_trend_api(request, period='24h'):
    """
    API endpoint for response time trend data.
    Returns latency measurements for all monitored services.
    
    Query params:
        - server_id: 'all' for average across all servers, or specific server ID
    Path params:
        - period: time period (24h, 7d, etc.)
    """
    try:
        from datetime import timedelta
        from core.models import ServiceLatencyMeasurement, Service
        from django.db.models import Avg
        
        hours = _parse_period_to_hours(period)
        server_id = request.GET.get('server_id', 'all')
        since = timezone.now() - timedelta(hours=hours)
        
        # Build query based on server filter
        if server_id and server_id != 'all':
            try:
                server = Server.objects.get(id=int(server_id))
                measurements = ServiceLatencyMeasurement.objects.filter(
                    service__server=server,
                    service__monitoring_enabled=True,
                    timestamp__gte=since,
                    success=True
                )
            except (Server.DoesNotExist, ValueError):
                return JsonResponse({"success": False, "error": "Invalid server ID"}, status=400)
        else:
            measurements = ServiceLatencyMeasurement.objects.filter(
                service__monitoring_enabled=True,
                timestamp__gte=since,
                success=True
            )
        
        # Group by hour and calculate averages
        hourly_data = {}
        for m in measurements.values('timestamp', 'latency_ms'):
            hour_key = m['timestamp'].replace(minute=0, second=0, microsecond=0)
            if hour_key not in hourly_data:
                hourly_data[hour_key] = []
            if m['latency_ms'] is not None:
                hourly_data[hour_key].append(m['latency_ms'])
        
        # Prepare data points
        data_points = []
        for hour, values in sorted(hourly_data.items()):
            avg_latency = sum(values) / len(values) if values else 0
            data_points.append({
                'timestamp': hour.isoformat(),
                'value': round(avg_latency, 2)
            })
        
        # Calculate statistics
        all_values = [p['value'] for p in data_points if p['value'] > 0]
        current = data_points[-1]['value'] if data_points else 0
        peak = max(all_values) if all_values else 0
        average = sum(all_values) / len(all_values) if all_values else 0
        
        # Get monitored services count
        if server_id and server_id != 'all':
            monitored_count = Service.objects.filter(
                server_id=int(server_id),
                monitoring_enabled=True,
                port__isnull=False
            ).exclude(port=0).count()
        else:
            monitored_count = Service.objects.filter(
                monitoring_enabled=True,
                port__isnull=False
            ).exclude(port=0).count()
        
        return JsonResponse({
            'success': True,
            'data': {
                'points': data_points,
                'current': round(current, 2),
                'peak': round(peak, 2),
                'average': round(average, 2),
                'monitored_services': monitored_count,
                'measurement_count': measurements.count()
            },
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_RESPONSE_TIME_TREND_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_monitored_services_api(request):
    """
    API endpoint to get list of monitored services with their latest latency.
    Used by the response time dashboard component.
    """
    try:
        from core.models import ServiceLatencyMeasurement, Service
        
        server_id = request.GET.get('server_id', 'all')
        
        # Get monitored services
        if server_id and server_id != 'all':
            try:
                services = Service.objects.filter(
                    server_id=int(server_id),
                    monitoring_enabled=True,
                    port__isnull=False
                ).exclude(port=0).select_related('server')
            except ValueError:
                return JsonResponse({"success": False, "error": "Invalid server ID"}, status=400)
        else:
            services = Service.objects.filter(
                monitoring_enabled=True,
                port__isnull=False
            ).exclude(port=0).select_related('server')
        
        services_data = []
        for service in services:
            # Get latest measurement
            latest = ServiceLatencyMeasurement.objects.filter(
                service=service
            ).order_by('-timestamp').first()
            
            services_data.append({
                'id': service.id,
                'name': service.name,
                'port': service.port,
                'bind_address': service.bind_address,
                'server_id': service.server.id,
                'server_name': service.server.name,
                'status': service.status,
                'is_localhost': service.bind_address in ('127.0.0.1', '::1', 'localhost') if service.bind_address else False,
                'latest_latency': {
                    'latency_ms': latest.latency_ms if latest else None,
                    'success': latest.success if latest else None,
                    'measurement_type': latest.measurement_type if latest else None,
                    'timestamp': latest.timestamp.isoformat() if latest else None,
                    'error_message': latest.error_message if latest else None
                } if latest else None
            })
        
        return JsonResponse({
            'success': True,
            'data': {
                'services': services_data,
                'total_count': len(services_data)
            }
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_MONITORED_SERVICES_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_trend_insights_api(request):
    """
    API endpoint for trend insights - detects recurring alert patterns.
    
    Query params:
        - lookback_days: Number of days to analyze (default: 30)
        - alert_types: Comma-separated alert types (default: CPU,MEMORY,DISK)
    
    Returns detected patterns like:
        - CPU spikes at 2AM daily
        - Memory alerts on Fridays
    """
    try:
        from core.trend_detection import get_trend_summary, detect_all_server_patterns
        
        lookback_days = int(request.GET.get('lookback_days', 30))
        alert_types_param = request.GET.get('alert_types', 'CPU,MEMORY,DISK')
        alert_types = [t.strip().upper() for t in alert_types_param.split(',')]
        
        # Get insights
        insights = detect_all_server_patterns(
            alert_types=alert_types,
            lookback_days=lookback_days,
            min_alerts=5
        )
        
        # Format for API response
        formatted_insights = []
        for insight in insights:
            pattern = insight['pattern']
            formatted_insights.append({
                'server_id': insight['server_id'],
                'server_name': insight['server_name'],
                'alert_type': insight['alert_type'],
                'pattern_type': pattern['pattern_type'],
                'pattern_description': pattern['pattern_description'],
                'confidence': round(pattern['confidence'], 1),
                'peak_hour': pattern.get('peak_hour'),
                'peak_day': pattern.get('peak_day'),
                'total_alerts': pattern['total_alerts'],
                'recommendation': pattern['recommendation']
            })
        
        # Count unique servers with patterns
        servers_with_patterns = len(set(i['server_id'] for i in insights))
        
        return JsonResponse({
            'success': True,
            'data': {
                'insights': formatted_insights,
                'total_patterns': len(formatted_insights),
                'servers_with_patterns': servers_with_patterns,
                'analysis_period_days': lookback_days
            }
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_TREND_INSIGHTS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET"])
def dashboard_reliability_metrics_api(request):
    """
    API endpoint for reliability metrics time-series data.
    
    Returns CPU, Memory, Disk, and Error Rate data over time for charting.
    
    Query params:
        - period: '24h', '7d', or '30d' (default: '24h')
        - server_id: 'all' for average across servers, or specific server ID
    
    Returns:
        {
            'success': True,
            'data': {
                'cpu': [{timestamp, value}, ...],
                'memory': [{timestamp, value}, ...],
                'disk': [{timestamp, value}, ...],
                'error_rate': [{timestamp, value}, ...],
                'period': '24h',
                'interval': 'hour',
                'start_date': ISO string,
                'end_date': ISO string
            }
        }
    """
    try:
        from core.sli_utils import get_reliability_metrics_timeseries
        
        period = request.GET.get('period', '24h')
        server_id = request.GET.get('server_id', 'all')
        
        # Validate period
        if period not in ['24h', '7d', '30d']:
            period = '24h'
        
        # Get time-series data
        data = get_reliability_metrics_timeseries(server_id, period)
        
        # Calculate current/average values for summary
        def calc_stats(points):
            if not points:
                return {'current': 0, 'average': 0, 'peak': 0}
            values = [p['value'] for p in points if p['value'] is not None]
            if not values:
                return {'current': 0, 'average': 0, 'peak': 0}
            return {
                'current': values[-1] if values else 0,
                'average': round(sum(values) / len(values), 2),
                'peak': max(values)
            }
        
        data['stats'] = {
            'cpu': calc_stats(data.get('cpu', [])),
            'memory': calc_stats(data.get('memory', [])),
            'disk': calc_stats(data.get('disk', [])),
            'error_rate': calc_stats(data.get('error_rate', []))
        }
        
        return JsonResponse({
            'success': True,
            'data': data
        })
    except Exception as e:
        error_logger.error(f"DASHBOARD_RELIABILITY_METRICS_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["GET", "POST"])
def server_slo_config_api(request, server_id):
    """API endpoint for SLO configuration: GET to retrieve, POST to update"""
    try:
        from core.models import SLOConfig
        import json
        
        server = get_object_or_404(Server, id=server_id)
        
        if request.method == "GET":
            # Get all SLO configs for this server (including global defaults)
            slo_configs = SLOConfig.objects.filter(
                Q(server=server) | Q(server=None)
            ).order_by('metric_type')
            
            configs_list = []
            for config in slo_configs:
                configs_list.append({
                    'id': config.id,
                    'server_id': config.server.id if config.server else None,
                    'metric_type': config.metric_type,
                    'target_value': config.target_value,
                    'target_operator': config.target_operator,
                    'time_window_days': config.time_window_days,
                    'enabled': config.enabled,
                    'is_global': config.server is None
                })
            
            return JsonResponse({
                'success': True,
                'data': configs_list
            })
        
        elif request.method == "POST":
            # Update or create SLO config
            data = json.loads(request.body)
            metric_type = data.get('metric_type')
            target_value = data.get('target_value')
            target_operator = data.get('target_operator', 'gte')
            time_window_days = data.get('time_window_days')
            enabled = data.get('enabled', True)
            
            if not metric_type or target_value is None:
                return JsonResponse({
                    'success': False,
                    'error': 'metric_type and target_value are required'
                }, status=400)
            
            # Update or create
            slo_config, created = SLOConfig.objects.update_or_create(
                server=server,
                metric_type=metric_type,
                defaults={
                    'target_value': float(target_value),
                    'target_operator': target_operator,
                    'time_window_days': int(time_window_days) if time_window_days else None,
                    'enabled': enabled
                }
            )
            
            return JsonResponse({
                'success': True,
                'created': created,
                'data': {
                    'id': slo_config.id,
                    'server_id': slo_config.server.id if slo_config.server else None,
                    'metric_type': slo_config.metric_type,
                    'target_value': slo_config.target_value,
                    'target_operator': slo_config.target_operator,
                    'time_window_days': slo_config.time_window_days,
                    'enabled': slo_config.enabled
                }
            })
    
    except Exception as e:
        error_logger.error(f"SERVER_SLO_CONFIG_API error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_http_methods(["DELETE"])
def server_slo_config_delete_api(request, server_id, metric_type):
    """API endpoint to delete server-specific SLO config (revert to global default)"""
    try:
        from core.models import SLOConfig
        
        server = get_object_or_404(Server, id=server_id)
        
        # Find server-specific SLO config
        try:
            slo_config = SLOConfig.objects.get(server=server, metric_type=metric_type)
            slo_config.delete()
            
            return JsonResponse({
                'success': True,
                'message': f'SLO config for {metric_type} deleted. Server will use global default.'
            })
        except SLOConfig.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': 'SLO config not found'
            }, status=404)
    
    except Exception as e:
        error_logger.error(f"SERVER_SLO_CONFIG_DELETE_API error: {str(e)}")
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


# ---------------------------------------------------------------------------
# Monitoring domains (taxonomy-based landing pages)
#
# Part of redefining monitoring around the standard taxonomy:
# Infrastructure, APM, Logs, Security/SIEM, User Experience, Business.
# Infrastructure and Logs already have full pages (the dashboard and Logs
# Analysis); these landing pages give the remaining domains a real home and
# describe what they monitor + what is being built.
# ---------------------------------------------------------------------------

MONITORING_DOMAINS = {
    "security": {
        "title": "Security Monitoring",
        "tagline": "Detect suspicious access and protect the fleet (SIEM).",
        "pillar": "Logs + Metrics",
        "status": "building",
        "monitors": [
            "Login attempts (success / failure) and their sources",
            "Suspicious authentication patterns (brute force, new locations)",
            "File-integrity changes on monitored hosts (planned)",
            "Security-relevant log events (planned)",
        ],
        "roadmap": [
            "Failed-login spike detection + alerting",
            "Per-host file-integrity monitoring via the agent",
            "Security event timeline and SIEM-style search",
        ],
    },
    "uptime": {
        "title": "User Experience Monitoring",
        "tagline": "Catch outages before users do, with synthetic uptime checks.",
        "pillar": "Metrics (synthetic)",
        "status": "building",
        "monitors": [
            "Scheduled HTTP/TCP checks against your endpoints (synthetic)",
            "Uptime %, response time and status-code tracking",
            "Multi-step journey checks (planned)",
            "Real User Monitoring from browsers (planned)",
        ],
        "roadmap": [
            "Synthetic check engine (URL/port probes on a schedule)",
            "Uptime + latency dashboards and SLOs",
            "Downtime alerting via email / Slack",
        ],
    },
    "business": {
        "title": "Business Monitoring",
        "tagline": "Tie system health to business outcomes (KPIs).",
        "pillar": "Metrics (business)",
        "status": "planned",
        "monitors": [
            "Custom business KPIs (signups/hour, orders, revenue, ...)",
            "KPI trends and thresholds",
            "Correlation of KPI dips with infrastructure events (planned)",
        ],
        "roadmap": [
            "KPI ingestion API + data model",
            "KPI dashboards and alerting",
            "Link KPI anomalies to infra / app incidents",
        ],
    },
    "apm": {
        "title": "Application Performance (APM)",
        "tagline": "Code- and request-level performance and tracing.",
        "pillar": "Metrics + Traces",
        "status": "planned",
        "monitors": [
            "Request latency, error rates and throughput",
            "Service-to-service traces (planned)",
            "Database query timing (planned)",
        ],
        "roadmap": [
            "Expand existing service-latency probes",
            "Request tracing via app instrumentation",
        ],
        "existing_note": "Some of this exists today as service-latency and response-time metrics on the dashboard.",
    },
}


@staff_member_required
def monitoring_domain(request, slug):
    """Render the taxonomy landing page for a monitoring domain."""
    domain = MONITORING_DOMAINS.get(slug)
    if not domain:
        from django.http import Http404
        raise Http404("Unknown monitoring domain")

    context = {"show_sidebar": True, "slug": slug, "domain": domain}

    # The Security domain already has live data: surface recent login activity.
    if slug == "security":
        since = timezone.now() - timedelta(days=7)
        recent = LoginActivity.objects.filter(timestamp__gte=since)
        context["security_stats"] = {
            "total_7d": recent.count(),
            "failed_7d": recent.filter(status="failed").count(),
            "recent": list(LoginActivity.objects.order_by("-timestamp")[:8]),
        }

    context.update(admin.site.each_context(request))
    return render(request, "core/domain_landing.html", context)


# ---------------------------------------------------------------------------
# User Experience monitoring (synthetic uptime checks)
# ---------------------------------------------------------------------------
def _synthetic_check_summary(check):
    """Build a display summary (status, uptime, latency) for a check."""
    from .synthetic import uptime_percentage, avg_response_ms
    last = check.results.order_by("-timestamp").first()
    return {
        "check": check,
        "uptime_24h": uptime_percentage(check, 24),
        "avg_ms_24h": avg_response_ms(check, 24),
        "last_result": last,
    }


@staff_member_required
def synthetic_checks_list(request):
    """User Experience domain: list all synthetic uptime checks with status."""
    checks = SyntheticCheck.objects.all()
    summaries = [_synthetic_check_summary(c) for c in checks]
    up = sum(1 for s in summaries if s["check"].last_status == SyntheticCheck.Status.UP)
    down = sum(1 for s in summaries if s["check"].last_status == SyntheticCheck.Status.DOWN)
    context = {
        "show_sidebar": True,
        "summaries": summaries,
        "total": len(summaries),
        "up_count": up,
        "down_count": down,
    }
    context.update(admin.site.each_context(request))
    return render(request, "core/uptime_list.html", context)


def _apply_check_form(request, check):
    """Populate a SyntheticCheck from POST data. Returns error message or None."""
    check.name = request.POST.get("name", "").strip()
    check.check_type = request.POST.get("check_type", SyntheticCheck.CheckType.HTTP)
    check.url = request.POST.get("url", "").strip()
    check.method = (request.POST.get("method", "GET") or "GET").strip().upper()
    check.expected_status = request.POST.get("expected_status", "200-399").strip() or "200-399"
    check.expected_substring = request.POST.get("expected_substring", "").strip()
    check.verify_tls = request.POST.get("verify_tls") == "on"
    check.host = request.POST.get("host", "").strip()
    port = request.POST.get("port", "").strip()
    check.port = int(port) if port.isdigit() else None
    try:
        check.timeout_seconds = max(1, int(request.POST.get("timeout_seconds", 10)))
        check.interval_seconds = max(10, int(request.POST.get("interval_seconds", 60)))
        check.failure_threshold = max(1, int(request.POST.get("failure_threshold", 2)))
    except (ValueError, TypeError):
        return "Timeout, interval and failure threshold must be numbers."
    check.enabled = request.POST.get("enabled") == "on"
    check.alert_on_failure = request.POST.get("alert_on_failure") == "on"

    if not check.name:
        return "Name is required."
    if check.check_type == SyntheticCheck.CheckType.HTTP:
        if not check.url:
            return "URL is required for HTTP checks."
    else:
        if not check.host or not check.port:
            return "Host and port are required for TCP checks."
    return None


@staff_member_required
def synthetic_check_add(request):
    if request.method == "POST":
        check = SyntheticCheck()
        err = _apply_check_form(request, check)
        if err:
            messages.error(request, err)
            context = {"show_sidebar": True, "check": check, "is_edit": False}
            context.update(admin.site.each_context(request))
            return render(request, "core/uptime_form.html", context)
        check.save()
        _log_user_action(request, "ADD_SYNTHETIC_CHECK", f"Added check {check.name}")
        messages.success(request, f'Check "{check.name}" created.')
        return redirect("synthetic_check_detail", check_id=check.id)

    # GET: render blank form with sensible defaults
    check = SyntheticCheck(check_type=SyntheticCheck.CheckType.HTTP, method="GET",
                           expected_status="200-399", verify_tls=True, timeout_seconds=10,
                           interval_seconds=60, failure_threshold=2, enabled=True, alert_on_failure=True)
    context = {"show_sidebar": True, "check": check, "is_edit": False}
    context.update(admin.site.each_context(request))
    return render(request, "core/uptime_form.html", context)


@staff_member_required
def synthetic_check_edit(request, check_id):
    check = get_object_or_404(SyntheticCheck, id=check_id)
    if request.method == "POST":
        err = _apply_check_form(request, check)
        if err:
            messages.error(request, err)
        else:
            check.save()
            _log_user_action(request, "EDIT_SYNTHETIC_CHECK", f"Edited check {check.name}")
            messages.success(request, f'Check "{check.name}" updated.')
            return redirect("synthetic_check_detail", check_id=check.id)
    context = {"show_sidebar": True, "check": check, "is_edit": True}
    context.update(admin.site.each_context(request))
    return render(request, "core/uptime_form.html", context)


@staff_member_required
@require_http_methods(["POST"])
def synthetic_check_delete(request, check_id):
    check = get_object_or_404(SyntheticCheck, id=check_id)
    name = check.name
    check.delete()
    _log_user_action(request, "DELETE_SYNTHETIC_CHECK", f"Deleted check {name}")
    messages.success(request, f'Check "{name}" deleted.')
    return redirect("synthetic_checks_list")


@staff_member_required
@require_http_methods(["POST"])
def synthetic_check_run(request, check_id):
    check = get_object_or_404(SyntheticCheck, id=check_id)
    from .synthetic import run_check
    try:
        result, transition = run_check(check)
        msg = f'Ran "{check.name}": {"OK" if result.success else "FAILED"}'
        if transition:
            msg += f" (state changed to {transition})"
        messages.success(request, msg)
    except Exception as e:
        messages.error(request, f"Failed to run check: {e}")
    return redirect("synthetic_check_detail", check_id=check.id)


@staff_member_required
def synthetic_check_detail(request, check_id):
    from .synthetic import uptime_percentage
    check = get_object_or_404(SyntheticCheck, id=check_id)
    summary = _synthetic_check_summary(check)
    summary["uptime_7d"] = uptime_percentage(check, 24 * 7)
    recent = check.results.order_by("-timestamp")[:50]
    context = {
        "show_sidebar": True,
        "check": check,
        "summary": summary,
        "recent": recent,
    }
    context.update(admin.site.each_context(request))
    return render(request, "core/uptime_detail.html", context)


# ---------------------------------------------------------------------------
# Services (auto-detected across all servers)
# ---------------------------------------------------------------------------
# Background/OS-plumbing services that are usually not worth monitoring.
# These are hidden by default on the Services page (shown behind an expander).
_BACKGROUND_SERVICE_NAMES = {
    "modemmanager", "dbus", "dbus-broker", "polkit", "polkitd", "fwupd", "fwupd-refresh",
    "packagekit", "accounts-daemon", "udisks2", "snapd", "multipathd", "ntpsec", "ntp",
    "chrony", "chronyd", "rsyslog", "irqbalance", "thermald", "upower", "apport",
    "unattended-upgrades", "networkd-dispatcher", "apparmor", "lvm2-monitor",
    "open-vm-tools", "qemu-guest-agent", "cloud-init", "console-setup", "keyboard-setup",
    "setvtrgb", "blk-availability", "finalrd", "atd", "rpcbind", "gdm", "gdm3",
    "switcheroo-control", "power-profiles-daemon", "wpa_supplicant", "secureboot-db",
    "plymouth-quit", "plymouth-start", "rtkit-daemon", "kmod-static-nodes",
}
_BACKGROUND_SERVICE_PREFIXES = (
    "getty", "serial-getty", "systemd-", "user@", "user-runtime-dir@", "session-",
    "snap.", "cloud-", "e2scrub", "man-db", "logrotate", "fstrim", "motd",
    "dpkg", "apt-", "phpsessionclean", "ua-", "ubuntu-advantage", "update-notifier",
)

# Windows runs dozens of OS services, so we invert the Linux logic: a Windows service
# is background UNLESS it's a recognized web/app/database service. Names/prefixes are
# matched lowercase. W3SVC=IIS, WAS=its process-activation dependency, FTPSVC=IIS FTP.
_NOTABLE_WINDOWS_SERVICE_NAMES = {
    "w3svc", "was", "ftpsvc",                          # IIS (web), process activation, FTP
    "mssqlserver", "sqlserveragent", "sqlbrowser",     # SQL Server (default instance)
    "nginx", "docker", "com.docker.service",
    "rabbitmq", "redis", "memcached", "mongodb", "elasticsearch",
}
_NOTABLE_WINDOWS_SERVICE_PREFIXES = (
    "iis", "w3svc", "ftpsvc",
    "mssql$", "sqlagent$",          # SQL Server named instances (MSSQL$INSTANCE, ...)
    "postgresql", "mysql", "mariadb",
    "apache", "tomcat",
)


def _is_background_service(svc):
    """Heuristic: is this a low-value OS/background service (hidden by default)?"""
    if svc.service_type == "port":
        # A confirmed product (read from the service's banner) or a recognized
        # well-known port is a key service; unrecognized / ephemeral ports
        # (e.g. 52227, 44222) are background noise, collapsed by default.
        if svc.detected_via == "port-banner":
            return False
        return role_for_port(svc.port) is None
    name = (svc.name or "").lower()
    if svc.service_type == "windows":
        # Notable iff it's a known web/app/db service; everything else (OS services)
        # is collapsed as background but still monitored.
        if name in _NOTABLE_WINDOWS_SERVICE_NAMES:
            return False
        return not any(name.startswith(p) for p in _NOTABLE_WINDOWS_SERVICE_PREFIXES)
    if name in _BACKGROUND_SERVICE_NAMES:
        return True
    return any(name.startswith(p) for p in _BACKGROUND_SERVICE_PREFIXES)


@staff_member_required
def services_overview(request):
    """Services grouped by server, with the non-critical/background ones filtered
    out by default and a per-service monitoring toggle."""
    groups = {}
    total = running = monitored = 0
    # Include port-detected services: well-known / banner-identified ones are shown
    # as key services; unrecognized ephemeral ports fall into the background group.
    for svc in Service.objects.select_related("server").order_by("server__name", "name"):
        total += 1
        if svc.status == "running":
            running += 1
        if svc.monitoring_enabled:
            monitored += 1
        g = groups.setdefault(svc.server_id, {
            "server": svc.server, "notable": [], "background": [], "monitored": 0,
        })
        if svc.monitoring_enabled:
            g["monitored"] += 1
        (g["background"] if _is_background_service(svc) else g["notable"]).append(svc)

    context = {
        "show_sidebar": True,
        "groups": sorted(groups.values(), key=lambda x: x["server"].name.lower()),
        "stats": {
            "total": total,
            "running": running,
            "monitored": monitored,
            "servers": len(groups),
        },
    }
    context.update(admin.site.each_context(request))
    return render(request, "core/services_overview.html", context)


# ---------------------------------------------------------------------------
# Containers (auto-detected across all servers)
# ---------------------------------------------------------------------------
@staff_member_required
def containers_overview(request):
    """Containers grouped by server, with a per-container monitoring toggle."""
    groups = {}
    total = running = monitored = 0
    for c in Container.objects.select_related("server").order_by("server__name", "name"):
        total += 1
        if c.state == "running":
            running += 1
        if c.monitoring_enabled:
            monitored += 1
        g = groups.setdefault(c.server_id, {"server": c.server, "containers": [], "monitored": 0})
        if c.monitoring_enabled:
            g["monitored"] += 1
        g["containers"].append(c)

    context = {
        "show_sidebar": True,
        "groups": sorted(groups.values(), key=lambda x: x["server"].name.lower()),
        "stats": {"total": total, "running": running, "monitored": monitored, "servers": len(groups)},
    }
    context.update(admin.site.each_context(request))
    return render(request, "core/containers_overview.html", context)


@staff_member_required
@require_http_methods(["POST"])
def toggle_container_monitoring(request, server_id, container_id):
    server = get_object_or_404(Server, id=server_id)
    container = get_object_or_404(Container, id=container_id, server=server)
    container.monitoring_enabled = not container.monitoring_enabled
    container.save(update_fields=["monitoring_enabled", "updated_at"])
    _log_user_action(request, "TOGGLE_CONTAINER_MONITORING",
                     f"Container {container.name} on {server.name} - monitoring {'enabled' if container.monitoring_enabled else 'disabled'}")
    return JsonResponse({"success": True, "monitoring_enabled": container.monitoring_enabled})


@staff_member_required
@require_http_methods(["GET"])
def container_inspect_api(request, server_id, container_id):
    """Return the stored, sanitized `inspect` summary for one container (read-only).

    The agent collects this on a slow cadence (~5 min) and the value is rendered
    as a config report in the UI. `inspect` never modifies a container.
    """
    server = get_object_or_404(Server, id=server_id)
    container = get_object_or_404(Container, id=container_id, server=server)
    return JsonResponse({
        "name": container.name,
        "runtime": container.runtime,
        "image": container.image,
        "state": container.state,
        "inspect": container.inspect_data,
        "inspect_at": container.inspect_at.isoformat() if container.inspect_at else None,
    })


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def _resource_label(metric_type, metric_name):
    """Plain resource label for a report row from an anomaly's type/name."""
    mn = (metric_name or "").lower()
    if mn.startswith("memory_leak:"):
        return "Memory leak (" + metric_name.split(":", 1)[1] + ")"
    special = {"memory_leak": "Memory leak", "shm_leak": "Shared memory",
               "semaphore_leak": "Semaphores", "devshm_leak": "/dev/shm"}
    if mn in special:
        return special[mn]
    return {"cpu": "CPU", "memory": "RAM", "disk": "Disk", "network": "Network"}.get(
        (metric_type or "").lower(), (metric_type or "").title() or "—")


@staff_member_required
def operations_report(request):
    """Operational Report — Resource Spikes (anomalies) with timing + admin notes."""
    from django.http import HttpResponse, HttpResponseForbidden
    from .permissions import user_can, VIEW_OPERATIONS
    if not user_can(request.user, VIEW_OPERATIONS):
        return HttpResponseForbidden("Access denied")

    try:
        days = int(request.GET.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    if days not in (7, 30, 90):
        days = 30
    since = timezone.now() - timedelta(days=days)

    server_id = request.GET.get("server_id") or ""
    qs = (Anomaly.objects.filter(timestamp__gte=since)
          .select_related("server", "resolved_by").order_by("-timestamp"))
    if server_id:
        try:
            qs = qs.filter(server_id=int(server_id))
        except (ValueError, TypeError):
            server_id = ""

    now = timezone.now()
    rows, durations = [], []
    sev_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    resolved_n = 0
    for a in qs[:5000]:
        end = _anomaly_window_end(a)  # back-to-normal time (preferred), else admin resolution
        if end:
            dur_sec = max(0, (end - a.timestamp).total_seconds())
            duration = _humanize_duration(dur_sec)
            durations.append(dur_sec)
        else:
            duration = "ongoing (" + _humanize_duration((now - a.timestamp).total_seconds()) + ")"
        if a.resolved and a.resolved_at:
            resolved_n += 1
        sev_counts[a.severity] = sev_counts.get(a.severity, 0) + 1
        rows.append({
            "server": a.server.name,
            "resource": _resource_label(a.metric_type, a.metric_name),
            "severity": a.severity,
            "value": round(a.metric_value, 2),
            "occurred": a.timestamp,
            "duration": duration,
            "resolved": a.resolved,
            "note": a.admin_note or "",
            "resolved_by": a.resolved_by.username if a.resolved_by else "",
        })

    if request.GET.get("format") == "csv":
        import csv as _csv
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="resource-spikes-{days}d.csv"'
        w = _csv.writer(resp)
        w.writerow(["Server", "Resource", "Severity", "Reading", "Occurred",
                    "Duration", "Status", "Admin note", "Resolved by"])
        for r in rows:
            w.writerow([r["server"], r["resource"], r["severity"], r["value"],
                        r["occurred"].strftime("%Y-%m-%d %H:%M:%S"), r["duration"],
                        "Resolved" if r["resolved"] else "Open", r["note"], r["resolved_by"]])
        return resp

    context = {
        "show_sidebar": True,
        "title": "Operational Report — Resource Spikes",
        "days": days,
        "server_id": str(server_id),
        "servers": Server.objects.all().order_by("name"),
        "rows": rows,
        "summary": {
            "total": len(rows), "resolved": resolved_n, "open": len(rows) - resolved_n,
            "sev": sev_counts,
            "avg_resolution": _humanize_duration(sum(durations) / len(durations)) if durations else "—",
        },
    }
    context.update(admin.site.each_context(request))
    return render(request, "core/operations_report.html", context)


_SEV_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _exec_incidents(days):
    """Per-server spike + downtime counts and a resolution log (with admin reasons)."""
    now = timezone.now()
    since = now - timedelta(days=days)
    rows = []
    for s in Server.objects.all().order_by("name"):
        alerts = AlertHistory.objects.filter(server=s, sent_at__gte=since)
        spikes = Anomaly.objects.filter(server=s, timestamp__gte=since).count()
        down = alerts.filter(alert_type="CONNECTION").count()
        svc = alerts.filter(alert_type="SERVICE").count()
        cont = alerts.filter(alert_type="CONTAINER").count()
        res = alerts.filter(alert_type__in=["CPU", "Memory", "Disk"]).count()
        total = spikes + down + svc + cont + res
        if total:
            rows.append({"server": s.name, "spikes": spikes, "downtime": down,
                         "service": svc, "container": cont, "resource": res, "total": total})
    rows.sort(key=lambda r: r["total"], reverse=True)

    log = []
    for a in (Anomaly.objects.filter(resolved=True, resolved_at__gte=since)
              .select_related("server", "resolved_by").order_by("-resolved_at")[:150]):
        log.append({"when": a.resolved_at, "server": a.server.name,
                    "what": _resource_label(a.metric_type, a.metric_name) + " spike",
                    "reason": a.admin_note or "", "by": a.resolved_by.username if a.resolved_by else ""})
    for al in (AlertHistory.objects.filter(status="resolved", resolved_at__gte=since)
               .select_related("server", "resolved_by").order_by("-resolved_at")[:150]):
        log.append({"when": al.resolved_at, "server": al.server.name,
                    "what": al.alert_type + (" — " + (al.message or "")[:60] if al.message else ""),
                    "reason": al.admin_note or "", "by": al.resolved_by.username if al.resolved_by else ""})
    log.sort(key=lambda r: r["when"], reverse=True)
    return rows, log[:120]


def _exec_risk(days):
    """Proactive 'likely to fail' rows: disk-full ETA, memory leaks, offline, frequency."""
    from .utils.forecast_engine import forecast_disk_usage
    from .utils.leak_detection import detect_leaks
    now = timezone.now()
    rows = []
    for s in Server.objects.select_related("monitoring_config").all().order_by("name"):
        if _calculate_server_status(s) == "offline":
            rows.append({"target": s.name, "kind": "Server", "risk": "Currently offline / no heartbeat",
                         "severity": "CRITICAL", "detail": "Agent not reporting", "eta": "now"})
        cfg = getattr(s, "monitoring_config", None)
        mounts = (cfg.monitored_disks if cfg and cfg.monitored_disks else ["/"])
        for mount in mounts:
            try:
                f = forecast_disk_usage(s, mount, days=30)
            except Exception:
                f = None
            if not f:
                continue
            cur, gr = f.get("current_usage", 0) or 0, f.get("growth_rate", 0) or 0
            if cur >= 90:
                rows.append({"target": s.name, "kind": "Disk " + mount, "risk": "Disk almost full",
                             "severity": "CRITICAL", "detail": f"{cur:.0f}% used now", "eta": "now"})
            elif gr > 0 and cur > 0:
                d90 = (90 - cur) / gr
                if d90 <= 30:
                    sev = "CRITICAL" if d90 <= 7 else "HIGH" if d90 <= 14 else "MEDIUM"
                    rows.append({"target": s.name, "kind": "Disk " + mount, "risk": "Disk filling up",
                                 "severity": sev, "detail": f"{cur:.0f}% now, +{gr:.1f}%/day",
                                 "eta": f"~{d90:.0f} days to 90%"})
        try:
            for lk in detect_leaks(s):
                rows.append({"target": s.name, "kind": "Memory", "risk": "Memory / shared-memory leak",
                             "severity": lk.get("severity", "MEDIUM"),
                             "detail": (lk.get("explanation") or "")[:140], "eta": ""})
        except Exception:
            pass
        recent = AlertHistory.objects.filter(server=s, sent_at__gte=now - timedelta(days=7)).count()
        if recent >= 5:
            rows.append({"target": s.name, "kind": "Server", "risk": "Frequent incidents",
                         "severity": "MEDIUM", "detail": f"{recent} alerts in the last 7 days", "eta": ""})
    rows.sort(key=lambda r: _SEV_RANK.get(r["severity"], 0), reverse=True)
    return rows


@staff_member_required
def executive_report(request):
    """Executive Reports: fleet-optimization numbers, incidents, and forecast/risk."""
    from django.http import HttpResponse, HttpResponseForbidden
    from .permissions import user_can, VIEW_EXECUTIVE
    if not user_can(request.user, VIEW_EXECUTIVE):
        return HttpResponseForbidden("Access denied")

    try:
        days = int(request.GET.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    if days not in (7, 30, 90):
        days = 30

    try:
        opt = build_executive_context()
    except Exception as e:
        app_logger.warning(f"executive_report optimization context failed: {e}")
        opt = {"counts": {"underutilized": 0, "overloaded": 0, "optimized": 0},
               "cost_opportunities": [], "total_reclaim_vcpu": 0,
               "total_reclaim_gb": 0, "eligible_count": 0}

    incident_rows, resolution_log = _exec_incidents(days)
    risk_rows = _exec_risk(days)

    fmt = request.GET.get("format")
    section = request.GET.get("section")
    if fmt == "csv":
        import csv as _csv
        resp = HttpResponse(content_type="text/csv")
        if section == "incidents":
            resp["Content-Disposition"] = f'attachment; filename="incidents-{days}d.csv"'
            w = _csv.writer(resp)
            w.writerow(["Server", "Spikes", "Downtime", "Service", "Container", "Resource", "Total"])
            for r in incident_rows:
                w.writerow([r["server"], r["spikes"], r["downtime"], r["service"], r["container"], r["resource"], r["total"]])
        elif section == "risk":
            resp["Content-Disposition"] = f'attachment; filename="forecast-risk.csv"'
            w = _csv.writer(resp)
            w.writerow(["Target", "Type", "Risk", "Severity", "Detail", "ETA"])
            for r in risk_rows:
                w.writerow([r["target"], r["kind"], r["risk"], r["severity"], r["detail"], r["eta"]])
        else:  # optimization
            resp["Content-Disposition"] = 'attachment; filename="fleet-optimization.csv"'
            w = _csv.writer(resp)
            w.writerow(["Server", "Category", "CPU avg %", "Mem avg %", "Current", "Suggested"])
            for a in opt.get("cost_opportunities", []):
                w.writerow([getattr(a, "name", ""), getattr(a, "category", ""),
                            getattr(getattr(a, "cpu", None), "avg", ""), getattr(getattr(a, "memory", None), "avg", ""),
                            f"{getattr(a,'current_vcpu','')}vCPU/{getattr(a,'current_gb','')}GB",
                            f"{getattr(a,'suggested_vcpu','')}vCPU/{getattr(a,'suggested_gb','')}GB"])
        return resp

    context = {
        "show_sidebar": True,
        "title": "Executive Reports",
        "days": days,
        "opt": opt,
        "incident_rows": incident_rows,
        "resolution_log": resolution_log,
        "risk_rows": risk_rows,
    }
    context.update(admin.site.each_context(request))
    return render(request, "core/executive_report.html", context)


# ---------------------------------------------------------------------------
# Business KPI monitoring
# ---------------------------------------------------------------------------
def _business_context(request, new_raw_token=None):
    kpis = BusinessKPI.objects.all()
    cfg = BusinessMonitorConfig.get_config()
    base_url = _public_base_url(request)
    summary = {"total": kpis.count(), "ok": 0, "warning": 0, "critical": 0}
    for k in kpis:
        if k.last_status in summary:
            summary[k.last_status] += 1
    context = {
        "show_sidebar": True,
        "kpis": kpis,
        "config": cfg,
        "summary": summary,
        "base_url": base_url,
        "new_raw_token": new_raw_token,
        "has_token": bool(cfg.ingest_token_hash),
    }
    context.update(admin.site.each_context(request))
    return context


@staff_member_required
def business_dashboard(request):
    return render(request, "core/business_list.html", _business_context(request))


def _apply_kpi_form(request, kpi):
    from django.utils.text import slugify
    kpi.name = request.POST.get("name", "").strip()
    kpi.key = slugify(request.POST.get("key", "").strip() or kpi.name)
    kpi.unit = request.POST.get("unit", "").strip()
    kpi.description = request.POST.get("description", "").strip()
    kpi.direction = request.POST.get("direction", BusinessKPI.Direction.HIGHER_BETTER)
    kpi.alert_enabled = request.POST.get("alert_enabled") == "on"
    kpi.enabled = request.POST.get("enabled") == "on"

    def _num(field):
        raw = request.POST.get(field, "").strip()
        return float(raw) if raw not in ("", None) else None

    try:
        kpi.warning_threshold = _num("warning_threshold")
        kpi.critical_threshold = _num("critical_threshold")
    except ValueError:
        return "Thresholds must be numbers."

    if not kpi.name:
        return "Name is required."
    if not kpi.key:
        return "Key is required."
    dupe = BusinessKPI.objects.filter(key=kpi.key).exclude(pk=kpi.pk).exists()
    if dupe:
        return f"A KPI with key '{kpi.key}' already exists."
    return None


@staff_member_required
def business_kpi_add(request):
    if request.method == "POST":
        kpi = BusinessKPI()
        err = _apply_kpi_form(request, kpi)
        if err:
            messages.error(request, err)
            context = {"show_sidebar": True, "kpi": kpi, "is_edit": False}
            context.update(admin.site.each_context(request))
            return render(request, "core/business_kpi_form.html", context)
        kpi.save()
        _log_user_action(request, "ADD_KPI", f"Added KPI {kpi.key}")
        messages.success(request, f'KPI "{kpi.name}" created.')
        return redirect("business_kpi_detail", kpi_id=kpi.id)
    kpi = BusinessKPI(direction=BusinessKPI.Direction.HIGHER_BETTER, enabled=True, alert_enabled=True)
    context = {"show_sidebar": True, "kpi": kpi, "is_edit": False}
    context.update(admin.site.each_context(request))
    return render(request, "core/business_kpi_form.html", context)


@staff_member_required
def business_kpi_edit(request, kpi_id):
    kpi = get_object_or_404(BusinessKPI, id=kpi_id)
    if request.method == "POST":
        err = _apply_kpi_form(request, kpi)
        if err:
            messages.error(request, err)
        else:
            kpi.save()
            _log_user_action(request, "EDIT_KPI", f"Edited KPI {kpi.key}")
            messages.success(request, "KPI updated.")
            return redirect("business_kpi_detail", kpi_id=kpi.id)
    context = {"show_sidebar": True, "kpi": kpi, "is_edit": True}
    context.update(admin.site.each_context(request))
    return render(request, "core/business_kpi_form.html", context)


@staff_member_required
def business_kpi_detail(request, kpi_id):
    kpi = get_object_or_404(BusinessKPI, id=kpi_id)
    recent = kpi.values.order_by("-timestamp")[:50]
    context = {"show_sidebar": True, "kpi": kpi, "recent": recent}
    context.update(admin.site.each_context(request))
    return render(request, "core/business_kpi_detail.html", context)


@staff_member_required
@require_http_methods(["POST"])
def business_kpi_record(request, kpi_id):
    kpi = get_object_or_404(BusinessKPI, id=kpi_id)
    raw = request.POST.get("value", "").strip()
    try:
        value = float(raw)
    except (ValueError, TypeError):
        messages.error(request, "Value must be a number.")
        return redirect("business_kpi_detail", kpi_id=kpi.id)
    from .business import record_value
    _, transition = record_value(kpi, value, source=BusinessKPIValue.Source.MANUAL,
                                 note=request.POST.get("note", "").strip())
    msg = f"Recorded {value}{kpi.unit}. Status: {kpi.last_status}."
    if transition:
        msg += f" (changed to {transition})"
    messages.success(request, msg)
    return redirect("business_kpi_detail", kpi_id=kpi.id)


@staff_member_required
@require_http_methods(["POST"])
def business_kpi_delete(request, kpi_id):
    kpi = get_object_or_404(BusinessKPI, id=kpi_id)
    name = kpi.name
    kpi.delete()
    _log_user_action(request, "DELETE_KPI", f"Deleted KPI {name}")
    messages.success(request, f'KPI "{name}" deleted.')
    return redirect("business_dashboard")


@staff_member_required
@require_http_methods(["POST"])
def business_regenerate_token(request):
    cfg = BusinessMonitorConfig.get_config()
    raw = cfg.generate_token()
    _log_user_action(request, "ROTATE_KPI_TOKEN", "Rotated business ingest token")
    messages.success(request, "New ingest token generated — copy it now, it won't be shown again.")
    return render(request, "core/business_list.html", _business_context(request, new_raw_token=raw))


# ---------------------------------------------------------------------------
# Security / SIEM monitoring
# ---------------------------------------------------------------------------
@staff_member_required
def security_dashboard(request):
    """Security domain: auth threat detection overview + open events."""
    from django.db.models import Count
    now = timezone.now()
    since = now - timedelta(hours=24)
    logins = LoginActivity.objects.filter(timestamp__gte=since)
    failed = logins.filter(status=LoginActivity.StatusChoices.FAILED)
    open_events_qs = SecurityEvent.objects.exclude(status=SecurityEvent.Status.RESOLVED)

    top_ips = list(
        failed.exclude(ip_address="0.0.0.0")
        .values("ip_address")
        .annotate(c=Count("id"))
        .order_by("-c")[:8]
    )
    context = {
        "show_sidebar": True,
        "stats": {
            "logins_24h": logins.count(),
            "failed_24h": failed.count(),
            "open_events": open_events_qs.count(),
            "unique_ips_24h": logins.exclude(ip_address="0.0.0.0").values("ip_address").distinct().count(),
        },
        "open_events": open_events_qs.order_by("-last_seen")[:50],
        "top_ips": top_ips,
        "recent_logins": LoginActivity.objects.order_by("-timestamp")[:15],
        "config": SecurityMonitorConfig.get_config(),
    }
    context.update(admin.site.each_context(request))
    return render(request, "core/security_dashboard.html", context)


@staff_member_required
@require_http_methods(["POST"])
def security_event_update(request, event_id):
    event = get_object_or_404(SecurityEvent, id=event_id)
    action = request.POST.get("action")
    if action == "acknowledge":
        event.status = SecurityEvent.Status.ACKNOWLEDGED
    elif action == "resolve":
        event.status = SecurityEvent.Status.RESOLVED
    else:
        messages.error(request, "Unknown action.")
        return redirect("security_dashboard")
    event.save(update_fields=["status", "updated_at"])
    _log_user_action(request, "SECURITY_EVENT", f"{action} event {event.id}")
    messages.success(request, f"Event {action}d.")
    return redirect("security_dashboard")


@staff_member_required
@require_http_methods(["POST"])
def security_run_now(request):
    from .security_monitor import detect_security_events
    try:
        new_events = detect_security_events()
        messages.success(request, f"Detection ran: {len(new_events)} new event(s).")
    except Exception as e:
        messages.error(request, f"Detection failed: {e}")
    return redirect("security_dashboard")


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
