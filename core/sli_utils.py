"""
Service Level Indicator (SLI) calculation utilities.

These functions calculate SLI values from SystemMetric and ServiceLatencyMeasurement data.
"""

from django.utils import timezone
from django.db.models import Avg, Q, Count, Sum
from datetime import timedelta
from core.models import (
    SystemMetric, ServiceLatencyMeasurement, Service, SLIConfig, SLOConfig
)


def get_slo_config(server, metric_type):
    """
    Get SLO configuration for a server and metric type.
    Checks server-specific SLO first, falls back to global default.
    """
    # Try server-specific SLO first
    server_slo = SLOConfig.objects.filter(
        server=server,
        metric_type=metric_type,
        enabled=True
    ).first()
    
    if server_slo:
        return server_slo
    
    # Fall back to global default (server=None)
    global_slo = SLOConfig.objects.filter(
        server=None,
        metric_type=metric_type,
        enabled=True
    ).first()
    
    return global_slo


def calculate_uptime_sli(server, start_date, end_date):
    """
    Calculate uptime SLI as percentage of time server was available.
    
    Returns: percentage (0-100)
    """
    try:
        # Get total time window in seconds
        total_seconds = (end_date - start_date).total_seconds()
        
        if total_seconds <= 0:
            return 0.0
        
        # Get metrics in time window
        metrics = SystemMetric.objects.filter(
            server=server,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).order_by('timestamp')
        
        if not metrics.exists():
            return 0.0
        
        # Calculate uptime based on system_uptime_seconds
        # If system_uptime_seconds exists, we can infer server was online
        # Count metrics with system_uptime_seconds > 0 as "up"
        up_metrics = metrics.filter(system_uptime_seconds__gt=0)
        total_metrics = metrics.count()
        
        if total_metrics == 0:
            return 0.0
        
        # Approximate uptime percentage based on metric availability
        # This is a simplified calculation - assumes metrics are collected regularly
        uptime_percentage = (up_metrics.count() / total_metrics) * 100
        
        return round(uptime_percentage, 2)
    except Exception:
        return 0.0


def calculate_cpu_sli(server, start_date, end_date):
    """
    Calculate CPU SLI as average CPU usage percentage.
    
    Returns: average CPU percentage
    """
    try:
        avg_cpu = SystemMetric.objects.filter(
            server=server,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).aggregate(avg=Avg('cpu_percent'))['avg']
        
        return round(avg_cpu or 0.0, 2)
    except Exception:
        return 0.0


def calculate_memory_sli(server, start_date, end_date):
    """
    Calculate Memory SLI as average memory usage percentage.
    
    Returns: average memory percentage
    """
    try:
        avg_memory = SystemMetric.objects.filter(
            server=server,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).aggregate(avg=Avg('memory_percent'))['avg']
        
        return round(avg_memory or 0.0, 2)
    except Exception:
        return 0.0


def calculate_disk_sli(server, start_date, end_date):
    """
    Calculate Disk SLI as average disk usage percentage.
    Uses the root partition (/) by default, or calculates average across all partitions.
    
    Returns: average disk percentage
    """
    try:
        metrics = SystemMetric.objects.filter(
            server=server,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        )
        
        if not metrics.exists():
            return 0.0
        
        # Calculate average disk usage from disk_usage JSON field
        total_percent = 0.0
        count = 0
        
        for metric in metrics:
            disk_usage = metric.disk_usage or {}
            if isinstance(disk_usage, dict):
                # Get root partition (/) or calculate average
                if '/' in disk_usage:
                    disk_info = disk_usage['/']
                    if isinstance(disk_info, dict) and 'percent' in disk_info:
                        total_percent += disk_info['percent']
                        count += 1
                else:
                    # Average across all partitions
                    percents = [
                        info.get('percent', 0)
                        for info in disk_usage.values()
                        if isinstance(info, dict) and 'percent' in info
                    ]
                    if percents:
                        total_percent += sum(percents) / len(percents)
                        count += 1
        
        if count == 0:
            return 0.0
        
        return round(total_percent / count, 2)
    except Exception:
        return 0.0


