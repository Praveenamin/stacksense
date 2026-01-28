"""
Trend Detection Utilities for Alert Pattern Analysis.

Analyzes AlertHistory to detect recurring patterns such as:
- Daily spikes at specific hours (e.g., 2AM backups)
- Weekly patterns (e.g., Fridays)
- Periodic cycles

Hours and weekdays use the server timezone (UTC). The description explicitly
says "server time" so it is clear the time is not in the app's display timezone.
"""

from django.utils import timezone
from django.db.models import Count
from django.db.models.functions import ExtractHour, ExtractWeekDay
from datetime import timedelta
from collections import defaultdict


def detect_alert_patterns(server, alert_type='CPU', lookback_days=30, min_alerts=5):
    """
    Analyze alert history to find recurring patterns for a specific server and alert type.
    
    Args:
        server: Server instance to analyze
        alert_type: Type of alert to analyze (CPU, MEMORY, DISK, etc.)
        lookback_days: Number of days to look back for pattern detection
        min_alerts: Minimum number of alerts required to detect a pattern
    
    Returns:
        dict with pattern information:
        {
            'has_pattern': bool,
            'pattern_type': 'hourly' | 'daily' | 'weekly' | None,
            'pattern_description': str,
            'confidence': float (0-100),
            'peak_hour': int or None,
            'peak_day': str or None,
            'total_alerts': int,
            'recommendation': str
        }
    """
    from core.models import AlertHistory
    
    end_date = timezone.now()
    start_date = end_date - timedelta(days=lookback_days)
    
    # Get alerts for this server and type
    alerts = AlertHistory.objects.filter(
        server=server,
        alert_type__iexact=alert_type,
        sent_at__gte=start_date,
        sent_at__lte=end_date
    )
    
    total_alerts = alerts.count()
    
    # Default response - no pattern detected
    result = {
        'has_pattern': False,
        'pattern_type': None,
        'pattern_description': None,
        'confidence': 0.0,
        'peak_hour': None,
        'peak_day': None,
        'total_alerts': total_alerts,
        'recommendation': None
    }
    
    if total_alerts < min_alerts:
        return result
    
    # Use server timezone (UTC) for hour and day
    by_hour = alerts.annotate(
        hour=ExtractHour('sent_at')
    ).values('hour').annotate(
        count=Count('id')
    ).order_by('-count')
    
    by_day = alerts.annotate(
        day=ExtractWeekDay('sent_at')
    ).values('day').annotate(
        count=Count('id')
    ).order_by('-count')
    
    hour_data = list(by_hour)
    day_data = list(by_day)
    
    # Check for hourly concentration pattern
    hourly_pattern = _detect_hourly_pattern(hour_data, total_alerts)
    
    # Check for daily/weekly pattern
    weekly_pattern = _detect_weekly_pattern(day_data, total_alerts)
    
    # Choose the strongest pattern
    if hourly_pattern['confidence'] >= weekly_pattern['confidence'] and hourly_pattern['confidence'] >= 40:
        result.update({
            'has_pattern': True,
            'pattern_type': 'hourly',
            'pattern_description': hourly_pattern['description'],
            'confidence': hourly_pattern['confidence'],
            'peak_hour': hourly_pattern['peak_hour'],
            'recommendation': hourly_pattern['recommendation']
        })
    elif weekly_pattern['confidence'] >= 50:
        result.update({
            'has_pattern': True,
            'pattern_type': 'weekly',
            'pattern_description': weekly_pattern['description'],
            'confidence': weekly_pattern['confidence'],
            'peak_day': weekly_pattern['peak_day'],
            'recommendation': weekly_pattern['recommendation']
        })
    
    return result


def _detect_hourly_pattern(hour_data, total_alerts):
    """
    Detect if alerts are concentrated in specific hours.
    Returns pattern info if concentration > 40% in a 2-hour window.
    """
    if not hour_data or total_alerts == 0:
        return {'confidence': 0, 'description': None, 'peak_hour': None, 'recommendation': None}
    
    # Find the peak hour
    peak = hour_data[0]
    peak_hour = peak['hour']
    peak_count = peak['count']
    
    # Check adjacent hours for 2-hour window concentration
    hour_counts = {h['hour']: h['count'] for h in hour_data}
    
    # Sum counts for peak hour and adjacent hour
    adjacent_hour = (peak_hour + 1) % 24
    prev_hour = (peak_hour - 1) % 24
    
    window_count = peak_count
    window_count += hour_counts.get(adjacent_hour, 0)
    window_count += hour_counts.get(prev_hour, 0)
    
    # Calculate concentration percentage
    concentration = (window_count / total_alerts) * 100
    
    if concentration >= 40:
        # Format hour for display
        hour_str = _format_hour(peak_hour)
        
        description = f"{concentration:.0f}% of {total_alerts} alerts occur around {hour_str} (server time)"
        
        # Generate recommendation based on hour
        if 0 <= peak_hour <= 6:
            recommendation = "Check scheduled overnight tasks, cron jobs, or backup processes running at this time"
        elif 7 <= peak_hour <= 9:
            recommendation = "Check morning batch jobs or system startup processes"
        elif 10 <= peak_hour <= 17:
            recommendation = "Check business-hour workloads or scheduled reports"
        else:
            recommendation = "Check evening maintenance tasks or scheduled processes"
        
        return {
            'confidence': concentration,
            'description': description,
            'peak_hour': peak_hour,
            'recommendation': recommendation
        }
    
    return {'confidence': concentration, 'description': None, 'peak_hour': None, 'recommendation': None}


