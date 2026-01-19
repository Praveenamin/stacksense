#!/bin/bash
# Comprehensive admin user fix script

echo "=========================================="
echo "Fixing Admin Login"
echo "=========================================="
echo ""

# Generate a new password
NEW_PASSWORD=$(openssl rand -base64 16)
echo "Generated new admin password: $NEW_PASSWORD"
echo ""

# Check database connection first
echo "Step 1: Checking database connection..."
if ! docker exec monitoring_web python manage.py shell -c "
from django.db import connection
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" > /dev/null 2>&1; then
    echo "✗ Database connection failed!"
    echo "  This is why login isn't working."
    exit 1
fi
echo "✓ Database connection OK"
echo ""

# Check if admin user exists
echo "Step 2: Checking admin user..."
ADMIN_EXISTS=$(docker exec monitoring_web python manage.py shell -c "
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.contrib.auth.models import User

try:
    user = User.objects.get(username='admin')
    print('EXISTS')
except User.DoesNotExist:
    print('NOT_EXISTS')
" 2>&1 | grep -E "(EXISTS|NOT_EXISTS)")

if echo "$ADMIN_EXISTS" | grep -q "NOT_EXISTS"; then
    echo "  Admin user does not exist. Creating..."
    CREATE_RESULT=$(docker exec monitoring_web python manage.py shell << PYTHON_EOF
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.contrib.auth.models import User

try:
    user = User.objects.create_superuser(
        username='admin',
        email='admin@example.com',
        password='$NEW_PASSWORD'
    )
    print('CREATED')
except Exception as e:
    print(f'ERROR: {e}')
PYTHON_EOF
)
    
    if echo "$CREATE_RESULT" | grep -q "CREATED"; then
        echo "✓ Admin user created"
    else
        echo "✗ Failed to create admin user:"
        echo "$CREATE_RESULT"
        exit 1
    fi
else
    echo "  Admin user exists. Resetting password..."
    RESET_RESULT=$(docker exec monitoring_web python manage.py shell << PYTHON_EOF
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.contrib.auth.models import User

try:
    user = User.objects.get(username='admin')
    user.set_password('$NEW_PASSWORD')
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.save()
    print('RESET')
except Exception as e:
    print(f'ERROR: {e}')
PYTHON_EOF
)
    
    if echo "$RESET_RESULT" | grep -q "RESET"; then
        echo "✓ Admin password reset"
    else
        echo "✗ Failed to reset password:"
        echo "$RESET_RESULT"
        exit 1
    fi
fi
echo ""

# Verify the password works
echo "Step 3: Verifying password..."
VERIFY_RESULT=$(docker exec monitoring_web python manage.py shell << PYTHON_EOF
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.contrib.auth import authenticate

user = authenticate(username='admin', password='$NEW_PASSWORD')
if user and user.is_active:
    print('VERIFIED')
else:
    print('FAILED')
PYTHON_EOF
)

if echo "$VERIFY_RESULT" | grep -q "VERIFIED"; then
    echo "✓ Password verification successful"
else
    echo "✗ Password verification failed!"
    exit 1
fi
echo ""

echo "=========================================="
echo "✓ Admin Login Fixed!"
echo "=========================================="
echo ""
echo "Login Credentials:"
echo "  Username: admin"
echo "  Password: $NEW_PASSWORD"
echo "  URL: https://dev-stacksense.assistanz.com:8005/admin/"
echo ""
echo "Save this password!"
echo "=========================================="

