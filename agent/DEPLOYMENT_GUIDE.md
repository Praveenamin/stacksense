# Heartbeat System Deployment Guide - SSH-Based (No Client Installation)

## Overview

The heartbeat system uses **SSH connections from the monitoring server** to check if client servers are online. **No installation or agents are needed on client servers.**

## How It Works

- Monitoring server runs a cron job every 30 seconds
- Cron job SSH connects to each client server
- On successful connection: Updates heartbeat record
- Dashboard shows server status based on heartbeat timestamps

## Requirements

- **Monitoring Server**: Must have SSH access to all client servers
- **Client Servers**: No installation needed - just need to be accessible via SSH
- **SSH Credentials**: Already configured in Server model (used for metric collection)

## Setup (One-Time on Monitoring Server)

### Step 1: Verify SSH Heartbeat Checker Command

Test the command manually:

```bash
cd /home/ubuntu/stacksense-repo
POSTGRES_HOST=localhost POSTGRES_PORT=5433 python3 manage.py check_heartbeats_ssh --verbose
```

You should see connection attempts to all servers and heartbeat updates.

### Step 2: Setup Cron Job

Run the setup script:

```bash
cd /home/ubuntu/stacksense-repo
./setup_heartbeat_cron.sh
```

This will:
- Add two cron entries (runs every 30 seconds)
- Configure proper environment variables
- Set up logging

### Step 3: Verify Cron Job

Check that cron job is configured:

```bash
crontab -l | grep check_heartbeats_ssh
```

You should see two entries.

### Step 4: Monitor Status

Check heartbeat status:

```bash
cd /home/ubuntu/stacksense-repo
POSTGRES_HOST=localhost POSTGRES_PORT=5433 python3 manage.py check_heartbeats --verbose
```

Servers should show as "ONLINE" if SSH connections are successful.

## Manual Cron Setup (Alternative)

If you prefer to set up cron manually:

```bash
# Edit crontab
crontab -e

# Add these two lines (runs every 30 seconds):
* * * * * cd /home/ubuntu/stacksense-repo && POSTGRES_HOST=localhost POSTGRES_PORT=5433 /usr/bin/python3 /home/ubuntu/stacksense-repo/manage.py check_heartbeats_ssh > /dev/null 2>&1
* * * * * sleep 30 && cd /home/ubuntu/stacksense-repo && POSTGRES_HOST=localhost POSTGRES_PORT=5433 /usr/bin/python3 /home/ubuntu/stacksense-repo/manage.py check_heartbeats_ssh > /dev/null 2>&1
```

## How It Works

1. **Cron triggers** the `check_heartbeats_ssh` command every 30 seconds
2. **Command iterates** through all servers in database
3. **SSH connects** to each server using existing credentials
4. **On success**: Updates `ServerHeartbeat.last_heartbeat` timestamp
5. **On failure**: Leaves heartbeat unchanged (shows offline if > 60s old)
6. **Dashboard** reads heartbeat timestamps to show status

## Status Logic

- **OFFLINE**: No heartbeat OR heartbeat older than 60 seconds
- **ONLINE**: Heartbeat within 60 seconds AND no active alerts/anomalies  
- **WARNING**: Heartbeat within 60 seconds BUT has active alerts/anomalies

## Troubleshooting

### Servers Show Offline

1. **Check SSH connectivity**:
   ```bash
   ssh -p <port> <username>@<server-ip>
   ```

2. **Test command manually**:
   ```bash
   python3 manage.py check_heartbeats_ssh --verbose
   ```

3. **Check SSH key**:
   - Verify SSH key exists at configured path
   - Default: `/app/ssh_keys/id_rsa`
   - Check `SSH_PRIVATE_KEY_PATH` in settings

4. **Check cron logs**:
   ```bash
   grep CRON /var/log/syslog | tail -20
   ```

### Cron Job Not Running

1. **Verify cron service**:
   ```bash
   sudo systemctl status cron
   ```

2. **Check crontab**:
   ```bash
   crontab -l
   ```

3. **Test command manually**:
   ```bash
   python3 manage.py check_heartbeats_ssh --verbose
   ```

### SSH Connection Failures

1. **Verify credentials** in Server model:
   - IP address
   - Username
   - Port
   - SSH key deployed

2. **Test SSH manually**:
   ```bash
   ssh -i /app/ssh_keys/id_rsa -p <port> <username>@<server-ip>
   ```

3. **Check firewall** on client servers (inbound SSH must be allowed)

## Advantages

- ✅ **No client installation** - Everything runs on monitoring server
- ✅ **No client dependencies** - No Python, no libraries needed on clients
- ✅ **Uses existing SSH** - Reuses credentials already configured
- ✅ **Centralized** - Easy to monitor and debug
- ✅ **Lightweight** - Just connection test, no data transfer

## Considerations

- Requires SSH access to all client servers (already needed for metrics)
- SSH connection overhead (~1-2 seconds per server)
- If monitoring server is down, no heartbeats (expected behavior)
- Client servers must allow inbound SSH connections

## Removing Cron Job

To remove the heartbeat cron job:

```bash
crontab -e
# Remove lines containing "check_heartbeats_ssh"
```

Or use:

```bash
crontab -l | grep -v "check_heartbeats_ssh" | crontab -
```

## Manual Testing

Test the heartbeat checker manually:

```bash
cd /home/ubuntu/stacksense-repo
POSTGRES_HOST=localhost POSTGRES_PORT=5433 python3 manage.py check_heartbeats_ssh --verbose
```

This will show:
- Connection attempts to each server
- Success/failure status
- Heartbeat update confirmations

## Monitoring

Check heartbeat status anytime:

```bash
python3 manage.py check_heartbeats --verbose
```

This shows:
- Which servers have recent heartbeats
- Last heartbeat timestamp
- Current status (online/offline/warning)

