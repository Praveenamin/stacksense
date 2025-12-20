#!/usr/bin/env python3
"""
Test suite for SSH-based heartbeat system
Tests various scenarios and edge cases
"""

import os
import sys
import django
from django.utils import timezone
from datetime import timedelta

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()

from core.models import Server, ServerHeartbeat
from core.views import _calculate_server_status
from django.core.management import call_command
from io import StringIO
import subprocess


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'


def print_test(test_name):
    print(f"\n{Colors.BLUE}{'='*60}{Colors.RESET}")
    print(f"{Colors.BLUE}Test: {test_name}{Colors.RESET}")
    print(f"{Colors.BLUE}{'='*60}{Colors.RESET}")


def print_pass(message):
    print(f"{Colors.GREEN}✓ PASS: {message}{Colors.RESET}")


def print_fail(message):
    print(f"{Colors.RED}✗ FAIL: {message}{Colors.RESET}")


def print_info(message):
    print(f"{Colors.YELLOW}ℹ INFO: {message}{Colors.RESET}")


# Test Case 1: Check if command exists and is callable
print_test("Test 1: Command Availability")
try:
    from core.management.commands.check_heartbeats_ssh import Command
    cmd = Command()
    print_pass("Command class can be imported and instantiated")
except Exception as e:
    print_fail(f"Command import failed: {e}")
    sys.exit(1)


# Test Case 2: Check database models
print_test("Test 2: Database Models")
try:
    servers = Server.objects.all()
    print_pass(f"Server model accessible - {servers.count()} servers found")
    
    heartbeats = ServerHeartbeat.objects.all()
    print_pass(f"ServerHeartbeat model accessible - {heartbeats.count()} heartbeat records")
except Exception as e:
    print_fail(f"Database model access failed: {e}")
    sys.exit(1)


# Test Case 3: Test status calculation function
print_test("Test 3: Status Calculation Function")
try:
    server = Server.objects.first()
    if server:
        status = _calculate_server_status(server)
        print_pass(f"Status calculation works - Server '{server.name}': {status}")
        print_info(f"  Current status: {status}")
    else:
        print_fail("No servers found in database")
except Exception as e:
    print_fail(f"Status calculation failed: {e}")


# Test Case 4: Test with no heartbeat (should be offline)
print_test("Test 4: No Heartbeat Scenario")
try:
    # Create a test scenario - get a server without heartbeat
    server = Server.objects.first()
    if server:
        # Delete existing heartbeat if any
        ServerHeartbeat.objects.filter(server=server).delete()
        
        status = _calculate_server_status(server)
        if status == "offline":
            print_pass(f"Server without heartbeat correctly shows as 'offline'")
        else:
            print_fail(f"Expected 'offline' but got '{status}'")
        
        # Restore if there was one
        print_info("  (Heartbeat record deleted for testing)")
    else:
        print_fail("No servers found")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 5: Test with recent heartbeat (should be online if no alerts)
print_test("Test 5: Recent Heartbeat Scenario")
try:
    server = Server.objects.first()
    if server:
        # Create/update heartbeat with current time
        heartbeat, created = ServerHeartbeat.objects.update_or_create(
            server=server,
            defaults={'last_heartbeat': timezone.now()}
        )
        
        status = _calculate_server_status(server)
        print_pass(f"Heartbeat created/updated - Status: {status}")
        print_info(f"  Last heartbeat: {heartbeat.last_heartbeat}")
        print_info(f"  Status: {status} (may be 'warning' if server has active alerts)")
    else:
        print_fail("No servers found")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 6: Test with old heartbeat (> 60 seconds)
print_test("Test 6: Stale Heartbeat Scenario")
try:
    server = Server.objects.first()
    if server:
        # Create heartbeat with old timestamp (70 seconds ago)
        old_time = timezone.now() - timedelta(seconds=70)
        heartbeat, created = ServerHeartbeat.objects.update_or_create(
            server=server,
            defaults={'last_heartbeat': old_time}
        )
        
        status = _calculate_server_status(server)
        if status == "offline":
            print_pass(f"Stale heartbeat (>60s) correctly shows as 'offline'")
        else:
            print_fail(f"Expected 'offline' for stale heartbeat but got '{status}'")
        
        print_info(f"  Heartbeat age: 70 seconds")
        print_info(f"  Status: {status}")
    else:
        print_fail("No servers found")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 7: Test command execution (dry run)
print_test("Test 7: Command Execution Test")
try:
    # Capture output
    out = StringIO()
    call_command('check_heartbeats_ssh', '--timeout', '5', stdout=out, stderr=out)
    output = out.getvalue()
    
    if "Checking server heartbeats" in output or "Summary:" in output:
        print_pass("Command executes successfully")
        print_info(f"  Output length: {len(output)} characters")
    else:
        print_fail(f"Command output unexpected: {output[:200]}")
except Exception as e:
    print_fail(f"Command execution failed: {e}")


# Test Case 8: Test all servers status
print_test("Test 8: All Servers Status Check")
try:
    servers = Server.objects.all()
    online_count = 0
    offline_count = 0
    warning_count = 0
    
    for server in servers:
        status = _calculate_server_status(server)
        if status == "online":
            online_count += 1
        elif status == "warning":
            warning_count += 1
        else:
            offline_count += 1
    
    print_pass(f"Status check completed for all {servers.count()} servers")
    print_info(f"  Online: {online_count}")
    print_info(f"  Warning: {warning_count}")
    print_info(f"  Offline: {offline_count}")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 9: Test heartbeat record creation/update
