# Agent Connection Troubleshooting Guide

## Overview

This guide explains how to manually fix agent connection issues on client servers. There are two types of monitoring connections:

1. **SSH Connection** - The monitoring server SSH connects to client servers to check connectivity
2. **Heartbeat Agent** - A lightweight agent script runs on client servers and sends heartbeat signals to the monitoring server

## Common Issues and Fixes

### Issue 1: SSH Connection Failure

**Symptoms:**
- Server shows as "OFFLINE" in monitoring dashboard
- Alert: "Server is OFFLINE"
- Server is actually running and pingable

**Causes:**
- SSH service not running on client server
- SSH key authentication issues
- Firewall blocking SSH port
- SSH service configuration issues

**Fix Steps:**

1. **Check SSH Service Status:**
   ```bash
   sudo systemctl status ssh
   # or
   sudo systemctl status sshd
   ```

2. **Start SSH Service if Stopped:**
   ```bash
   sudo systemctl start ssh
   # or
   sudo systemctl start sshd
   ```

3. **Enable SSH Service on Boot:**
   ```bash
   sudo systemctl enable ssh
   # or
   sudo systemctl enable sshd
   ```

4. **Check SSH Service is Listening:**
   ```bash
   sudo ss -tlnp | grep :22
   # Should show: LISTEN 0 128 0.0.0.0:22
   ```

5. **Verify SSH Key Access:**
   - The monitoring server should have SSH key access to the client server
   - Check if you can SSH from monitoring server to client:
     ```bash
     ssh -i /path/to/ssh/key username@client_server_ip
     ```

6. **Check Firewall Rules:**
   ```bash
   # Ubuntu/Debian (UFW)
   sudo ufw status
   sudo ufw allow 22/tcp
   
   # CentOS/RHEL (firewalld)
   sudo firewall-cmd --list-all
   sudo firewall-cmd --permanent --add-service=ssh
   sudo firewall-cmd --reload
   
   # iptables
   sudo iptables -L -n | grep 22
   sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT
   ```

### Issue 2: Heartbeat Agent Not Running

**Symptoms:**
- Server shows intermittent connection status
- Heartbeat signals not being received
- Server appears offline even though SSH works

**Causes:**
- Heartbeat agent script not installed/running
- Agent process crashed or stopped
- Network connectivity issues from client to monitoring server
- Agent configuration incorrect

**Fix Steps:**

1. **Check if Agent is Running:**
   ```bash
   ps aux | grep heartbeat_agent
   # or
   systemctl status stacksense-heartbeat
   ```

2. **Check Agent Logs:**
   ```bash
   # If running as systemd service
   sudo journalctl -u stacksense-heartbeat -n 50
   
   # If running manually, check log file location
   tail -f /var/log/stacksense-heartbeat.log
   ```

3. **Restart Agent Service:**
   ```bash
   sudo systemctl restart stacksense-heartbeat
   # or if running manually
   nohup python3 /path/to/heartbeat_agent.py > /var/log/stacksense-heartbeat.log 2>&1 &
   ```

4. **Verify Agent Configuration:**
   - Check configuration file: `~/.stacksense_heartbeat.conf`
   - Or check environment variables:
     ```bash
     echo $STACKSENSE_SERVER_ID
     echo $STACKSENSE_API_URL
     ```

5. **Test Agent Connectivity:**
   ```bash
   # Test if agent can reach monitoring server
   curl -X POST https://your-monitoring-server.com/api/heartbeat/YOUR_SERVER_ID/ \
     -H "Content-Type: application/json" \
     -d '{"agent_version": "test"}'
   ```

6. **Reinstall Agent (if needed):**
   - Download agent script to client server
   - Place in `/opt/stacksense/` or `/usr/local/bin/`
   - Create systemd service file (see below)
   - Start and enable service

### Issue 3: Network Connectivity Issues

**Symptoms:**
- Intermittent connection failures
- Timeout errors
- Connection refused errors

