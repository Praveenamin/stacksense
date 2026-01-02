"""
Utility functions for timezone handling and datetime operations.
"""
from django.utils import timezone as django_timezone
from django.conf import settings
from datetime import datetime
import pytz
import paramiko
import os
import logging

logger = logging.getLogger(__name__)


def has_privilege(user, privilege_key):
    """
    Check if a user has a specific privilege.
    
    Args:
        user: Django User object
        privilege_key: String key of the privilege (e.g., 'add_server', 'manage_users')
    
    Returns:
        bool: True if user has the privilege, False otherwise
    """
    if not user or not user.is_authenticated:
        return False
    
    # Superusers always have all privileges
    if user.is_superuser:
        return True
    
    try:
        from .models import UserACL
        acl = UserACL.objects.get(user=user)
        return acl.has_privilege(privilege_key)
    except Exception:
        # If UserACL doesn't exist or other error, return False
        return False


def parse_iso_datetime(dt_str):
    """
    Parse an ISO format datetime string and return a timezone-aware datetime.
    
    Handles both timezone-aware and timezone-naive ISO strings.
    If timezone-naive, assumes UTC.
    
    Args:
        dt_str: ISO format datetime string (e.g., '2025-12-18T09:06:03.253308+00:00')
    
    Returns:
        timezone-aware datetime object using Django's configured timezone
    """
    try:
        # Parse ISO format string
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        
        # If timezone-naive, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        
        # Convert to Django's configured timezone for consistency
        return dt.astimezone(django_timezone.get_current_timezone())
    except (ValueError, AttributeError) as e:
        # Fallback: return current time if parsing fails
        return django_timezone.now()


def get_app_heartbeat_timestamp():
    """
    Get the current timestamp as ISO format string for app heartbeat tracking.
    
    Returns:
        ISO format string with timezone info
    """
    return django_timezone.now().isoformat()


def parse_app_heartbeat(dt_str):
    """
    Parse app heartbeat timestamp string.
    
    Args:
        dt_str: ISO format datetime string from cache or file
    
    Returns:
        timezone-aware datetime object, or None if parsing fails
    """
    try:
        return parse_iso_datetime(dt_str)
    except Exception:
        return None


def get_display_timezone():
    """
    Get the display timezone from AppConfig, settings, or default to UTC.
    Results are cached for 1 hour for performance.
    
    Returns:
        str: Timezone name (e.g., 'Asia/Kolkata', 'UTC')
    """
    from django.core.cache import cache
    
    # Check cache first (gracefully handle cache errors)
    try:
        cached_tz = cache.get('app_display_timezone')
        if cached_tz is not None:
            return cached_tz
    except Exception:
        # If cache fails (e.g., Redis not available), continue without cache
        pass
    
    # Try AppConfig model first
    try:
        from core.models import AppConfig
        config = AppConfig.get_config()
        display_tz = config.display_timezone
    except Exception as e:
        # Fallback to settings if AppConfig fails
        display_tz = getattr(settings, 'DISPLAY_TIME_ZONE', None)
        if not display_tz:
            # Last resort: use TIME_ZONE (should be UTC)
            display_tz = settings.TIME_ZONE
    
    # Validate timezone
    try:
        pytz.timezone(display_tz)
    except pytz.UnknownTimeZoneError:
        # Invalid timezone, fallback to UTC
        display_tz = 'UTC'
    
    # Cache for 1 hour (gracefully handle cache errors)
    try:
        cache.set('app_display_timezone', display_tz, 3600)
    except Exception:
        # If cache fails (e.g., Redis not available), continue without cache
        pass
    
    return display_tz


def convert_to_display_timezone(dt):
    """
    Convert a UTC datetime to the display timezone.
    
    Args:
        dt: timezone-aware datetime object (typically in UTC from database)
    
    Returns:
        timezone-aware datetime object in the display timezone
    """
    if dt is None:
        return None
    
    # Ensure timezone-aware (assume UTC if naive)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.UTC)
    
    # Get display timezone
    display_tz_name = get_display_timezone()
    
    # Convert to display timezone
    try:
        display_tz = pytz.timezone(display_tz_name)
        return dt.astimezone(display_tz)
    except (pytz.UnknownTimeZoneError, AttributeError):
        # Fallback: return as-is if conversion fails
        return dt


def format_datetime_for_display(dt, format_string="M d, Y H:i:s"):
    """
    Format a datetime for display in the user's preferred timezone.
    
    This function converts UTC datetimes to the display timezone without
    affecting core application functions (which always use UTC).
    
    Priority: AppConfig > DISPLAY_TIME_ZONE setting > TIME_ZONE setting
    
    Args:
        dt: timezone-aware datetime object (typically in UTC from database)
        format_string: Django date format string (default: "M d, Y H:i:s")
    
    Returns:
        Formatted string in the display timezone
    """
    if dt is None:
        return ""
    
    # Convert to display timezone
    dt_in_display_tz = convert_to_display_timezone(dt)
    if dt_in_display_tz is None:
        return ""
    
    # Format using strftime - convert Django format to Python format
    # Django: "M d, Y H:i:s" -> Python: "%b %d, %Y %H:%M:%S"
    format_map = [
        ('M', '%b'),   # Short month name (Jan, Feb, etc.)
        ('d', '%d'),   # Day of month (01-31)
        ('Y', '%Y'),   # Year (4 digits)
        ('H', '%H'),   # Hour (00-23)
        ('i', '%M'),   # Minute (00-59)
        ('s', '%S'),   # Second (00-59)
        ('g', '%I'),   # Hour (1-12) in 12-hour format
        ('A', '%p'),   # AM/PM
    ]
    
    python_format = format_string
    
    # Handle n (month without leading zeros), j (day without leading zeros), and g (hour without leading zeros)
    # These need special handling because Python's strftime doesn't have a direct equivalent
    has_n_or_j = 'n' in python_format or 'j' in python_format
    has_g = 'g' in python_format
    
    for django_fmt, python_fmt in format_map:
        python_format = python_format.replace(django_fmt, python_fmt)
    
    # Replace n and j after other replacements to avoid conflicts
    python_format = python_format.replace('n', '%m').replace('j', '%d')
    
    result = dt_in_display_tz.strftime(python_format)
    
    # Remove leading zeros from month and day if n or j was used
    if has_n_or_j:
        import re
        # Remove leading zero from month (01 -> 1, but 10 stays 10)
        result = re.sub(r'\b0(\d)/', r'\1/', result)
        # Remove leading zero from day (/01 -> /1, but /10 stays /10)
        result = re.sub(r'/(\d{2})\b', lambda m: '/' + str(int(m.group(1))), result)
        # Handle cases where month/day is at start of string
        result = re.sub(r'^0(\d)', r'\1', result)
    
    # Remove leading zero from hour if g was used (01 -> 1, but 10 stays 10)
    if has_g:
        import re
        # Match hour in format like "01:05:00" or " 01:05:00" and remove leading zero
        result = re.sub(r'(\s|,|/|^)0(\d):', r'\1\2:', result)
    
    return result


