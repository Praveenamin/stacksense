# Dockerized Server Monitoring System

Comprehensive Linux server monitoring with anomaly detection, built with Django and Docker.

## Features

- **Push-Agent Monitoring**: A lightweight agent installed on each monitored VM POSTs metrics, services, and containers to StackSense over HTTPS using a per-server bearer token. StackSense never connects out to monitored servers.
- **Enhanced Resource Monitoring**: 
  - CPU: Physical cores, logical cores, usage percentage
  - RAM: Total, used, available, buffers, cached, shared memory
  - Disk: Disk count, types (SSD/HDD/NVMe), RAID detection, partition-level usage
- **Service Detection**: Fast, lightweight systemctl-based service scanning
- **Anomaly Detection**: ADTK (primary) and IsolationForest (fallback) for time-series anomaly detection
- **LLM Explanations**: Human-readable anomaly explanations using Ollama
- **Data Retention**: Automatic aggregation and cleanup of old metrics
- **Adaptive Collection**: Configurable and adaptive metric collection frequency

## Deploy on a New Server

See **[DEPLOY_NEW_SERVER.md](DEPLOY_NEW_SERVER.md)** for step-by-step instructions. Summary:

- **Automated**: Copy repo to `/opt/stacksense`, then `sudo ./deploy.sh YOUR_DOMAIN admin@example.com`. Access at `https://YOUR_DOMAIN:8005`.
- **Docker Compose only**: `cp .env.example .env`, configure it, then `docker compose up -d` and create a superuser.
- **Migration**: Fresh deploy on new server, then run `./migrate_to_new_server.sh user@old user@new`.

## Quick Start (local development)

1. Copy `.env.example` to `.env` and configure:
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

2. Build and run with Docker Compose:
   ```bash
   docker-compose up -d
   ```

3. Create superuser:
   ```bash
   docker-compose exec web python manage.py createsuperuser
   ```

4. Access the application:
   - Web UI: http://localhost:8000
   - Admin: http://localhost:8000/admin

To monitor a server, add it from the web UI and run the generated one-line
`curl … | sudo bash` agent install command on the target VM. The agent then
pushes metrics to StackSense; no SSH access from StackSense is required.

## Development

```bash
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up
```

## Production

```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Management Commands

Metrics arrive via the push agent, so there is no metric-collection command. The
in-container scheduler (`metrics_scheduler.py`) runs the analysis/housekeeping
commands on a cadence; they can also be run manually:

- `check_heartbeats` / `check_server_connectivity`: Evaluate pushed heartbeats to mark servers up/down (no SSH).
- `collect_service_latency`: Probe reachable services over TCP/HTTP (localhost-only services are skipped).
- `run_synthetic_checks`: Run synthetic uptime/latency checks.
- `detect_anomalies`: Run anomaly detection on collected metrics.
- `detect_memory_leaks`: Detect sustained memory-growth patterns.
- `detect_security_events`: Detect security events (e.g. SSH brute-force) from agent-pushed auth data.
- `scan_logs`: Housekeeping — purge old LogEvent rows (no longer collects logs over SSH).
- `aggregate_metrics`: Aggregate old metrics into hourly/daily summaries.
- `cleanup_metrics`: Delete old raw metrics based on retention period.
- `create_agent_token`: Issue or rotate a per-server agent bearer token.

## Roles & Access Control (RBAC)

Server-enforced roles (Admin / CEO / Operator), account impersonation, and an
audit trail. See [docs/RBAC.md](docs/RBAC.md).

## Executive Dashboard (VM right-sizing)

The Executive persona surfaces CPU/memory right-sizing recommendations with
confidence gating and cost estimates. See
[docs/EXECUTIVE_DASHBOARD.md](docs/EXECUTIVE_DASHBOARD.md) for thresholds,
confidence tiers, pricing, and how to tune them.

## Architecture

- **Web**: Django application (Gunicorn)
- **Database**: PostgreSQL
- **Cache**: Redis (optional)
- **LLM**: Ollama (external service)

## Kubernetes Ready

The application is designed to be Kubernetes-ready with:
- Health check endpoints (`/health/`, `/ready/`)
- Stateless design
- Environment-based configuration
- Horizontal scaling support