**Fix Steps:**

1. **Test Network Connectivity:**
   ```bash
   # Ping monitoring server
   ping monitoring_server_ip
   
   # Test port connectivity
   telnet monitoring_server_ip 8000
   # or
   nc -zv monitoring_server_ip 8000
   ```

2. **Check DNS Resolution:**
   ```bash
   nslookup monitoring_server_domain
   # or
   dig monitoring_server_domain
   ```

3. **Check Routing:**
   ```bash
   traceroute monitoring_server_ip
   # or
   mtr monitoring_server_ip
   ```

### Issue 4: Agent Service Setup

**Create Systemd Service (Recommended):**

Create `/etc/systemd/system/stacksense-heartbeat.service`:

```ini
[Unit]
Description=StackSense Heartbeat Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/stacksense
ExecStart=/usr/bin/python3 /opt/stacksense/heartbeat_agent.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Environment variables (optional, can use config file instead)
Environment="STACKSENSE_SERVER_ID=YOUR_SERVER_ID"
Environment="STACKSENSE_API_URL=https://your-monitoring-server.com"

[Install]
WantedBy=multi-user.target
```

**Enable and Start Service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable stacksense-heartbeat
sudo systemctl start stacksense-heartbeat
sudo systemctl status stacksense-heartbeat
```

### Issue 5: SSH Key Authentication Issues

**Fix Steps:**

1. **On Monitoring Server - Generate SSH Key (if not exists):**
   ```bash
   ssh-keygen -t rsa -b 4096 -f /path/to/ssh_keys/id_rsa -N ""
   ```

2. **Copy Public Key to Client Server:**
   ```bash
   # From monitoring server
   ssh-copy-id -i /path/to/ssh_keys/id_rsa.pub username@client_server_ip
   
   # Or manually add to client server's ~/.ssh/authorized_keys
   cat /path/to/ssh_keys/id_rsa.pub | ssh username@client_server_ip "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```

3. **Set Correct Permissions on Client Server:**
   ```bash
   chmod 700 ~/.ssh
   chmod 600 ~/.ssh/authorized_keys
   ```

4. **Test SSH Connection:**
   ```bash
   ssh -i /path/to/ssh_keys/id_rsa username@client_server_ip
   ```

## Quick Diagnostic Commands

Run these commands on the **client server** to diagnose issues:

```bash
# 1. Check SSH service
sudo systemctl status ssh

# 2. Check if agent is running
ps aux | grep heartbeat_agent

# 3. Check network connectivity
ping -c 4 monitoring_server_ip

# 4. Check SSH key authentication
ssh -v username@monitoring_server_ip

# 5. Check firewall rules
sudo ufw status  # Ubuntu/Debian
sudo firewall-cmd --list-all  # CentOS/RHEL

# 6. Check system resources
top
df -h
free -h

# 7. Check system logs
sudo journalctl -xe | tail -50
```

## Monitoring Server Retry Logic

The monitoring server now uses a retry mechanism before triggering offline alerts:

- **Retry Attempts:** 3 attempts (configurable)
- **Retry Interval:** 20 seconds between attempts (configurable)
- **Alert Trigger:** Only triggers alert after all retry attempts fail

This prevents false alarms from temporary network glitches or brief SSH service interruptions.

## Getting Help

If issues persist after following these steps:

1. Check monitoring server logs: `docker logs monitoring_web`
2. Check client server system logs: `sudo journalctl -xe`
3. Verify all configuration settings
4. Test connectivity manually using the diagnostic commands above
5. Contact system administrator with diagnostic information

## Related Files

- Monitoring Server: `/home/ubuntu/stacksense-repo/core/management/commands/check_heartbeats_ssh.py`
- Client Agent: `/home/ubuntu/stacksense-repo/agent/heartbeat_agent.py`
- SSH Configuration: `/home/ubuntu/stacksense-repo/ssh_keys/`

