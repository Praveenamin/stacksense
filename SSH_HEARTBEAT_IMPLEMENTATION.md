# SSH-Based Heartbeat System - Implementation Complete

## Overview

The heartbeat system now uses **SSH connections from the monitoring server** to check client server status. **No client-side installation is required.**

## What Was Implemented

### 1. SSH Heartbeat Checker Command
**File**: `core/management/commands/check_heartbeats_ssh.py`

- Django management command that:
  - Connects to each server via SSH using existing credentials
  - Updates `ServerHeartbeat.last_heartbeat` on successful connection
  - Handles connection failures gracefully
  - Uses 5-second timeout for quick checks
  - Skips servers with monitoring suspended

### 2. Cron Setup Script
**File**: `setup_heartbeat_cron.sh`

- Automated script to configure cron job
- Sets up two cron entries (runs every 30 seconds)
- Configures proper environment variables
- Can be run once to set up the heartbeat system

### 3. Updated Documentation
**File**: `agent/DEPLOYMENT_GUIDE.md`

- Complete guide for SSH-based approach
- No client installation instructions
- Troubleshooting section
- Manual testing procedures

## Quick Start

### Step 1: Test the Command

```bash
cd /home/ubuntu/stacksense-repo
POSTGRES_HOST=localhost POSTGRES_PORT=5433 python3 manage.py check_heartbeats_ssh --verbose
```

### Step 2: Setup Cron Job

```bash
cd /home/ubuntu/stacksense-repo
./setup_heartbeat_cron.sh
```

### Step 3: Verify

```bash
# Check cron job
crontab -l | grep check_heartbeats_ssh

# Check heartbeat status
POSTGRES_HOST=localhost POSTGRES_PORT=5433 python3 manage.py check_heartbeats --verbose
```

## How It Works

1. **Cron runs** `check_heartbeats_ssh` every 30 seconds
2. **Command iterates** through all servers
3. **SSH connects** to each server (using existing credentials)
4. **On success**: Updates heartbeat timestamp in database
5. **On failure**: Leaves heartbeat unchanged (shows offline if > 60s old)
6. **Dashboard** reads heartbeat timestamps to display status

## Status Logic

- **OFFLINE**: No heartbeat OR heartbeat older than 60 seconds
- **ONLINE**: Heartbeat within 60 seconds AND no active alerts/anomalies
- **WARNING**: Heartbeat within 60 seconds BUT has active alerts/anomalies

## Requirements

- SSH access to all client servers (already configured for metric collection)
- SSH key deployed (same as used for metric collection)
- Cron service running on monitoring server

## Benefits

- ✅ No client installation needed
- ✅ No client dependencies
- ✅ Uses existing SSH infrastructure
- ✅ Centralized management
- ✅ Easy to monitor and debug

## Files Created

1. `core/management/commands/check_heartbeats_ssh.py` - SSH heartbeat checker
2. `setup_heartbeat_cron.sh` - Cron setup script
3. `agent/DEPLOYMENT_GUIDE.md` - Updated deployment guide

## Notes

- SSH authentication errors are expected if SSH keys aren't configured yet
- The command uses the same SSH setup as metric collection
- Servers with monitoring suspended are automatically skipped
- Connection timeout is 5 seconds (configurable via --timeout flag)