def calculate_network_sli(server, start_date, end_date):
    """
    Calculate Network SLI as average network utilization percentage.
    
    Returns: average network utilization percentage
    """
    try:
        metrics = SystemMetric.objects.filter(
            server=server,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).exclude(
            Q(net_utilization_sent__isnull=True) | Q(net_utilization_recv__isnull=True)
        )
        
        if not metrics.exists():
            return 0.0
        
        # Calculate average of sent and received utilization
        total_util = 0.0
        count = 0
        
        for metric in metrics:
            sent = metric.net_utilization_sent or 0.0
            recv = metric.net_utilization_recv or 0.0
            # Average of sent and received
            avg_util = (sent + recv) / 2.0
            total_util += avg_util
            count += 1
        
        if count == 0:
            return 0.0
        
        return round(total_util / count, 2)
    except Exception:
        return 0.0


def calculate_response_time_sli(server, start_date, end_date):
    """
    Calculate Response Time SLI as average latency in milliseconds.
    Only includes services with monitoring_enabled=True.
    
    Returns: average latency in milliseconds
    """
    try:
        # Get all monitored services for this server
        monitored_services = Service.objects.filter(
            server=server,
            monitoring_enabled=True
        )
        
        if not monitored_services.exists():
            return 0.0
        
        # Get latency measurements for monitored services in time window
        measurements = ServiceLatencyMeasurement.objects.filter(
            service__in=monitored_services,
            timestamp__gte=start_date,
            timestamp__lte=end_date,
            success=True
        )
        
        if not measurements.exists():
            return 0.0
        
        # Calculate average latency
        avg_latency = measurements.aggregate(avg=Avg('latency_ms'))['avg']
        
        return round(avg_latency or 0.0, 2)
    except Exception:
        return 0.0


def calculate_error_rate_sli(server, start_date, end_date):
    """
    Calculate Error Rate SLI as percentage of time alerts were triggered.
    
    This measures the alert rate - the percentage of monitoring intervals
    where at least one alert (CPU, MEMORY, DISK, SERVICE) was triggered.
    
    Returns: error rate percentage (0-100) - lower is better
    """
    try:
        from core.models import AlertHistory
        
        # Get total time window in hours
        total_hours = (end_date - start_date).total_seconds() / 3600
        
        if total_hours <= 0:
            return 0.0
        
        # Count unique hours where alerts were triggered
        # This gives us a more accurate picture than just counting alerts
        alerts = AlertHistory.objects.filter(
            server=server,
            sent_at__gte=start_date,
            sent_at__lte=end_date,
            status='triggered'
        )
        
        total_alerts = alerts.count()
        
        if total_alerts == 0:
            return 0.0
        
        # Count unique hours with alerts (to avoid double-counting burst alerts)
        unique_alert_hours = set()
        for alert in alerts.values('sent_at'):
            # Round to nearest hour
            hour_key = alert['sent_at'].replace(minute=0, second=0, microsecond=0)
            unique_alert_hours.add(hour_key)
        
        # Calculate error rate as percentage of hours with alerts
        # This measures "what percentage of time had issues"
        error_rate = (len(unique_alert_hours) / total_hours) * 100
        
        # Cap at 100%
        return round(min(error_rate, 100.0), 2)
    except Exception:
        return 0.0


def calculate_sli_value(server, metric_type, start_date, end_date):
    """
    Calculate SLI value for a given metric type.
    
    Args:
        server: Server instance
        metric_type: One of the MetricType choices
        start_date: Start of time window
        end_date: End of time window
    
    Returns: SLI value (float)
    """
    metric_type_map = {
        'UPTIME': calculate_uptime_sli,
        'CPU': calculate_cpu_sli,
        'MEMORY': calculate_memory_sli,
        'DISK': calculate_disk_sli,
        'NETWORK': calculate_network_sli,
        'RESPONSE_TIME': calculate_response_time_sli,
        'ERROR_RATE': calculate_error_rate_sli,
    }
    
    calculator = metric_type_map.get(metric_type)
    if not calculator:
        return 0.0
    
    return calculator(server, start_date, end_date)


