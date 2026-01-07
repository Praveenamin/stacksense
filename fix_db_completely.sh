#!/bin/bash
# Complete database password fix - ensures everything is recreated correctly

set -e

echo "=========================================="
echo "Complete Database Password Fix"
echo "=========================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

APP_DIR="${1:-/opt/stacksense}"

if [ ! -f "$APP_DIR/.env" ]; then
    echo -e "${RED}Error: .env file not found at $APP_DIR/.env${NC}"
    exit 1
fi

echo "Step 1: Reading .env file..."
source "$APP_DIR/.env"

if [ -z "$POSTGRES_PASSWORD" ] || [ -z "$POSTGRES_USER" ] || [ -z "$POSTGRES_DB" ]; then
    echo -e "${RED}Error: Missing database credentials in .env file${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Found database credentials"
echo "  User: $POSTGRES_USER"
echo "  Database: $POSTGRES_DB"
echo "  Password: ${POSTGRES_PASSWORD:0:10}... (hidden)"
echo ""

echo "Step 2: Stopping all containers..."
docker stop monitoring_web monitoring_db monitoring_redis > /dev/null 2>&1 || true
echo -e "${GREEN}✓${NC} Containers stopped"
echo ""

echo "Step 3: Removing containers..."
docker rm monitoring_web monitoring_db monitoring_redis > /dev/null 2>&1 || true
echo -e "${GREEN}✓${NC} Containers removed"
echo ""

echo "Step 4: Removing database volume..."
if docker volume ls | grep -q "stacksense_postgres_data"; then
    docker volume rm stacksense_postgres_data > /dev/null 2>&1 || {
        echo -e "${YELLOW}⚠${NC} Volume in use, forcing removal..."
        # Try to remove containers that might be using it
        docker ps -a --filter volume=stacksense_postgres_data -q | xargs docker rm -f > /dev/null 2>&1 || true
        sleep 2
        docker volume rm stacksense_postgres_data > /dev/null 2>&1 || true
    }
    echo -e "${GREEN}✓${NC} Database volume removed"
else
    echo -e "${GREEN}✓${NC} No database volume found (clean state)"
fi
echo ""

echo "Step 5: Recreating database container with correct password..."
docker run -d \
    --name monitoring_db \
    --network stacksense_network \
    -e POSTGRES_DB="$POSTGRES_DB" \
    -e POSTGRES_USER="$POSTGRES_USER" \
    -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    -p 5433:5432 \
    -v stacksense_postgres_data:/var/lib/postgresql/data \
    --restart unless-stopped \
    postgres:15-alpine > /dev/null 2>&1

echo -e "${GREEN}✓${NC} Database container created"
echo ""

echo "Step 6: Waiting for database to be ready..."
for i in {1..60}; do
    if docker exec monitoring_db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Database is ready"
        break
    fi
    if [ $i -eq 60 ]; then
        echo -e "${RED}✗${NC} Database failed to start"
        docker logs monitoring_db --tail 50
        exit 1
    fi
    echo -n "."
    sleep 1
done
echo ""

echo "Step 7: Testing database connection..."
if docker exec monitoring_db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Database connection successful"
else
    echo -e "${RED}✗${NC} Database connection failed"
    exit 1
fi
echo ""

echo "Step 8: Restarting Redis (if needed)..."
if ! docker ps | grep -q monitoring_redis; then
    if docker ps -a | grep -q monitoring_redis; then
        docker start monitoring_redis > /dev/null 2>&1
    else
        echo "  Redis container not found, will be created by docker-compose"
    fi
fi
echo -e "${GREEN}✓${NC} Redis ready"
echo ""

echo "Step 9: Restarting web container..."
cd "$APP_DIR"
if command -v docker-compose > /dev/null 2>&1; then
    docker-compose up -d web > /dev/null 2>&1 || docker compose up -d web > /dev/null 2>&1
else
    docker compose up -d web > /dev/null 2>&1 || docker-compose up -d web > /dev/null 2>&1
fi

echo -e "${GREEN}✓${NC} Web container started"
echo ""

echo "Step 10: Waiting for web container to be ready..."
for i in {1..60}; do
    if docker exec monitoring_web python manage.py check --database default > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Web container is ready"
        break
    fi
    if [ $i -eq 60 ]; then
        echo -e "${YELLOW}⚠${NC} Web container may still be starting..."
    fi
    echo -n "."
    sleep 2
done
echo ""

echo "Step 11: Running migrations..."
docker exec monitoring_web python manage.py migrate --noinput > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} Migrations completed"
else
    echo -e "${YELLOW}⚠${NC} Migrations may have errors, but continuing..."
fi
echo ""

echo "Step 12: Creating admin user..."
# Generate a new admin password
NEW_ADMIN_PASSWORD=$(openssl rand -base64 16)
DJANGO_SUPERUSER_EMAIL=$(grep "^DJANGO_SUPERUSER_EMAIL=" "$APP_DIR/.env" | cut -d'=' -f2 || echo "admin@example.com")

docker exec monitoring_web python manage.py shell << PYTHON_EOF
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()

from django.contrib.auth.models import User

username = 'admin'
password = '$NEW_ADMIN_PASSWORD'
email = '$DJANGO_SUPERUSER_EMAIL'

try:
    user = User.objects.get(username=username)
    user.set_password(password)
    user.email = email
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.save()
    print('✓ Updated existing admin user')
except User.DoesNotExist:
    user = User.objects.create_superuser(username, email, password)
    print('✓ Created new admin user')
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()

print(f'Username: {username}')
print(f'Password: {password}')
PYTHON_EOF

echo ""
echo "=========================================="
echo -e "${GREEN}✓ FIX COMPLETE${NC}"
echo "=========================================="
echo ""
echo -e "${BLUE}Admin Credentials:${NC}"
echo "  Username: ${GREEN}admin${NC}"
echo "  Password: ${GREEN}$NEW_ADMIN_PASSWORD${NC}"
echo "  Email: ${GREEN}$DJANGO_SUPERUSER_EMAIL${NC}"
echo ""
ALLOWED_HOST=$(grep "^ALLOWED_HOSTS=" "$APP_DIR/.env" | cut -d'=' -f2 | cut -d',' -f1 | tr -d ' ')
if [ -n "$ALLOWED_HOST" ]; then
    echo "  Login URL: https://$ALLOWED_HOST:8005/admin/"
else
    echo "  Login URL: https://<your-server-ip>:8005/admin/"
fi
echo ""
echo -e "${YELLOW}⚠ IMPORTANT: Save these credentials!${NC}"
echo ""
echo "=========================================="

# Save credentials to file
CREDENTIALS_FILE="/opt/stacksense/deployment_credentials.txt"
cat > "$CREDENTIALS_FILE" << EOF
==========================================
StackSense Deployment Credentials
==========================================

Database:
  Host: localhost:5433
  Database: $POSTGRES_DB
  User: $POSTGRES_USER
  Password: $POSTGRES_PASSWORD

Django Admin:
  Username: admin
  Password: $NEW_ADMIN_PASSWORD
  Email: $DJANGO_SUPERUSER_EMAIL

Login URL: https://${ALLOWED_HOST:-<your-server-ip>}:8005/admin/

Generated: $(date)

==========================================
EOF

echo -e "${GREEN}✓${NC} Credentials saved to: $CREDENTIALS_FILE"
echo ""

