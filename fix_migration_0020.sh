#!/bin/bash
# Fix migration 0020 - fake it if columns exist

echo "Fixing migration 0020..."

# Check if columns exist and fake the migration
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

print(f'Found {len(existing_columns)} existing columns: {existing_columns}')

# If columns exist but migration not applied, fake it
if len(existing_columns) >= 2:
    if migration_key not in applied:
        print('Faking migration 0020...')
        recorder.record_applied('core', '0020_monitoredlog_enabled_monitoredlog_last_scan_time_and_more')
        print('✓ Migration 0020 faked successfully')
    else:
        print('✓ Migration 0020 already applied')
else:
    print('Columns do not exist, migration should run normally')
PYTHON_EOF

# Now run migrations
echo "Running migrations..."
docker exec monitoring_web python manage.py migrate --noinput

echo "✓ Done!"

