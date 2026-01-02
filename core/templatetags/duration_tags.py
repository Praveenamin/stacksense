from django import template
from datetime import timedelta

register = template.Library()

@register.filter
def format_duration(seconds):
    """Format duration in seconds to human-readable format (e.g., '2h 2m', '47m')"""
    if seconds is None or seconds <= 0:
        return "—"
    
    try:
        seconds = float(seconds)
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    except (ValueError, TypeError):
        return "—"


