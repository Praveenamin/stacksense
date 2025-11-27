# Dockerized Server Monitoring System

Comprehensive Linux server monitoring with anomaly detection, built with Django and Docker.

## Features

- **SSH Key Management**: Automatic SSH public key deployment to monitored servers
- **Enhanced Resource Monitoring**: 
  - CPU: Physical cores, logical cores, usage percentage
  - RAM: Total, used, available, buffers, cached, shared memory
  - Disk: Disk count, types (SSD/HDD/NVMe), RAID detection, partition-level usage
- **Service Detection**: Fast, lightweight systemctl-based service scanning
- **Anomaly Detection**: ADTK (primary) and IsolationForest (fallback) for time-series anomaly detection
- **LLM Explanations**: Human-readable anomaly explanations using Ollama
- **Data Retention**: Automatic aggregation and cleanup of old metrics
- **Adaptive Collection**: Configurable and adaptive metric collection frequency

## Quick Start

1. Copy `.env.example` to `.env` and configure:
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

2. Generate SSH keys (if not exists):
   ```bash
   mkdir -p ssh_keys
   ssh-keygen -t rsa -b 4096 -f ssh_keys/id_rsa -N ""
   ```

3. Build and run with Docker Compose:
   ```bash
   docker-compose up -d
   ```

4. Create superuser:
   ```bash
   docker-compose exec web python manage.py createsuperuser
   ```

5. Access the application:
   - Web UI: http://localhost:8000
   - Admin: http://localhost:8000/admin

## Development

```bash
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up
```

## Production

```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Management Commands

- `collect_metrics`: Collect metrics from all enabled servers
- `detect_anomalies`: Run anomaly detection on collected metrics
- `aggregate_metrics`: Aggregate old metrics into hourly/daily summaries
- `cleanup_metrics`: Delete old raw metrics based on retention period

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
