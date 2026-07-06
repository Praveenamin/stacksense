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


# Resource reliability thresholds: the resource SLIs report the % of samples whose value is
# at/under these (a real "how often did we stay healthy" indicator, not a raw average).
RESOURCE_THRESHOLDS = {"CPU": 85.0, "MEMORY": 90.0, "DISK": 90.0, "NETWORK": 80.0}


def _synthetic_results_for_server(server, start_date, end_date):
    """SyntheticCheckResult queryset for all of a server's synthetic checks in the window."""
    from core.models import SyntheticCheckResult
    return SyntheticCheckResult.objects.filter(
        synthetic_check__server=server,
        timestamp__gte=start_date,
        timestamp__lte=end_date,
    )


def _pct_at_or_under(values, threshold):
    """% of the (non-None) values that are at or under `threshold`. None if no values."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    under = sum(1 for v in vals if v <= threshold)
    return round(under / len(vals) * 100.0, 2)


def calculate_uptime_sli(server, start_date, end_date):
    """Availability SLI = % of this server's synthetic probes that SUCCEEDED in the window.

    This is real ENDPOINT availability from persisted SyntheticCheckResult rows (higher is
    better). Returns None when the server has no synthetic-check data in the window -- we do
    NOT fabricate an uptime number from metric presence."""
    try:
        qs = _synthetic_results_for_server(server, start_date, end_date)
        total = qs.count()
        if not total:
            return None
        ok = qs.filter(success=True).count()
        return round(ok / total * 100.0, 2)
    except Exception:
        return None


def calculate_error_rate_sli(server, start_date, end_date):
    """Check-failure rate = % of this server's synthetic probes that FAILED in the window
    (i.e. 100 - availability), from real SyntheticCheckResult data (lower is better).
    Returns None when there is no probe data. (Replaces the old alert-frequency proxy.)"""
    try:
        qs = _synthetic_results_for_server(server, start_date, end_date)
        total = qs.count()
        if not total:
            return None
        failed = qs.exclude(success=True).count()
        return round(failed / total * 100.0, 2)
    except Exception:
        return None


def calculate_response_time_sli(server, start_date, end_date):
    """Response-time SLI = average synthetic-probe latency (ms) for SUCCESSFUL probes in the
    window (lower is better). This is outside-in probe latency to the server's checks.
    Returns None when there is no probe latency data. (Replaces the disabled service-latency feed.)"""
    try:
        val = _synthetic_results_for_server(server, start_date, end_date).filter(
            success=True, response_time_ms__isnull=False
        ).aggregate(a=Avg("response_time_ms"))["a"]
        return round(val, 2) if val is not None else None
    except Exception:
        return None


def calculate_cpu_sli(server, start_date, end_date):
    """CPU reliability SLI = % of samples with CPU at/under the reliability threshold
    (higher is better). None if no samples."""
    try:
        vals = SystemMetric.objects.filter(
            server=server, timestamp__gte=start_date, timestamp__lte=end_date
        ).values_list("cpu_percent", flat=True)
        return _pct_at_or_under(list(vals), RESOURCE_THRESHOLDS["CPU"])
    except Exception:
        return None


def calculate_memory_sli(server, start_date, end_date):
    """Memory reliability SLI = % of samples with memory at/under the threshold (higher is
    better). None if no samples."""
    try:
        vals = SystemMetric.objects.filter(
            server=server, timestamp__gte=start_date, timestamp__lte=end_date
        ).values_list("memory_percent", flat=True)
        return _pct_at_or_under(list(vals), RESOURCE_THRESHOLDS["MEMORY"])
    except Exception:
        return None


def calculate_disk_sli(server, start_date, end_date):
    """Disk reliability SLI = % of samples with primary-disk usage at/under the threshold
    (higher is better). Parses the disk_usage JSON per sample. None if no samples."""
    try:
        metrics = SystemMetric.objects.filter(
            server=server, timestamp__gte=start_date, timestamp__lte=end_date
        ).only("disk_usage")
        percents = []
        for metric in metrics:
            disk_usage = metric.disk_usage or {}
            if not isinstance(disk_usage, dict):
                continue
            if "/" in disk_usage and isinstance(disk_usage["/"], dict) and "percent" in disk_usage["/"]:
                percents.append(disk_usage["/"]["percent"])
            else:
                ps = [i.get("percent", 0) for i in disk_usage.values()
                      if isinstance(i, dict) and "percent" in i]
                if ps:
                    percents.append(sum(ps) / len(ps))
        return _pct_at_or_under(percents, RESOURCE_THRESHOLDS["DISK"])
    except Exception:
        return None


def calculate_network_sli(server, start_date, end_date):
    """Network reliability SLI = % of samples with network utilization at/under the threshold
    (higher is better). None if no samples."""
    try:
        metrics = SystemMetric.objects.filter(
            server=server, timestamp__gte=start_date, timestamp__lte=end_date
        ).only("net_utilization_sent", "net_utilization_recv")
        vals = []
        for metric in metrics:
            sent = metric.net_utilization_sent
            recv = metric.net_utilization_recv
            if sent is None and recv is None:
                continue
            vals.append(((sent or 0.0) + (recv or 0.0)) / 2.0)
        return _pct_at_or_under(vals, RESOURCE_THRESHOLDS["NETWORK"])
    except Exception:
        return None


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


def _anomaly_resolution_end(anomaly):
    """When an anomaly's window ended: recovered_at (auto-detected back-to-normal) preferred,
    else the admin resolution time, else None (still ongoing). Mirrors views._anomaly_window_end
    so MTTR here matches the Operations Report 'Avg time-to-resolve'."""
    if getattr(anomaly, "recovered_at", None):
        return anomaly.recovered_at
    if getattr(anomaly, "resolved", False) and getattr(anomaly, "resolved_at", None):
        return anomaly.resolved_at
    return None


def calculate_mttr_seconds(server, start_date, end_date):
    """MTTR = mean time to resolve, in seconds, over anomalies in the window that have ENDED
    (auto-recovered or admin-resolved). Duration is start -> end per anomaly.

    Returns None when there is no ended anomaly in the window -- we do not fabricate an MTTR.
    (Reuses the same window-end preference as the Operations Report.)"""
    from core.models import Anomaly
    qs = Anomaly.objects.filter(timestamp__gte=start_date, timestamp__lte=end_date)
    if server is not None:
        qs = qs.filter(server=server)
    durations = []
    for a in qs.only("timestamp", "recovered_at", "resolved", "resolved_at")[:5000]:
        end = _anomaly_resolution_end(a)
        if end:
            durations.append(max(0.0, (end - a.timestamp).total_seconds()))
    if not durations:
        return None
    return round(sum(durations) / len(durations), 2)


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


def _synthetic_buckets(server, start_date, end_date, trunc_func):
    """Per-period (total, ok) counts of synthetic-probe results -- the real basis for the
    availability and check-failure series. Only returns periods that actually have probe data
    (no fabricated zero-fill). Empty when the server/window has no synthetic checks."""
    from core.models import SyntheticCheckResult
    query = SyntheticCheckResult.objects.filter(
        timestamp__gte=start_date, timestamp__lte=end_date
    )
    if server:
        query = query.filter(synthetic_check__server=server)
    return (query.annotate(period=trunc_func('timestamp'))
                 .values('period')
                 .annotate(total=Count('id'), ok=Count('id', filter=Q(success=True)))
                 .order_by('period'))


def _avg_response_ms(server, start_date, end_date):
    """Average successful synthetic-probe latency (ms) over the window, for one server or all
    (server=None). This is the honest 'performance' number -- outside-in endpoint response
    time. None when there is no successful probe latency to report (we don't fabricate)."""
    from core.models import SyntheticCheckResult
    qs = SyntheticCheckResult.objects.filter(
        timestamp__gte=start_date, timestamp__lte=end_date,
        success=True, response_time_ms__isnull=False,
    )
    if server is not None:
        qs = qs.filter(synthetic_check__server=server)
    val = qs.aggregate(a=Avg("response_time_ms"))["a"]
    return round(val, 2) if val is not None else None


def _get_availability_timeseries(server, start_date, end_date, trunc_func):
    """Real endpoint availability % per period = successful probes / total probes (from
    SyntheticCheckResult). Empty list when there is no probe data."""
    return [
        {'timestamp': b['period'].isoformat(),
         'value': round((b['ok'] / b['total']) * 100.0, 2) if b['total'] else None}
        for b in _synthetic_buckets(server, start_date, end_date, trunc_func)
    ]


def _get_error_rate_timeseries(server, start_date, end_date, trunc_func, interval):
    """Real check-failure % per period = failed probes / total probes (100 - availability),
    from SyntheticCheckResult. Replaces the old alert-frequency (alert_count x 10) proxy.
    Empty list when there is no probe data -- we don't fabricate a zero-filled timeline."""
    return [
        {'timestamp': b['period'].isoformat(),
         'value': round(((b['total'] - b['ok']) / b['total']) * 100.0, 2) if b['total'] else None}
        for b in _synthetic_buckets(server, start_date, end_date, trunc_func)
    ]


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
    
    from django.db.models.functions import TruncHour, TruncDay
    trunc_func = TruncHour if interval == 'hour' else TruncDay

    # error_rate is now the REAL synthetic check-failure %, and we add a real availability series.
    # cpu/memory/disk stay as true utilization trends (context). MTTR is a scalar (None if no
    # ended anomalies in the window) -- the view humanizes it.
    return {
        'availability': _get_availability_timeseries(server, start_date, end_date, trunc_func),
        'cpu': get_metric_timeseries(server, 'CPU', start_date, end_date, interval),
        'memory': get_metric_timeseries(server, 'MEMORY', start_date, end_date, interval),
        'disk': get_metric_timeseries(server, 'DISK', start_date, end_date, interval),
        'error_rate': get_metric_timeseries(server, 'ERROR_RATE', start_date, end_date, interval),
        'mttr_seconds': calculate_mttr_seconds(server, start_date, end_date),
        'response_time_ms': _avg_response_ms(server, start_date, end_date),
        'period': period,
        'interval': interval,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat()
    }





