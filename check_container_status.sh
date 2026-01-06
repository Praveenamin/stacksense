#!/bin/bash
# Quick container status check

echo "=== Container Status ==="
docker ps -a | grep monitoring_web

echo ""
echo "=== Recent Container Logs (last 50 lines) ==="
docker logs monitoring_web --tail 50 2>&1

echo ""
echo "=== Checking if app is listening on port 8000 ==="
docker exec monitoring_web netstat -tlnp 2>/dev/null | grep 8000 || echo "Port 8000 not found or container not accessible"

echo ""
echo "=== Testing Django check ==="
docker exec monitoring_web python manage.py check 2>&1 | tail -20

echo ""
echo "=== Testing database connection ==="
docker exec monitoring_web python manage.py shell -c "from django.db import connection; connection.ensure_connection(); print('DB OK')" 2>&1

echo ""
echo "=== Container resource usage ==="
docker stats monitoring_web --no-stream 2>/dev/null || echo "Container not running"

