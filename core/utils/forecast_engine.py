"""
Forecast Engine - Disk space forecasting using linear regression
"""
from django.utils import timezone
from datetime import timedelta
from core.models import Server, SystemMetric
import json


def forecast_disk_usage(server, mount_point, days=30):
    """
    Forecast disk usage for the next N days using linear regression.
    
    Args:
        server: Server instance
        mount_point: Disk mount point (e.g., '/', '/var')
        days: Number of days to forecast (default: 30)
    
    Returns:
        Dictionary with current_usage, forecast points, and warning message
    """
    # Get historical disk usage data
    thirty_days_ago = timezone.now() - timedelta(days=30)
    
    metrics = SystemMetric.objects.filter(
        server=server,
        timestamp__gte=thirty_days_ago
    ).order_by('timestamp')
    
    if metrics.count() < 5:
        # Not enough data for forecasting
        return {
            'current_usage': 0,
            'forecast': [],
            'warning': None,
            'disk_size': None
        }
    
    # Extract disk usage data points
    usage_points = []
    for metric in metrics:
        if metric.disk_usage:
            try:
                disk_data = json.loads(metric.disk_usage) if isinstance(metric.disk_usage, str) else metric.disk_usage
                if isinstance(disk_data, dict) and mount_point in disk_data:
                    partition_data = disk_data[mount_point]
                    usage_points.append({
                        'timestamp': metric.timestamp,
                        'usage_percent': partition_data.get('percent', 0),
                        'total': partition_data.get('total', 0),
                        'used': partition_data.get('used', 0)
                    })
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    
    if len(usage_points) < 5:
        return {
            'current_usage': 0,
            'forecast': [],
            'warning': None,
            'disk_size': None
        }
    
    # Get current usage
    current_point = usage_points[-1]
    current_usage = current_point['usage_percent']
    disk_size_bytes = current_point.get('total', 0)
    
    # Format disk size to human-readable format
    def format_size(bytes_val):
        if not bytes_val or bytes_val == 0:
            return None
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_val < 1024.0:
                return f"{bytes_val:.1f} {unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.1f} PB"
    
    disk_size = format_size(disk_size_bytes) if disk_size_bytes else None
    
    # Simple linear regression
    # Calculate slope (growth rate per day)
    if len(usage_points) >= 2:
        time_diffs = []
        usage_diffs = []
        
        for i in range(1, len(usage_points)):
            time_diff = (usage_points[i]['timestamp'] - usage_points[i-1]['timestamp']).total_seconds() / 86400  # days
            usage_diff = usage_points[i]['usage_percent'] - usage_points[i-1]['usage_percent']
            
            if time_diff > 0:
                time_diffs.append(time_diff)
                usage_diffs.append(usage_diff / time_diff)  # usage change per day
        
        if usage_diffs:
            avg_growth_rate = sum(usage_diffs) / len(usage_diffs)
        else:
            avg_growth_rate = 0
    else:
        avg_growth_rate = 0
    
    # Generate forecast points
    forecast = []
    base_date = timezone.now()
    last_usage = current_usage
    
    for day in range(1, days + 1):
        forecast_date = base_date + timedelta(days=day)
        predicted_usage = last_usage + (avg_growth_rate * day)
        
        # Clamp between 0 and 100
        predicted_usage = max(0, min(100, predicted_usage))
        
        forecast.append({
            'date': forecast_date.isoformat(),
            'current_usage': current_usage,
            'predicted_usage': round(predicted_usage, 2)
        })
        
        last_usage = predicted_usage
    
    # Generate warning if forecasted usage exceeds threshold
    final_forecast = forecast[-1]['predicted_usage'] if forecast else current_usage
    warning = None
    
    if final_forecast > 90:
        warning = f"Warning: Projected to reach {final_forecast:.1f}% capacity in {days} days. Consider cleanup or expansion."
    elif final_forecast > 77:
        warning = f"Warning: Projected to reach {final_forecast:.1f}% capacity in {days} days. Consider cleanup or expansion."
    
    return {
        'current_usage': round(current_usage, 1),
        'forecast': forecast,
        'warning': warning,
        'disk_size': disk_size,
        'growth_rate': round(avg_growth_rate, 3)
    }

