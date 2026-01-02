#!/bin/bash

###############################################################################
# StackSense Deployment Script v2 - Docker Compose Based
# This script deploys using docker-compose for better reliability
# Usage: sudo ./deploy-v2.sh [domain] [email]
# Example: sudo ./deploy-v2.sh stacksense.assistanz.com admin@example.com
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
APP_DIR="${APP_DIR:-$(pwd)}"
NGINX_PORT=8005

# Database configuration
POSTGRES_DB="${POSTGRES_DB:-monitoring_db}"
POSTGRES_USER="${POSTGRES_USER:-monitoring_user}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -base64 32)}"

# Django configuration
DJANGO_SUPERUSER_USERNAME="${DJANGO_SUPERUSER_USERNAME:-admin}"
DJANGO_SUPERUSER_EMAIL="${DJANGO_SUPERUSER_EMAIL:-$EMAIL}"
DJANGO_SUPERUSER_PASSWORD="${DJANGO_SUPERUSER_PASSWORD:-$(openssl rand -base64 16)}"
SECRET_KEY="${SECRET_KEY:-$(openssl rand -base64 50)}"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}StackSense Deployment Script v2${NC}"
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

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    echo -e "${YELLOW}docker-compose not found, installing...${NC}"
    apt-get update -qq
    apt-get install -y docker-compose > /dev/null 2>&1
fi

cd "$APP_DIR"

# Check if required files exist
if [ ! -f "Dockerfile" ]; then
    echo -e "${RED}Error: Dockerfile not found in $APP_DIR${NC}"
    exit 1
fi

if [ ! -f "docker-compose.yml" ]; then
    echo -e "${RED}Error: docker-compose.yml not found in $APP_DIR${NC}"
    exit 1
fi

###############################################################################
# Step 1: Create .env file
###############################################################################
echo -e "${BLUE}[1/6] Creating environment file...${NC}"

cat > "$APP_DIR/.env" << EOF
# Database
POSTGRES_DB=$POSTGRES_DB
POSTGRES_USER=$POSTGRES_USER
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_HOST=monitoring_db
POSTGRES_PORT=5432

# Redis
REDIS_URL=redis://monitoring_redis:6379/0

# Django
SECRET_KEY=$SECRET_KEY
DEBUG=False
ALLOWED_HOSTS=$DOMAIN,localhost,127.0.0.1,0.0.0.0,tracker.stackbill.com
CSRF_TRUSTED_ORIGINS=https://$DOMAIN:$NGINX_PORT,https://$DOMAIN,http://$DOMAIN:$NGINX_PORT,http://$DOMAIN,http://localhost:8000,https://localhost:8000
USE_TLS=True
BEHIND_PROXY=True
DJANGO_SUPERUSER_USERNAME=$DJANGO_SUPERUSER_USERNAME
DJANGO_SUPERUSER_EMAIL=$DJANGO_SUPERUSER_EMAIL
DJANGO_SUPERUSER_PASSWORD=$DJANGO_SUPERUSER_PASSWORD
CREATE_SUPERUSER=true
EOF

chmod 600 "$APP_DIR/.env"
echo -e "${GREEN}✓${NC} Environment file created"

###############################################################################
# Step 2: Stop existing containers
###############################################################################
echo -e "${BLUE}[2/6] Stopping existing containers...${NC}"

cd "$APP_DIR"
docker-compose down 2>/dev/null || true
docker stop monitoring_web monitoring_db monitoring_redis 2>/dev/null || true
docker rm monitoring_web monitoring_db monitoring_redis 2>/dev/null || true

echo -e "${GREEN}✓${NC} Existing containers stopped"

###############################################################################
# Step 3: Build and start containers
###############################################################################
echo -e "${BLUE}[3/6] Building and starting containers...${NC}"

cd "$APP_DIR"
docker-compose build --no-cache

echo -e "  Starting services..."
docker-compose up -d

# Wait for services to be healthy
echo -e "  Waiting for services to be ready..."
sleep 10

for i in {1..60}; do
    if docker exec monitoring_web python manage.py check > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Application is ready"
        break
    fi
    if [ $i -eq 60 ]; then
        echo -e "${YELLOW}⚠ Application may not be fully ready yet${NC}"
    fi
    sleep 2
done

# Create superuser if needed
echo -e "  Creating superuser..."
docker exec monitoring_web python manage.py createsuperuser --noinput 2>/dev/null || true

echo -e "${GREEN}✓${NC} Containers started"

###############################################################################
# Step 4: Setup SSL Certificate
###############################################################################
echo -e "${BLUE}[4/6] Setting up SSL certificate...${NC}"

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
# Step 5: Configure Nginx
###############################################################################
echo -e "${BLUE}[5/6] Configuring Nginx...${NC}"

NGINX_CONFIG="/etc/nginx/sites-available/$DOMAIN"

cat > "$NGINX_CONFIG" << EOF
server {
    listen $NGINX_PORT ssl http2;
    server_name $DOMAIN tracker.stackbill.com;

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
# Step 6: Configure Firewall
###############################################################################
echo -e "${BLUE}[6/6] Configuring firewall...${NC}"

# Enable UFW if not already enabled
if ! ufw status | grep -q "Status: active"; then
    ufw --force enable > /dev/null 2>&1
fi

# Allow required ports
ufw allow 22/tcp > /dev/null 2>&1  # SSH
ufw allow 80/tcp > /dev/null 2>&1  # HTTP
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
echo -e "  Django Admin Username: ${GREEN}$DJANGO_SUPERUSER_USERNAME${NC}"
echo -e "  Django Admin Password: ${GREEN}$DJANGO_SUPERUSER_PASSWORD${NC}"
echo ""
echo -e "${BLUE}Useful Commands:${NC}"
echo -e "  View logs: ${GREEN}docker-compose logs -f${NC}"
echo -e "  Restart: ${GREEN}docker-compose restart${NC}"
echo -e "  Stop: ${GREEN}docker-compose down${NC}"
echo -e "  Start: ${GREEN}docker-compose up -d${NC}"
echo ""

# Save credentials
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

Django Admin:
  Username: $DJANGO_SUPERUSER_USERNAME
  Email: $DJANGO_SUPERUSER_EMAIL
  Password: $DJANGO_SUPERUSER_PASSWORD
  URL: https://$DOMAIN:$NGINX_PORT/admin/
EOF

chmod 600 "$CREDENTIALS_FILE"
echo -e "${GREEN}✓${NC} Credentials saved to: $CREDENTIALS_FILE"
echo ""

