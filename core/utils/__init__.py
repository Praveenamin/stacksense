# Core utilities package

from django.utils import timezone as django_timezone
import pytz
import sys
import os
import importlib.util

# Import has_privilege and get_app_heartbeat_timestamp from core/utils.py (the file, not this package)
# We need to use importlib to avoid circular import issues
_core_dir = os.path.dirname(os.path.dirname(__file__))
utils_file_path = os.path.join(_core_dir, 'utils.py')

if os.path.exists(utils_file_path):
    spec = importlib.util.spec_from_file_location("_core_utils_module", utils_file_path)
    _core_utils_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_core_utils_module)
    has_privilege = _core_utils_module.has_privilege
    get_app_heartbeat_timestamp = _core_utils_module.get_app_heartbeat_timestamp
    get_display_timezone = _core_utils_module.get_display_timezone
    convert_to_display_timezone = _core_utils_module.convert_to_display_timezone
    format_datetime_for_display = _core_utils_module.format_datetime_for_display
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
    
    def get_app_heartbeat_timestamp():
        """Get the current timestamp as ISO format string for app heartbeat tracking."""
        return django_timezone.now().isoformat()


# format_datetime_for_display is imported from utils.py above
# (line 22) - this fallback is not used when utils.py exists
