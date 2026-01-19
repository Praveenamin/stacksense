#!/bin/bash
# Fix migration 0011_add_server_toggles by faking it if column already exists

docker exec monitoring_web python manage.py shell << 'PYTHON_EOF'
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
column_exists = cursor.fetchone() is not None

# Check if migration is already applied
applied = recorder.applied_migrations()
migration_key = ('core', '0011_add_server_toggles')

if column_exists and migration_key not in applied:
    print(f'Column suppress_alerts exists but migration not applied. Faking migration...')
    recorder.record_applied('core', '0011_add_server_toggles')
    print('✓ Migration 0011_add_server_toggles faked successfully')
elif column_exists and migration_key in applied:
    print('✓ Migration 0011_add_server_toggles already applied')
elif not column_exists:
    print('Column suppress_alerts does not exist. Migration should run normally.')
else:
    print('Unknown state')
PYTHON_EOF

echo ""
echo "Now running migrations..."
docker exec monitoring_web python manage.py migrate core --noinput

