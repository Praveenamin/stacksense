#!/usr/bin/env python3
"""
Comprehensive test suite for SSH-based heartbeat system
"""

import os
import sys
import django
from django.utils import timezone
from datetime import timedelta

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()

from core.models import Server, ServerHeartbeat, AlertHistory, Anomaly
from core.views import _calculate_server_status
from django.core.management import call_command
from io import StringIO

class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.tests = []
    
    def test(self, name, func):
        self.tests.append((name, func))
    
    def run_all(self):
        print("\n" + "="*70)
        print("SSH Heartbeat System - Comprehensive Test Suite")
        print("="*70 + "\n")
        
        for name, func in self.tests:
            print(f"\n[TEST] {name}")
            print("-" * 70)
            try:
                result = func()
                if result:
                    print(f"✓ PASS: {name}")
                    self.passed += 1
                else:
                    print(f"✗ FAIL: {name}")
                    self.failed += 1
            except Exception as e:
                print(f"✗ ERROR: {name} - {e}")
                self.failed += 1
        
        print("\n" + "="*70)
        print(f"Test Results: {self.passed} passed, {self.failed} failed")
        print("="*70 + "\n")
        return self.failed == 0

runner = TestRunner()

# Test 1: Command Import
def test_command_import():
    try:
        from core.management.commands.check_heartbeats_ssh import Command
        cmd = Command()
        print("  Command class imported successfully")
        return True
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Command Import", test_command_import)

# Test 2: Database Access
def test_database_access():
    try:
        servers = Server.objects.all()
        heartbeats = ServerHeartbeat.objects.all()
        print(f"  Servers: {servers.count()}")
        print(f"  Heartbeats: {heartbeats.count()}")
        return True
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Database Access", test_database_access)

# Test 3: Status Calculation - No Heartbeat
def test_status_no_heartbeat():
    try:
        server = Server.objects.first()
        if not server:
            print("  No servers found")
            return False
        
        ServerHeartbeat.objects.filter(server=server).delete()
        status = _calculate_server_status(server)
        print(f"  Server: {server.name}")
        print(f"  Status (no heartbeat): {status}")
        result = status == "offline"
        if result:
            print("  ✓ Correctly shows offline")
        else:
            print(f"  ✗ Expected 'offline', got '{status}'")
        return result
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Status: No Heartbeat → Offline", test_status_no_heartbeat)

# Test 4: Status Calculation - Fresh Heartbeat
def test_status_fresh_heartbeat():
    try:
        server = Server.objects.first()
        if not server:
            return False
        
        ServerHeartbeat.objects.update_or_create(
            server=server,
            defaults={'last_heartbeat': timezone.now()}
        )
        status = _calculate_server_status(server)
        print(f"  Server: {server.name}")
        print(f"  Status (fresh heartbeat): {status}")
        result = status in ["online", "warning"]
        if result:
            print(f"  ✓ Status is '{status}' (online or warning based on alerts)")
        else:
            print(f"  ✗ Expected 'online' or 'warning', got '{status}'")
        return result
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Status: Fresh Heartbeat → Online/Warning", test_status_fresh_heartbeat)

# Test 5: Status Calculation - Stale Heartbeat
def test_status_stale_heartbeat():
    try:
        server = Server.objects.first()
        if not server:
            return False
        
        old_time = timezone.now() - timedelta(seconds=70)
        ServerHeartbeat.objects.update_or_create(
            server=server,
            defaults={'last_heartbeat': old_time}
        )
        status = _calculate_server_status(server)
        print(f"  Server: {server.name}")
        print(f"  Heartbeat age: 70 seconds")
        print(f"  Status: {status}")
        result = status == "offline"
        if result:
            print("  ✓ Correctly shows offline for stale heartbeat")
        else:
            print(f"  ✗ Expected 'offline', got '{status}'")
        return result
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Status: Stale Heartbeat (>60s) → Offline", test_status_stale_heartbeat)

# Test 6: Status Calculation - Boundary Test (60 seconds)
def test_status_boundary():
    try:
        server = Server.objects.first()
        if not server:
            return False
        
        # Test exactly 60 seconds (should be online)
        boundary_time = timezone.now() - timedelta(seconds=60)
        ServerHeartbeat.objects.update_or_create(
            server=server,
            defaults={'last_heartbeat': boundary_time}
        )
        status = _calculate_server_status(server)
        print(f"  Heartbeat age: 60 seconds (boundary)")
        print(f"  Status: {status}")
        result = status in ["online", "warning"]
        if result:
            print("  ✓ Correctly shows online/warning at boundary")
        else:
            print(f"  ✗ Expected 'online' or 'warning', got '{status}'")
        return result
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Status: Boundary Test (60s)", test_status_boundary)

