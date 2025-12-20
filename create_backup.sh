#!/bin/bash

# StackSense Backup Script
# Creates a complete backup of the application, database, and configuration

set -e

# Configuration
BACKUP_DIR="/home/ubuntu/stacksense_backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_NAME="stacksense_backup_${TIMESTAMP}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

echo "🔄 Creating StackSense backup: $BACKUP_NAME"

# Stop containers to ensure consistent backup
echo "⏹️  Stopping containers..."
docker-compose -f docker-compose.yml down

# Create temporary backup directory
TEMP_BACKUP_DIR="${BACKUP_DIR}/temp_backup_${TIMESTAMP}"
mkdir -p "$TEMP_BACKUP_DIR"

# Copy application code
echo "📁 Copying application code..."
cp -r /home/ubuntu/stacksense-repo "$TEMP_BACKUP_DIR/application_code"

# Backup database
echo "💾 Backing up PostgreSQL database..."
docker run --rm \
  --network stacksense_default \
  -v stacksense_postgres_data:/var/lib/postgresql/data \
  -v "$TEMP_BACKUP_DIR":/backup \
  postgres:13-alpine \
  sh -c "pg_dump -h postgres -U stacksense_user -d stacksense_db > /backup/database.sql"

# Backup Redis data if needed
echo "🔴 Backing up Redis data..."
docker run --rm \
  --network stacksense_default \
  -v stacksense_redis_data:/data \
  -v "$TEMP_BACKUP_DIR":/backup \
  redis:7-alpine \
  sh -c "redis-cli -h redis SAVE && cp /data/dump.rdb /backup/redis_dump.rdb" || echo "Redis backup failed, continuing..."

# Copy environment files
echo "⚙️  Copying environment files..."
if [ -f /home/ubuntu/stacksense-repo/.env ]; then
    cp /home/ubuntu/stacksense-repo/.env "$TEMP_BACKUP_DIR/"
fi

# Copy SSH keys if they exist
echo "🔑 Copying SSH keys..."
if [ -d /home/ubuntu/.ssh ]; then
    cp -r /home/ubuntu/.ssh "$TEMP_BACKUP_DIR/ssh_keys"
fi

# Create backup manifest
echo "📋 Creating backup manifest..."
cat > "$TEMP_BACKUP_DIR/BACKUP_MANIFEST.txt" << EOF
StackSense Backup Manifest
Created: $(date)
Backup Name: $BACKUP_NAME
Version: 1.0

Contents:
- Application code (/home/ubuntu/stacksense-repo)
- PostgreSQL database dump
- Redis data dump (if available)
- Environment files (.env)
- SSH keys (~/.ssh)

Restore Instructions:
1. Extract backup: tar -xzf $BACKUP_NAME.tar.gz
2. Run restore script: ./restore_stacksense.sh
3. Or manually restore using the extracted files

Notes:
- This backup was created with containers stopped
- Database and Redis data are included
- SSH keys are backed up for server access
EOF

# Create compressed archive
echo "📦 Creating compressed archive..."
cd "$BACKUP_DIR"
tar -czf "$BACKUP_NAME.tar.gz" -C "$BACKUP_DIR" "temp_backup_${TIMESTAMP}"

# Cleanup
rm -rf "$TEMP_BACKUP_DIR"

# Restart containers
echo "▶️  Restarting containers..."
cd /home/ubuntu/stacksense-repo
docker-compose -f docker-compose.yml up -d

echo "✅ Backup completed successfully!"
echo "📁 Backup location: $BACKUP_PATH"
echo "📊 Backup size: $(du -h "$BACKUP_PATH" | cut -f1)"
echo ""
echo "To restore this backup, run:"
echo "  ./restore_stacksense.sh $BACKUP_PATH"







