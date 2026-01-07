#!/bin/bash
# Diagnostic script for 502 Bad Gateway error

echo "=========================================="
echo "502 Bad Gateway Diagnostic Script"
echo "=========================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "1. Checking container status..."
if docker ps | grep -q "monitoring_web"; then
    echo -e "${GREEN}✓${NC} Container is running"
    docker ps | grep monitoring_web
else
    echo -e "${RED}✗${NC} Container is NOT running"
    echo "Checking stopped containers..."
    docker ps -a | grep monitoring_web
fi
echo ""

echo "2. Checking container logs (last 30 lines)..."
echo "----------------------------------------"
docker logs monitoring_web --tail 30 2>&1
echo ""

echo "3. Checking for errors in logs..."
ERRORS=$(docker logs monitoring_web 2>&1 | grep -iE "error|traceback|exception|failed|permission" | tail -10)
if [ -n "$ERRORS" ]; then
    echo -e "${RED}Found errors:${NC}"
    echo "$ERRORS"
else
    echo -e "${GREEN}✓${NC} No obvious errors found in recent logs"
fi
echo ""

echo "4. Checking if application is listening on port 8000..."
if docker exec monitoring_web netstat -tlnp 2>/dev/null | grep -q ":8000"; then
    echo -e "${GREEN}✓${NC} Application is listening on port 8000"
    docker exec monitoring_web netstat -tlnp | grep ":8000"
else
    echo -e "${RED}✗${NC} Application is NOT listening on port 8000"
    echo "Trying alternative method..."
    docker exec monitoring_web ss -tlnp 2>/dev/null | grep ":8000" || echo "Port 8000 not found"
fi
echo ""

echo "5. Checking logs directory..."
if docker exec monitoring_web test -d /app/logs; then
    echo -e "${GREEN}✓${NC} /app/logs directory exists"
    docker exec monitoring_web ls -la /app/logs
else
    echo -e "${RED}✗${NC} /app/logs directory does NOT exist"
fi
echo ""

echo "6. Checking logs directory permissions..."
PERMS=$(docker exec monitoring_web ls -ld /app/logs 2>/dev/null | awk '{print $1, $3, $4}')
if [ -n "$PERMS" ]; then
    echo "Permissions: $PERMS"
else
    echo -e "${RED}✗${NC} Cannot read permissions"
fi
echo ""

echo "7. Testing Django check command..."
if docker exec monitoring_web python manage.py check > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Django check passed"
else
    echo -e "${RED}✗${NC} Django check failed"
    echo "Output:"
    docker exec monitoring_web python manage.py check 2>&1 | tail -20
fi
echo ""

echo "8. Testing database connection..."
DB_TEST=$(docker exec monitoring_web python manage.py shell -c "from django.db import connection; connection.ensure_connection(); print('OK')" 2>&1)
if echo "$DB_TEST" | grep -q "OK"; then
    echo -e "${GREEN}✓${NC} Database connection works"
else
    echo -e "${RED}✗${NC} Database connection failed"
    echo "$DB_TEST"
fi
echo ""

echo "9. Checking Nginx configuration..."
if sudo nginx -t 2>&1 | grep -q "successful"; then
    echo -e "${GREEN}✓${NC} Nginx configuration is valid"
else
    echo -e "${RED}✗${NC} Nginx configuration has errors"
    sudo nginx -t
fi
echo ""

echo "10. Checking Nginx error logs..."
if [ -f /var/log/nginx/error.log ]; then
    echo "Last 10 Nginx errors:"
    sudo tail -10 /var/log/nginx/error.log
else
    echo "Nginx error log not found"
fi
echo ""

echo "11. Testing connection to Django from host..."
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/admin/login/ | grep -q "200\|301\|302"; then
    echo -e "${GREEN}✓${NC} Django is responding on port 8000"
else
    echo -e "${RED}✗${NC} Django is NOT responding on port 8000"
    curl -v http://127.0.0.1:8000/admin/login/ 2>&1 | head -20
fi
echo ""

echo "12. Checking container resource usage..."
docker stats monitoring_web --no-stream --format "CPU: {{.CPUPerc}}, Memory: {{.MemUsage}}"
echo ""

echo "=========================================="
echo "Diagnostic Complete"
echo "=========================================="
echo ""
echo "Common fixes:"
echo "1. If container is not running: docker start monitoring_web"
echo "2. If logs directory missing: docker exec monitoring_web mkdir -p /app/logs && docker exec monitoring_web chmod 755 /app/logs"
echo "3. If permission errors: docker exec monitoring_web chown -R 1000:1000 /app/logs"
echo "4. If database connection fails: Check .env file and database container"
echo "5. Restart container: docker restart monitoring_web"
echo "6. View full logs: docker logs monitoring_web"


