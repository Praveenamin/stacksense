from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.contrib.auth import logout as auth_logout
from django.utils import timezone
from datetime import timedelta
from .models import Server, SystemMetric, Anomaly, MonitoringConfig, Service, EmailAlertConfig, AlertHistory, UserACL
from django.http import JsonResponse, HttpResponseRedirect
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
        # Log error and return error response
        app_logger.error(f"Error in anomaly_status_api for server {server_id}: {e}")
        return JsonResponse(
            {"error": "Internal server error", "message": str(e)},
            status=500
        )


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
    
    context = {
        "server": server,
        "server_status": server_status,
        "latest_metric": latest_metric,
        "recent_metrics": recent_metrics,
        "all_services": all_services,
        "recent_anomalies": recent_anomalies,
        "disk_summary": disk_summary,
        "chart_data": chart_data_json,
        "recent_alerts": recent_alerts,
        "all_alerts": all_alerts,
        "alert_types": alert_types,
        "alert_statuses": alert_statuses,
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
            alert_suppressed = server.monitoring_config.alert_suppressed
            monitoring_suspended = server.monitoring_config.monitoring_suspended
        except:
            monitoring_enabled = False
            alert_suppressed = False
            monitoring_suspended = False
        
        # Calculate status with 30-second offline detection
        # CRITICAL: If monitoring is disabled or suspended, skip all checks
        status = "offline"  # Default fallback
        previous_connection_state = None
        
        # Check if monitoring is enabled first
        if not monitoring_enabled:
            # Monitoring disabled - skip all checks, show as disabled
            status = "disabled"
            # Don't count in any category
            # Don't update connection state
            # Don't send any alerts
        elif monitoring_suspended:
            # Monitoring suspended - skip checks, show as suspended
            # CRITICAL: Don't update connection state or send alerts when suspended
            status = "suspended"
            # Don't count in any category
            # Don't update connection state cache
            # Don't send any alerts
            # CRITICAL: Clear connection state cache to prevent any alerts
            connection_state_key = f"connection_state:{server.id}"
            cache.delete(connection_state_key)
            # Keep the last known status visually, but don't process it
        else:
            # Monitoring is active - proceed with status checks
            # Get previous connection state from cache
            connection_state_key = f"connection_state:{server.id}"
            previous_connection_state = cache.get(connection_state_key, None)
            
            # CRITICAL: Only check status if we have metrics
            # If no metrics exist, server is offline (can't collect = offline)
            if latest_metric:
                # Calculate time difference
                time_diff = timezone.now() - latest_metric.timestamp
                time_diff_seconds = time_diff.total_seconds()
                
                # Offline detection threshold
                # Use 2x the collection interval, with minimum of 60 seconds
                # This prevents false positives when collection interval is longer
                try:
                    collection_interval = server.monitoring_config.collection_interval_seconds or 30
                    # Use 2x collection interval, but at least 60 seconds
                    OFFLINE_THRESHOLD_SECONDS = max(collection_interval * 2, 60)
                except:
                    # Fallback to 60 seconds if config not available
                    OFFLINE_THRESHOLD_SECONDS = 60
                
                if time_diff_seconds <= OFFLINE_THRESHOLD_SECONDS:
                    # Server is online - metrics are recent (within 30 seconds)
                    cpu = latest_metric.cpu_percent or 0
                    memory = latest_metric.memory_percent or 0
                    
                    # Determine status based on resource usage
                    if cpu > 80 or memory > 85:
                        status = "warning"
                        warning_count += 1
                    else:
                        status = "online"
                        online_count += 1
                    
                    # Update connection state cache ONLY if state changed
                    # This prevents unnecessary cache writes
                    current_connection_state = "online"
                    if previous_connection_state != "online":
                        cache.set(connection_state_key, current_connection_state, timeout=3600)
                    
                    # Check if state changed from offline to online
                    # Only send alert if we had a previous offline state
                    # DOUBLE CHECK: Verify monitoring is still active before sending alert
                    if previous_connection_state == "offline":
                        # Check if we just resumed monitoring (within last 60 seconds)
                        resume_timestamp_key = f"resume_timestamp:{server.id}"
                        resume_timestamp = cache.get(resume_timestamp_key)
                        if resume_timestamp:
                            # Just resumed - skip alert to prevent false positives
                            print(f"[STATUS] Skipping online alert for {server.name} - monitoring just resumed")
                        else:
                            # Refresh server config to ensure monitoring is still active
                            server.refresh_from_db()
                            if (server.monitoring_config.enabled and 
                                not server.monitoring_config.monitoring_suspended):
                                # Server came back online - send resolved alert
                                print(f"[STATUS] Server {server.name} came back online, sending alert")
                                _send_connection_alert(server, "online")
                    
                else:
                    # Metrics are older than 30 seconds - server is offline
                    # This means the server is not responding to metric collection
                    status = "offline"
                    offline_count += 1
                    
                    # Update connection state cache ONLY if state changed
                    current_connection_state = "offline"
                    if previous_connection_state != "offline":
                        cache.set(connection_state_key, current_connection_state, timeout=3600)
                    
                    # Check if state changed from online to offline
                    # CRITICAL: Only send alert if state actually changed from "online" to "offline"
                    # Don't send alert if previous state was None (first check) or "offline" (already offline)
                    # DOUBLE CHECK: Verify monitoring is still active before sending alert
                    if previous_connection_state == "online":
                        # Check if we just suspended or resumed monitoring (within last 60 seconds)
                        suspend_timestamp_key = f"suspend_timestamp:{server.id}"
                        resume_timestamp_key = f"resume_timestamp:{server.id}"
                        suspend_timestamp = cache.get(suspend_timestamp_key)
                        resume_timestamp = cache.get(resume_timestamp_key)
                        if suspend_timestamp or resume_timestamp:
                            # Just suspended or resumed - skip alert to prevent false positives
                            print(f"[STATUS] Skipping offline alert for {server.name} - monitoring just suspended/resumed")
                        else:
                            # Refresh server config to ensure monitoring is still active
                            server.refresh_from_db()
                            if (server.monitoring_config.enabled and 
                                not server.monitoring_config.monitoring_suspended):
                                # Server went offline - send alert
                                print(f"[STATUS] Server {server.name} went offline, sending alert")
                                _send_connection_alert(server, "offline")
            else:
                # No metrics at all - server is offline
                # This means metric collection has never succeeded or has stopped
                status = "offline"
                offline_count += 1
                
                # Update connection state cache ONLY if state changed
                current_connection_state = "offline"
                if previous_connection_state != "offline":
                    cache.set(connection_state_key, current_connection_state, timeout=3600)
                
                # Check if state changed from online to offline
                # CRITICAL: Only send alert if state actually changed from "online" to "offline"
                # Don't send alert if previous state was None (first check) or "offline" (already offline)
                # DOUBLE CHECK: Verify monitoring is still active before sending alert
                if previous_connection_state == "online":
                    # Check if we just suspended or resumed monitoring (within last 60 seconds)
                    suspend_timestamp_key = f"suspend_timestamp:{server.id}"
                    resume_timestamp_key = f"resume_timestamp:{server.id}"
                    suspend_timestamp = cache.get(suspend_timestamp_key)
                    resume_timestamp = cache.get(resume_timestamp_key)
                    if suspend_timestamp or resume_timestamp:
                        # Just suspended or resumed - skip alert to prevent false positives
                        print(f"[STATUS] Skipping offline alert for {server.name} - monitoring just suspended/resumed")
                    else:
                        # Refresh server config to ensure monitoring is still active
                        server.refresh_from_db()
                        if (server.monitoring_config.enabled and 
                            not server.monitoring_config.monitoring_suspended):
                            # Server went offline - send alert
                            print(f"[STATUS] Server {server.name} went offline (no metrics), sending alert")
                            _send_connection_alert(server, "offline")
        
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

        # Calculate server status for this specific server
        # Consider server online if metrics are within last 15 minutes (3x typical collection interval)
        calculated_status = "offline"
        if latest_metric:
            time_diff = timezone.now() - latest_metric.timestamp
            if time_diff < timedelta(minutes=15):
                cpu = latest_metric.cpu_percent or 0
                memory = latest_metric.memory_percent or 0
                if cpu > 80 or memory > 85:
                    calculated_status = "warning"
                else:
                    calculated_status = "online"
            else:
                calculated_status = "offline"

        servers_data.append({
            "server": server,
            "server_status": calculated_status,
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
        context = {}
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
    
    # CRITICAL: Check if monitoring is suspended or disabled before collecting
    try:
        monitoring_config = server.monitoring_config
        if not monitoring_config.enabled:
            print(f"[METRICS] Skipping metric collection for {server.name} - monitoring disabled")
            return False
        if monitoring_config.monitoring_suspended:
            print(f"[METRICS] Skipping metric collection for {server.name} - monitoring suspended")
            return False
    except:
        # If no config exists, allow collection (for new servers)
        pass
    
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
        
        recipients = [email.strip() for email in email_config.alert_recipients.split(',')]
        
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
            if email_config.use_tls:
                server_smtp = smtplib.SMTP(email_config.smtp_host, email_config.smtp_port)
                server_smtp.starttls()
                server_smtp.login(email_config.smtp_username, email_config.smtp_password)
            else:
                server_smtp = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
                server_smtp.login(email_config.smtp_username, email_config.smtp_password)
            
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
            
            # Send alert if:
            # 1. Service is in failed state (immediate alert)
            # 2. OR service has been down for 2 consecutive checks
            should_alert = False
            if is_failed:
                # Service is in failed state - send alert immediately
                should_alert = True
                print(f"[SERVICE_ALERT] Service {service.name} on {server.name} is in FAILED state")
            elif failure_count >= 2:
                # Service has been down for 2 consecutive checks
                should_alert = True
                print(f"[SERVICE_ALERT] Service {service.name} on {server.name} is down (2 consecutive failures)")
            
            if should_alert:
                # Check if we already sent an alert for this failure
                alert_sent = cache.get(alert_sent_key, False)
                if not alert_sent:
                    # Send alert
                    _send_service_alert(server, service, "triggered")
                    cache.set(alert_sent_key, True, 300)  # Prevent duplicate alerts
        
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
        
        recipients = [email.strip() for email in email_config.alert_recipients.split(',')]
        
        if status == "triggered":
            subject = f"🚨 Service Alert: {service.name} is DOWN on {server.name}"
            body = f"""
Service Monitoring Alert

Service: {service.name}
Server: {server.name} ({server.ip_address})
Status: DOWN
Time: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}

The service has been down for 2 consecutive checks (2 minutes).

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
                server_smtp.starttls()
            else:
                server_smtp = smtplib.SMTP_SSL(email_config.smtp_host, email_config.smtp_port)
            
            server_smtp.login(email_config.smtp_username, email_config.smtp_password)
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
                recipients=email_config.alert_recipients
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
    from .models import UserACL
    from .utils import has_privilege

    if not has_privilege(request.user, 'manage_users'):
        messages.error(request, "You don't have permission to manage users.")
        return redirect('monitoring_dashboard')

    users = User.objects.filter(is_staff=True).select_related('acl').order_by("username")
    # Ensure all users have ACL records
    for user in users:
        UserACL.get_or_create_for_user(user)
    
    # Calculate stats
    total_users = users.count()
    superusers_count = users.filter(is_superuser=True).count()
    active_users_count = users.filter(is_active=True).count()
    staff_users_count = total_users
    
    return render(request, "core/admin_users.html", {
        "users": users,
        "total_users": total_users,
        "superusers_count": superusers_count,
        "active_users_count": active_users_count,
        "staff_users_count": staff_users_count,
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
        'available_roles': available_roles
    })

@staff_member_required
def edit_admin_user(request, user_id):
    from .models import UserACL
    user = get_object_or_404(User, id=user_id, is_staff=True)
    acl = UserACL.get_or_create_for_user(user)
    
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
        
        # Update ACL for non-superuser staff
        if not is_superuser:
            acl.can_view_dashboard = request.POST.get("can_view_dashboard") == "on"
            acl.can_edit_thresholds = request.POST.get("can_edit_thresholds") == "on"
            acl.can_halt_monitoring = request.POST.get("can_halt_monitoring") == "on"
            acl.can_mute_notifications = request.POST.get("can_mute_notifications") == "on"
            acl.can_add_server = request.POST.get("can_add_server") == "on"
            acl.can_edit_server = request.POST.get("can_edit_server") == "on"
            acl.can_delete_server = request.POST.get("can_delete_server") == "on"
            acl.save()
        else:
            # Delete ACL if user becomes superuser
            if acl.pk:
                acl.delete()
        
        messages.success(request, f"User {username} updated successfully.")
        return redirect("admin_users")
    return render(request, "core/edit_admin_user.html", {"user": user, "user_id": user_id, "acl": acl})

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
        _log_user_action(request, "CREATE_ADMIN_USER", f"Created user: {username} (superuser: {is_superuser})")
        return JsonResponse({"success": True, "message": f"User {username} created successfully."})
    except Exception as e:
        _log_user_action(request, "CREATE_ADMIN_USER", f"Failed: {str(e)}")
        error_logger.error(f"CREATE_ADMIN_USER error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


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
@ensure_csrf_cookie
@require_http_methods(["POST"])
def toggle_alert_suppression(request, server_id, action):
    """API endpoint to suppress or resume alerts for a server"""
    try:
        server = get_object_or_404(Server, id=server_id)
        monitoring_config, created = MonitoringConfig.objects.get_or_create(server=server)
        
        # Validate action
        if action not in ["suppress", "resume"]:
            return JsonResponse({"success": False, "error": "Invalid action. Use 'suppress' or 'resume'."}, status=400)
        
        # Update the state based on action
        if action == "suppress":
            monitoring_config.alert_suppressed = True
            action_text = "suppressed"
        else:  # resume
            monitoring_config.alert_suppressed = False
            action_text = "resumed"
        
        monitoring_config.save()
        _log_user_action(request, f"ALERT_{action.upper()}", f"Server: {server.name} (ID: {server_id})")
        
        return JsonResponse({
            "success": True,
            "message": f"Alert suppression {action_text} for {server.name}",
            "alert_suppressed": monitoring_config.alert_suppressed
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
        monitoring_config, created = MonitoringConfig.objects.get_or_create(server=server)
        
        # Validate action
        if action not in ["suspend", "resume"]:
            return JsonResponse({"success": False, "error": "Invalid action. Use 'suspend' or 'resume'."}, status=400)
        
        # Update the state based on action
        if action == "suspend":
            monitoring_config.monitoring_suspended = True
            action_text = "suspended"
            # CRITICAL: Clear connection state cache when suspending to prevent false offline alerts
            connection_state_key = f"connection_state:{server_id}"
            cache.delete(connection_state_key)
            # Set a flag to prevent alerts for the next 60 seconds after suspend
            suspend_timestamp_key = f"suspend_timestamp:{server_id}"
            cache.set(suspend_timestamp_key, timezone.now().isoformat(), timeout=60)
        else:  # resume
            monitoring_config.monitoring_suspended = False
            action_text = "resumed"
            # CRITICAL: When resuming, clear cache to prevent false alerts on first check after resume
            # This ensures we don't send alerts based on stale state from before suspension
            connection_state_key = f"connection_state:{server_id}"
            cache.delete(connection_state_key)
            # Set a flag to prevent alerts for the next 60 seconds after resume
            resume_timestamp_key = f"resume_timestamp:{server_id}"
            cache.set(resume_timestamp_key, timezone.now().isoformat(), timeout=60)
        
        monitoring_config.save()
        _log_user_action(request, f"MONITORING_{action.upper()}", f"Server: {server.name} (ID: {server_id})")
        
        return JsonResponse({
            "success": True,
            "message": f"Monitoring {action_text} for {server.name}",
            "monitoring_suspended": monitoring_config.monitoring_suspended
        })
    except Exception as e:
        error_logger.error(f"TOGGLE_MONITORING error: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


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
        error_logger.error(f"GET_ACTIVE_SERVICES error: {str(e)}")
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
def help_docs(request):
    """
    Help documentation page.
    Provides non-technical feature descriptions and usage guides.
    """
    return render(request, 'core/help_docs.html')


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

    from .models import Role, Privilege

    roles = Role.objects.all().prefetch_related('role_privileges__privilege')
    privileges = Privilege.objects.all().order_by('key')

    context = {
        'roles': roles,
        'privileges': privileges,
        'can_manage_roles': has_privilege(request.user, 'manage_roles'),
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
    return render(request, 'core/create_role.html', {'privileges': privileges})


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
    Demo dashboard showcasing all UI components from the design system
    """
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
        ]
    }
    return render(request, 'core/demo_dashboard.html', context)
