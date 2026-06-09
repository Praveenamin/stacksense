# StackSense Monitoring Application - Workflow Documentation

## Overview

StackSense is a server monitoring application that tracks server health, collects metrics, detects anomalies, and sends alerts. This document describes the complete workflow and architecture of the system.

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Application Startup Flow](#application-startup-flow)
3. [Server Registration Flow](#server-registration-flow)
4. [Metrics Collection Workflow](#metrics-collection-workflow)
5. [Heartbeat System Workflow](#heartbeat-system-workflow)
6. [Status Calculation Workflow](#status-calculation-workflow)
7. [Anomaly Detection Workflow](#anomaly-detection-workflow)
8. [Alert System Workflow](#alert-system-workflow)
9. [App Downtime Handling](#app-downtime-handling)
10. [Dashboard Rendering Flow](#dashboard-rendering-flow)

---

## System Architecture

### Components

```
┌─────────────────────────────────────────────────────────────┐
│                    StackSense Application                    │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │   Django     │    │   Metrics    │    │  Heartbeat   │  │
│  │   Web App    │◄──►│  Scheduler   │◄──►│    Checker   │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│         │                    │                    │          │
│         │                    │                    │          │
│         ▼                    ▼                    ▼          │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Database (PostgreSQL)                    │   │
│  │  - Servers, Metrics, Heartbeats, Alerts, Anomalies   │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                               │
│         │                    │                    │          │
│         ▼                    ▼                    ▼          │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │   Redis      │    │   Ingest     │    │   Email      │  │
│  │   Cache      │    │   Endpoint   │    │   Alerts     │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│                                                               │
└─────────────────────────────────────────────────────────────┘
                              ▲
                              │ HTTPS push (Bearer token)
                              │   — initiated BY each monitored VM
                    ┌──────────────────┐
                    │  Monitored       │
                    │  Servers (agent) │
                    └──────────────────┘
```

### Technology Stack

- **Framework**: Django (Python)
- **Database**: PostgreSQL
- **Cache**: Redis
- **Ingestion**: Push agent → HTTPS POST with per-server bearer token (StackSense never connects out)
- **Web Server**: Gunicorn
- **Reverse Proxy**: Nginx
- **Container**: Docker

---

## Application Startup Flow

### 1. Container Initialization

```
docker-entrypoint.sh (Entry Point)
    │
    ├─► Check Database Connection
    │   └─► Wait up to 30 attempts (60 seconds total)
    │
    ├─► Run Migrations
    │   └─► python manage.py migrate --noinput
    │
    ├─► Collect Static Files
    │   └─► python manage.py collectstatic --noinput --clear
    │
    ├─► Create Superuser (if CREATE_SUPERUSER=true)
    │
    ├─► Start Metrics Scheduler (Background)
    │   └─► python3 metrics_scheduler.py
    │       └─► On a cadence runs analysis/housekeeping (no metric collection — metrics arrive via the push agent):
    │           - check_heartbeats / check_server_connectivity (read pushed heartbeats)
    │           - detect_anomalies (every 5 minutes)
    │           - track_app_heartbeat
    │
    └─► Start Gunicorn Web Server
        └─► gunicorn log_analyzer.wsgi:application --bind 0.0.0.0:8000
```

### 2. Metrics Scheduler Loop

```python
# metrics_scheduler.py runs continuously:

Every cycle:
    1. Track app heartbeat (record that monitoring app is running)
    2. Evaluate freshness of agent-pushed heartbeats (check_heartbeats /
       check_server_connectivity) to mark servers online/offline
    # Metrics themselves are written by the ingest endpoint when the agent POSTs
    # them — the scheduler does NOT collect metrics over the network.

Every 5 minutes:
    3. Run anomaly detection on collected metrics
    4. Generate alerts if anomalies detected
```

---

## Server Registration Flow

### Adding a New Server

```
User fills form (name + IP) → POST /api/add-server/
    │
    ├─► Step 1: Create Server record
    │   └─► Database: INSERT INTO core_server
    │
    ├─► Step 2: Create MonitoringConfig
    │   └─► Database: INSERT INTO core_monitoringconfig
    │
    ├─► Step 3: Issue per-server agent bearer token
    │   └─► Used by the agent to authenticate its HTTPS pushes
    │
    ├─► Step 4: Present the one-line agent install command
    │   └─► `curl … | sudo bash` (run by the admin on the target VM)
    │
    └─► Return success response with server_id + install command
```

There is **no SSH onboarding step** — no username/password/port/key. StackSense
never connects out. The server stays "pending" until the agent is installed and
its first push arrives.

### Agent Onboarding

1. Admin runs the one-line `curl … | sudo bash` installer on the target VM.
2. The installer drops the agent, writes its config (StackSense URL +
   per-server token), and installs a systemd service.
3. The agent begins POSTing metrics/services/containers/heartbeats over HTTPS.
4. StackSense's ingest endpoint authenticates the token and stores the data;
   the server flips to "online" once heartbeats are fresh.

---

## Metrics Collection Workflow

### Agent Push (initiated by each monitored VM)

```
Agent on monitored VM (runs continuously):
    │
    ├─► Collect locally via psutil/procfs:
    │       - CPU usage
    │       - Memory usage
    │       - Disk usage
    │       - Network I/O
    │       - Disk I/O
    │       - System uptime
    │       - Running processes
    │       - Load average
    │       - Services / containers / SSH auth events
    │
    └─► HTTPS POST to StackSense ingest endpoint
        │   (Authorization: Bearer <per-server token>)
        │
        ▼
StackSense ingest endpoint:
    │
    ├─► Authenticate the bearer token → resolve Server
    │
    ├─► Create SystemMetric record
    │   └─► Database: INSERT INTO core_systemmetric
    │       - Stores all pushed metrics
    │       - Links to server via ForeignKey
    │       - Timestamped
    │
    └─► Update ServerHeartbeat
        └─► Database: UPDATE core_serverheartbeat
            SET last_heartbeat = NOW()
            WHERE server_id = <id>
```

StackSense does **not** poll or SSH into servers; data only arrives when the
agent pushes it.

### Metrics Data Structure

```python
SystemMetric {
    server: ForeignKey(Server)
    timestamp: DateTimeField
    cpu_percent: FloatField
    memory_percent: FloatField
    disk_usage: JSONField
    network_io: JSONField
    disk_io: JSONField
    system_uptime_seconds: IntegerField
    load_average: JSONField
    process_count: IntegerField
}
```

---

## Heartbeat System Workflow

### Purpose

The heartbeat system verifies that servers are reachable and responding. There are two types of heartbeats:

1. **Server Heartbeat**: Updated when metrics collection succeeds
2. **App Heartbeat**: Tracks when the monitoring app itself is running

### Server Heartbeat Flow

```
On each agent push (initiated by the monitored VM):
    │
    ├─► Push received and token authenticated?
    │   │
    │   YES ──► UPDATE ServerHeartbeat
    │   │       SET last_heartbeat = NOW()
    │   │
    │   NO ───► Keep existing heartbeat timestamp
    │           (no fresh push → server appears offline)
    │
    └─► Status calculated based on heartbeat age
```

### App Heartbeat Flow

```
Every 30 seconds:
    │
    ├─► track_app_heartbeat command
    │   │
    │   ├─► Write to Redis cache
    │   │   └─► Key: "monitoring_app_heartbeat"
    │   │   └─► Value: Current timestamp (ISO format)
    │   │   └─► TTL: 300 seconds (5 minutes)
    │   │
    │   └─► Write to file
    │       └─► /tmp/monitoring_app_heartbeat.txt
    │       └─► Persists across container restarts
    │
    └─► Used by status calculation to detect app downtime
```

### Heartbeat Check Command

```bash
python manage.py check_heartbeats
# (or check_server_connectivity)
```

This command:
1. Tracks app heartbeat
2. For each server:
   - Reads the freshness of the latest agent-pushed heartbeat (no SSH)
   - Marks the server online/offline based on heartbeat age
   - Sends alerts on status change (online ↔ offline)

---

## Status Calculation Workflow

### Function: `_calculate_server_status(server)`

```
Status Calculation:
    │
    ├─► Step 1: Check monitoring suspended?
    │   └─► YES → Return "offline"
    │
    ├─► Step 2: Determine threshold (adaptive)
    │   │
    │   ├─► Check app heartbeat (cache + file)
    │   │   │
    │   ├─► App was down recently? (>5 minutes ago)
    │   │   │
    │   ├─► YES → Use 10-minute grace period
    │   │   └─► Threshold = 600 seconds
    │   │   └─► Allows servers to show online after app restart
    │   │
    │   └─► NO → Use normal threshold
    │       └─► Threshold = 60 seconds
    │
    ├─► Step 3: Get server heartbeat
    │   └─► SELECT FROM core_serverheartbeat WHERE server_id = <id>
    │
    ├─► Step 4: Calculate heartbeat age
    │   └─► age = NOW() - heartbeat.last_heartbeat
    │
    ├─► Step 5: Compare with threshold
    │   │
    │   ├─► age > threshold?
    │   │   │
    │   ├─► YES → Return "offline"
    │   │
    │   └─► NO → Continue to Step 6
    │
    ├─► Step 6: Check for active alerts/anomalies
    │   │
    │   ├─► Active anomalies? (unresolved)
    │   ├─► Active alerts? (status="triggered")
    │   │
    │   ├─► YES → Return "warning"
    │   │
    │   └─► NO → Return "online"
    │
    └─► Status returned: "offline" | "warning" | "online"
```

### Status States

| Status  | Meaning | Conditions |
|---------|---------|------------|
| **online** | Server is healthy | Heartbeat < threshold AND no active alerts/anomalies |
| **warning** | Server has issues | Heartbeat < threshold BUT has active alerts/anomalies |
| **offline** | Server unreachable | Heartbeat > threshold OR monitoring suspended |

---

## Anomaly Detection Workflow

### Periodic Detection (Every 5 minutes)

```
detect_anomalies command:
    │
    ├─► For each server:
    │   │
    │   ├─► Get recent metrics (last 30 minutes)
    │   │
    │   ├─► Calculate statistical baselines:
    │   │   ├─► CPU: Mean, StdDev
    │   │   ├─► Memory: Mean, StdDev
    │   │   ├─► Disk: Mean, StdDev
    │   │   └─► Network: Mean, StdDev
    │   │
    │   ├─► Compare current metrics to baselines:
    │   │   ├─► CPU > (mean + 2*stddev)?
    │   │   ├─► Memory > (mean + 2*stddev)?
    │   │   ├─► Disk usage > threshold?
    │   │   └─► Network spike detected?
    │   │
    │   ├─► If anomaly detected:
    │   │   ├─► Create Anomaly record
    │   │   │   └─► Database: INSERT INTO core_anomaly
    │   │   │       - server: ForeignKey
    │   │   │       - metric_type: "cpu" | "memory" | "disk" | "network"
    │   │   │       - severity: "low" | "medium" | "high"
    │   │   │       - resolved: False
    │   │   │       - detected_at: NOW()
    │   │   │
    │   │   └─► Trigger alert (if configured)
    │   │
    │   └─► Continue to next server
    │
    └─► Log summary (anomalies detected count)
```

### Anomaly Resolution

Anomalies are marked as resolved when:
1. Metrics return to normal levels
2. User manually resolves via UI
3. Server monitoring is suspended

---

## Alert System Workflow

### Alert Configuration

Each server has an `EmailAlertConfig` that defines:
- SMTP server settings
- Recipient email addresses
- Alert thresholds
- Enabled/disabled status

### Alert Triggering

```
Alert Conditions:
    │
    ├─► Server Status Change:
    │   │
    │   ├─► Online → Offline
    │   │   └─► _send_connection_alert(server, "offline")
    │   │       └─► Send email: "Server is OFFLINE"
    │   │
    │   └─► Offline → Online
    │       └─► _send_connection_alert(server, "online")
    │           └─► Send email: "Server connection restored"
    │
    ├─► Anomaly Detected:
    │   └─► _send_anomaly_alert(anomaly)
    │       └─► Send email: "Anomaly detected: <metric_type>"
    │
    └─► Service Status Change:
        │
        ├─► Service Down
        │   └─► _send_service_alert(server, service, "down")
        │       └─► Send email: "Service <name> is DOWN"
        │
        └─► Service Up
            └─► _send_service_alert(server, service, "up")
                └─► Send email: "Service <name> is UP"
```

### Alert History

All alerts are logged to `core_alerthistory` table:
- Timestamp
- Server reference
- Alert type
- Status (triggered/resolved)
- Recipients
- Message content

---

## App Downtime Handling

### Problem

When the monitoring application itself goes down:
- Server heartbeats stop updating
- Fixed 60-second threshold marks all servers offline
- False offline alerts sent when app restarts

### Solution: Adaptive Threshold

```
Status Calculation with App Downtime Detection:
    │
    ├─► Check app heartbeat timestamp
    │   │
    │   ├─► Read from Redis cache (monitoring_app_heartbeat)
    │   └─► Fallback to file (/tmp/monitoring_app_heartbeat.txt)
    │
    ├─► Calculate app downtime
    │   └─► app_downtime = NOW() - app_last_heartbeat
    │
    ├─► Determine threshold:
    │   │
    │   ├─► app_downtime > 5 minutes?
    │   │   │
    │   ├─► YES → App was down recently
    │   │   │   └─► Use 10-minute grace period
    │   │   │   └─► Threshold = 600 seconds
    │   │   │   └─► Prevents false offline statuses
    │   │   │
    │   └─► NO → App running normally
    │       └─► Use normal threshold
    │       └─► Threshold = 60 seconds
    │
    └─► Apply threshold to server heartbeat age
```

### Benefits

1. **Grace Period**: 10 minutes after app restart
2. **Prevents False Alerts**: Servers don't show offline immediately
3. **Automatic Detection**: No manual intervention needed
4. **Graceful Degradation**: Falls back to 60 seconds if tracking fails

---

## Dashboard Rendering Flow

### Monitoring Dashboard (`/monitoring/`)

```
Request → monitoring_dashboard view
    │
    ├─► Fetch all servers
    │   └─► SELECT FROM core_server ORDER BY name
    │
    ├─► For each server:
    │   │
    │   ├─► Get latest metric
    │   │   └─► SELECT FROM core_systemmetric
    │   │       WHERE server_id = <id>
    │   │       ORDER BY timestamp DESC LIMIT 1
    │   │
    │   ├─► Calculate server status
    │   │   └─► _calculate_server_status(server)
    │   │
    │   ├─► Get active anomalies
    │   │   └─► SELECT FROM core_anomaly
    │   │       WHERE server_id = <id> AND resolved = False
    │   │
    │   ├─► Get active alerts
    │   │   └─► SELECT FROM core_alerthistory
    │   │       WHERE server_id = <id> AND status = "triggered"
    │   │
    │   ├─► Calculate uptime
    │   │   └─► From latest metric.system_uptime_seconds
    │   │
    │   └─► Build server data object
    │       - server info
    │       - status (online/warning/offline)
    │       - metrics (CPU, memory, disk, network)
    │       - uptime
    │       - alerts count
    │
    ├─► Calculate summary statistics:
    │   ├─► online_count
    │   ├─► warning_count
    │   ├─► offline_count
    │   └─► alert_count
    │
    ├─► Render template (core/monitoring_dashboard.html)
    │   └─► Pass server data and summary stats
    │
    └─► Return HTML response
```

### Server Details Page (`/server/<id>/`)

```
Request → server_details view
    │
    ├─► Get server by ID
    │
    ├─► Get latest metric
    │
    ├─► Get recent metrics (last 100)
    │   └─► For charts/graphs
    │
    ├─► Get active services
    │   └─► SELECT FROM core_service WHERE server_id = <id>
    │
    ├─► Get recent anomalies
    │   └─► SELECT FROM core_anomaly WHERE server_id = <id>
    │       ORDER BY detected_at DESC LIMIT 50
    │
    ├─► Get disk summary
    │   └─► Aggregate disk usage from latest metric
    │
    ├─► Calculate server status
    │
    └─► Render template (core/server_details.html)
```

---

## Data Flow Summary

```
┌──────────────────┐
│ Servers (agent)  │
└──────┬───────────┘
       │ HTTPS push (Bearer token), initiated by the agent
       ▼
┌──────────────────┐
│  Ingest Endpoint │
└──────┬───────────┘
       │
       ▼
┌──────────────────┐      ┌──────────────┐
│   PostgreSQL     │◄────►│    Redis     │
│   Database       │      │    Cache     │
└──────┬───────────┘      └──────────────┘
       │
       ▼
┌──────────────────┐
│ Status Calculator│
└──────┬───────────┘
       │
       ▼
┌──────────────────┐      ┌──────────────┐
│   Dashboard      │      │   Alert      │
│   Views          │      │   System     │
└──────────────────┘      └──────────────┘
       │                          │
       ▼                          ▼
┌──────────────┐         ┌──────────────┐
│   Browser    │         │     Email    │
│   (UI)       │         │   (SMTP)     │
└──────────────┘         └──────────────┘
```

---

## Key Configuration Points

### Environment Variables

- `POSTGRES_HOST`: Database host
- `POSTGRES_PORT`: Database port (default: 5432)
- `POSTGRES_DB`: Database name
- `POSTGRES_USER`: Database user
- `POSTGRES_PASSWORD`: Database password
- `REDIS_HOST`: Redis host
- `REDIS_PORT`: Redis port (default: 6379)
- `ALLOWED_HOSTS`: Comma-separated list of allowed hosts
- `CSRF_TRUSTED_ORIGINS`: Comma-separated list of trusted origins
- `USE_TLS`: Enable TLS/HTTPS mode ("True"/"False")

### Timing Configuration

- **Metrics Collection**: Every 30 seconds
- **Anomaly Detection**: Every 5 minutes (300 seconds)
- **Heartbeat Threshold (normal)**: 60 seconds
- **Heartbeat Threshold (after app restart)**: 600 seconds (10 minutes)
- **App Heartbeat Cache TTL**: 300 seconds (5 minutes)

---

## Error Handling

### Missing Agent Pushes

- No fresh push → heartbeat not updated
- Server status shows as "offline" once the heartbeat ages past the threshold
- Scheduler keeps running; a stale server does not crash it

### Database Connection Failures

- Connection pooling via Django
- Automatic retries on query failures
- Graceful degradation (cache fallback if available)

### Cache Failures

- Redis unavailable: Falls back to database queries
- App heartbeat tracking: Falls back to file-based storage
- No impact on core functionality

---

## Performance Considerations

### Database Optimization

- Indexes on:
  - `core_serverheartbeat.last_heartbeat`
  - `core_systemmetric.timestamp`
  - `core_systemmetric.server_id`
- Query optimization with `select_related()` and `only()`

### Caching Strategy

- Server status cached in Redis
- Cache key: `server_status:{server_id}`
- TTL: Varies by use case
- Cache invalidation on status changes

### Scalability

- Ingestion is push-based; agents POST concurrently and independently
- Database queries optimized for large datasets
- Pagination on list views
- Heartbeat checks read stored push timestamps (no outbound connections)

---

## Security Considerations

1. **Per-Server Agent Tokens**: Each agent authenticates with a bearer token scoped to one server; tokens can be rotated/revoked. StackSense never holds SSH credentials for monitored servers.
2. **Database Credentials**: Environment variables, not hardcoded
3. **CSRF Protection**: Enabled for all POST requests
4. **Authentication**: Django admin authentication required
5. **HTTPS Support**: Configurable via USE_TLS
6. **SQL Injection**: Django ORM prevents SQL injection
7. **XSS Protection**: Django template escaping

---

This workflow document provides a comprehensive overview of how the StackSense monitoring application operates. For implementation details, refer to the source code and API documentation.
