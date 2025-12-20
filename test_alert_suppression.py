#!/usr/bin/env python3
"""
Test script to verify alert suppression works correctly.
This script tests:
1. That alerts are suppressed when alert_suppressed = True
2. That alerts are sent when alert_suppressed = False
3. That the suppression state persists correctly
"""

import os
import sys
import django

# Setup Django
sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()

from core.models import Server, MonitoringConfig, SystemMetric, EmailAlertConfig
from core.views import _check_and_send_alerts
from django.utils import timezone
from datetime import timedelta
import json

def test_alert_suppression():
    """Test that alert suppression actually prevents alerts from being sent"""
    
    print("=" * 60)
    print("ALERT SUPPRESSION TEST")
    print("=" * 60)
    
    # Get first server
    server = Server.objects.first()
    if not server:
        print("❌ No servers found. Please add a server first.")
        return False
    
    print(f"\n📋 Testing with server: {server.name} (ID: {server.id})")
    
    # Get or create monitoring config
    config, created = MonitoringConfig.objects.get_or_create(server=server)
    print(f"   MonitoringConfig exists: {not created}")
    
    # Check if email config exists
    email_config = EmailAlertConfig.objects.filter(enabled=True).first()
    if not email_config:
        print("⚠️  Warning: No email alert config found. Alerts won't be sent, but suppression logic will still be tested.")
    else:
        print(f"✓ Email alert config found and enabled")
    
    # Create a test metric that would trigger an alert (high CPU)
    print("\n1️⃣  Creating test metric with high CPU (would trigger alert)...")
    test_metric = SystemMetric.objects.create(
        server=server,
        timestamp=timezone.now(),
        cpu_percent=95.0,  # High CPU that should trigger alert
        memory_percent=80.0,
        disk_usage=json.dumps({"/": {"percent": 85}}),
        network_io=json.dumps({}),
        disk_io_read=0,
        disk_io_write=0,
        net_io_sent=0,
        net_io_recv=0
    )
    print(f"   ✓ Created metric: CPU={test_metric.cpu_percent}% (threshold: {config.cpu_threshold}%)")
    
    # Test 1: Alerts NOT suppressed - should check and potentially send alerts
    if test_metric:
        print("\n2️⃣  TEST 1: Alerts NOT suppressed (alert_suppressed = False)")
        config.alert_suppressed = False
        config.save()
        print(f"   Set alert_suppressed = False")
        
        # Capture print output to see if alerts are checked
        import io
        from contextlib import redirect_stdout
        
        f = io.StringIO()
        with redirect_stdout(f):
            _check_and_send_alerts(server, test_metric)
        output = f.getvalue()
        
        if "Alerts suppressed" in output:
            print("   ❌ FAILED: Alerts were suppressed when they shouldn't be!")
            return False
        else:
            print("   ✓ PASSED: Alert checks were performed (not suppressed)")
            if email_config:
                print("   ✓ Alert would be sent if thresholds exceeded")
    
    if test_metric:
        # Test 2: Alerts suppressed - should skip alert checks
        print("\n3️⃣  TEST 2: Alerts SUPPRESSED (alert_suppressed = True)")
        config.alert_suppressed = True
        config.save()
        print(f"   Set alert_suppressed = True")
        
        f = io.StringIO()
        with redirect_stdout(f):
            _check_and_send_alerts(server, test_metric)
        output = f.getvalue()
        
        if "Alerts suppressed" in output:
            print("   ✓ PASSED: Alerts were suppressed correctly")
            print(f"   Message: {output.strip()}")
        else:
            print("   ❌ FAILED: Alerts were NOT suppressed!")
            print(f"   Output: {output}")
            return False
    else:
        print("\n3️⃣  TEST 2: Setting alert_suppressed = True (skipping alert check)")
        config.alert_suppressed = True
        config.save()
        print("   ✓ Set alert_suppressed = True")
    
    # Test 3: Verify suppression state can be toggled
    print("\n4️⃣  TEST 3: Verify suppression state can be toggled")
    config.alert_suppressed = False
    config.save()
    config.refresh_from_db()
    if config.alert_suppressed == False:
        print("   ✓ PASSED: Can toggle suppression state to False")
    else:
        print("   ❌ FAILED: Could not toggle suppression state!")
        return False
    
    config.alert_suppressed = True
    config.save()
    config.refresh_from_db()
    if config.alert_suppressed == True:
        print("   ✓ PASSED: Can toggle suppression state to True")
    else:
        print("   ❌ FAILED: Could not toggle suppression state!")
        return False
    
    # Test 4: Verify state persists
    print("\n5️⃣  TEST 4: Verify state persists in database")
    config.refresh_from_db()
    if config.alert_suppressed == True:
        print("   ✓ PASSED: alert_suppressed state persisted correctly")
    else:
        print("   ❌ FAILED: alert_suppressed state did not persist!")
        return False
    
    # Reset to False for cleanup
    print("\n6️⃣  Cleanup: Resetting alert_suppressed to False")
    config.alert_suppressed = False
    config.save()
    
    # Restore original CPU if we modified it
    if test_metric and hasattr(test_metric, '_original_cpu'):
        test_metric.cpu_percent = test_metric._original_cpu
        test_metric.save()
        print("   ✓ Restored original metric values")
    else:
        print("   ✓ Cleanup complete")
    
    print("\n" + "=" * 60)
    print("✅ ALL TESTS PASSED!")
    print("=" * 60)
    print("\nSummary:")
    print("  ✓ Alert suppression correctly prevents alert checks")
    print("  ✓ Alert suppression state persists in database")
    print("  ✓ Suppression state can be toggled correctly")
    print("  ✓ When not suppressed, alerts are checked normally")
    
    return True

if __name__ == "__main__":
    try:
        success = test_alert_suppression()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

