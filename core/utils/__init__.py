# Core utilities package

from django.utils import timezone as django_timezone
import pytz
import sys
import os
import importlib.util

# Import has_privilege from core/utils.py (the file, not this package)
# We need to use importlib to avoid circular import issues
_core_dir = os.path.dirname(os.path.dirname(__file__))
utils_file_path = os.path.join(_core_dir, 'utils.py')

if os.path.exists(utils_file_path):
    spec = importlib.util.spec_from_file_location("_core_utils_module", utils_file_path)
    _core_utils_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_core_utils_module)
    has_privilege = _core_utils_module.has_privilege
else:
    # Fallback: define has_privilege directly
    def has_privilege(user, privilege_key):
        """Check if a user has a specific privilege."""
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        try:
            from core.models import UserACL
            acl = UserACL.objects.get(user=user)
            return acl.has_privilege(privilege_key)
        except Exception:
            return False


def format_datetime_for_display(dt, timezone_str=None):
    """
    Format datetime for display with optional timezone conversion.
    
    Args:
        dt: datetime object (naive or aware)
        timezone_str: Optional timezone string (e.g., "UTC", "America/New_York")
    
    Returns:
        Formatted datetime string
    """
    if not dt:
        return ''
    
    # If datetime is naive, assume it'''s in UTC
    if django_timezone.is_naive(dt):
        dt = django_timezone.make_aware(dt, django_timezone.utc)
    
    # Convert to specified timezone if provided
    if timezone_str:
        try:
            tz = pytz.timezone(timezone_str)
            dt = dt.astimezone(tz)
        except pytz.UnknownTimeZoneError:
            pass  # Use original timezone if invalid
    
    # Format for display
    return dt.strftime('%Y-%m-%d %H:%M:%S %Z')
