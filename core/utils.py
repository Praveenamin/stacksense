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


def format_datetime_for_display(dt, format_string="M d, Y H:i:s"):
    """
    Format a datetime for display in the user's preferred timezone.
    
    This function converts UTC datetimes to the display timezone without
    affecting core application functions (which always use UTC).
    
    Args:
        dt: timezone-aware datetime object (typically in UTC from database)
        format_string: Django date format string (default: "M d, Y H:i:s")
    
    Returns:
        Formatted string in the display timezone
    """
    if dt is None:
        return ""
    
    # Get display timezone from settings (defaults to TIME_ZONE if not set)
    display_tz_name = getattr(settings, 'DISPLAY_TIME_ZONE', settings.TIME_ZONE)
    
    # Convert to display timezone
    if dt.tzinfo is None:
        # If naive, assume UTC
        dt = dt.replace(tzinfo=pytz.UTC)
    
    # Convert to display timezone
    try:
        display_tz = pytz.timezone(display_tz_name)
        dt_in_display_tz = dt.astimezone(display_tz)
    except (pytz.UnknownTimeZoneError, AttributeError):
        # Fallback to UTC if timezone invalid
        dt_in_display_tz = dt
    
    # Format using strftime - convert Django format to Python format
    # Django: "M d, Y H:i:s" -> Python: "%b %d, %Y %H:%M:%S"
    format_map = {
        'M': '%b',   # Short month name (Jan, Feb, etc.)
        'd': '%d',   # Day of month (01-31)
        'Y': '%Y',   # Year (4 digits)
        'H': '%H',   # Hour (00-23)
        'i': '%M',   # Minute (00-59)
        's': '%S',   # Second (00-59)
    }
    
    python_format = format_string
    for django_fmt, python_fmt in format_map.items():
        python_format = python_format.replace(django_fmt, python_fmt)
    
    return dt_in_display_tz.strftime(python_format)


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
