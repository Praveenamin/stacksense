import time
import signal
import sys
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "log_analyzer.settings")
django.setup()

from django.core.management import call_command
from django.utils import timezone

running = True

def signal_handler(signum, frame):
    global running
    print("\nReceived shutdown signal. Stopping scheduler...")
    running = False

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

print("Starting metrics collection scheduler (every 30 seconds)...")
interval = 30  # 30 seconds

while running:
    try:
        print(f"\n[{timezone.now().strftime("%Y-%m-%d %H:%M:%S")}] Running metrics collection...")
        call_command("collect_metrics", verbosity=1)
        print(f"Metrics collection completed. Next run in {interval} seconds.")
    except Exception as e:
        print(f"Error: {str(e)}")
    
    for _ in range(interval):
        if not running:
            break
        time.sleep(1)

print("\nScheduler stopped.")
