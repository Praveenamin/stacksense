# StackSense Heartbeat Agent

Lightweight agent script that sends heartbeat signals to the monitoring server every 30 seconds.

## Features

- **Ultra Lightweight**: < 5MB memory, minimal CPU usage
- **Simple**: Just HTTP POST requests every 30 seconds
- **Reliable**: Automatic retry logic for network issues
- **Easy Deployment**: Works with systemd, cron, or as standalone script

## Requirements

- Python 3.6+
- `requests` library (install with `pip install requests`)

## Quick Start

### 1. Install Dependencies

```bash
pip3 install requests
```

### 2. Configure Agent

#### Option A: Environment Variables (Recommended)

```bash
export STACKSENSE_SERVER_ID=1
export STACKSENSE_API_URL=http://your-monitoring-server.com
export STACKSENSE_INTERVAL=30  # Optional, defaults to 30 seconds
```

#### Option B: Config File

Create `~/.stacksense_heartbeat.conf`:

```json
{
    "server_id": 1,
    "api_url": "http://your-monitoring-server.com",
    "interval": 30
}
```

**Note**: Get your `server_id` from the StackSense dashboard. It's the ID of your server in the monitoring system.

### 3. Run Agent

```bash
python3 heartbeat_agent.py
```

## Deployment Options

### Option 1: Systemd Service (Recommended)

Create `/etc/systemd/system/stacksense-heartbeat.service`:

```ini
[Unit]
Description=StackSense Heartbeat Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/stacksense-agent
Environment="STACKSENSE_SERVER_ID=1"
Environment="STACKSENSE_API_URL=http://your-monitoring-server.com"
Environment="STACKSENSE_INTERVAL=30"
ExecStart=/usr/bin/python3 /opt/stacksense-agent/heartbeat_agent.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Then:

```bash
# Copy agent script
sudo mkdir -p /opt/stacksense-agent
sudo cp heartbeat_agent.py /opt/stacksense-agent/
sudo chmod +x /opt/stacksense-agent/heartbeat_agent.py

# Install requests if needed
sudo pip3 install requests

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable stacksense-heartbeat
sudo systemctl start stacksense-heartbeat

# Check status
sudo systemctl status stacksense-heartbeat

# View logs
sudo journalctl -u stacksense-heartbeat -f
```

### Option 2: Cron Job

```bash
# Install agent
sudo mkdir -p /opt/stacksense-agent
sudo cp heartbeat_agent.py /opt/stacksense-agent/
sudo chmod +x /opt/stacksense-agent/heartbeat_agent.py

# Create wrapper script
sudo tee /opt/stacksense-agent/run_heartbeat.sh << 'EOF'
#!/bin/bash
export STACKSENSE_SERVER_ID=1
export STACKSENSE_API_URL=http://your-monitoring-server.com
cd /opt/stacksense-agent
python3 heartbeat_agent.py
EOF

sudo chmod +x /opt/stacksense-agent/run_heartbeat.sh

# Add to crontab (runs every minute, agent handles 30s interval internally)
# Or use a loop in the script itself
```

### Option 3: Standalone with Loop

The agent script already includes a loop, so you can run it directly:

```bash
nohup python3 heartbeat_agent.py > /var/log/stacksense-heartbeat.log 2>&1 &
```

## Configuration

### Environment Variables

- `STACKSENSE_SERVER_ID`: Your server ID from the monitoring dashboard (required)
- `STACKSENSE_API_URL`: Base URL of your monitoring server (required)
- `STACKSENSE_INTERVAL`: Heartbeat interval in seconds (optional, default: 30)

### Config File

Location: `~/.stacksense_heartbeat.conf`

Format: JSON

```json
{
    "server_id": 1,
    "api_url": "http://your-monitoring-server.com",
    "interval": 30
}
```

## Troubleshooting

### Agent Not Sending Heartbeats

1. **Check Configuration**:
   ```bash
   echo $STACKSENSE_SERVER_ID
   echo $STACKSENSE_API_URL
   ```

2. **Test Connection**:
   ```bash
   curl -X POST http://your-monitoring-server.com/api/heartbeat/1/ \
        -H "Content-Type: application/json" \
        -d '{}'
   ```

3. **Check Firewall**:
   - Ensure outbound HTTP/HTTPS is allowed
   - No inbound ports needed

4. **Check Logs**:
   ```bash
   # For systemd
   sudo journalctl -u stacksense-heartbeat -f
   
   # For standalone
   tail -f /var/log/stacksense-heartbeat.log
   ```

### Server Shows Offline

1. **Verify Server ID**: Make sure you're using the correct server ID from dashboard
2. **Check API URL**: Ensure the URL is correct and accessible
3. **Check Agent Status**: Verify agent is running and sending heartbeats
4. **Check Network**: Ensure agent can reach monitoring server

### Permission Errors

If running as non-root user, ensure:
- Script has execute permissions: `chmod +x heartbeat_agent.py`
- User has write access to log directory (if using file logging)

## Security Notes

- The heartbeat endpoint is currently unauthenticated (can be secured later)
- Agent only makes outbound requests (no inbound ports needed)
- Use HTTPS in production for encrypted communication
- Consider adding API key authentication for production deployments

## API Endpoint

The agent sends POST requests to:
```
{API_URL}/api/heartbeat/{SERVER_ID}/
```

Payload:
```json
{
    "agent_version": "1.0.0"
}
```

Response:
```json
{
    "status": "ok",
    "server_id": 1,
    "server_name": "My Server",
    "heartbeat_received": true,
    "timestamp": "2024-01-01T12:00:00Z"
}
```

## Support

For issues or questions:
1. Check logs for error messages
2. Verify configuration is correct
3. Test network connectivity to monitoring server
4. Ensure server ID matches your server in the dashboard

