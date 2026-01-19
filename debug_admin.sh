#!/bin/bash
# Debug admin login issue

echo "=== Debugging Admin Login ==="
echo ""

# Test 1: Database connection
echo "1. Testing database connection..."
docker exec monitoring_web python manage.py shell << 'PYTHON_EOF'
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.db import connection

try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    print('✓ Database connection works')
except Exception as e:
    print(f'✗ Database connection failed: {e}')
    import traceback
    traceback.print_exc()
PYTHON_EOF

echo ""

# Test 2: Check if admin user exists
echo "2. Checking if admin user exists..."
docker exec monitoring_web python manage.py shell << 'PYTHON_EOF'
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.contrib.auth.models import User

try:
    user = User.objects.get(username='admin')
    print(f'✓ Admin user exists')
    print(f'  ID: {user.id}')
    print(f'  Email: {user.email}')
    print(f'  Is staff: {user.is_staff}')
    print(f'  Is superuser: {user.is_superuser}')
    print(f'  Is active: {user.is_active}')
    print(f'  Has usable password: {user.has_usable_password()}')
except User.DoesNotExist:
    print('✗ Admin user does NOT exist')
except Exception as e:
    print(f'✗ Error checking user: {e}')
    import traceback
    traceback.print_exc()
PYTHON_EOF

echo ""

# Test 3: List all users
echo "3. Listing all users..."
docker exec monitoring_web python manage.py shell << 'PYTHON_EOF'
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.contrib.auth.models import User

users = User.objects.all()
print(f'Total users: {users.count()}')
for u in users:
    print(f'  - {u.username} (staff={u.is_staff}, superuser={u.is_superuser}, active={u.is_active})')
PYTHON_EOF

echo ""

# Test 4: Try to create/reset admin with detailed output
echo "4. Creating/resetting admin user..."
NEW_PASS="TestPass123!"
docker exec monitoring_web python manage.py shell << PYTHON_EOF
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
import django
django.setup()
from django.contrib.auth.models import User

try:
    user = User.objects.get(username='admin')
    print('Admin exists, resetting password...')
    user.set_password('$NEW_PASS')
    user.is_staff = True
    user.is_superuser = True
    user.is_active = True
    user.save()
    print('✓ Password reset successfully')
    
    # Verify it worked
    user.refresh_from_db()
    print(f'  Verified: is_staff={user.is_staff}, is_superuser={user.is_superuser}, is_active={user.is_active}')
    
    # Test authentication
    from django.contrib.auth import authenticate
    auth_user = authenticate(username='admin', password='$NEW_PASS')
    if auth_user:
        print('✓ Authentication test PASSED')
    else:
        print('✗ Authentication test FAILED')
        
except User.DoesNotExist:
    print('Admin does not exist, creating...')
    user = User.objects.create_superuser('admin', 'admin@example.com', '$NEW_PASS')
    print('✓ Admin user created')
    
    # Test authentication
    from django.contrib.auth import authenticate
    auth_user = authenticate(username='admin', password='$NEW_PASS')
    if auth_user:
        print('✓ Authentication test PASSED')
    else:
        print('✗ Authentication test FAILED')
        
except Exception as e:
    print(f'✗ Error: {e}')
    import traceback
    traceback.print_exc()
PYTHON_EOF

echo ""
echo "Test password: TestPass123!"
echo "Try logging in with: admin / TestPass123!"

