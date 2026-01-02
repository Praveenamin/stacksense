"""
Django management command to track when the monitoring application itself is running.

This command runs periodically (e.g., every 30 seconds) to record that the monitoring
app is alive. This allows the system to distinguish between:
- App downtime (app was down, servers might still be online)
- Server downtime (app was running, but server didn't respond)

Usage:
    python manage.py track_app_heartbeat
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.cache import cache
from django.conf import settings

# Try to import get_app_heartbeat_timestamp, fallback to direct implementation if import fails
try:
    from core.utils import get_app_heartbeat_timestamp
except ImportError:
    # Fallback implementation
    def get_app_heartbeat_timestamp():
        return timezone.now().isoformat()


class Command(BaseCommand):
    help = "Track monitoring application heartbeat to distinguish app downtime from server downtime"

    def handle(self, *args, **options):
        """Record that the monitoring app is currently running"""
        now = timezone.now()
        
        # Use utility function to ensure consistent timezone handling
        heartbeat_str = get_app_heartbeat_timestamp()
        
        # Store app heartbeat in cache (expires after 5 minutes)
        # This way, if app goes down, we know it was down
        app_heartbeat_key = "monitoring_app_heartbeat"
        cache.set(app_heartbeat_key, heartbeat_str, timeout=300)  # 5 minute expiry
        
        # Also store in a file for persistence across restarts
        try:
            heartbeat_file = getattr(settings, "APP_HEARTBEAT_FILE", "/tmp/monitoring_app_heartbeat.txt")
            with open(heartbeat_file, 'w') as f:
                f.write(heartbeat_str)
        except Exception as e:
            if options.get('verbosity', 1) >= 2:
                self.stdout.write(self.style.WARNING(f"Could not write heartbeat file: {e}"))
        
        if options.get('verbosity', 1) >= 2:
            self.stdout.write(f"App heartbeat recorded: {now}")

