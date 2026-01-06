#!/bin/bash
# Script to fix database password mismatch and reset admin password

set -e

echo "=========================================="
echo "Database Password Fix & Admin Reset"
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

echo "1. Reading current .env file..."
POSTGRES_PASSWORD=$(grep "^POSTGRES_PASSWORD=" "$APP_DIR/.env" | cut -d'=' -f2)
POSTGRES_USER=$(grep "^POSTGRES_USER=" "$APP_DIR/.env" | cut -d'=' -f2)
POSTGRES_DB=$(grep "^POSTGRES_DB=" "$APP_DIR/.env" | cut -d'=' -f2)

if [ -z "$POSTGRES_PASSWORD" ]; then
    echo -e "${RED}Error: POSTGRES_PASSWORD not found in .env file${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Found database credentials in .env"
echo "  User: $POSTGRES_USER"
echo "  Database: $POSTGRES_DB"
echo "  Password: ${POSTGRES_PASSWORD:0:10}... (hidden)"
echo ""

echo "2. Testing database connection with .env password..."
if docker exec monitoring_db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Database connection works with .env password"
    DB_PASSWORD_CORRECT=true
else
    echo -e "${YELLOW}⚠${NC} Database connection failed with .env password"
    echo "  This means the database was created with a different password"
    DB_PASSWORD_CORRECT=false
fi
echo ""

if [ "$DB_PASSWORD_CORRECT" = false ]; then
    echo "3. Database password mismatch detected!"
    echo ""
    echo "Options:"
    echo "  A) Reset database with new password (WILL DELETE ALL DATA)"
    echo "  B) Try to find the correct password"
    echo ""
    read -p "Choose option (A/B): " choice
    
    if [ "$choice" = "A" ] || [ "$choice" = "a" ]; then
        echo ""
        echo "Resetting database with new password..."
        
        # Stop and remove containers
        docker stop monitoring_web monitoring_db > /dev/null 2>&1 || true
        docker rm monitoring_web > /dev/null 2>&1 || true
        docker rm monitoring_db > /dev/null 2>&1 || true
        
        # Remove database volume
        echo "  Removing old database volume..."
        docker volume rm stacksense_postgres_data > /dev/null 2>&1 || true
        
        # Recreate database container with .env password
        echo "  Creating new database container..."
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
        
        # Wait for database
        echo "  Waiting for database to be ready..."
        for i in {1..30}; do
            if docker exec monitoring_db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; then
                echo -e "${GREEN}✓${NC} Database is ready"
                break
            fi
            sleep 1
        done
        
        echo ""
        echo "4. Restarting web container..."
        # The web container should restart automatically, but let's wait a bit
        sleep 5
        
        if docker ps | grep -q monitoring_web; then
            echo -e "${GREEN}✓${NC} Web container is running"
        else
            echo -e "${YELLOW}⚠${NC} Web container not running. You may need to restart it manually."
        fi
    else
        echo "Please check the deployment logs or credentials file for the correct password."
        exit 1
    fi
fi

echo ""
echo "5. Creating/Resetting admin user..."
echo ""

# Generate a new admin password
NEW_ADMIN_PASSWORD=$(openssl rand -base64 16)
echo "Generated new admin password: $NEW_ADMIN_PASSWORD"
echo ""

# Wait for web container to be ready
echo "  Waiting for web container to be ready..."
for i in {1..30}; do
    if docker exec monitoring_web python manage.py check --database default > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Web container is ready"
        break
    fi
    sleep 2
done

# Create or update admin user
echo "  Creating/updating admin user..."
docker exec monitoring_web python manage.py shell << PYTHON_EOF
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()

from django.contrib.auth.models import User

username = 'admin'
password = '$NEW_ADMIN_PASSWORD'
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')

try:
    user = User.objects.get(username=username)
    user.set_password(password)
    user.email = email
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.save()
    print(f'✓ Updated existing admin user')
except User.DoesNotExist:
    user = User.objects.create_superuser(username, email, password)
    print(f'✓ Created new admin user')

print(f'Username: {username}')
print(f'Password: {password}')
print(f'Email: {email}')
PYTHON_EOF

echo ""
echo "=========================================="
echo -e "${GREEN}Admin Credentials${NC}"
echo "=========================================="
echo "Username: admin"
echo "Password: $NEW_ADMIN_PASSWORD"
echo "Email: $(grep "^DJANGO_SUPERUSER_EMAIL=" "$APP_DIR/.env" | cut -d'=' -f2 || echo 'admin@example.com')"
echo ""
echo "Login URL: https://$(grep "^ALLOWED_HOSTS=" "$APP_DIR/.env" | cut -d'=' -f2 | cut -d',' -f1):8005/admin/"
echo ""
echo "=========================================="
echo "Save these credentials!"
echo "=========================================="

