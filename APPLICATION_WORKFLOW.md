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
│  │   Redis      │    │   SSH        │    │   Email      │  │
│  │   Cache      │    │   Connection │    │   Alerts     │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│                                                               │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ SSH Connection
                              ▼
                    ┌──────────────────┐
                    │  Monitored       │
                    │  Servers         │
                    └──────────────────┘
```

### Technology Stack

- **Framework**: Django (Python)
- **Database**: PostgreSQL
- **Cache**: Redis
- **SSH**: Paramiko (Python library)
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
    │       └─► Runs every 30 seconds:
    │           - collect_metrics
    │           - detect_anomalies (every 5 minutes)
    │           - track_app_heartbeat
    │
    └─► Start Gunicorn Web Server
        └─► gunicorn log_analyzer.wsgi:application --bind 0.0.0.0:8000
```

### 2. Metrics Scheduler Loop

```python
# metrics_scheduler.py runs continuously:

Every 30 seconds:
    1. Track app heartbeat (record that monitoring app is running)
    2. Collect metrics from all servers (SSH connection)
    3. Store metrics in database
    
Every 5 minutes:
    4. Run anomaly detection on collected metrics
    5. Generate alerts if anomalies detected
```

---

## Server Registration Flow

### Adding a New Server

```
User fills form → POST /api/add-server/
    │
    ├─► Step 1: Create Server record
    │   └─► Database: INSERT INTO core_server
    │
    ├─► Step 2: Create MonitoringConfig
    │   └─► Database: INSERT INTO core_monitoringconfig
    │
    ├─► Step 3: Deploy SSH Key
    │   ├─► Connect to server via SSH (password auth)
    │   ├─► Install public key to ~/.ssh/authorized_keys
    │   └─► Mark ssh_key_deployed = True
    │
    ├─► Step 4: Install psutil on target server
    │   └─► SSH: pip install psutil
    │
    ├─► Step 5: Create initial ServerHeartbeat record
    │   └─► Database: INSERT INTO core_serverheartbeat
    │
    ├─► Step 6: Collect initial metrics
    │   └─► First metrics collection run
    │
    └─► Return success response with server_id
```

### SSH Key Deployment

1. Connect to server using password authentication
2. Read public SSH key from `/app/ssh_keys/id_rsa.pub`
3. Append to `~/.ssh/authorized_keys` on target server
4. Verify key deployment
5. Future connections use key-based authentication

---

## Metrics Collection Workflow

### Periodic Collection (Every 30 seconds)

```
metrics_scheduler.py triggers:
    │
    └─► collect_metrics command
        │
        ├─► For each server:
        │   │
        │   ├─► Check if monitoring suspended
        │   │   └─► Skip if suspended
        │   │
        │   ├─► Establish SSH connection
        │   │   ├─► Use SSH key from /app/ssh_keys/id_rsa
        │   │   ├─► Connect to server.ip_address:server.port
        │   │   └─► Authenticate as server.username
        │   │
        │   ├─► Execute metrics collection script via SSH:
        │   │   │
        │   │   └─► Python script collects:
        │   │       - CPU usage (psutil.cpu_percent)
        │   │       - Memory usage (psutil.virtual_memory)
        │   │       - Disk usage (psutil.disk_usage)
        │   │       - Network I/O (psutil.net_io_counters)
        │   │       - Disk I/O (psutil.disk_io_counters)
        │   │       - System uptime
        │   │       - Running processes
        │   │       - Load average
        │   │
        │   ├─► Parse JSON response from SSH command
        │   │
        │   ├─► Create SystemMetric record
        │   │   └─► Database: INSERT INTO core_systemmetric
        │   │       - Stores all collected metrics
        │   │       - Links to server via ForeignKey
        │   │       - Timestamped
        │   │
        │   └─► Update ServerHeartbeat
        │       └─► Database: UPDATE core_serverheartbeat
        │           SET last_heartbeat = NOW()
        │           WHERE server_id = <id>
        │
        └─► Log results (success/failure count)
```

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
Every 30 seconds (during metrics collection):
    │
    ├─► SSH connection successful?
    │   │
    │   YES ──► UPDATE ServerHeartbeat
    │   │       SET last_heartbeat = NOW()
    │   │
    │   NO ───► Keep existing heartbeat timestamp
    │           (server appears offline)
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
python manage.py check_heartbeats_ssh
```

This command:
1. Tracks app heartbeat
2. For each server:
   - Attempts SSH connection (5-second timeout)
   - Updates heartbeat on success
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
┌─────────────┐
│   Servers   │
└──────┬──────┘
       │ SSH (every 30s)
       ▼
┌──────────────────┐
│ Metrics Collector│
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
- `SSH_PRIVATE_KEY_PATH`: Path to SSH private key (/app/ssh_keys/id_rsa)

### Timing Configuration

- **Metrics Collection**: Every 30 seconds
- **Anomaly Detection**: Every 5 minutes (300 seconds)
- **Heartbeat Threshold (normal)**: 60 seconds
- **Heartbeat Threshold (after app restart)**: 600 seconds (10 minutes)
- **App Heartbeat Cache TTL**: 300 seconds (5 minutes)

---

## Error Handling

### SSH Connection Failures

- Retry logic in metrics collection
- Heartbeat not updated on failure
- Server status shows as "offline"
- Error logged but doesn't crash scheduler

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

- Metrics collection runs asynchronously (background scheduler)
- Database queries optimized for large datasets
- Pagination on list views
- Efficient heartbeat checks (SSH connection pooling)

---

## Security Considerations

1. **SSH Key Management**: Private keys stored securely, not in code
2. **Database Credentials**: Environment variables, not hardcoded
3. **CSRF Protection**: Enabled for all POST requests
4. **Authentication**: Django admin authentication required
5. **HTTPS Support**: Configurable via USE_TLS
6. **SQL Injection**: Django ORM prevents SQL injection
7. **XSS Protection**: Django template escaping

---

This workflow document provides a comprehensive overview of how the StackSense monitoring application operates. For implementation details, refer to the source code and API documentation.