def check_compliance(sli_value, slo_config):
    """
    Check if SLI value meets SLO target.
    
    Args:
        sli_value: Calculated SLI value
        slo_config: SLOConfig instance
    
    Returns: (is_compliant: bool, compliance_percentage: float)
    """
    if not slo_config:
        return False, 0.0
    
    target_value = slo_config.target_value
    operator = slo_config.target_operator
    
    # Determine compliance based on operator
    if operator == 'gte':
        is_compliant = sli_value >= target_value
    elif operator == 'lte':
        is_compliant = sli_value <= target_value
    elif operator == 'eq':
        is_compliant = abs(sli_value - target_value) < 0.01  # Small tolerance for float comparison
    else:
        is_compliant = False
    
    # Calculate compliance percentage
    if target_value == 0:
        compliance_percentage = 100.0 if is_compliant else 0.0
    else:
        # For >= operators, percentage is (actual / target) * 100, capped at 100
        # For <= operators, percentage is (target / actual) * 100, capped at 100
        if operator == 'gte':
            compliance_percentage = min((sli_value / target_value) * 100, 100.0)
        elif operator == 'lte':
            compliance_percentage = min((target_value / sli_value) * 100, 100.0) if sli_value > 0 else 0.0
        else:  # eq
            compliance_percentage = 100.0 if is_compliant else 0.0
    
    return is_compliant, round(compliance_percentage, 2)


def get_metric_timeseries(server, metric_type, start_date, end_date, interval='hour'):
    """
    Get time-series data for a metric, suitable for charting.
    
    Args:
        server: Server instance (or None for all servers average)
        metric_type: One of 'CPU', 'MEMORY', 'DISK', 'ERROR_RATE'
        start_date: Start of time window
        end_date: End of time window
        interval: 'hour' or 'day' - granularity of data points
    
    Returns:
        List of {'timestamp': ISO string, 'value': float}
    """
    from django.db.models.functions import TruncHour, TruncDay
    from core.models import AlertHistory
    
    data_points = []
    
    # Choose truncation function based on interval
    trunc_func = TruncHour if interval == 'hour' else TruncDay
    
    try:
        if metric_type == 'CPU':
            data_points = _get_cpu_timeseries(server, start_date, end_date, trunc_func)
        elif metric_type == 'MEMORY':
            data_points = _get_memory_timeseries(server, start_date, end_date, trunc_func)
        elif metric_type == 'DISK':
            data_points = _get_disk_timeseries(server, start_date, end_date, trunc_func)
        elif metric_type == 'ERROR_RATE':
            data_points = _get_error_rate_timeseries(server, start_date, end_date, trunc_func, interval)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error getting timeseries for {metric_type}: {e}")
        return []
    
    return data_points


def _get_cpu_timeseries(server, start_date, end_date, trunc_func):
    """Get CPU usage time-series data with average and peak values."""
    from django.db.models import Max
    
    query = SystemMetric.objects.filter(
        timestamp__gte=start_date,
        timestamp__lte=end_date
    )
    
    if server:
        query = query.filter(server=server)
    
    aggregated = query.annotate(
        period=trunc_func('timestamp')
    ).values('period').annotate(
        avg_value=Avg('cpu_percent'),
        max_value=Max('cpu_percent')
    ).order_by('period')
    
    return [
        {
            'timestamp': item['period'].isoformat(),
            'value': round(item['avg_value'] or 0, 2),
            'peak': round(item['max_value'] or 0, 2)
        }
        for item in aggregated
    ]


def _get_memory_timeseries(server, start_date, end_date, trunc_func):
    """Get Memory usage time-series data with average and peak values."""
    from django.db.models import Max
    
    query = SystemMetric.objects.filter(
        timestamp__gte=start_date,
        timestamp__lte=end_date
    )
    
    if server:
        query = query.filter(server=server)
    
    aggregated = query.annotate(
        period=trunc_func('timestamp')
    ).values('period').annotate(
        avg_value=Avg('memory_percent'),
        max_value=Max('memory_percent')
    ).order_by('period')
    
    return [
        {
            'timestamp': item['period'].isoformat(),
            'value': round(item['avg_value'] or 0, 2),
            'peak': round(item['max_value'] or 0, 2)
        }
        for item in aggregated
    ]


