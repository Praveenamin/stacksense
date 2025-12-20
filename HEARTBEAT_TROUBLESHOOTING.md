# Heartbeat System Troubleshooting

## Current Status

The heartbeat system is **fully operational**. Here's what you need to know:

### System Status
- ✅ Database table created (`core_serverheartbeat`)
- ✅ API endpoint working (`/api/heartbeat/<server_id>/`)
- ✅ Status calculation using heartbeats
- ✅ CSRF exemption added for API endpoint

### Current Server Status
- **Server 1 (cpanel-test)**: Has heartbeat → Shows as "warning" (has 19 active alerts)
- **Server 3 (Kimai)**: No heartbeat → Shows as "offline"
- **Server 5 (Metrics-Kb-Server)**: No heartbeat → Shows as "offline"

## Why Servers Show Offline

Servers will show as **offline** until:
1. Agent scripts are deployed to those servers
2. Agents start sending heartbeats every 30 seconds

## Status Logic

- **OFFLINE**: No heartbeat OR heartbeat older than 60 seconds
- **ONLINE**: Heartbeat within 60 seconds AND no active alerts/anomalies
- **WARNING**: Heartbeat within 60 seconds BUT has active alerts/anomalies

## Deploying Agents

To get servers showing as "online", you need to:

1. **Copy agent script** to each monitored server:
   ```bash
   scp agent/heartbeat_agent.py user@server:/opt/stacksense-agent/
   ```

2. **Install dependencies** on each server:
   ```bash
   pip3 install requests
   ```

3. **Configure agent** (set server ID and API URL):
   ```bash
   export STACKSENSE_SERVER_ID=1  # Get from dashboard
   export STACKSENSE_API_URL=http://your-monitoring-server.com
   ```

4. **Run agent** (see `agent/README.md` for systemd service setup)

## Testing Heartbeat API

Test the API endpoint manually:
```bash
curl -X POST http://your-server/api/heartbeat/1/ \
     -H "Content-Type: application/json" \
     -d '{"agent_version": "1.0.0"}'
```

Expected response:
```json
{
    "status": "ok",
    "server_id": 1,
    "server_name": "cpanel-test",
    "heartbeat_received": true,
    "timestamp": "2025-12-17T05:11:03.598464+00:00"
}
```

## Verifying Status

Check heartbeat status:
```bash
python3 manage.py check_heartbeats --verbose
```

This will show:
- Which servers have heartbeats
- Last heartbeat timestamp
- Current status (online/offline/warning)

## Common Issues

### Server Shows Offline
- **Cause**: No heartbeat received
- **Solution**: Deploy agent script and verify it's running

### Server Shows Warning (not Online)
- **Cause**: Server has active alerts or anomalies
- **Solution**: This is correct behavior - resolve alerts to show "online"

### API Returns 404
- **Cause**: URL path incorrect or server not running
- **Solution**: Verify URL is `/api/heartbeat/<server_id>/` and Django server is running

### CSRF Error
- **Cause**: CSRF token required
- **Solution**: Already fixed - API endpoint has CSRF exemption

## Next Steps

1. Deploy agent scripts to all monitored servers
2. Verify agents are sending heartbeats (check logs)
3. Monitor dashboard - servers should show online once heartbeats are received
4. Servers with alerts will show "warning" status (this is correct)

