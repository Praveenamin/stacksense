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
log_scan_interval = 300  # Run log scanning every 5 minutes
latency_collection_interval = 60  # Run latency collection every minute
synthetic_check_interval = 30  # Run due synthetic (uptime) checks every 30s
security_detection_interval = 60  # Run security detection every minute
connectivity_check_interval = 60  # Check for down/recovered servers every minute
leak_detection_interval = 3600  # Run memory-leak detection hourly (leaks evolve slowly)
aggregate_interval = 86400  # Roll raw metrics into hourly/daily summaries once a day
prune_interval = 86400  # Enforce the data-retention window once a day (runs after aggregation)

last_anomaly_check = timezone.now()
last_log_scan = timezone.now()
last_latency_collection = timezone.now()
last_synthetic_check = timezone.now()
last_security_detection = timezone.now()
last_connectivity_check = timezone.now()
last_leak_check = timezone.now()
last_aggregate = timezone.now()
last_prune = timezone.now()

while running:
    try:
        # Track that monitoring app is running (non-critical, continue even if it fails)
        try:
            call_command("track_app_heartbeat", verbosity=0)
        except Exception as heartbeat_error:
            # Log but don't stop metrics collection if heartbeat tracking fails
            print(f"Warning: Heartbeat tracking failed (non-critical): {heartbeat_error}")
        
        # Metrics arrive via the push agent (no server-side SSH pull).
        # Run anomaly detection every 5 minutes
        time_since_last_anomaly_check = (timezone.now() - last_anomaly_check).total_seconds()
        if time_since_last_anomaly_check >= anomaly_detection_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Running anomaly detection...")
                call_command("detect_anomalies", verbosity=1)
                last_anomaly_check = timezone.now()
                print("Anomaly detection completed.")
            except Exception as e:
                print(f"Error in anomaly detection: {str(e)}")
        
        # Run log scanning every 5 minutes
        time_since_last_log_scan = (timezone.now() - last_log_scan).total_seconds()
        if time_since_last_log_scan >= log_scan_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Running log scanning...")
                call_command("scan_logs", verbosity=1)
                last_log_scan = timezone.now()
                print("Log scanning completed.")
            except Exception as e:
                print(f"Error in log scanning: {str(e)}")
        
        # Run service latency collection every minute
        time_since_last_latency = (timezone.now() - last_latency_collection).total_seconds()
        if time_since_last_latency >= latency_collection_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Running service latency collection...")
                call_command("collect_service_latency", verbosity=0)
                last_latency_collection = timezone.now()
                print("Service latency collection completed.")
            except Exception as e:
                print(f"Error in service latency collection: {str(e)}")

        # Run due synthetic (uptime) checks every 30 seconds
        time_since_last_synthetic = (timezone.now() - last_synthetic_check).total_seconds()
        if time_since_last_synthetic >= synthetic_check_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Running synthetic checks...")
                call_command("run_synthetic_checks", verbosity=0)
                last_synthetic_check = timezone.now()
                print("Synthetic checks completed.")
            except Exception as e:
                print(f"Error in synthetic checks: {str(e)}")

        # Run security detection every minute
        time_since_last_security = (timezone.now() - last_security_detection).total_seconds()
        if time_since_last_security >= security_detection_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Running security detection...")
                call_command("detect_security_events", verbosity=0)
                last_security_detection = timezone.now()
                print("Security detection completed.")
            except Exception as e:
                print(f"Error in security detection: {str(e)}")

        # Check server connectivity (down/recovered) every minute
        time_since_last_connectivity = (timezone.now() - last_connectivity_check).total_seconds()
        if time_since_last_connectivity >= connectivity_check_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking server connectivity...")
                call_command("check_server_connectivity", verbosity=0)
                last_connectivity_check = timezone.now()
                print("Connectivity check completed.")
            except Exception as e:
                print(f"Error in connectivity check: {str(e)}")

        # Run memory-leak detection hourly (system RAM / per-process RSS / SysV IPC)
        time_since_last_leak = (timezone.now() - last_leak_check).total_seconds()
        if time_since_last_leak >= leak_detection_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Running memory-leak detection...")
                call_command("detect_memory_leaks", verbosity=1)
                last_leak_check = timezone.now()
                print("Memory-leak detection completed.")
            except Exception as e:
                print(f"Error in memory-leak detection: {str(e)}")

        # Roll raw metrics into hourly/daily summaries once a day (BEFORE the prune,
        # so daily roll-ups exist before the raw they summarize is deleted).
        time_since_last_aggregate = (timezone.now() - last_aggregate).total_seconds()
        if time_since_last_aggregate >= aggregate_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Running metric aggregation...")
                call_command("aggregate_metrics", verbosity=1)
                last_aggregate = timezone.now()
                print("Metric aggregation completed.")
            except Exception as e:
                print(f"Error in metric aggregation: {str(e)}")

        # Enforce the data-retention window once a day (sliding-window prune)
        time_since_last_prune = (timezone.now() - last_prune).total_seconds()
        if time_since_last_prune >= prune_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Running data-retention prune...")
                call_command("prune_old_data", verbosity=1)
                last_prune = timezone.now()
                print("Data-retention prune completed.")
            except Exception as e:
                print(f"Error in data-retention prune: {str(e)}")
    except Exception as e:
        print(f"Error: {str(e)}")
    
    for _ in range(interval):
        if not running:
            break
        time.sleep(1)

print("\nScheduler stopped.")
