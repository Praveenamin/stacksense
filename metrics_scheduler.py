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
anomaly_detection_interval = 300  # Run anomaly detection every 5 minutes

last_anomaly_check = timezone.now()

while running:
    try:
        # Track that monitoring app is running
        call_command("track_app_heartbeat", verbosity=0)
        
        print(f"\n[{timezone.now().strftime("%Y-%m-%d %H:%M:%S")}] Running metrics collection...")
        call_command("collect_metrics", verbosity=1)
        print(f"Metrics collection completed. Next run in {interval} seconds.")
        
        # Run anomaly detection every 5 minutes
        time_since_last_anomaly_check = (timezone.now() - last_anomaly_check).total_seconds()
        if time_since_last_anomaly_check >= anomaly_detection_interval:
            try:
                print(f"[{timezone.now().strftime("%Y-%m-%d %H:%M:%S")}] Running anomaly detection...")
                call_command("detect_anomalies", verbosity=1)
                last_anomaly_check = timezone.now()
                print("Anomaly detection completed.")
            except Exception as e:
                print(f"Error in anomaly detection: {str(e)}")
    except Exception as e:
        print(f"Error: {str(e)}")
    
    for _ in range(interval):
        if not running:
            break
        time.sleep(1)

print("\nScheduler stopped.")
