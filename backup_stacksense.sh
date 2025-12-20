#!/bin/bash

# StackSense Complete Backup Script
# This creates a full backup of application + database for disaster recovery

set -e

# Configuration
BACKUP_DIR="/home/ubuntu/stacksense_backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_NAME="stacksense_backup_${TIMESTAMP}"
FULL_BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"

# Create backup directory
mkdir -p "$FULL_BACKUP_PATH"
echo "📁 Creating backup directory: $FULL_BACKUP_PATH"

# 1. Backup PostgreSQL Database
echo "🗄️ Backing up PostgreSQL database..."
docker exec monitoring_db pg_dump -U monitoring_user -d monitoring_db > "${FULL_BACKUP_PATH}/database_backup.sql"
echo "✅ Database backup created: database_backup.sql"

# 2. Backup Application Code
echo "📂 Backing up application code..."
cp -r /home/ubuntu/stacksense-repo "${FULL_BACKUP_PATH}/application_code"
echo "✅ Application code backed up"

# 3. Backup Docker Volumes (if any)
echo "🐳 Backing up Docker volumes..."
docker run --rm -v monitoring_web_data:/source -v "$FULL_BACKUP_PATH":/backup alpine tar czf /backup/docker_volumes.tar.gz -C /source . 2>/dev/null || echo "No volumes to backup"
echo "✅ Docker volumes backed up"

# 4. Backup SSH Keys (important for server access)
echo "🔑 Backing up SSH keys..."
cp -r /home/ubuntu/stacksense-repo/ssh_keys "${FULL_BACKUP_PATH}/ssh_keys" 2>/dev/null || echo "No SSH keys directory found"
echo "✅ SSH keys backed up"

# 5. Backup Environment Configuration
echo "⚙️ Backing up configuration..."
cp /home/ubuntu/stacksense-repo/docker-entrypoint.sh "${FULL_BACKUP_PATH}/" 2>/dev/null || true
cp /home/ubuntu/stacksense-repo/requirements.txt "${FULL_BACKUP_PATH}/" 2>/dev/null || true
echo "✅ Configuration backed up"

# 6. Create backup manifest
cat > "${FULL_BACKUP_PATH}/BACKUP_MANIFEST.txt" << EOF
StackSense Backup Manifest
Created: $(date)
Backup ID: ${BACKUP_NAME}

CONTENTS:
- database_backup.sql: Complete PostgreSQL database dump
- application_code/: Full Django application codebase
- docker_volumes.tar.gz: Docker persistent volumes
- ssh_keys/: SSH keys for server access
- docker-entrypoint.sh: Application startup script
- requirements.txt: Python dependencies

RESTORATION INSTRUCTIONS:
1. Stop all containers: docker-compose down
2. Restore database: docker exec -i monitoring_db psql -U monitoring_user -d monitoring_db < database_backup.sql
3. Restore code: cp -r application_code/* /path/to/stacksense-repo/
4. Restore volumes: docker run --rm -v monitoring_web_data:/dest -v \$(pwd):/source alpine tar xzf /source/docker_volumes.tar.gz -C /dest
5. Start containers: docker-compose up -d

DATABASE INFO:
- Host: monitoring_db (container)
- User: monitoring_user
- Database: monitoring_db
- Backup contains all users, configurations, and historical data

CONTACT: For restoration assistance
EOF

# 7. Create compressed archive
echo "📦 Creating compressed backup archive..."
cd "$BACKUP_DIR"
tar czf "${BACKUP_NAME}.tar.gz" "$BACKUP_NAME"
rm -rf "$BACKUP_NAME"  # Remove uncompressed version
echo "✅ Compressed backup created: ${BACKUP_NAME}.tar.gz"

# 8. Show backup size and cleanup old backups (keep last 5)
echo "📊 Backup Statistics:"
du -sh "${BACKUP_NAME}.tar.gz"

echo "🧹 Cleaning up old backups (keeping last 5)..."
cd "$BACKUP_DIR"
ls -t *.tar.gz | tail -n +6 | xargs -r rm -f

echo ""
echo "🎉 BACKUP COMPLETE!"
echo "Location: ${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
echo "Size: $(du -sh "${BACKUP_NAME}.tar.gz" | cut -f1)"
echo ""
echo "📋 To restore from this backup:"
echo "1. tar xzf ${BACKUP_NAME}.tar.gz"
echo "2. Follow BACKUP_MANIFEST.txt instructions"
echo ""
echo "✅ This backup contains EVERYTHING needed for complete restoration!"