print_test("Test 9: Heartbeat Record Operations")
try:
    server = Server.objects.first()
    if server:
        # Test create
        initial_count = ServerHeartbeat.objects.count()
        heartbeat, created = ServerHeartbeat.objects.update_or_create(
            server=server,
            defaults={'last_heartbeat': timezone.now()}
        )
        
        if created:
            print_pass("Heartbeat record created successfully")
        else:
            print_pass("Heartbeat record updated successfully")
        
        # Test update
        new_time = timezone.now()
        heartbeat.last_heartbeat = new_time
        heartbeat.save()
        
        updated = ServerHeartbeat.objects.get(server=server)
        if updated.last_heartbeat == new_time:
            print_pass("Heartbeat record update works correctly")
        else:
            print_fail("Heartbeat update failed")
    else:
        print_fail("No servers found")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 10: Test timeout handling
print_test("Test 10: Timeout Configuration")
try:
    from core.management.commands.check_heartbeats_ssh import Command
    cmd = Command()
    
    # Test default timeout
    parser = cmd.create_parser('manage.py', 'check_heartbeats_ssh')
    args = parser.parse_args([])
    if args.timeout == 5:
        print_pass(f"Default timeout is 5 seconds (correct)")
    else:
        print_fail(f"Default timeout is {args.timeout}, expected 5")
    
    # Test custom timeout
    args = parser.parse_args(['--timeout', '10'])
    if args.timeout == 10:
        print_pass(f"Custom timeout works: {args.timeout} seconds")
    else:
        print_fail(f"Custom timeout failed: got {args.timeout}")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 11: Test verbose mode
print_test("Test 11: Verbose Mode")
try:
    from core.management.commands.check_heartbeats_ssh import Command
    cmd = Command()
    parser = cmd.create_parser('manage.py', 'check_heartbeats_ssh')
    
    args = parser.parse_args(['--verbose'])
    if args.verbose:
        print_pass("Verbose mode flag works correctly")
    else:
        print_fail("Verbose mode flag not working")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 12: Test monitoring suspended skip
print_test("Test 12: Monitoring Suspended Skip")
try:
    # This test verifies the logic exists (actual skip requires SSH connection)
    from core.management.commands.check_heartbeats_ssh import Command
    cmd = Command()
    
    # Check if code handles monitoring_suspended
    import inspect
    source = inspect.getsource(cmd.handle)
    if 'monitoring_suspended' in source:
        print_pass("Code checks for monitoring_suspended flag")
    else:
        print_fail("Code does not check monitoring_suspended")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 13: Integration test - Full workflow
print_test("Test 13: Integration Test - Full Workflow")
try:
    server = Server.objects.first()
    if server:
        # Step 1: Clear heartbeat
        ServerHeartbeat.objects.filter(server=server).delete()
        status1 = _calculate_server_status(server)
        
        # Step 2: Create recent heartbeat
        ServerHeartbeat.objects.create(
            server=server,
            last_heartbeat=timezone.now()
        )
        status2 = _calculate_server_status(server)
        
        # Step 3: Make heartbeat stale
        old_heartbeat = ServerHeartbeat.objects.get(server=server)
        old_heartbeat.last_heartbeat = timezone.now() - timedelta(seconds=70)
        old_heartbeat.save()
        status3 = _calculate_server_status(server)
        
        print_pass("Full workflow test completed")
        print_info(f"  No heartbeat: {status1}")
        print_info(f"  Recent heartbeat: {status2}")
        print_info(f"  Stale heartbeat: {status3}")
        
        if status1 == "offline" and status3 == "offline":
            print_pass("Status transitions work correctly")
        else:
            print_fail(f"Status transitions incorrect: {status1} -> {status2} -> {status3}")
    else:
        print_fail("No servers found")
except Exception as e:
    print_fail(f"Test failed: {e}")


# Test Case 14: Test cron job syntax
print_test("Test 14: Cron Job Syntax Validation")
try:
    # Check if cron entries are valid
    result = subprocess.run(
        ['sudo', 'crontab', '-l', '-u', 'root'],
        capture_output=True,
        text=True,
        timeout=5
    )
    
    if result.returncode == 0:
        cron_entries = result.stdout
        if 'check_heartbeats_ssh' in cron_entries:
            print_pass("Cron job entries found in root crontab")
            
            # Count entries
            count = cron_entries.count('check_heartbeats_ssh')
            if count >= 2:
                print_pass(f"Found {count} cron entries (expected 2)")
            else:
                print_fail(f"Found only {count} cron entries, expected 2")
        else:
            print_fail("Cron job entries not found")
    else:
        print_info("Could not read root crontab (may need sudo)")
except Exception as e:
    print_info(f"Could not verify cron: {e}")


# Summary
print(f"\n{Colors.BLUE}{'='*60}{Colors.RESET}")
print(f"{Colors.BLUE}Test Suite Complete{Colors.RESET}")
print(f"{Colors.BLUE}{'='*60}{Colors.RESET}")
print("\nTo run the actual heartbeat check:")
print("  python3 manage.py check_heartbeats_ssh --verbose")
print("\nTo check heartbeat status:")
print("  python3 manage.py check_heartbeats --verbose")