def collect_processes_on_demand(server, metric_type, timeout=5):
    """
    Attempt to collect top processes on-demand (fallback).
    Only use when process data missing from metric record.
    
    This function uses SSH to collect top processes from the server.
    Uses short timeout to avoid blocking during overload situations.
    
    Args:
        server: Server model instance
        metric_type: Type of metric ('cpu' or 'memory')
        timeout: SSH connection timeout in seconds (default: 5)
    
    Returns:
        dict with 'cpu' and/or 'memory' process lists, or None on failure
        Format: {'cpu': [{'pid': '12345', 'cpu_percent': 95.8, 'command': 'yes'}], 'memory': [...]}
    """
    try:
        import paramiko
        import os
        import logging
        
        logger = logging.getLogger(__name__)
        
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect using SSH key
        ssh_key_path = getattr(settings, "SSH_PRIVATE_KEY_PATH", "/app/ssh_keys/id_rsa")
        if not os.path.exists(ssh_key_path):
            logger.warning(f"SSH key not found at {ssh_key_path}")
            return None
            
        private_key = paramiko.RSAKey.from_private_key_file(ssh_key_path)
        
        ssh.connect(
            hostname=server.ip_address,
            port=server.port,
            username=server.username,
            pkey=private_key,
            timeout=timeout
        )
        
        top_processes = {"cpu": [], "memory": []}
        
        # Collect CPU processes if needed
        if metric_type == 'cpu':
            try:
                # Get top 3 CPU processes
                command = "ps aux --sort=-%cpu | head -4 | tail -3 | awk '{print $2\"|\"$3\"|\"$11\" \"$12\" \"$13\" \"$14\" \"$15\" \"$16\" \"$17\" \"$18\" \"$19\" \"$20}'"
                stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
                
                output = stdout.read().decode('utf-8').strip()
                errors = stderr.read().decode('utf-8').strip()
                
                if not errors:
                    processes = []
                    for line in output.split('\n'):
                        if line.strip():
                            parts = line.split('|')
                            if len(parts) >= 3:
                                try:
                                    pid = parts[0].strip()
                                    cpu_percent = float(parts[1].strip())
                                    command = '|'.join(parts[2:]).strip()[:100]  # Limit command length
                                    
                                    processes.append({
                                        'pid': pid,
                                        'cpu_percent': round(cpu_percent, 1),
                                        'command': command
                                    })
                                except (ValueError, IndexError):
                                    continue
                    
                    top_processes["cpu"] = processes[:3]
            except Exception as e:
                logger.warning(f"Failed to collect CPU processes: {e}")
        
        # Collect memory processes if needed
        if metric_type == 'memory':
            try:
                # Get top 3 memory processes
                command = "ps aux --sort=-%mem | head -4 | tail -3 | awk '{print $2\"|\"$4\"|\"$11\" \"$12\" \"$13\" \"$14\" \"$15\" \"$16\" \"$17\" \"$18\" \"$19\" \"$20}'"
                stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
                
                output = stdout.read().decode('utf-8').strip()
                errors = stderr.read().decode('utf-8').strip()
                
                if not errors:
                    processes = []
                    for line in output.split('\n'):
                        if line.strip():
                            parts = line.split('|')
                            if len(parts) >= 3:
                                try:
                                    pid = parts[0].strip()
                                    memory_percent = float(parts[1].strip())
                                    command = '|'.join(parts[2:]).strip()[:100]  # Limit command length
                                    
                                    processes.append({
                                        'pid': pid,
                                        'memory_percent': round(memory_percent, 1),
                                        'command': command
                                    })
                                except (ValueError, IndexError):
                                    continue
                    
                    top_processes["memory"] = processes[:3]
            except Exception as e:
                logger.warning(f"Failed to collect memory processes: {e}")
        
        ssh.close()
        
        # Return None if no processes collected
        if not top_processes.get("cpu") and not top_processes.get("memory"):
            return None
            
        return top_processes
        
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        try:
            import paramiko
            if isinstance(e, paramiko.AuthenticationException):
                logger.warning(f"SSH authentication failed for on-demand process collection: {e}")
            elif isinstance(e, paramiko.SSHException):
                logger.warning(f"SSH connection error during on-demand process collection: {e}")
            else:
                logger.warning(f"On-demand process collection failed: {e}")
        except:
            logger.warning(f"On-demand process collection failed: {e}")
        return None
