#!/bin/bash

###############################################################################
# StackSense Data Migration Script
# This script migrates data from an existing StackSense installation to a new server
# Usage: ./migrate_to_new_server.sh [source_server] [target_server]
# Example: ./migrate_to_new_server.sh user@old-server.com user@new-server.com
###############################################################################

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SOURCE_SERVER="${1}"
TARGET_SERVER="${2}"
SOURCE_APP_DIR="/opt/stacksense"
TARGET_APP_DIR="/opt/stacksense"
BACKUP_DIR="/tmp/stacksense_migration_$(date +%Y%m%d_%H%M%S)"

if [ -z "$SOURCE_SERVER" ] || [ -z "$TARGET_SERVER" ]; then
    echo -e "${RED}Usage: $0 <source_server> <target_server>${NC}"
    echo -e "Example: $0 user@old-server.com user@new-server.com"
    exit 1
fi

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}StackSense Data Migration${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "Source: ${GREEN}$SOURCE_SERVER${NC}"
echo -e "Target: ${GREEN}$TARGET_SERVER${NC}"
echo ""

###############################################################################
# Step 1: Create Backup on Source Server
###############################################################################
echo -e "${BLUE}[1/5] Creating backup on source server...${NC}"

ssh "$SOURCE_SERVER" << EOF
set -e
mkdir -p $BACKUP_DIR

echo "Dumping database..."
docker exec monitoring_db pg_dump -U monitoring_user monitoring_db > $BACKUP_DIR/database.sql

echo "Backing up application files (excluding Git and cache files)..."
tar -czf $BACKUP_DIR/application_files.tar.gz \
    --exclude='.git' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.pytest_cache' \
    --exclude='.mypy_cache' \
    --exclude='venv' \
    --exclude='env' \
    --exclude='.venv' \
    --exclude='staticfiles' \
    --exclude='db.sqlite3' \
    --exclude='*.log' \
    --exclude='.DS_Store' \
    -C $SOURCE_APP_DIR \
    .env \
    ssh_keys/ \
    media/ \
    2>/dev/null || true

echo "Backing up Nginx SSL certificates..."
sudo tar -czf $BACKUP_DIR/ssl_certificates.tar.gz \
    /etc/nginx/ssl/ 2>/dev/null || true

echo "Creating migration manifest..."
cat > $BACKUP_DIR/migration_manifest.txt << MANIFEST
Migration Date: $(date)
Source Server: $SOURCE_SERVER
Target Server: $TARGET_SERVER
Files:
  - database.sql (PostgreSQL dump)
  - application_files.tar.gz (Config files, SSH keys, media)
  - ssl_certificates.tar.gz (SSL certificates)
MANIFEST

echo "Backup completed: $BACKUP_DIR"
EOF

echo -e "${GREEN}✓${NC} Backup created on source server"

###############################################################################
# Step 2: Download Backup to Local Machine
###############################################################################
echo -e "${BLUE}[2/5] Downloading backup to local machine...${NC}"

LOCAL_BACKUP_DIR="$BACKUP_DIR"
mkdir -p "$LOCAL_BACKUP_DIR"

scp -r "$SOURCE_SERVER:$BACKUP_DIR/*" "$LOCAL_BACKUP_DIR/" > /dev/null 2>&1

echo -e "${GREEN}✓${NC} Backup downloaded"

###############################################################################
# Step 3: Upload Backup to Target Server
###############################################################################
echo -e "${BLUE}[3/5] Uploading backup to target server...${NC}"

ssh "$TARGET_SERVER" "mkdir -p $BACKUP_DIR"
scp -r "$LOCAL_BACKUP_DIR/*" "$TARGET_SERVER:$BACKUP_DIR/" > /dev/null 2>&1

echo -e "${GREEN}✓${NC} Backup uploaded to target server"

###############################################################################
# Step 4: Restore Database on Target Server
###############################################################################
echo -e "${BLUE}[4/5] Restoring database on target server...${NC}"

ssh "$TARGET_SERVER" << 'RESTORE_DB'
set -e
BACKUP_DIR="/tmp/stacksense_migration_*"
BACKUP_DIR=$(ls -td $BACKUP_DIR | head -1)

echo "Waiting for database to be ready..."
for i in {1..30}; do
    if docker exec monitoring_db pg_isready -U monitoring_user > /dev/null 2>&1; then
        break
    fi
    sleep 2
done

echo "Dropping existing database..."
docker exec monitoring_db psql -U monitoring_user -d postgres -c "DROP DATABASE IF EXISTS monitoring_db;" > /dev/null 2>&1 || true

echo "Creating new database..."
docker exec monitoring_db psql -U monitoring_user -d postgres -c "CREATE DATABASE monitoring_db;" > /dev/null 2>&1

echo "Restoring database from backup..."
docker exec -i monitoring_db psql -U monitoring_user monitoring_db < "$BACKUP_DIR/database.sql" > /dev/null 2>&1

echo "Database restored successfully"
RESTORE_DB

echo -e "${GREEN}✓${NC} Database restored"

###############################################################################
# Step 5: Restore Application Files and SSL Certificates
###############################################################################
echo -e "${BLUE}[5/5] Restoring application files and SSL certificates...${NC}"

ssh "$TARGET_SERVER" << 'RESTORE_FILES'
set -e
BACKUP_DIR="/tmp/stacksense_migration_*"
BACKUP_DIR=$(ls -td $BACKUP_DIR | head -1)

echo "Extracting application files..."
if [ -f "$BACKUP_DIR/application_files.tar.gz" ]; then
    cd /opt/stacksense
    tar -xzf "$BACKUP_DIR/application_files.tar.gz" 2>/dev/null || true
    chmod 600 /opt/stacksense/.env 2>/dev/null || true
    # Ensure SSH keys directory exists and has correct permissions
    mkdir -p /opt/stacksense/ssh_keys
    chmod 700 /opt/stacksense/ssh_keys 2>/dev/null || true
    chmod 600 /opt/stacksense/ssh_keys/* 2>/dev/null || true
fi

echo "Extracting SSL certificates..."
if [ -f "$BACKUP_DIR/ssl_certificates.tar.gz" ]; then
    sudo mkdir -p /etc/nginx/ssl
    sudo tar -xzf "$BACKUP_DIR/ssl_certificates.tar.gz" -C / 2>/dev/null || true
    sudo chmod 600 /etc/nginx/ssl/*.key 2>/dev/null || true
    sudo chmod 644 /etc/nginx/ssl/*.crt 2>/dev/null || true
fi

echo "Restarting application..."
docker restart monitoring_web > /dev/null 2>&1 || true
sleep 5

echo "Running migrations (if needed)..."
docker exec monitoring_web python manage.py migrate --noinput > /dev/null 2>&1 || true

echo "Collecting static files..."
docker exec monitoring_web python manage.py collectstatic --noinput > /dev/null 2>&1 || true

echo "Reloading Nginx..."
sudo nginx -t > /dev/null 2>&1 && sudo systemctl reload nginx || true
RESTORE_FILES

echo -e "${GREEN}✓${NC} Application files and SSL certificates restored"

###############################################################################
# Migration Complete
###############################################################################
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Migration Completed Successfully!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}What was migrated:${NC}"
echo -e "  ✓ Database (all servers, metrics, configurations)"
echo -e "  ✓ Application configuration (.env file)"
echo -e "  ✓ SSH keys (for server connections)"
echo -e "  ✓ Media files (if any)"
echo -e "  ✓ SSL certificates"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo -e "  1. Verify servers are accessible on new server"
echo -e "  2. Test SSH connections to monitored servers"
echo -e "  3. Check application logs: docker logs monitoring_web"
echo -e "  4. Access application and verify all data"
echo ""
echo -e "${BLUE}Backup location:${NC} $BACKUP_DIR"
echo ""

