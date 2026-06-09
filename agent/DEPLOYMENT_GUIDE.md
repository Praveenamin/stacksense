# Heartbeat System Deployment Guide - Push Agent

## Overview

StackSense is **push-agent only**. A lightweight agent installed on each
monitored server POSTs heartbeats (and metrics, services, containers) to the
StackSense server over HTTPS using a per-server bearer token. **The StackSense
server never connects out to monitored servers — there is no SSH from StackSense
to clients.** Server online/offline status is derived from the freshness of the
agent's pushed heartbeats.

## How It Works

- The agent on each monitored server pushes a heartbeat on an interval.
- StackSense's ingest endpoint authenticates the token and updates the server's
  `ServerHeartbeat.last_heartbeat`.
- The in-container scheduler runs `check_heartbeats` /
  `check_server_connectivity` to evaluate heartbeat freshness and flip servers
  online/offline (no SSH, no outbound connection).
- The dashboard shows status based on heartbeat timestamps.

## Requirements

- **StackSense Server**: Reachable from monitored servers over HTTPS.
- **Monitored Servers**: The agent installed and running (`stacksense-agent`),
  plus outbound HTTPS to StackSense.
- **Per-Server Token**: Issued when the server is added; the agent uses it as a
  bearer token. StackSense holds **no** SSH credentials for monitored servers.

## Setup

### Step 1: Add the Server in StackSense

In the UI, go to **Instances → Add Server**, enter a name + IP, and save.
StackSense issues a per-server token and shows a one-line agent install command.

### Step 2: Install the Agent on the Monitored Server

Run the generated installer on the target server (as root/sudo):

```bash
curl -sSL https://your-stacksense-host/agent/install/<token> | sudo bash
```

This drops the agent, writes its config (StackSense URL + per-server token), and
installs a `stacksense-agent` systemd service that starts on boot and restarts
on failure.

### Step 3: Verify the Agent Is Pushing

On the monitored server:

```bash
systemctl status stacksense-agent
sudo journalctl -u stacksense-agent -n 50
```

In StackSense, evaluate heartbeat freshness (this reads pushed heartbeats — it
does **not** connect out):

```bash
cd /home/ubuntu/stacksense-repo
POSTGRES_HOST=localhost POSTGRES_PORT=5433 python3 manage.py check_heartbeats --verbose
```

Servers should show as "ONLINE" once their first heartbeats arrive and are
fresh.

## Scheduling

Heartbeat evaluation runs inside the container via `metrics_scheduler.py`. If you
want to run it on cron instead, use the agent-driven command (no SSH):

```bash
* * * * * cd /home/ubuntu/stacksense-repo && POSTGRES_HOST=localhost POSTGRES_PORT=5433 /usr/bin/python3 /home/ubuntu/stacksense-repo/manage.py check_heartbeats > /dev/null 2>&1
```

## Status Logic

- **OFFLINE**: No heartbeat OR most recent pushed heartbeat older than the threshold (default 60 seconds).
- **ONLINE**: Heartbeat within the threshold AND no active alerts/anomalies.
- **WARNING**: Heartbeat within the threshold BUT has active alerts/anomalies.

## Troubleshooting

### Servers Show Offline

1. **Check the agent is running** on the monitored server:
   ```bash
   systemctl status stacksense-agent
   ```

2. **Check agent logs** for auth/connection errors:
   ```bash
   sudo journalctl -u stacksense-agent -n 50
   ```
   A `401`/`403` means the per-server token is wrong or revoked; a timeout means
   a network/URL problem.

3. **Test connectivity** from the monitored server to StackSense:
   ```bash
   curl -sS -o /dev/null -w "%{http_code}\n" https://your-stacksense-host/health/
   ```

4. **Re-run the installer** to rewrite the agent config (URL + token) if needed:
   ```bash
   curl -sSL https://your-stacksense-host/agent/install/<token> | sudo bash
   ```

### Heartbeat Evaluation

To inspect status on the StackSense side (reads stored heartbeats; no SSH):

```bash
python3 manage.py check_heartbeats --verbose
```

This shows which servers have recent heartbeats, the last heartbeat timestamp,
and the current status (online/offline/warning).

## Advantages

- ✅ **No outbound access required** - StackSense never connects to clients.
- ✅ **Token-authenticated** - Each agent uses a per-server bearer token.
- ✅ **Firewall-friendly** - Clients only need outbound HTTPS; no inbound ports.
- ✅ **Lightweight** - The agent pushes small payloads on an interval.

## Considerations

- The agent must be installed and running on each monitored server.
- Clients need outbound HTTPS reachability to the StackSense host.
- If a monitored server or its agent is down, no heartbeats arrive and the
  server is shown offline (expected behavior).
