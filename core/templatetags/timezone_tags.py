"""
Django template tags for timezone-aware datetime formatting and anomaly explanations.
"""
from django import template
# Import from the actual utils.py file, not the package __init__.py
import sys
import os
_core_dir = os.path.dirname(os.path.dirname(__file__))
utils_file_path = os.path.join(_core_dir, 'utils.py')
if os.path.exists(utils_file_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("_core_utils_for_tags", utils_file_path)
    _utils_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_utils_module)
    format_datetime_for_display = _utils_module.format_datetime_for_display
else:
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
    from core.utils import get_display_timezone
    return get_display_timezone()


@register.filter(name='timezone_abbrev')
def timezone_abbrev(value):
    """
    Get timezone abbreviation (e.g., 'IST', 'EST', 'UTC').
    
    Usage in template:
        {{ alert.sent_at|timezone_abbrev }}
    """
    if value is None:
        return ""
    from core.utils import convert_to_display_timezone
    from datetime import datetime
    import pytz
    
    dt = convert_to_display_timezone(value)
    if dt is None:
        return ""
    
    try:
        # Get timezone abbreviation
        tz = dt.tzinfo
        if hasattr(tz, 'zone'):
            # Use pytz timezone
            tz_obj = pytz.timezone(tz.zone)
            abbrev = dt.strftime('%Z')
            if not abbrev or abbrev == dt.strftime('%z'):
                # Fallback: try to get abbreviation from timezone name
                try:
                    # Get abbreviation from timezone
                    now = datetime.now(tz_obj)
                    abbrev = now.strftime('%Z')
                    if not abbrev:
                        abbrev = tz.zone.split('/')[-1][:3].upper()
                except:
                    abbrev = tz.zone.split('/')[-1][:3].upper()
            return abbrev
        return dt.strftime('%Z') or 'UTC'
    except Exception:
        return 'UTC'


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

