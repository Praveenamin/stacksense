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
    Calculate Error Rate SLI as percentage of errors.
    Currently returns 0.0 as error tracking from logs is not yet implemented.
    
    Returns: error rate percentage (0-100)
    """
    # TODO: Implement error rate calculation from log events
    # For now, return 0.0
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

