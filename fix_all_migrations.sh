#!/bin/bash
# Complete fix for all migration conflicts

echo "Fixing all migration conflicts..."

# Step 1: Check what columns exist in core_monitoredlog
echo "Checking existing columns in core_monitoredlog..."
docker exec monitoring_db \
  psql -U monitoring_user -d monitoring_db \
  -c "SELECT column_name FROM information_schema.columns WHERE table_name='core_monitoredlog' AND column_name IN ('enabled', 'last_scan_time', 'scan_from_days', 'service_type');"

# Step 2: Fake migration 0020 if columns already exist
echo "Faking migration 0020 if needed..."
docker exec monitoring_web python manage.py shell << 'PYTHON_EOF'
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

cursor = connection.cursor()
recorder = MigrationRecorder(connection)

# Check if migration 0020 columns exist
cursor.execute("""
    SELECT column_name 
    FROM information_schema.columns 
    WHERE table_name='core_monitoredlog' 
    AND column_name IN ('enabled', 'last_scan_time', 'scan_from_days', 'service_type')
""")
existing_columns = [row[0] for row in cursor.fetchall()]

# Check if migration 0020 is already applied
applied = recorder.applied_migrations()
migration_key = ('core', '0020_monitoredlog_enabled_monitoredlog_last_scan_time_and_more')

# If columns exist but migration not applied, fake it
if len(existing_columns) >= 2 and migration_key not in applied:
    print(f'Found {len(existing_columns)} columns, faking migration 0020...')
    recorder.record_applied('core', '0020_monitoredlog_enabled_monitoredlog_last_scan_time_and_more')
    print('✓ Migration 0020 faked')
elif migration_key in applied:
    print('✓ Migration 0020 already applied')
else:
    print(f'Only {len(existing_columns)} columns found, migration should run normally')
PYTHON_EOF

# Step 3: Run migrations
echo "Running migrations..."
docker exec monitoring_web python manage.py migrate --noinput

echo "✓ All migrations fixed!"