def _detect_weekly_pattern(day_data, total_alerts):
    """
    Detect if alerts are concentrated on specific days of the week.
    Returns pattern info if concentration > 50% on a single day.
    """
    if not day_data or total_alerts == 0:
        return {'confidence': 0, 'description': None, 'peak_day': None, 'recommendation': None}
    
    day_names = {
        1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday',
        5: 'Thursday', 6: 'Friday', 7: 'Saturday'
    }
    
    # Find the peak day
    peak = day_data[0]
    peak_day_num = peak['day']
    peak_count = peak['count']
    peak_day_name = day_names.get(peak_day_num, 'Unknown')
    
    # Calculate concentration percentage
    concentration = (peak_count / total_alerts) * 100
    
    if concentration >= 50:
        description = f"{concentration:.0f}% of {total_alerts} alerts occur on {peak_day_name}s (server time)"
        
        # Generate recommendation based on day
        if peak_day_num in [1, 7]:  # Weekend
            recommendation = "Check weekend maintenance windows or backup schedules"
        elif peak_day_num == 2:  # Monday
            recommendation = "Check Monday startup processes or weekly batch jobs"
        elif peak_day_num == 6:  # Friday
            recommendation = "Check end-of-week reports or weekly cleanup tasks"
        else:
            recommendation = "Check scheduled mid-week processes or recurring tasks"
        
        return {
            'confidence': concentration,
            'description': description,
            'peak_day': peak_day_name,
            'recommendation': recommendation
        }
    
    return {'confidence': concentration, 'description': None, 'peak_day': None, 'recommendation': None}


def _format_hour(hour):
    """Format hour (0-23) as readable string like '2 AM' or '2 PM'."""
    if hour == 0:
        return "12 AM (midnight)"
    elif hour == 12:
        return "12 PM (noon)"
    elif hour < 12:
        return f"{hour} AM"
    else:
        return f"{hour - 12} PM"


def detect_all_server_patterns(alert_types=None, lookback_days=30, min_alerts=5):
    """
    Analyze all servers for alert patterns.
    
    Args:
        alert_types: List of alert types to check (default: ['CPU', 'MEMORY', 'DISK'])
        lookback_days: Number of days to look back
        min_alerts: Minimum alerts required to detect a pattern
    
    Returns:
        List of detected patterns with server and alert type info:
        [
            {
                'server_id': int,
                'server_name': str,
                'alert_type': str,
                'pattern': dict (from detect_alert_patterns)
            },
            ...
        ]
    """
    from core.models import Server
    
    if alert_types is None:
        alert_types = ['CPU', 'MEMORY', 'DISK']
    
    insights = []
    
    for server in Server.objects.all():
        for alert_type in alert_types:
            pattern = detect_alert_patterns(
                server=server,
                alert_type=alert_type,
                lookback_days=lookback_days,
                min_alerts=min_alerts
            )
            
            if pattern['has_pattern']:
                insights.append({
                    'server_id': server.id,
                    'server_name': server.name,
                    'alert_type': alert_type,
                    'pattern': pattern
                })
    
    # Sort by confidence (highest first)
    insights.sort(key=lambda x: x['pattern']['confidence'], reverse=True)
    
    return insights


def get_trend_summary(lookback_days=30):
    """
    Get a summary of all detected trends for the dashboard.
    
    Returns:
        dict with:
        {
            'total_patterns_detected': int,
            'servers_with_patterns': int,
            'insights': list of pattern insights,
            'analysis_period_days': int
        }
    """
    insights = detect_all_server_patterns(lookback_days=lookback_days)
    
    # Count unique servers with patterns
    servers_with_patterns = len(set(i['server_id'] for i in insights))
    
    return {
        'total_patterns_detected': len(insights),
        'servers_with_patterns': servers_with_patterns,
        'insights': insights,
        'analysis_period_days': lookback_days
    }
