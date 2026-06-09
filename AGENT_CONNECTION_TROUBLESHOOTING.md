# Agent Connection Troubleshooting Guide

## Overview

This guide explains how to fix agent connection issues on monitored servers.
StackSense is **push-agent only**: a lightweight agent installed on each
monitored VM POSTs metrics, services, containers, and heartbeats to the
StackSense server over HTTPS using a per-server bearer token. **The StackSense
server never connects out to monitored servers** — there is no SSH from
StackSense to clients. If a server shows as offline, the cause is almost always
the agent on the client, the per-server token, or network/firewall on the path
from the client to StackSense.

## Common Issues and Fixes

### Issue 1: Server Shows "OFFLINE" (no heartbeats arriving)

**Symptoms:**
- Server shows as "OFFLINE" in monitoring dashboard
- Alert: "Server is OFFLINE"
- Server is actually running and reachable

**Causes:**
- Agent not installed or not running on the client server
- Wrong or revoked per-server bearer token
- Wrong StackSense API URL configured in the agent
- Firewall blocking the agent's outbound HTTPS to StackSense

**Fix Steps:**

1. **Confirm the agent is running on the client server:**
   ```bash
   systemctl status stacksense-agent
   # or
   ps aux | grep stacksense
   ```

2. **Check the agent logs for auth/connection errors:**
   ```bash
   sudo journalctl -u stacksense-agent -n 50
   ```
   A `401`/`403` means the token is wrong or revoked; a timeout/connection
   refused means a network or URL problem.

3. **Verify the agent config** (API URL and token):
   - Check the agent config file on the client server for the StackSense URL and
     the per-server bearer token.
   - The token must match the one issued for this server. Re-issue it from the
     StackSense UI or with `create_agent_token` if needed, then update the agent.

4. **Test outbound connectivity from the client to StackSense:**
   ```bash
   curl -sS -o /dev/null -w "%{http_code}\n" https://your-stacksense-host/health/
   ```

5. **Check the client's outbound firewall** (the agent must reach StackSense on
   443/HTTPS):
   ```bash
   # Ubuntu/Debian (UFW)
   sudo ufw status

   # CentOS/RHEL (firewalld)
   sudo firewall-cmd --list-all
   ```

### Issue 2: Agent Not Running

**Symptoms:**
- Server shows intermittent connection status
- Heartbeats/metrics not being received

**Causes:**
- Agent service not installed/running
- Agent process crashed or stopped
- Network connectivity issues from client to StackSense
- Agent configuration incorrect

**Fix Steps:**

1. **Check if Agent is Running:**
   ```bash
   systemctl status stacksense-agent
   # or
   ps aux | grep stacksense
   ```

2. **Check Agent Logs:**
   ```bash
   # If running as a systemd service
   sudo journalctl -u stacksense-agent -n 50
   ```

3. **Restart the Agent Service:**
   ```bash
   sudo systemctl restart stacksense-agent
   ```

4. **Verify Agent Configuration:**
   - Check the agent's config file for the StackSense API URL and the
     per-server bearer token.

5. **Test Agent Connectivity:**
   ```bash
   # Test if the agent host can reach StackSense
   curl -sS -o /dev/null -w "%{http_code}\n" https://your-stacksense-host/health/
   ```

6. **Reinstall the Agent (if needed):**
   - From the StackSense UI, open the server and copy its one-line
     `curl … | sudo bash` agent install command.
   - Run it on the client server. It installs/updates the agent and writes the
     correct API URL and token.

### Issue 3: Network Connectivity Issues

**Symptoms:**
- Intermittent connection failures
- Timeout errors
- Connection refused errors

**Fix Steps:**

1. **Test Network Connectivity (from the client toward StackSense):**
   ```bash
   # Test port connectivity to StackSense
   nc -zv your-stacksense-host 443
   ```

2. **Check DNS Resolution:**
   ```bash
   nslookup your-stacksense-host
   # or
   dig your-stacksense-host
   ```

3. **Check Routing:**
   ```bash
   traceroute your-stacksense-host
   # or
   mtr your-stacksense-host
   ```

### Issue 4: Agent Service Setup

The recommended way to (re)install the agent is the one-line installer copied
from the server's page in the StackSense UI:

```bash
curl -sSL https://your-stacksense-host/agent/install/<token> | sudo bash
```

This drops the agent on the host, writes its config (StackSense URL +
per-server token), and installs a `stacksense-agent` systemd service that starts
on boot and restarts on failure.

**Manage the service:**
```bash
sudo systemctl status stacksense-agent
sudo systemctl restart stacksense-agent
sudo systemctl enable stacksense-agent
```

### Issue 5: Token Authentication Issues

If the agent logs show `401 Unauthorized` or `403 Forbidden`, the per-server
bearer token is wrong, revoked, or doesn't match the server record.

**Fix Steps:**

1. **Re-issue the token** for the server (from the StackSense UI, or with the
   `create_agent_token` management command).

2. **Update the agent** with the new token — easiest is to re-run the one-line
   installer for that server, which rewrites the agent config:
   ```bash
   curl -sSL https://your-stacksense-host/agent/install/<token> | sudo bash
   ```

3. **Restart and verify:**
   ```bash
   sudo systemctl restart stacksense-agent
   sudo journalctl -u stacksense-agent -n 20
   ```

## Quick Diagnostic Commands

Run these commands on the **client (monitored) server** to diagnose issues:

```bash
# 1. Check the agent service
systemctl status stacksense-agent

# 2. Check agent logs (auth/connection errors)
sudo journalctl -u stacksense-agent -n 50

# 3. Check connectivity to StackSense
curl -sS -o /dev/null -w "%{http_code}\n" https://your-stacksense-host/health/

# 4. Check outbound firewall
sudo ufw status               # Ubuntu/Debian
sudo firewall-cmd --list-all  # CentOS/RHEL

# 5. Check system resources
top
df -h
free -h

# 6. Check system logs
sudo journalctl -xe | tail -50
```

## StackSense Offline Detection

StackSense marks a server offline based on the freshness of the **pushed**
heartbeats it has received (evaluated by `check_heartbeats` /
`check_server_connectivity`). A short grace window avoids false alarms from a
single missed push. There is no outbound SSH probe.

## Getting Help

If issues persist after following these steps:

1. Check StackSense server logs: `docker logs monitoring_web`
2. Check client server system logs: `sudo journalctl -xe`
3. Verify the agent's API URL and per-server token
4. Test connectivity manually using the diagnostic commands above
5. Contact your system administrator with diagnostic information

## Related Files

- StackSense heartbeat evaluation: `core/management/commands/check_heartbeats.py`
  and `core/management/commands/check_server_connectivity.py`
- Agent: `agent/` (install via the one-line `curl … | sudo bash` command from the UI)
