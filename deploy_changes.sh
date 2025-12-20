#!/bin/bash

# StackSense Deployment Script
# Copies changes to container and rebuilds static files

set -e

echo "🚀 Deploying StackSense changes..."

# Copy template changes
echo "📄 Copying template changes..."
docker cp /home/ubuntu/stacksense-repo/core/templates/core/monitoring_dashboard.html monitoring_web:/app/core/templates/core/monitoring_dashboard.html

# Copy CSS changes if any
echo "🎨 Copying CSS changes..."
docker cp /home/ubuntu/stacksense-repo/core/static/core/css/design-system.css monitoring_web:/app/core/static/core/css/design-system.css

# Collect static files
echo "📦 Collecting static files..."
docker exec monitoring_web python manage.py collectstatic --noinput --clear

# Restart web container
echo "🔄 Restarting web container..."
docker restart monitoring_web

# Wait for container to be ready
echo "⏳ Waiting for container to be ready..."
sleep 5

# Test Django
echo "✅ Testing Django..."
docker exec monitoring_web python manage.py check

echo "🎉 Deployment completed successfully!"
echo "🌐 Your application is available at: http://23.82.14.228:8000"







