#!/bin/bash

###############################################################################
# StackSense Complete Deployment Script
# This script deploys the entire StackSense monitoring application
# Usage: sudo ./deploy.sh [domain] [email]
# Example: sudo ./deploy.sh stacksense.assistanz.com admin@example.com
###############################################################################

set -e

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
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -base64 32)}"
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

if docker ps -a | grep -q "monitoring_db"; then
    echo -e "  Stopping existing database container..."
    docker stop monitoring_db > /dev/null 2>&1 || true
    docker rm monitoring_db > /dev/null 2>&1 || true
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
for i in {1..30}; do
    if docker exec monitoring_db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Database is ready"
        break
    fi
    sleep 1
done

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

# Create .env file for Django
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

# Stop existing web container if it exists
if docker ps -a | grep -q "monitoring_web"; then
    echo -e "  Stopping existing web container..."
    docker stop monitoring_web > /dev/null 2>&1 || true
    docker rm monitoring_web > /dev/null 2>&1 || true
fi

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
    sh -c "python manage.py migrate --noinput && \
           python manage.py collectstatic --noinput && \
           python manage.py createsuperuser --noinput || true && \
           nohup python3 metrics_scheduler.py > /tmp/metrics_scheduler.log 2>&1 & \
           python manage.py runserver 0.0.0.0:8000" > /dev/null 2>&1 || {
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
               python manage.py migrate --noinput && \
               python manage.py collectstatic --noinput && \
               python manage.py createsuperuser --noinput || true && \
               nohup python3 metrics_scheduler.py > /tmp/metrics_scheduler.log 2>&1 & \
               python manage.py runserver 0.0.0.0:8000" > /dev/null 2>&1
}

# Wait for application to be ready
echo -e "  Waiting for application to be ready..."
sleep 10
for i in {1..30}; do
    if docker exec monitoring_web python manage.py check > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Application is ready"
        break
    fi
    sleep 2
done

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

        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
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
nginx -t > /dev/null 2>&1
systemctl reload nginx

echo -e "${GREEN}✓${NC} Nginx configured"

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
echo -e "${YELLOW}Important Information:${NC}"
echo -e "  Database Password: ${GREEN}$POSTGRES_PASSWORD${NC}"
echo -e "  Redis Password: ${GREEN}$REDIS_PASSWORD${NC}"
echo -e "  Django Admin Username: ${GREEN}$DJANGO_SUPERUSER_USERNAME${NC}"
echo -e "  Django Admin Password: ${GREEN}$DJANGO_SUPERUSER_PASSWORD${NC}"
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


