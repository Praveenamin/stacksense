#!/bin/bash
# Setup cron job for SSH-based heartbeat checker
# This script configures a cron job to run every 30 seconds

set -e

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$SCRIPT_DIR"
MANAGE_PY="$PROJECT_DIR/manage.py"

# Check if manage.py exists
if [ ! -f "$MANAGE_PY" ]; then
    echo "Error: manage.py not found at $MANAGE_PY"
    exit 1
fi

# Get Python path
PYTHON_PATH=$(which python3)
if [ -z "$PYTHON_PATH" ]; then
    echo "Error: python3 not found in PATH"
    exit 1
fi

# Get current user
CURRENT_USER=$(whoami)

echo "=========================================="
echo "Setting up Heartbeat Cron Job"
echo "=========================================="
echo "Project Directory: $PROJECT_DIR"
echo "Python Path: $PYTHON_PATH"
echo "User: $CURRENT_USER"
echo ""

# Create cron entry
# Since cron minimum is 1 minute, we use two entries at 0 and 30 seconds
# Run heartbeat check inside Docker container where SSH keys are available
CRON_ENTRY_1="* * * * * docker exec monitoring_web python3 /app/manage.py check_heartbeats_ssh > /dev/null 2>&1"
CRON_ENTRY_2="* * * * * sleep 30 && docker exec monitoring_web python3 /app/manage.py check_heartbeats_ssh > /dev/null 2>&1"

# Check if cron job already exists
CRON_EXISTS=$(crontab -l 2>/dev/null | grep -c "check_heartbeats_ssh" || true)

if [ "$CRON_EXISTS" -gt 0 ]; then
    echo "Cron job already exists. Removing old entries..."
    crontab -l 2>/dev/null | grep -v "check_heartbeats_ssh" | crontab -
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
echo "Cron job configured successfully!"
echo ""
echo "Current crontab for user $CRON_USER:"
if [ "$CRON_USER" = "root" ]; then
    crontab -l -u root | grep "check_heartbeats_ssh" || echo "  (No entries found - this may be normal if running as different user)"
else
    crontab -l | grep "check_heartbeats_ssh" || echo "  (No entries found)"
fi
echo ""
echo "The heartbeat checker will run every 30 seconds."
echo ""
echo "To view cron logs, check:"
echo "  - /var/log/syslog (for cron output)"
echo "  - Or run manually: python3 manage.py check_heartbeats_ssh --verbose"
echo ""
echo "To remove the cron job:"
echo "  crontab -e"
echo "  (Remove the lines containing 'check_heartbeats_ssh')"

