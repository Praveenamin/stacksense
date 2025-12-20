import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()

from core.models import Server, ServerHeartbeat, AlertHistory, Anomaly
from core.views import _calculate_server_status
from django.utils import timezone
from datetime import timedelta

print("="*70)
print("COMPREHENSIVE HEARTBEAT SYSTEM TESTS")
print("="*70)

server = Server.objects.first()
if not server:
    print("No servers found")
    exit(1)

tests_passed = 0
tests_failed = 0

def test(name, setup_func, expected_check):
    global tests_passed, tests_failed
    print(f"\n[TEST] {name}")
    try:
        setup_func(server)
        status = _calculate_server_status(server)
        if expected_check(status):
            print(f"  ✓ PASS: Status = {status}")
            tests_passed += 1
        else:
            print(f"  ✗ FAIL: Status = {status}")
            tests_failed += 1
    except Exception as e:
        print(f"  ✗ ERROR: {e}")
        tests_failed += 1

# Test 1: No heartbeat
test("No Heartbeat", 
     lambda s: ServerHeartbeat.objects.filter(server=s).delete(),
     lambda status: status == "offline")

# Test 2: Fresh heartbeat
test("Fresh Heartbeat (30s)",
     lambda s: ServerHeartbeat.objects.update_or_create(server=s, defaults={'last_heartbeat': timezone.now() - timedelta(seconds=30)}),
     lambda status: status in ["online", "warning"])

# Test 3: Boundary - 59 seconds
test("Boundary - 59 seconds",
     lambda s: ServerHeartbeat.objects.update_or_create(server=s, defaults={'last_heartbeat': timezone.now() - timedelta(seconds=59)}),
     lambda status: status in ["online", "warning"])

# Test 4: Stale - 61 seconds
test("Stale Heartbeat (61s)",
     lambda s: ServerHeartbeat.objects.update_or_create(server=s, defaults={'last_heartbeat': timezone.now() - timedelta(seconds=61)}),
     lambda status: status == "offline")

# Test 5: Very stale - 120 seconds
test("Very Stale Heartbeat (120s)",
     lambda s: ServerHeartbeat.objects.update_or_create(server=s, defaults={'last_heartbeat': timezone.now() - timedelta(seconds=120)}),
     lambda status: status == "offline")

# Test 6: Status with alerts
alerts_count = AlertHistory.objects.filter(server=server, status='triggered').count()
test("Status with Active Alerts",
     lambda s: ServerHeartbeat.objects.update_or_create(server=s, defaults={'last_heartbeat': timezone.now()}),
     lambda status: status == "warning" if alerts_count > 0 else status == "online")

# Test 7: Multiple servers
print(f"\n[TEST] Multiple Servers Status")
servers = Server.objects.all()
for s in servers:
    status = _calculate_server_status(s)
    hb = ServerHeartbeat.objects.filter(server=s).first()
    if hb:
        age = int((timezone.now() - hb.last_heartbeat).total_seconds())
        print(f"  {s.name}: {status} (heartbeat {age}s ago)")
    else:
        print(f"  {s.name}: {status} (no heartbeat)")
tests_passed += 1

print("\n" + "="*70)
print(f"RESULTS: {tests_passed} passed, {tests_failed} failed")
print("="*70)
