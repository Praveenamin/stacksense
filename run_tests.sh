#!/bin/bash
# Run comprehensive heartbeat system tests

set -e

cd /home/ubuntu/stacksense-repo

echo "=========================================="
echo "Heartbeat System Test Suite"
echo "=========================================="
echo ""

export POSTGRES_HOST=localhost
export POSTGRES_PORT=5433

# Test 1: Command availability
echo "[TEST 1] Command Availability"
python3 manage.py check_heartbeats_ssh --help > /dev/null 2>&1 && echo "✓ Command available" || echo "✗ Command not found"
echo ""

# Test 2: Status calculation - no heartbeat
echo "[TEST 2] Status: No Heartbeat"
python3 -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()
from core.models import Server, ServerHeartbeat
from core.views import _calculate_server_status
server = Server.objects.first()
if server:
    ServerHeartbeat.objects.filter(server=server).delete()
    status = _calculate_server_status(server)
    print(f'  Status: {status} (expected: offline)')
    [print('✓ PASS') if status == 'offline' else print('✗ FAIL')]
" 2>&1
echo ""

# Test 3: Status calculation - fresh heartbeat
echo "[TEST 3] Status: Fresh Heartbeat"
python3 -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()
from core.models import Server, ServerHeartbeat
from core.views import _calculate_server_status
from django.utils import timezone
server = Server.objects.first()
if server:
    ServerHeartbeat.objects.update_or_create(server=server, defaults={'last_heartbeat': timezone.now()})
    status = _calculate_server_status(server)
    print(f'  Status: {status} (expected: online or warning)')
    [print('✓ PASS') if status in ['online', 'warning'] else print('✗ FAIL')]
" 2>&1
echo ""

# Test 4: Status calculation - stale heartbeat
echo "[TEST 4] Status: Stale Heartbeat"
python3 -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()
from core.models import Server, ServerHeartbeat
from core.views import _calculate_server_status
from django.utils import timezone
from datetime import timedelta
server = Server.objects.first()
if server:
    ServerHeartbeat.objects.update_or_create(server=server, defaults={'last_heartbeat': timezone.now() - timedelta(seconds=70)})
    status = _calculate_server_status(server)
    print(f'  Status: {status} (expected: offline)')
    [print('✓ PASS') if status == 'offline' else print('✗ FAIL')]
" 2>&1
echo ""

# Test 5: Command execution
echo "[TEST 5] Command Execution"
python3 manage.py check_heartbeats_ssh --timeout 5 > /dev/null 2>&1 && echo "✓ Command executes" || echo "✗ Command failed"
echo ""

# Test 6: All servers status
echo "[TEST 6] All Servers Status"
python3 manage.py check_heartbeats 2>&1 | tail -10
echo ""

echo "=========================================="
echo "Test Suite Complete"
echo "=========================================="

