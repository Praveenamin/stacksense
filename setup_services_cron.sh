#!/bin/bash
# Setup cron job for service status checker
# This script configures a cron job to run every 30 seconds

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "Setting up Service Check Cron Job"
echo "=================================="

# Detect current user
CURRENT_USER=$(whoami)

# Change to project directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if running in Docker or on host
if [ -f /.dockerenv ] || [ -n "$DOCKER_CONTAINER" ]; then
    echo -e "${YELLOW}Note: Running inside Docker container${NC}"
    CRON_ENTRY_1="* * * * * cd /app && python3 manage.py check_services > /dev/null 2>&1"
    CRON_ENTRY_2="* * * * * sleep 30 && cd /app && python3 manage.py check_services > /dev/null 2>&1"
else
    echo -e "${YELLOW}Note: Running on host machine${NC}"
    CRON_ENTRY_1="* * * * * docker exec monitoring_web python3 /app/manage.py check_services > /dev/null 2>&1"
    CRON_ENTRY_2="* * * * * sleep 30 && docker exec monitoring_web python3 /app/manage.py check_services > /dev/null 2>&1"
fi

# Check if cron job already exists
CRON_EXISTS=$(crontab -l 2>/dev/null | grep -c "check_services" || true)

if [ "$CRON_EXISTS" -gt 0 ]; then
    echo "Cron job already exists. Removing old entries..."
    crontab -l 2>/dev/null | grep -v "check_services" | crontab -
fi

# Add new cron entries
echo "Adding cron job entries..."

if [ "$CURRENT_USER" = "root" ]; then
    (crontab -l -u root 2>/dev/null; echo "$CRON_ENTRY_1"; echo "$CRON_ENTRY_2") | crontab -u root -
    CRON_USER="root"
else
    (crontab -l 2>/dev/null; echo "$CRON_ENTRY_1"; echo "$CRON_ENTRY_2") | crontab -
    CRON_USER="$CURRENT_USER"
fi

echo ""
echo -e "${GREEN}Cron job configured successfully!${NC}"
echo ""
echo "Current crontab for user $CRON_USER:"
if [ "$CRON_USER" = "root" ]; then
    crontab -l -u root | grep "check_services" || echo "  (No entries found - this may be normal if running as different user)"
else
    crontab -l | grep "check_services" || echo "  (No entries found)"
fi

echo ""
echo "Service checks will run every 30 seconds"
echo "Alerts will trigger after 2 consecutive failures (60 seconds)"
echo ""
echo "To view service check logs, run manually:"
echo "  docker exec monitoring_web python3 manage.py check_services --verbose"
echo ""
echo "To remove the cron job:"
echo "  crontab -e"
echo "  (Remove the lines containing 'check_services')"
