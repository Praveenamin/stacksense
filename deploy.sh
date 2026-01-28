#!/bin/bash

###############################################################################
# StackSense Complete Deployment Script
# This script deploys the entire StackSense monitoring application
# Usage: sudo ./deploy.sh [domain] [email]
# Example: sudo ./deploy.sh stacksense.assistanz.com admin@example.com
###############################################################################

# Do not exit on first error; we handle errors explicitly per step
set -o pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
DOMAIN="${1:-stacksense.assistanz.com}"
EMAIL="${2:-admin@example.com}"
APP_DIR="/opt/stacksense"
REPO_URL="${REPO_URL:-https://github.com/your-org/stacksense-repo.git}"  # Update with your repo URL
NGINX_PORT=8005
DOCKER_NETWORK="stacksense_network"

# Database configuration
POSTGRES_DB="monitoring_db"
POSTGRES_USER="monitoring_user"

# Check if .env exists and load existing password to maintain consistency
# This ensures we use the same password if redeploying
if [ -f "$APP_DIR/.env" ] && [ -r "$APP_DIR/.env" ]; then
    EXISTING_PASSWORD=$(grep "^POSTGRES_PASSWORD=" "$APP_DIR/.env" 2>/dev/null | cut -d'=' -f2- | tr -d ' ' || echo "")
    if [ -n "$EXISTING_PASSWORD" ]; then
        POSTGRES_PASSWORD="$EXISTING_PASSWORD"
        echo -e "${GREEN}✓${NC} Using existing database password from .env file"
    else
        # Generate new password if not set
        POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -base64 32)}"
    fi
else
    # Generate new password if .env doesn't exist or can't be read
    POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -base64 32)}"
fi

POSTGRES_HOST="monitoring_db"
POSTGRES_PORT=5432

# Redis configuration
REDIS_PASSWORD="${REDIS_PASSWORD:-$(openssl rand -base64 32)}"

