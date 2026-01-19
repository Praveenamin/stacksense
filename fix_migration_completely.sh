#!/bin/bash
# Complete fix for migration 0011 - drops all conflicting columns and fakes the migration

echo "Fixing migration 0011 conflicts..."

# Drop all columns that migration 0011 tries to add
echo "Dropping conflicting columns..."
docker exec -it monitoring_db \
  psql -U monitoring_user -d monitoring_db \
  -c 'ALTER TABLE core_server DROP COLUMN IF EXISTS suppress_alerts, DROP COLUMN IF EXISTS suspend_monitoring;'

# Fake the migration
echo "Faking migration 0011..."
docker exec monitoring_web python manage.py shell << 'PYTHON_EOF'
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

cursor = connection.cursor()
recorder = MigrationRecorder(connection)

# Check if columns exist
cursor.execute("""
    SELECT column_name 
    FROM information_schema.columns 
    WHERE table_name='core_server' 
    AND column_name IN ('suppress_alerts', 'suspend_monitoring')
""")
existing_columns = [row[0] for row in cursor.fetchall()]

# Check if migration is already applied
applied = recorder.applied_migrations()
migration_key = ('core', '0011_add_server_toggles')

# If migration is marked as applied, unmark it first
if migration_key in applied:
    print('Unmarking migration 0011...')
    with connection.cursor() as c:
        c.execute("DELETE FROM django_migrations WHERE app='core' AND name='0011_add_server_toggles'")

# Now fake it (mark as applied without running)
print('Faking migration 0011...')
recorder.record_applied('core', '0011_add_server_toggles')
print('✓ Migration 0011 faked successfully')
PYTHON_EOF

# Now run migrations - it should skip 0011 and continue with others
echo "Running remaining migrations..."
docker exec monitoring_web python manage.py migrate core --noinput

echo "✓ Migration fix complete!"