def _get_disk_timeseries(server, start_date, end_date, trunc_func):
    """Get Disk usage time-series data from JSON field."""
    query = SystemMetric.objects.filter(
        timestamp__gte=start_date,
        timestamp__lte=end_date
    )
    
    if server:
        query = query.filter(server=server)
    
    # Since disk_usage is a JSON field, we need to process it manually
    # Group by time period
    metrics = query.annotate(
        period=trunc_func('timestamp')
    ).order_by('period')
    
    # Aggregate manually
    period_data = {}
    for metric in metrics:
        period_key = metric.period.isoformat()
        disk_usage = metric.disk_usage or {}
        
        # Get root partition or average
        disk_percent = 0
        if isinstance(disk_usage, dict):
            if '/' in disk_usage:
                disk_info = disk_usage['/']
                if isinstance(disk_info, dict) and 'percent' in disk_info:
                    disk_percent = disk_info['percent']
            else:
                percents = [
                    info.get('percent', 0)
                    for info in disk_usage.values()
                    if isinstance(info, dict) and 'percent' in info
                ]
                if percents:
                    disk_percent = sum(percents) / len(percents)
        
        if period_key not in period_data:
            period_data[period_key] = {'total': 0, 'count': 0, 'timestamp': metric.period}
        period_data[period_key]['total'] += disk_percent
        period_data[period_key]['count'] += 1
    
    # Calculate averages
    data_points = []
    for period_key in sorted(period_data.keys()):
        item = period_data[period_key]
        avg_value = item['total'] / item['count'] if item['count'] > 0 else 0
        data_points.append({
            'timestamp': item['timestamp'].isoformat(),
            'value': round(avg_value, 2)
        })
    
    return data_points


def _get_error_rate_timeseries(server, start_date, end_date, trunc_func, interval):
    """Get Error Rate time-series data based on alert frequency."""
    from core.models import AlertHistory
    
    # Get all alerts in the time window
    query = AlertHistory.objects.filter(
        sent_at__gte=start_date,
        sent_at__lte=end_date,
        status='triggered'
    )
    
    if server:
        query = query.filter(server=server)
    
    # Count alerts per period
    aggregated = query.annotate(
        period=trunc_func('sent_at')
    ).values('period').annotate(
        alert_count=Count('id')
    ).order_by('period')
    
    # Build a complete timeline with zeros for periods without alerts
    alert_periods = {item['period'].isoformat(): item['alert_count'] for item in aggregated}
    
    # Generate all time periods
    data_points = []
    current = start_date.replace(minute=0, second=0, microsecond=0)
    
    if interval == 'day':
        current = current.replace(hour=0)
        delta = timedelta(days=1)
    else:
        delta = timedelta(hours=1)
    
    while current <= end_date:
        period_key = current.isoformat()
        # Convert alert count to error rate percentage (scaled)
        # For visualization: 0 alerts = 0%, 1+ alerts = shows as percentage
        # We use a scaling factor to make the error rate visible on the same chart
        alert_count = alert_periods.get(period_key, 0)
        # Scale: each alert in a period adds to the error rate (capped at 100)
        error_rate = min(alert_count * 10, 100)  # Each alert = 10% for visibility
        
        data_points.append({
            'timestamp': period_key,
            'value': error_rate
        })
        current += delta
    
    return data_points


def get_reliability_metrics_timeseries(server_id, period='24h'):
    """
    Get all reliability metrics time-series data for the dashboard chart.
    
    Args:
        server_id: Server ID or 'all' for average across all servers
        period: '24h', '7d', or '30d'
    
    Returns:
        dict with 'cpu', 'memory', 'disk', 'error_rate' keys,
        each containing list of {timestamp, value} data points
    """
    from core.models import Server
    
    end_date = timezone.now()
    
    # Determine time range and interval
    if period == '24h':
        start_date = end_date - timedelta(hours=24)
        interval = 'hour'
    elif period == '7d':
        start_date = end_date - timedelta(days=7)
        interval = 'hour'  # Still hourly for 7 days, but could be 'day' for less granularity
    else:  # 30d
        start_date = end_date - timedelta(days=30)
        interval = 'day'  # Daily for 30 days to reduce data points
    
    # Get server if specified
    server = None
    if server_id and server_id != 'all':
        try:
            server = Server.objects.get(id=int(server_id))
        except (Server.DoesNotExist, ValueError):
            pass
    
    # Get time-series for each metric
    return {
        'cpu': get_metric_timeseries(server, 'CPU', start_date, end_date, interval),
        'memory': get_metric_timeseries(server, 'MEMORY', start_date, end_date, interval),
        'disk': get_metric_timeseries(server, 'DISK', start_date, end_date, interval),
        'error_rate': get_metric_timeseries(server, 'ERROR_RATE', start_date, end_date, interval),
        'period': period,
        'interval': interval,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat()
    }