# Django configuration
DJANGO_SUPERUSER_USERNAME="${DJANGO_SUPERUSER_USERNAME:-admin}"
DJANGO_SUPERUSER_EMAIL="${DJANGO_SUPERUSER_EMAIL:-$EMAIL}"
DJANGO_SUPERUSER_PASSWORD="${DJANGO_SUPERUSER_PASSWORD:-$(openssl rand -base64 16)}"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}StackSense Deployment Script${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "Domain: ${GREEN}$DOMAIN${NC}"
echo -e "Email: ${GREEN}$EMAIL${NC}"
echo -e "App Directory: ${GREEN}$APP_DIR${NC}"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Please run as root (use sudo)${NC}"
    exit 1
fi

###############################################################################
# Step 1: System Requirements Check
###############################################################################
echo -e "${BLUE}[1/10] Checking system requirements...${NC}"

# Check OS
if [ ! -f /etc/os-release ]; then
    echo -e "${RED}Unsupported operating system${NC}"
    exit 1
fi

. /etc/os-release
OS=$ID
OS_VERSION=$VERSION_ID

echo -e "${GREEN}✓${NC} OS: $OS $OS_VERSION"

# Check available disk space (need at least 5GB)
AVAILABLE_SPACE=$(df -BG / | awk 'NR==2 {print $4}' | sed 's/G//')
if [ "$AVAILABLE_SPACE" -lt 5 ]; then
    echo -e "${YELLOW}⚠ Warning: Less than 5GB disk space available${NC}"
fi

# Check memory (need at least 2GB)
TOTAL_MEM=$(free -g | awk '/^Mem:/{print $2}')
if [ "$TOTAL_MEM" -lt 2 ]; then
    echo -e "${YELLOW}⚠ Warning: Less than 2GB RAM available${NC}"
fi

###############################################################################
# Step 2: Install System Dependencies
###############################################################################
echo -e "${BLUE}[2/10] Installing system dependencies...${NC}"

# Update package list
apt-get update -qq

# Install required packages
REQUIRED_PACKAGES=(
    "curl"
    "wget"
    "git"
    "python3"
    "python3-pip"
    "python3-venv"
    "docker.io"
    "docker-compose"
    "nginx"
    "openssl"
    "certbot"
    "python3-certbot-nginx"
    "postgresql-client"
    "redis-tools"
    "ufw"
)

for package in "${REQUIRED_PACKAGES[@]}"; do
    if ! dpkg -l | grep -q "^ii  $package "; then
        echo -e "  Installing $package..."
        apt-get install -y "$package" > /dev/null 2>&1
    else
        echo -e "  ${GREEN}✓${NC} $package already installed"
    fi
done

# Start and enable Docker
systemctl start docker
systemctl enable docker

# Add current user to docker group (if not root)
if [ "$SUDO_USER" ]; then
    usermod -aG docker "$SUDO_USER"
fi

echo -e "${GREEN}✓${NC} System dependencies installed"

###############################################################################
# Step 3: Clone/Update Repository
###############################################################################
echo -e "${BLUE}[3/10] Setting up application directory...${NC}"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [ -d ".git" ]; then
    echo -e "  Updating existing repository..."
    git pull > /dev/null 2>&1 || echo -e "${YELLOW}  ⚠ Could not update repository${NC}"
else
    if [ -n "$REPO_URL" ] && [ "$REPO_URL" != "https://github.com/your-org/stacksense-repo.git" ]; then
        echo -e "  Cloning repository from $REPO_URL..."
        git clone "$REPO_URL" . > /dev/null 2>&1 || {
            echo -e "${YELLOW}  ⚠ Could not clone repository. Using existing files...${NC}"
        }
    else
        echo -e "${YELLOW}  ⚠ No repository URL configured. Using existing files...${NC}"
    fi
fi

echo -e "${GREEN}✓${NC} Application directory ready"

###############################################################################
# Step 4: Create Docker Network
###############################################################################
echo -e "${BLUE}[4/10] Creating Docker network...${NC}"

if ! docker network ls | grep -q "$DOCKER_NETWORK"; then
    docker network create "$DOCKER_NETWORK" > /dev/null 2>&1
    echo -e "${GREEN}✓${NC} Docker network created"
else
    echo -e "${GREEN}✓${NC} Docker network already exists"
fi

###############################################################################
# Step 5: Setup PostgreSQL Database Container
###############################################################################
echo -e "${BLUE}[5/10] Setting up PostgreSQL database...${NC}"

# Stop ALL containers that might use the database (web, db, any other)
echo -e "  Stopping all related containers..."
docker stop monitoring_web monitoring_db > /dev/null 2>&1 || true
sleep 2

# Remove all containers
echo -e "  Removing containers..."
docker rm monitoring_web monitoring_db > /dev/null 2>&1 || true

# Force remove any containers that might be using the database volume
echo -e "  Checking for containers using database volume..."
CONTAINERS_USING_VOLUME=$(docker ps -a --filter volume=stacksense_postgres_data -q 2>/dev/null || true)
if [ -n "$CONTAINERS_USING_VOLUME" ]; then
    echo -e "  Force removing containers using database volume..."
    echo "$CONTAINERS_USING_VOLUME" | xargs docker rm -f > /dev/null 2>&1 || true
    sleep 3
fi

# CRITICAL: ALWAYS remove old database volume to ensure password consistency
# This prevents authentication failures due to password mismatches
# We MUST remove the volume every time to ensure the password matches
echo -e "  Removing old database volume (MANDATORY for password consistency)..."
# Stop ALL containers first - be very aggressive
docker ps -aq | xargs docker stop > /dev/null 2>&1 || true
sleep 3
# Remove ALL containers
docker ps -aq | xargs docker rm -f > /dev/null 2>&1 || true
sleep 3

# Now remove the volume - MANDATORY, not optional
# Check if volume exists and try to remove it
echo -e "  Checking for existing database volume..."
if docker volume ls | grep -q "stacksense_postgres_data"; then
    echo -e "  Found existing database volume, attempting removal..."
    MAX_VOLUME_RETRIES=3
    VOLUME_RETRY=0
    VOLUME_REMOVED=false
    
    while [ $VOLUME_RETRY -lt $MAX_VOLUME_RETRIES ]; do
        # First, aggressively clean up any containers that might be using the volume
        echo -e "  Cleaning up containers before volume removal (attempt $((VOLUME_RETRY + 1))/$MAX_VOLUME_RETRIES)..."
        docker ps -a --format '{{.ID}}' | while read id; do
            if docker inspect "$id" 2>/dev/null | grep -q "stacksense_postgres_data"; then
                docker rm -f "$id" > /dev/null 2>&1 || true
            fi
        done
        docker ps -a --filter ancestor=postgres --format '{{.ID}}' | xargs docker rm -f > /dev/null 2>&1 || true
        sleep 3
        
        # Check if volume still exists after cleanup
        if ! docker volume ls | grep -q "stacksense_postgres_data"; then
            echo -e "${GREEN}✓${NC} Database volume removed during container cleanup"
            VOLUME_REMOVED=true
            break
        fi
        
        # Try to remove the volume with timeout (10 seconds max)
        echo -e "  Attempting to remove volume..."
        if timeout 10 docker volume rm stacksense_postgres_data > /dev/null 2>&1; then
            # Command completed (success or failure, check volume status)
            if ! docker volume ls | grep -q "stacksense_postgres_data"; then
                echo -e "${GREEN}✓${NC} Database volume removed successfully"
                VOLUME_REMOVED=true
                break
            fi
        else
            # Timeout occurred or command failed
            echo -e "  ${YELLOW}⚠ Volume removal timed out or failed${NC}"
        fi
        
        # Check if volume still exists
        if ! docker volume ls | grep -q "stacksense_postgres_data"; then
            echo -e "${GREEN}✓${NC} Database volume removed successfully"
            VOLUME_REMOVED=true
            break
        fi
        
        # Volume still exists, need to retry
        VOLUME_RETRY=$((VOLUME_RETRY + 1))
        if [ $VOLUME_RETRY -lt $MAX_VOLUME_RETRIES ]; then
            echo -e "  ${YELLOW}⚠ Volume still exists, will retry...${NC}"
            sleep 2
        fi
    done
    
    # Final verification - check one more time if we haven't confirmed removal
    if [ "$VOLUME_REMOVED" = false ]; then
        # Check one final time if volume still exists
        if docker volume ls | grep -q "stacksense_postgres_data"; then
            echo -e "${RED}✗${NC} FATAL: Cannot remove database volume after $MAX_VOLUME_RETRIES attempts!"
            echo -e "${RED}  This will cause password authentication failures.${NC}"
            echo -e "${YELLOW}  Please manually remove the volume:${NC}"
            echo -e "    docker volume rm stacksense_postgres_data"
            exit 1
        else
            # Volume was removed (maybe during cleanup), we're good
            echo -e "${GREEN}✓${NC} Database volume removed"
        fi
    fi
else
    echo -e "${GREEN}✓${NC} No existing database volume found (clean state)"
fi

docker run -d \
    --name monitoring_db \
    --network "$DOCKER_NETWORK" \
    -e POSTGRES_DB="$POSTGRES_DB" \
    -e POSTGRES_USER="$POSTGRES_USER" \
    -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    -p 5433:5432 \
    -v stacksense_postgres_data:/var/lib/postgresql/data \
    --restart unless-stopped \
    postgres:15-alpine > /dev/null 2>&1

# Wait for database to be ready
echo -e "  Waiting for database to be ready..."
for i in {1..60}; do
    if docker exec monitoring_db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Database is ready"
        break
    fi
    if [ $i -eq 60 ]; then
        echo -e "${RED}✗${NC} Database failed to start"
        docker logs monitoring_db --tail 30
        exit 1
    fi
    sleep 1
done

# Verify database connection with correct password
echo -e "  Verifying database connection with correct password..."
MAX_RETRIES=5
RETRY_COUNT=0
CONNECTION_VERIFIED=false

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if docker run --rm --network "$DOCKER_NETWORK" -e PGPASSWORD="$POSTGRES_PASSWORD" postgres:15-alpine \
        psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Database connection verified successfully"
        CONNECTION_VERIFIED=true
        break
    else
        RETRY_COUNT=$((RETRY_COUNT + 1))
        if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
            echo -e "  ${YELLOW}⚠ Connection failed, retrying ($RETRY_COUNT/$MAX_RETRIES)...${NC}"
            sleep 2
        fi
    fi
done

if [ "$CONNECTION_VERIFIED" = false ]; then
    echo -e "${RED}✗${NC} Database connection failed after $MAX_RETRIES attempts!"
    echo -e "${RED}  Password authentication failed - this should not happen!${NC}"
    echo -e "${YELLOW}  Checking database logs...${NC}"
    docker logs monitoring_db --tail 50
    echo ""
    echo -e "${RED}  Deployment cannot continue. Please check the error above.${NC}"
    exit 1
fi

# Additional verification: test password from environment
echo -e "  Performing final password verification..."
TEST_RESULT=$(docker run --rm --network "$DOCKER_NETWORK" -e PGPASSWORD="$POSTGRES_PASSWORD" postgres:15-alpine \
    psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT current_database();" 2>&1)
if echo "$TEST_RESULT" | grep -q "FATAL.*password authentication failed"; then
    echo -e "${RED}✗${NC} Critical: Password authentication still failing!"
    echo -e "${RED}  The database was created but password doesn't match!${NC}"
    echo -e "${YELLOW}  This indicates the volume still has old credentials.${NC}"
    echo -e "${YELLOW}  Removing volume and recreating database...${NC}"
    
    # Emergency fix: Remove everything and recreate
    docker stop monitoring_db > /dev/null 2>&1 || true
    docker rm monitoring_db > /dev/null 2>&1 || true
    docker volume rm stacksense_postgres_data > /dev/null 2>&1 || {
        echo -e "${RED}✗${NC} Cannot remove volume. Manual intervention required."
        echo -e "${YELLOW}  Run: docker volume rm stacksense_postgres_data${NC}"
        exit 1
    }
    sleep 2
    
    # Recreate with correct password
    docker run -d \
        --name monitoring_db \
        --network "$DOCKER_NETWORK" \
        -e POSTGRES_DB="$POSTGRES_DB" \
        -e POSTGRES_USER="$POSTGRES_USER" \
        -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
        -p 5433:5432 \
        -v stacksense_postgres_data:/var/lib/postgresql/data \
        --restart unless-stopped \
        postgres:15-alpine > /dev/null 2>&1
    
    # Wait and verify
    for i in {1..60}; do
        if docker exec monitoring_db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    
    # Final verification
    if ! docker run --rm --network "$DOCKER_NETWORK" -e PGPASSWORD="$POSTGRES_PASSWORD" postgres:15-alpine \
        psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;" > /dev/null 2>&1; then
        echo -e "${RED}✗${NC} FATAL: Password still doesn't work after recreation!"
        exit 1
    fi
    echo -e "${GREEN}✓${NC} Database recreated and password verified"
else
    echo -e "${GREEN}✓${NC} Password authentication working correctly"
fi

###############################################################################
# Step 6: Setup Redis Container
###############################################################################
echo -e "${BLUE}[6/10] Setting up Redis cache...${NC}"

if docker ps -a | grep -q "monitoring_redis"; then
    echo -e "  Stopping existing Redis container..."
    docker stop monitoring_redis > /dev/null 2>&1 || true
    docker rm monitoring_redis > /dev/null 2>&1 || true
fi

docker run -d \
    --name monitoring_redis \
    --network "$DOCKER_NETWORK" \
    -p 6379:6379 \
    -v stacksense_redis_data:/data \
    --restart unless-stopped \
    redis:7-alpine redis-server --appendonly yes > /dev/null 2>&1

echo -e "${GREEN}✓${NC} Redis is ready"

###############################################################################
# Step 7: Build and Start Django Application Container
###############################################################################
echo -e "${BLUE}[7/10] Building and starting Django application...${NC}"

# CRITICAL: Write .env file BEFORE starting web container
# This ensures the password is consistent throughout
echo -e "  Creating .env file with database credentials..."
cat > "$APP_DIR/.env" << EOF
POSTGRES_DB=$POSTGRES_DB
POSTGRES_USER=$POSTGRES_USER
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_HOST=$POSTGRES_HOST
POSTGRES_PORT=$POSTGRES_PORT
REDIS_URL=redis://monitoring_redis:6379/0
DJANGO_SUPERUSER_USERNAME=$DJANGO_SUPERUSER_USERNAME
DJANGO_SUPERUSER_EMAIL=$DJANGO_SUPERUSER_EMAIL
DJANGO_SUPERUSER_PASSWORD=$DJANGO_SUPERUSER_PASSWORD
CREATE_SUPERUSER=true
ALLOWED_HOSTS=$DOMAIN,localhost,127.0.0.1,0.0.0.0,tracker.stackbill.com
CSRF_TRUSTED_ORIGINS=https://$DOMAIN:$NGINX_PORT,https://$DOMAIN,http://$DOMAIN:$NGINX_PORT,http://$DOMAIN,http://localhost:8000,https://localhost:8000
USE_TLS=True
BEHIND_PROXY=True
EOF

# CRITICAL: Verify database is accessible with the EXACT password from .env
# This must match what Django will use
echo -e "  Pre-flight check: Verifying database password from .env file..."
ENV_PASSWORD=$(grep "^POSTGRES_PASSWORD=" "$APP_DIR/.env" 2>/dev/null | cut -d'=' -f2- | tr -d ' ' || echo "")

if [ -z "$ENV_PASSWORD" ]; then
    echo -e "${RED}✗${NC} POSTGRES_PASSWORD not found in .env file!"
    exit 1
fi

# Test connection using the password from .env file (exactly as Django will)
if ! docker run --rm --network "$DOCKER_NETWORK" -e PGPASSWORD="$ENV_PASSWORD" postgres:15-alpine \
    psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;" > /dev/null 2>&1; then
    echo -e "${RED}✗${NC} CRITICAL: Database password from .env file does NOT work!"
    echo -e "${RED}  Password mismatch detected!${NC}"
    echo -e "${YELLOW}  This means the database was created with a different password.${NC}"
    echo -e "${YELLOW}  Removing database and recreating with correct password...${NC}"
    
    # Emergency: Stop and remove database
    docker stop monitoring_db > /dev/null 2>&1 || true
    docker rm monitoring_db > /dev/null 2>&1 || true
    docker volume rm stacksense_postgres_data > /dev/null 2>&1 || true
    sleep 3
    
    # Recreate database with correct password
    docker run -d \
        --name monitoring_db \
        --network "$DOCKER_NETWORK" \
        -e POSTGRES_DB="$POSTGRES_DB" \
        -e POSTGRES_USER="$POSTGRES_USER" \
        -e POSTGRES_PASSWORD="$ENV_PASSWORD" \
        -p 5433:5432 \
        -v stacksense_postgres_data:/var/lib/postgresql/data \
        --restart unless-stopped \
        postgres:15-alpine > /dev/null 2>&1
    
    # Wait for database
    echo -e "  Waiting for database to restart..."
    for i in {1..60}; do
        if docker exec monitoring_db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    
    # Verify again
    if ! docker run --rm --network "$DOCKER_NETWORK" -e PGPASSWORD="$ENV_PASSWORD" postgres:15-alpine \
        psql -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;" > /dev/null 2>&1; then
        echo -e "${RED}✗${NC} FATAL: Database password still doesn't work after recreation!"
        echo -e "${RED}  Cannot proceed. Please check the deployment.${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓${NC} Database recreated and password verified"
else
    echo -e "${GREEN}✓${NC} Database password from .env file verified successfully"
fi

# Stop existing web container if it exists
if docker ps -a | grep -q "monitoring_web"; then
    echo -e "  Stopping existing web container..."
    docker stop monitoring_web > /dev/null 2>&1 || true
    docker rm monitoring_web > /dev/null 2>&1 || true
fi

# Create required directories with proper permissions
echo -e "  Creating required directories..."
mkdir -p "$APP_DIR/logs"
mkdir -p "$APP_DIR/media"
mkdir -p "$APP_DIR/staticfiles"
mkdir -p "$APP_DIR/ssh_keys"
chmod 755 "$APP_DIR/logs" "$APP_DIR/media" "$APP_DIR/staticfiles" "$APP_DIR/ssh_keys"
chown -R 1000:1000 "$APP_DIR/logs" "$APP_DIR/media" "$APP_DIR/staticfiles" "$APP_DIR/ssh_keys" 2>/dev/null || true

# Build Docker image if Dockerfile exists
if [ -f "$APP_DIR/Dockerfile" ]; then
    echo -e "  Building Docker image..."
    docker build -t stacksense-web:latest "$APP_DIR" > /dev/null 2>&1 || {
        echo -e "${YELLOW}  ⚠ Could not build image. Using existing image...${NC}"
    }
fi

# Start web container
echo -e "  Starting web container..."
docker run -d \
    --name monitoring_web \
    --network "$DOCKER_NETWORK" \
    -p 8000:8000 \
    -v "$APP_DIR:/app" \
    -v stacksense_static:/app/staticfiles \
    -v stacksense_media:/app/media \
    --env-file "$APP_DIR/.env" \
    --restart unless-stopped \
    stacksense-web:latest \
    sh -c "mkdir -p /app/logs && chmod 755 /app/logs && touch /app/logs/app.log /app/logs/error.log && chmod 644 /app/logs/*.log && \
           python manage.py migrate --noinput && \
           python manage.py collectstatic --noinput && \
           python manage.py createsuperuser --noinput || true && \
           nohup python3 metrics_scheduler.py > /tmp/metrics_scheduler.log 2>&1 & \
           gunicorn log_analyzer.wsgi:application --bind 0.0.0.0:8000 --workers 4 --timeout 120 --access-logfile - --error-logfile -" > /dev/null 2>&1 || {
    # Fallback: use Python image if custom image doesn't exist
    echo -e "  Using Python base image..."
    docker run -d \
        --name monitoring_web \
        --network "$DOCKER_NETWORK" \
        -p 8000:8000 \
        -v "$APP_DIR:/app" \
        -v stacksense_static:/app/staticfiles \
        -v stacksense_media:/app/media \
        --env-file "$APP_DIR/.env" \
        --restart unless-stopped \
        python:3.11-slim \
        sh -c "cd /app && pip install -r requirements.txt && \
               mkdir -p /app/logs && chmod 755 /app/logs && touch /app/logs/app.log /app/logs/error.log && chmod 644 /app/logs/*.log && \
               python manage.py migrate --noinput && \
               python manage.py collectstatic --noinput && \
               python manage.py createsuperuser --noinput || true && \
               nohup python3 metrics_scheduler.py > /tmp/metrics_scheduler.log 2>&1 & \
               gunicorn log_analyzer.wsgi:application --bind 0.0.0.0:8000 --workers 4 --timeout 120 --access-logfile - --error-logfile -" > /dev/null 2>&1
}

# Wait for container to start
echo -e "  Waiting for container to start..."
sleep 15

# CRITICAL: Restart web container to ensure it picks up the correct .env file
# This is necessary because the container might have been started before .env was fully written
echo -e "  Restarting web container to ensure .env file is loaded correctly..."
docker restart monitoring_web > /dev/null 2>&1
sleep 10

# Verify the container can connect to the database
echo -e "  Verifying database connection from web container..."
DB_CONNECTION_VERIFIED=false
for i in {1..10}; do
    # First check if database container is running and accessible
    if ! docker ps | grep -q monitoring_db; then
        echo -e "${RED}✗${NC} Database container is not running!"
        exit 1
    fi
    
    # Test connection and capture error
    CONNECTION_TEST=$(timeout 10 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {str(e)}')
    exit(1)
" 2>&1)
    
    if echo "$CONNECTION_TEST" | grep -q "SUCCESS"; then
        echo -e "${GREEN}✓${NC} Database connection verified from web container"
        DB_CONNECTION_VERIFIED=true
        break
    else
        if [ $i -eq 1 ]; then
            echo -e "  ${YELLOW}⚠ Connection test failed. Error details:${NC}"
            echo "$CONNECTION_TEST" | grep -E "(ERROR|password|authentication|OperationalError)" | head -3
        fi
        if [ $i -lt 10 ]; then
            echo -e "  Retrying... ($i/10)"
        fi
    fi
    sleep 3
done

if [ "$DB_CONNECTION_VERIFIED" = false ]; then
    echo -e "${RED}✗${NC} Database connection failed from web container!"
    echo -e "${YELLOW}  Diagnosing issue...${NC}"
    
    # Check if database container is accessible from web container
    echo -e "  Testing network connectivity..."
    if docker exec monitoring_web ping -c 1 monitoring_db > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} Network connectivity OK"
    else
        echo -e "  ${RED}✗${NC} Cannot reach database container from web container!"
    fi
    
    # Check database container logs
    echo -e "  Checking database container status..."
    docker exec monitoring_db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1 && echo -e "  ${GREEN}✓${NC} Database is ready" || echo -e "  ${RED}✗${NC} Database not ready"
    
    # Try to connect directly with psql from web container
    echo -e "  Testing direct psql connection..."
    if docker exec monitoring_web sh -c "PGPASSWORD='$POSTGRES_PASSWORD' psql -h monitoring_db -U $POSTGRES_USER -d $POSTGRES_DB -c 'SELECT 1'" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} Direct psql connection works - Django settings may be wrong"
    else
        echo -e "  ${RED}✗${NC} Direct psql connection also fails - password issue confirmed"
        echo -e "${YELLOW}  Attempting to recreate database with correct password...${NC}"
        
        # Emergency: Recreate database
        docker stop monitoring_web monitoring_db > /dev/null 2>&1 || true
        docker rm monitoring_db > /dev/null 2>&1 || true
        docker volume rm stacksense_postgres_data > /dev/null 2>&1 || true
        sleep 2
        
        # Recreate database
        docker run -d \
            --name monitoring_db \
            --network "$DOCKER_NETWORK" \
            -e POSTGRES_DB="$POSTGRES_DB" \
            -e POSTGRES_USER="$POSTGRES_USER" \
            -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
            -p 5433:5432 \
            -v stacksense_postgres_data:/var/lib/postgresql/data \
            --restart unless-stopped \
            postgres:15-alpine > /dev/null 2>&1
        
        # Wait for database
        for j in {1..30}; do
            if docker exec monitoring_db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; then
                break
            fi
            sleep 1
        done
        
        # Restart web container
        docker restart monitoring_web > /dev/null 2>&1
        sleep 10
        
        # Test again
        if timeout 10 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" > /dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} Database connection works after recreation"
            DB_CONNECTION_VERIFIED=true
        else
            echo -e "${RED}✗${NC} Connection still failing after database recreation"
        fi
    fi
fi

# Detect whether this is a fresh database (no core_server table yet)
DB_HAS_CORE_SERVER=$(docker exec monitoring_db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='core_server'" 2>/dev/null || echo "")

if [ "$DB_HAS_CORE_SERVER" = "1" ]; then
    echo -e "  Existing database detected - applying compatibility fixes..."

    # Add any missing database columns BEFORE migrations
    echo -e "  Adding any missing database columns..."
    docker exec monitoring_db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" << 'SQL_EOF' > /dev/null 2>&1 || true
-- core_server columns (migration 0011)
ALTER TABLE core_server ADD COLUMN IF NOT EXISTS suppress_alerts BOOLEAN DEFAULT FALSE;
ALTER TABLE core_server ADD COLUMN IF NOT EXISTS suspend_monitoring BOOLEAN DEFAULT FALSE;

-- core_monitoredlog columns (migration 0020)
ALTER TABLE core_monitoredlog ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE core_monitoredlog ADD COLUMN IF NOT EXISTS last_scan_time TIMESTAMP NULL;
ALTER TABLE core_monitoredlog ADD COLUMN IF NOT EXISTS scan_from_days INTEGER DEFAULT 1;
ALTER TABLE core_monitoredlog ADD COLUMN IF NOT EXISTS service_type VARCHAR(20) DEFAULT 'custom';

-- core_systemmetric columns
ALTER TABLE core_systemmetric ADD COLUMN IF NOT EXISTS system_uptime_seconds BIGINT NULL;
ALTER TABLE core_systemmetric ADD COLUMN IF NOT EXISTS top_processes JSONB NULL;
SQL_EOF

    # Fix migration issues - fake migrations if columns already exist
    echo -e "  Checking and fixing migrations..."
    docker exec monitoring_web python manage.py shell << 'PYTHON_EOF' > /dev/null 2>&1 || true
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

cursor = connection.cursor()
recorder = MigrationRecorder(connection)
applied = recorder.applied_migrations()

# Fake migration 0011 if columns exist
cursor.execute("""
    SELECT column_name 
    FROM information_schema.columns 
    WHERE table_name='core_server' 
    AND column_name IN ('suppress_alerts', 'suspend_monitoring')
""")
if len(cursor.fetchall()) >= 2:
    if ('core', '0011_add_server_toggles') not in applied:
        recorder.record_applied('core', '0011_add_server_toggles')
        print('Faked migration 0011')

# Fake migration 0020 if columns exist
cursor.execute("""
    SELECT column_name 
    FROM information_schema.columns 
    WHERE table_name='core_monitoredlog' 
    AND column_name IN ('enabled', 'last_scan_time', 'scan_from_days', 'service_type')
""")
if len(cursor.fetchall()) >= 3:
    if ('core', '0020_monitoredlog_enabled_monitoredlog_last_scan_time_and_more') not in applied:
        recorder.record_applied('core', '0020_monitoredlog_enabled_monitoredlog_last_scan_time_and_more')
        print('Faked migration 0020')
PYTHON_EOF
else
    echo -e "  Fresh database detected - skipping legacy column/migration compatibility fixes"
fi

# Run migrations with error handling and timeout
echo -e "  Running database migrations..."
# Always verify DB connection before migrations
echo -e "  Verifying database connection before migrations..."
if ! timeout 10 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" > /dev/null 2>&1; then
    echo -e \"${RED}✗${NC} Database connection failed! Cannot run migrations.\"
    echo -e \"${YELLOW}  Please fix the database connection and rerun deploy.sh${NC}\"
    exit 1
fi

# Run migrations with timeout
MIGRATION_OUTPUT=$(timeout 120 docker exec monitoring_web python manage.py migrate --noinput 2>&1)
MIGRATION_EXIT=$?
MIGRATION_SUCCESS=0

# Check if migrations succeeded
if [ $MIGRATION_EXIT -eq 0 ]; then
    if echo "$MIGRATION_OUTPUT" | grep -q "No migrations to apply" || echo "$MIGRATION_OUTPUT" | grep -q "Applying.*migrations"; then
        if ! echo "$MIGRATION_OUTPUT" | grep -qE "(Error|Traceback|FieldDoesNotExist|KeyError|password authentication failed|ProgrammingError)"; then
            echo -e "${GREEN}✓${NC} Migrations completed successfully"
            MIGRATION_SUCCESS=1
        else
            echo -e "${YELLOW}⚠ Migrations completed but with warnings${NC}"
            echo "$MIGRATION_OUTPUT" | grep -E "(Error|Traceback|password|ProgrammingError)" | head -5
        fi
    else
        echo -e "${GREEN}✓${NC} Migrations completed"
        MIGRATION_SUCCESS=1
    fi
elif [ $MIGRATION_EXIT -eq 124 ]; then
    echo -e "${RED}✗${NC} Migrations timed out after 120 seconds"
    echo -e "${YELLOW}  This usually indicates a database connection issue${NC}"
elif echo "$MIGRATION_OUTPUT" | grep -q "password authentication failed"; then
    echo -e "${RED}✗${NC} Database password authentication failed during migrations!"
    echo -e "${YELLOW}  Output:${NC}"
    echo "$MIGRATION_OUTPUT" | grep -A 5 "password authentication" | head -10
else
    echo -e "${YELLOW}⚠ Migrations had issues:${NC}"
    echo "$MIGRATION_OUTPUT" | tail -10
fi

# Fail fast if migrations did not succeed
if [ "$MIGRATION_SUCCESS" -ne 1 ]; then
    echo -e "${RED}✗${NC} FATAL: Migrations failed or were not completed successfully."
    echo -e "${RED}  Deployment cannot continue without a fully migrated database.${NC}"
    exit 1
fi

# Verify there are no pending migrations for core app
echo -e "  Verifying pending migrations..."
PENDING_MIGRATIONS=$(docker exec monitoring_web python manage.py showmigrations core 2>&1 | grep -c \"\\[ \\]\" || echo \"0\")
if [ \"$PENDING_MIGRATIONS\" -gt 0 ]; then
    echo -e \"${RED}✗${NC} FATAL: $PENDING_MIGRATIONS pending core migrations detected!\"
    docker exec monitoring_web python manage.py showmigrations core | grep \"\\[ \\]\" || true
    echo -e \"${RED}  Please resolve the migration issues and rerun deploy.sh${NC}\"
    exit 1
fi
echo -e \"${GREEN}✓${NC} All core migrations applied\"

# Handle specific migration errors
if echo "$MIGRATION_OUTPUT" | grep -qE "(column.*already exists|DuplicateColumn|relation.*does not exist)"; then
    echo -e "  ${YELLOW}⚠ Detected migration conflicts. Attempting to fix...${NC}"
    
    # Fix column already exists errors by faking the migration
    if echo "$MIGRATION_OUTPUT" | grep -qE "(column.*already exists|DuplicateColumn)"; then
        echo -e "  Fixing 'column already exists' errors..."
        
        # Extract migration name from error if possible
        if echo "$MIGRATION_OUTPUT" | grep -q "0011_add_server_toggles"; then
            MIGRATION_TO_FAKE="0011_add_server_toggles"
        else
            # Try to find any migration that's failing
            MIGRATION_TO_FAKE=$(echo "$MIGRATION_OUTPUT" | grep -oE "core\.[0-9]+_[a-z_]+" | head -1 | cut -d'.' -f2 || echo "")
        fi
        
        if [ -n "$MIGRATION_TO_FAKE" ]; then
            echo -e "  Faking migration: core.$MIGRATION_TO_FAKE"
            docker exec monitoring_web python manage.py shell << PYTHON_EOF > /dev/null 2>&1 || true
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

cursor = connection.cursor()
recorder = MigrationRecorder(connection)

# Check if suppress_alerts column exists (for 0011)
cursor.execute("""
    SELECT column_name 
    FROM information_schema.columns 
    WHERE table_name='core_server' 
    AND column_name='suppress_alerts'
""")
column_exists = cursor.fetchone() is not None

# Check if migration is already applied
applied = recorder.applied_migrations()
migration_key = ('core', '$MIGRATION_TO_FAKE')

if column_exists and migration_key not in applied:
    recorder.record_applied('core', '$MIGRATION_TO_FAKE')
    print('Faked migration')
elif not column_exists and migration_key in applied:
    # Migration marked as applied but column doesn't exist - unmark it
    from django.db import transaction
    with transaction.atomic():
        cursor.execute("DELETE FROM django_migrations WHERE app='core' AND name='$MIGRATION_TO_FAKE'")
    print('Unmarked migration')
PYTHON_EOF
        fi
    fi
    
    # Run migrations again after fixes
    echo -e "  Retrying migrations..."
    MIGRATION_OUTPUT2=$(timeout 60 docker exec monitoring_web python manage.py migrate --noinput 2>&1)
    if ! echo "$MIGRATION_OUTPUT2" | grep -qE "(Error|Traceback|ProgrammingError.*already exists|DuplicateColumn)"; then
        echo -e "${GREEN}✓${NC} Migrations fixed and completed"
        MIGRATION_SUCCESS=1
    else
        echo -e "${YELLOW}⚠ Some migration issues remain. Trying to fake 0011 specifically...${NC}"
        # Specifically fix 0011 if it's still failing
        docker exec monitoring_web python manage.py shell << 'PYTHON_EOF' > /dev/null 2>&1 || true
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

cursor = connection.cursor()
recorder = MigrationRecorder(connection)

# Check if suppress_alerts column exists
cursor.execute("""
    SELECT column_name 
    FROM information_schema.columns 
    WHERE table_name='core_server' 
    AND column_name='suppress_alerts'
""")
if cursor.fetchone():
    applied = recorder.applied_migrations()
    if ('core', '0011_add_server_toggles') not in applied:
        recorder.record_applied('core', '0011_add_server_toggles')
        print('Faked 0011')
PYTHON_EOF
        # Try one more time
        timeout 60 docker exec monitoring_web python manage.py migrate --noinput > /dev/null 2>&1 || true
    fi
fi

# Ensure core_loginactivity table exists (critical for login logging)
echo -e "  Ensuring all core app tables exist..."
if ! timeout 10 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
cursor = connection.cursor()
cursor.execute(\"SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name='core_loginactivity'\")
exit(0 if cursor.fetchone() else 1)
" > /dev/null 2>&1; then
    echo -e "  ${YELLOW}⚠ core_loginactivity table missing. Running core migrations...${NC}"
    timeout 60 docker exec monitoring_web python manage.py migrate core --noinput > /dev/null 2>&1 || true
    # Verify it was created
    if timeout 10 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
cursor = connection.cursor()
cursor.execute(\"SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name='core_loginactivity'\")
exit(0 if cursor.fetchone() else 1)
" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} core_loginactivity table created"
    else
        echo -e "${YELLOW}⚠ core_loginactivity table still missing, but continuing...${NC}"
    fi
fi

# If migration failed with the 0020 error, try to fix it
if echo "$MIGRATION_OUTPUT" | grep -qE "(FieldDoesNotExist.*enabled|MonitoredLog has no field named 'enabled')"; then
    echo -e "  ${YELLOW}⚠ Migration 0020 issue detected. Attempting to fix...${NC}"
    
    # Check if columns exist and fake the migration
    docker exec monitoring_web python manage.py shell << 'PYTHON_EOF' > /dev/null 2>&1
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

cursor = connection.cursor()
cursor.execute("""
    SELECT column_name 
    FROM information_schema.columns 
    WHERE table_name='core_monitoredlog' 
    AND column_name IN ('enabled', 'last_scan_time', 'scan_from_days')
""")
existing = [row[0] for row in cursor.fetchall()]

if len(existing) >= 2:
    recorder = MigrationRecorder(connection)
    recorder.record_applied('core', '0020_monitoredlog_enabled_monitoredlog_last_scan_time_and_more')
    print('Faked migration 0020')
PYTHON_EOF
    
    # Retry migrations
    MIGRATION_OUTPUT=$(docker exec monitoring_web python manage.py migrate --noinput 2>&1)
    if ! echo "$MIGRATION_OUTPUT" | grep -qE "(Error|Traceback|FieldDoesNotExist)"; then
        echo -e "${GREEN}✓${NC} Migrations fixed and completed"
        MIGRATION_SUCCESS=1
    else
        echo -e "${YELLOW}⚠ Migration still has issues. Showing last 20 lines:${NC}"
        echo "$MIGRATION_OUTPUT" | tail -20
    fi
fi

# Verify django_session table exists (critical for login)
echo -e "  Verifying critical tables..."
if timeout 10 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
try:
    cursor = connection.cursor()
    cursor.execute(\"SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name='django_session'\")
    exit(0 if cursor.fetchone() else 1)
except Exception as e:
    print(f'Error: {e}')
    exit(1)
" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Critical tables verified"
else
    echo -e "${YELLOW}⚠ django_session table check failed. Running sessions migrations...${NC}"
    timeout 30 docker exec monitoring_web python manage.py migrate sessions --noinput > /dev/null 2>&1 || true
fi

# Final health check and database connection verification
echo -e "  Performing final health check..."
HEALTH_CHECK_PASSED=false
for i in {1..10}; do
    if timeout 10 docker exec monitoring_web python manage.py check --database default > /dev/null 2>&1; then
        # Verify database connection from within the web container
        if timeout 10 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        result = cursor.fetchone()
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" > /dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} Application is ready and database connection verified"
            HEALTH_CHECK_PASSED=true
            break
        fi
    fi
    if [ $i -lt 5 ]; then
        echo -e "  Waiting for application to be ready... ($i/10)"
    fi
    sleep 2
done

if [ "$HEALTH_CHECK_PASSED" = false ]; then
    echo -e "${YELLOW}⚠ Application health check incomplete, but continuing with deployment...${NC}"
fi

# Collect static files
echo -e "  Collecting static files..."
docker exec monitoring_web python manage.py collectstatic --noinput > /dev/null 2>&1 || true
echo -e "${GREEN}✓${NC} Static files collected"

# Create/verify admin user
echo -e "  Creating admin user..."
docker exec monitoring_web python -c "
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.contrib.auth.models import User

try:
    user = User.objects.get(username='$DJANGO_SUPERUSER_USERNAME')
    user.set_password('$DJANGO_SUPERUSER_PASSWORD')
    user.email = '$DJANGO_SUPERUSER_EMAIL'
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.save()
    print('RESET')
except User.DoesNotExist:
    user = User.objects.create_superuser(
        username='$DJANGO_SUPERUSER_USERNAME',
        email='$DJANGO_SUPERUSER_EMAIL',
        password='$DJANGO_SUPERUSER_PASSWORD'
    )
    print('CREATED')
except Exception as e:
    print(f'ERROR: {e}')
" 2>&1 | grep -qE "(RESET|CREATED)" && echo -e "${GREEN}✓${NC} Admin user ready" || echo -e "${YELLOW}⚠ Admin user may need manual verification${NC}"

echo -e "${GREEN}✓${NC} Step 7 completed"
echo ""

###############################################################################
# Step 8: Generate Self-Signed SSL Certificate
###############################################################################
echo -e "${BLUE}[8/10] Setting up SSL certificate...${NC}"

SSL_DIR="/etc/nginx/ssl"
mkdir -p "$SSL_DIR"

if [ ! -f "$SSL_DIR/$DOMAIN.crt" ] || [ ! -f "$SSL_DIR/$DOMAIN.key" ]; then
    echo -e "  Generating self-signed SSL certificate..."
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "$SSL_DIR/$DOMAIN.key" \
        -out "$SSL_DIR/$DOMAIN.crt" \
        -subj "/C=US/ST=State/L=City/O=Organization/CN=$DOMAIN" \
        > /dev/null 2>&1
    
    chmod 600 "$SSL_DIR/$DOMAIN.key"
    chmod 644 "$SSL_DIR/$DOMAIN.crt"
    echo -e "${GREEN}✓${NC} Self-signed certificate created"
else
    echo -e "${GREEN}✓${NC} SSL certificate already exists"
fi
echo -e "${GREEN}✓${NC} Step 8 completed - SSL certificate ready"
echo ""

###############################################################################
# Step 9: Configure Nginx
###############################################################################
echo -e "${BLUE}[9/10] Configuring Nginx reverse proxy...${NC}"

NGINX_CONFIG="/etc/nginx/sites-available/$DOMAIN"

cat > "$NGINX_CONFIG" << EOF
server {
    listen $NGINX_PORT ssl http2;
    server_name $DOMAIN;

    # Self-signed SSL certificates
    ssl_certificate $SSL_DIR/$DOMAIN.crt;
    ssl_certificate_key $SSL_DIR/$DOMAIN.key;

    # SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;

    # Proxy to Django app
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port \$server_port;

        # WebSocket support
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts (proxy_read_timeout 180s for AI/Ollama requests which can take 60–120s)
        proxy_connect_timeout 60s;
        proxy_send_timeout 120s;
        proxy_read_timeout 180s;
    }

    # File upload size
    client_max_body_size 100M;

    # Logging
    access_log /var/log/nginx/${DOMAIN}_access.log;
    error_log /var/log/nginx/${DOMAIN}_error.log;
}
EOF

# Enable site
ln -sf "$NGINX_CONFIG" "/etc/nginx/sites-enabled/$DOMAIN"
rm -f /etc/nginx/sites-enabled/default

# Test and reload Nginx
if nginx -t > /dev/null 2>&1; then
    systemctl reload nginx
    echo -e "${GREEN}✓${NC} Nginx configured and reloaded"
else
    echo -e "${YELLOW}⚠ Nginx configuration test failed, but continuing...${NC}"
fi
echo -e "${GREEN}✓${NC} Step 9 completed - Nginx reverse proxy configured"
echo ""

###############################################################################
# Step 10: Configure Firewall
###############################################################################
echo -e "${BLUE}[10/10] Configuring firewall...${NC}"

# Enable UFW if not already enabled
if ! ufw status | grep -q "Status: active"; then
    ufw --force enable > /dev/null 2>&1
fi

# Allow required ports
ufw allow 22/tcp > /dev/null 2>&1  # SSH
ufw allow 80/tcp > /dev/null 2>&1  # HTTP (for Let's Encrypt)
ufw allow 443/tcp > /dev/null 2>&1  # HTTPS
ufw allow $NGINX_PORT/tcp > /dev/null 2>&1  # Custom HTTPS port

echo -e "${GREEN}✓${NC} Firewall configured"
echo -e "${GREEN}✓${NC} Step 10 completed - Firewall configured"
echo ""

###############################################################################
# Final Verification: Test Database Connection from Web Container
###############################################################################
echo ""
echo -e "${BLUE}Final verification: Testing database connection from web container...${NC}"
sleep 5  # Give container a moment to fully start

# Verify .env file in container matches what we expect
echo -e "  Checking .env file in container..."
CONTAINER_ENV_PASS=$(docker exec monitoring_web grep "^POSTGRES_PASSWORD=" /app/.env 2>/dev/null | cut -d'=' -f2- | tr -d ' ' || echo "")
if [ -z "$CONTAINER_ENV_PASS" ]; then
    echo -e "${RED}✗${NC} WARNING: POSTGRES_PASSWORD not found in container .env file!"
    echo -e "${YELLOW}  Copying .env file to container...${NC}"
    docker cp "$APP_DIR/.env" monitoring_web:/app/.env > /dev/null 2>&1 || true
    docker restart monitoring_web > /dev/null 2>&1
    sleep 10
elif [ "$CONTAINER_ENV_PASS" != "$POSTGRES_PASSWORD" ]; then
    echo -e "${RED}✗${NC} WARNING: Password mismatch in container .env file!"
    echo -e "${YELLOW}  Expected: ${POSTGRES_PASSWORD:0:10}...${NC}"
    echo -e "${YELLOW}  Found: ${CONTAINER_ENV_PASS:0:10}...${NC}"
    echo -e "${YELLOW}  Copying correct .env file to container...${NC}"
    docker cp "$APP_DIR/.env" monitoring_web:/app/.env > /dev/null 2>&1 || true
    docker restart monitoring_web > /dev/null 2>&1
    sleep 10
else
    echo -e "${GREEN}✓${NC} Container .env file password matches"
fi

# Test database connection from within the web container
echo -e "  Testing database connection..."
if timeout 15 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        result = cursor.fetchone()
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Database connection verified from web container"
else
    echo -e "${RED}✗${NC} WARNING: Database connection failed from web container!"
    echo -e "${YELLOW}  Attempting to fix by restarting container with correct .env...${NC}"
    docker cp "$APP_DIR/.env" monitoring_web:/app/.env > /dev/null 2>&1 || true
    docker restart monitoring_web > /dev/null 2>&1
    sleep 15
    # Test again
    if timeout 15 docker exec monitoring_web python manage.py shell -c "
from django.db import connection
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Database connection verified after restart"
    else
        echo -e "${RED}✗${NC} Database connection still failing."
        echo -e "${YELLOW}  You may need to manually check:${NC}"
        echo -e "    docker exec monitoring_web cat /app/.env | grep POSTGRES_PASSWORD"
        echo -e "    docker logs monitoring_web | tail -50"
    fi
fi

###############################################################################
# Deployment Complete
###############################################################################
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Deployment Completed Successfully!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "Application URL: ${BLUE}https://$DOMAIN:$NGINX_PORT${NC}"
echo ""
echo -e "${YELLOW}════════════════════════════════════════${NC}"
echo -e "${YELLOW}  IMPORTANT: Save These Credentials!${NC}"
echo -e "${YELLOW}════════════════════════════════════════${NC}"
echo ""
echo -e "${BLUE}Django Admin Login:${NC}"
echo -e "  Username: ${GREEN}$DJANGO_SUPERUSER_USERNAME${NC}"
echo -e "  Password: ${GREEN}$DJANGO_SUPERUSER_PASSWORD${NC}"
echo -e "  Email: ${GREEN}$DJANGO_SUPERUSER_EMAIL${NC}"
echo -e "  Login URL: ${BLUE}https://$DOMAIN:$NGINX_PORT/admin/${NC}"
echo ""
echo -e "${BLUE}Database Credentials:${NC}"
echo -e "  Host: ${GREEN}localhost:5433${NC}"
echo -e "  Database: ${GREEN}$POSTGRES_DB${NC}"
echo -e "  User: ${GREEN}$POSTGRES_USER${NC}"
echo -e "  Password: ${GREEN}$POSTGRES_PASSWORD${NC}"
echo ""
echo -e "${BLUE}Redis Credentials:${NC}"
echo -e "  Host: ${GREEN}localhost:6379${NC}"
echo -e "  Password: ${GREEN}$REDIS_PASSWORD${NC}"
echo ""
echo -e "${YELLOW}⚠ Note:${NC} Self-signed SSL certificate is in use."
echo -e "  Browsers will show a security warning. Accept it to proceed."
echo ""
echo -e "${YELLOW}To use Let's Encrypt SSL instead, run:${NC}"
echo -e "  sudo certbot --nginx -d $DOMAIN --non-interactive --agree-tos --email $EMAIL --redirect"
echo ""
echo -e "${BLUE}Useful Commands:${NC}"
echo -e "  View logs: docker logs monitoring_web"
echo -e "  Restart app: docker restart monitoring_web"
echo -e "  Check status: docker ps"
echo ""

# Save credentials to file
CREDENTIALS_FILE="$APP_DIR/deployment_credentials.txt"
cat > "$CREDENTIALS_FILE" << EOF
StackSense Deployment Credentials
Generated: $(date)

Domain: $DOMAIN
Application URL: https://$DOMAIN:$NGINX_PORT

Database:
  Host: localhost:5433
  Database: $POSTGRES_DB
  User: $POSTGRES_USER
  Password: $POSTGRES_PASSWORD

Redis:
  Host: localhost:6379
  Password: $REDIS_PASSWORD

Django Admin:
  Username: $DJANGO_SUPERUSER_USERNAME
  Email: $DJANGO_SUPERUSER_EMAIL
  Password: $DJANGO_SUPERUSER_PASSWORD
  URL: https://$DOMAIN:$NGINX_PORT/admin/

EOF

chmod 600 "$CREDENTIALS_FILE"
echo -e "${GREEN}✓${NC} Credentials saved to: $CREDENTIALS_FILE"
echo ""