# Test 7: Status with Active Alerts
def test_status_with_alerts():
    try:
        server = Server.objects.first()
        if not server:
            return False
        
        # Create fresh heartbeat
        ServerHeartbeat.objects.update_or_create(
            server=server,
            defaults={'last_heartbeat': timezone.now()}
        )
        
        # Check alerts
        alerts = AlertHistory.objects.filter(server=server, status='triggered').count()
        anomalies = Anomaly.objects.filter(server=server, resolved=False).count()
        status = _calculate_server_status(server)
        
        print(f"  Server: {server.name}")
        print(f"  Active alerts: {alerts}")
        print(f"  Active anomalies: {anomalies}")
        print(f"  Status: {status}")
        
        if alerts > 0 or anomalies > 0:
            expected = "warning"
            result = status == "warning"
            if result:
                print("  ✓ Correctly shows warning with active alerts")
            else:
                print(f"  ✗ Expected 'warning', got '{status}'")
        else:
            expected = "online"
            result = status == "online"
            if result:
                print("  ✓ Correctly shows online with no alerts")
            else:
                print(f"  ✗ Expected 'online', got '{status}'")
        
        return result
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Status: With Active Alerts → Warning", test_status_with_alerts)

# Test 8: Command Execution
def test_command_execution():
    try:
        out = StringIO()
        call_command('check_heartbeats_ssh', '--timeout', '5', stdout=out, stderr=out)
        output = out.getvalue()
        
        has_summary = "Summary:" in output or "Successful:" in output
        print(f"  Command executed")
        print(f"  Output length: {len(output)} chars")
        print(f"  Has summary: {has_summary}")
        return has_summary
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Command Execution", test_command_execution)

# Test 9: Heartbeat Record Operations
def test_heartbeat_operations():
    try:
        server = Server.objects.first()
        if not server:
            return False
        
        # Create
        hb1, created1 = ServerHeartbeat.objects.update_or_create(
            server=server,
            defaults={'last_heartbeat': timezone.now()}
        )
        print(f"  Create: {'Created' if created1 else 'Updated'}")
        
        # Update
        new_time = timezone.now()
        hb1.last_heartbeat = new_time
        hb1.save()
        hb2 = ServerHeartbeat.objects.get(server=server)
        print(f"  Update: {hb2.last_heartbeat == new_time}")
        
        return True
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Heartbeat Record Operations", test_heartbeat_operations)

# Test 10: Multiple Servers
def test_multiple_servers():
    try:
        servers = Server.objects.all()
        print(f"  Testing {servers.count()} servers")
        
        for server in servers:
            status = _calculate_server_status(server)
            hb = ServerHeartbeat.objects.filter(server=server).first()
            if hb:
                age = (timezone.now() - hb.last_heartbeat).total_seconds()
                print(f"    {server.name}: {status} (heartbeat {int(age)}s ago)")
            else:
                print(f"    {server.name}: {status} (no heartbeat)")
        
        return True
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Multiple Servers Status", test_multiple_servers)

# Test 11: Timeout Configuration
def test_timeout_config():
    try:
        from core.management.commands.check_heartbeats_ssh import Command
        cmd = Command()
        parser = cmd.create_parser('manage.py', 'check_heartbeats_ssh')
        
        # Default
        args = parser.parse_args([])
        default_ok = args.timeout == 5
        print(f"  Default timeout: {args.timeout}s {'✓' if default_ok else '✗'}")
        
        # Custom
        args = parser.parse_args(['--timeout', '10'])
        custom_ok = args.timeout == 10
        print(f"  Custom timeout: {args.timeout}s {'✓' if custom_ok else '✗'}")
        
        return default_ok and custom_ok
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Timeout Configuration", test_timeout_config)

# Test 12: Age Calculation Accuracy
def test_age_calculation():
    try:
        server = Server.objects.first()
        if not server:
            return False
        
        test_ages = [30, 60, 61, 90, 120]
        print("  Testing various heartbeat ages:")
        
        all_correct = True
        for age in test_ages:
            hb_time = timezone.now() - timedelta(seconds=age)
            ServerHeartbeat.objects.update_or_create(
                server=server,
                defaults={'last_heartbeat': hb_time}
            )
            status = _calculate_server_status(server)
            expected = "offline" if age > 60 else ("online" or "warning")
            correct = (age > 60 and status == "offline") or (age <= 60 and status in ["online", "warning"])
            print(f"    {age}s: {status} {'✓' if correct else '✗'}")
            if not correct:
                all_correct = False
        
        return all_correct
    except Exception as e:
        print(f"  Error: {e}")
        return False
runner.test("Age Calculation Accuracy", test_age_calculation)

# Run all tests
if __name__ == "__main__":
    success = runner.run_all()
    sys.exit(0 if success else 1)

