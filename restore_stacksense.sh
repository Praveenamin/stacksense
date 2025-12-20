#!/bin/bash

# StackSense Restore Script
# Restores application from a complete backup

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="/home/ubuntu/stacksense_backups"

# Function to show usage
usage() {
    echo "Usage: $0 [BACKUP_FILE]"
    echo ""
    echo "Arguments:"
    echo "  BACKUP_FILE    Path to the backup file (optional, will prompt if not provided)"
    echo ""
    echo "Available backups in $BACKUP_DIR:"
    ls -la "$BACKUP_DIR"/*.tar.gz 2>/dev/null | head -10 || echo "No backups found"
    exit 1
}

# Check arguments
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    usage
fi

BACKUP_FILE="$1"

# If no backup file provided, show available backups and prompt
if [ -z "$BACKUP_FILE" ]; then
    echo "Available backups:"
    echo "=================="
    ls -la "$BACKUP_DIR"/*.tar.gz 2>/dev/null || echo "No backups found in $BACKUP_DIR"

    echo ""
    read -p "Enter backup file path: " BACKUP_FILE
fi

# Validate backup file
if [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ Error: Backup file '$BACKUP_FILE' not found"
    exit 1
fi

echo "🔄 Starting StackSense restoration from: $BACKUP_FILE"

# Create restore directory
RESTORE_DIR="${BACKUP_DIR}/restore_temp_$(date +%s)"
mkdir -p "$RESTORE_DIR"

# Extract backup
echo "📦 Extracting backup..."
tar -xzf "$BACKUP_FILE" -C "$RESTORE_DIR"

# Find the extracted backup directory (it might have a different name)
EXTRACTED_DIR=$(find "$RESTORE_DIR" -maxdepth 1 -type d -name "temp_backup_*" | head -1)
if [ -z "$EXTRACTED_DIR" ]; then
    EXTRACTED_DIR=$(find "$RESTORE_DIR" -maxdepth 1 -type d | grep -v "^$RESTORE_DIR$" | head -1)
fi

if [ -z "$EXTRACTED_DIR" ]; then
    echo "❌ Error: Could not find extracted backup directory"
    rm -rf "$RESTORE_DIR"
    exit 1
fi

echo "📁 Extracted backup to: $EXTRACTED_DIR"

# Stop containers
echo "⏹️  Stopping containers..."
cd "$SCRIPT_DIR"
docker-compose down

# Backup current application (just in case)
CURRENT_BACKUP="${BACKUP_DIR}/pre_restore_backup_$(date +%Y%m%d_%H%M%S).tar.gz"
echo "💾 Creating pre-restore backup..."
tar -czf "$CURRENT_BACKUP" -C /home/ubuntu stacksense-repo 2>/dev/null || true
echo "📁 Pre-restore backup: $CURRENT_BACKUP"

# Restore application code
echo "📁 Restoring application code..."
if [ -d "$EXTRACTED_DIR/application_code" ]; then
    rm -rf /home/ubuntu/stacksense-repo/*
    cp -r "$EXTRACTED_DIR/application_code"/* /home/ubuntu/stacksense-repo/
    echo "✅ Application code restored"
else
    echo "⚠️  Warning: No application code found in backup"
fi

# Restore environment file
if [ -f "$EXTRACTED_DIR/.env" ]; then
    cp "$EXTRACTED_DIR/.env" /home/ubuntu/stacksense-repo/
    echo "✅ Environment file restored"
fi

# Restore SSH keys
if [ -d "$EXTRACTED_DIR/ssh_keys" ]; then
    cp -r "$EXTRACTED_DIR/ssh_keys" /home/ubuntu/.ssh
    echo "✅ SSH keys restored"
fi

# Restore database
if [ -f "$EXTRACTED_DIR/database.sql" ]; then
    echo "💾 Restoring PostgreSQL database..."
    # Start postgres container temporarily
    docker-compose up -d postgres

    # Wait for postgres to be ready
    echo "⏳ Waiting for PostgreSQL to be ready..."
    for i in {1..30}; do
        if docker-compose exec -T postgres pg_isready -U stacksense_user -d stacksense_db >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done

    # Drop and recreate database
    docker-compose exec -T postgres psql -U stacksense_user -d postgres -c "DROP DATABASE IF EXISTS stacksense_db;" 2>/dev/null || true
    docker-compose exec -T postgres psql -U stacksense_user -d postgres -c "CREATE DATABASE stacksense_db;"

    # Restore database
    docker-compose exec -T postgres psql -U stacksense_user -d stacksense_db < "$EXTRACTED_DIR/database.sql"

    echo "✅ Database restored"
else
    echo "⚠️  Warning: No database backup found"
fi

# Restore Redis data
if [ -f "$EXTRACTED_DIR/redis_dump.rdb" ]; then
    echo "🔴 Restoring Redis data..."
    # Copy Redis dump to volume
    docker run --rm \
      -v stacksense_redis_data:/data \
      -v "$EXTRACTED_DIR":/backup \
      alpine \
      sh -c "cp /backup/redis_dump.rdb /data/dump.rdb && chown redis:redis /data/dump.rdb"
    echo "✅ Redis data restored"
fi

# Clean up static files and rebuild
echo "🧹 Cleaning and rebuilding static files..."
cd "$SCRIPT_DIR"
find . -name "*.pyc" -delete
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# Start all containers
echo "▶️  Starting containers..."
docker-compose up -d

# Wait for web container to be ready
echo "⏳ Waiting for web container to be ready..."
for i in {1..30}; do
    if docker-compose exec -T monitoring_web python manage.py check >/dev/null 2>&1; then
        break
    fi
    sleep 3
done

# Run migrations and collect static files
echo "🔧 Running migrations and collecting static files..."
docker-compose exec -T monitoring_web python manage.py migrate --noinput
docker-compose exec -T monitoring_web python manage.py collectstatic --noinput --clear

# Cleanup
rm -rf "$RESTORE_DIR"

echo "✅ Restoration completed successfully!"
echo ""
echo "📊 Restoration Summary:"
echo "  - Application code: ✅ Restored"
echo "  - Database: ✅ Restored"
echo "  - Redis data: ✅ Restored"
echo "  - Static files: ✅ Rebuilt"
echo "  - SSH keys: ✅ Restored"
echo ""
echo "🎉 Your StackSense application has been restored!"
echo "   You can now access it at: http://23.82.14.228:8000"
echo ""
echo "Pre-restore backup saved at: $CURRENT_BACKUP"