#!/bin/bash
# StackSense Heartbeat Agent Deployment Script
# Usage: ./deploy_agent.sh <server-ip> <server-id> <api-url> [ssh-user]

set -e

SERVER_IP=$1
SERVER_ID=$2
API_URL=$3
SSH_USER=${4:-root}

if [ -z "$SERVER_IP" ] || [ -z "$SERVER_ID" ] || [ -z "$API_URL" ]; then
    echo "Usage: ./deploy_agent.sh <server-ip> <server-id> <api-url> [ssh-user]"
    echo ""
    echo "Example:"
    echo "  ./deploy_agent.sh 192.168.1.101 1 http://monitoring.example.com root"
    exit 1
fi

echo "=========================================="
echo "Deploying StackSense Heartbeat Agent"
echo "=========================================="
echo "Server IP: $SERVER_IP"
echo "Server ID: $SERVER_ID"
echo "API URL: $API_URL"
echo "SSH User: $SSH_USER"
echo ""

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Copy agent script
echo "Step 1: Copying agent script..."
scp "$SCRIPT_DIR/heartbeat_agent.py" ${SSH_USER}@${SERVER_IP}:/tmp/heartbeat_agent.py || {
    echo "Error: Failed to copy agent script. Check SSH connectivity."
    exit 1
}

# Deploy via SSH
echo "Step 2: Deploying on remote server..."
ssh ${SSH_USER}@${SERVER_IP} << EOF
    set -e
    
    # Create directory
    echo "Creating /opt/stacksense-agent directory..."
    sudo mkdir -p /opt/stacksense-agent
    
    # Move script
    sudo mv /tmp/heartbeat_agent.py /opt/stacksense-agent/
    sudo chmod +x /opt/stacksense-agent/heartbeat_agent.py
    
    # Install requests if needed
    echo "Installing dependencies..."
    if ! python3 -c "import requests" 2>/dev/null; then
        if command -v pip3 &> /dev/null; then
            sudo pip3 install requests
        elif command -v apt-get &> /dev/null; then
            sudo apt-get update -qq
            sudo apt-get install -y python3-pip
            sudo pip3 install requests
        elif command -v yum &> /dev/null; then
            sudo yum install -y python3-pip
            sudo pip3 install requests
        else
            echo "Warning: Could not install requests. Please install manually: pip3 install requests"
        fi
    fi
    
    # Create systemd service
    echo "Creating systemd service..."
    sudo tee /etc/systemd/system/stacksense-heartbeat.service > /dev/null << SERVICE_EOF
[Unit]
Description=StackSense Heartbeat Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/stacksense-agent
Environment="STACKSENSE_SERVER_ID=${SERVER_ID}"
Environment="STACKSENSE_API_URL=${API_URL}"
Environment="STACKSENSE_INTERVAL=30"
ExecStart=/usr/bin/python3 /opt/stacksense-agent/heartbeat_agent.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE_EOF

    # Enable and start service
    echo "Starting service..."
    sudo systemctl daemon-reload
    sudo systemctl enable stacksense-heartbeat
    sudo systemctl start stacksense-heartbeat
    
    # Wait a moment for service to start
    sleep 2
    
    # Check status
    echo ""
    echo "Service status:"
    sudo systemctl status stacksense-heartbeat --no-pager -l || true
    
    echo ""
    echo "Recent logs:"
    sudo journalctl -u stacksense-heartbeat -n 10 --no-pager || true
EOF

echo ""
echo "=========================================="
echo "Deployment Complete!"
echo "=========================================="
echo ""
echo "To check status on remote server:"
echo "  ssh ${SSH_USER}@${SERVER_IP} 'sudo systemctl status stacksense-heartbeat'"
echo ""
echo "To view logs:"
echo "  ssh ${SSH_USER}@${SERVER_IP} 'sudo journalctl -u stacksense-heartbeat -f'"
echo ""

