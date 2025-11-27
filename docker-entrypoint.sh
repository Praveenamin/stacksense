#!/bin/bash
set -e

echo "Waiting for database to be ready..."
python << END
import sys
import time
import psycopg2
import os

# Get database connection details from environment
db_name = os.environ.get('POSTGRES_DB', 'monitoring_db')
db_user = os.environ.get('POSTGRES_USER', 'monitoring_user')
db_password = os.environ.get('POSTGRES_PASSWORD', 'monitoring_pass')
db_host = os.environ.get('POSTGRES_HOST', 'db')
db_port = os.environ.get('POSTGRES_PORT', '5432')

max_attempts = 30
attempt = 0

while attempt < max_attempts:
    try:
        conn = psycopg2.connect(
            dbname=db_name,
            user=db_user,
            password=db_password,
            host=db_host,
            port=db_port
        )
        conn.close()
        print("Database is ready!")
        sys.exit(0)
    except Exception as e:
        attempt += 1
        if attempt >= max_attempts:
            print(f"Database connection failed after {max_attempts} attempts: {e}")
            sys.exit(1)
        print(f"Waiting for database... ({attempt}/{max_attempts})")
        time.sleep(2)
END

echo "Running migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput --clear

# Create superuser if environment variable is set
if [ "$CREATE_SUPERUSER" = "true" ]; then
    echo "Creating superuser..."
    python << END
import os
import django
django.setup()
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username=os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')).exists():
    User.objects.create_superuser(
        username=os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin'),
        email=os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com'),
        password=os.environ.get('DJANGO_SUPERUSER_PASSWORD', 'admin')
    )
    print("Superuser created successfully")
else:
    print("Superuser already exists")
END
fi

# Start metrics collection scheduler in background
echo "Starting metrics collection scheduler..."
cd /app && nohup python3 metrics_scheduler.py > /tmp/metrics_scheduler.log 2>&1 &
echo "Metrics scheduler started (PID: $!)"

echo "Starting application..."
exec "$@"
