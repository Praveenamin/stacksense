"""
Django template tags for timezone-aware datetime formatting and anomaly explanations.
"""
from django import template
from core.utils import format_datetime_for_display
from django.conf import settings
import re

register = template.Library()


@register.filter(name='timezone_date')
def timezone_date(value, format_string="M d, Y H:i:s"):
    """
    Format a datetime in the display timezone.
    
    Usage in template:
        {{ alert.sent_at|timezone_date:"M d, Y H:i:s" }}
        {{ alert.sent_at|timezone_date }}  (uses default format)
    
    This respects the DISPLAY_TIME_ZONE setting without affecting
    core application functions which use TIME_ZONE (UTC).
    """
    if value is None:
        return ""
    return format_datetime_for_display(value, format_string)


@register.simple_tag
def display_timezone():
    """
    Return the current display timezone name.
    
    Usage in template:
        {% display_timezone as tz %}
        <span>Times shown in {{ tz }}</span>
    """
    return getattr(settings, 'DISPLAY_TIME_ZONE', settings.TIME_ZONE)


@register.filter(name='parse_anomaly_explanation')
def parse_anomaly_explanation(value):
    """
    Parse anomaly explanation into cause and fix sections.
    
    Returns a dict with 'cause' and 'fix' keys if found, otherwise {'cause': value, 'fix': ''}
    
    Usage in template:
        {% with explanation=anomaly.explanation|parse_anomaly_explanation %}
            {% if explanation.cause %}CAUSE: {{ explanation.cause }}{% endif %}
            {% if explanation.fix %}FIX: {{ explanation.fix }}{% endif %}
        {% endwith %}
    """
    if not value:
        return {'cause': '', 'fix': ''}
    
    # Try to find CAUSE: and FIX: sections
    cause_match = re.search(r'CAUSE:\s*(.*?)(?=FIX:|$)', value, re.IGNORECASE | re.DOTALL)
    fix_match = re.search(r'FIX:\s*(.*?)$', value, re.IGNORECASE | re.DOTALL)
    
    cause = cause_match.group(1).strip() if cause_match else ''
    fix = fix_match.group(1).strip() if fix_match else ''
    
    # If no sections found, return full text as cause
    if not cause and not fix:
        cause = value.strip()
    
    return {'cause': cause, 'fix': fix}

