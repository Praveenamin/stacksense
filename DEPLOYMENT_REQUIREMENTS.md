# StackSense Deployment Requirements

This document outlines all requirements and prerequisites for deploying the StackSense monitoring application.

---

## System Requirements

### Minimum Hardware Specifications

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **CPU** | 2 cores | 4+ cores |
| **RAM** | 2 GB | 4+ GB |
| **Disk Space** | 10 GB | 20+ GB |
| **Network** | 10 Mbps | 100+ Mbps |

### Operating System

- **Supported OS**: Ubuntu 20.04 LTS, Ubuntu 22.04 LTS, Debian 11+, CentOS 8+
- **Architecture**: x86_64 (64-bit)
- **Kernel**: Linux 5.4+ (for Docker support)

---

## Software Dependencies

### Required System Packages

The deployment script will automatically install these, but they must be available in your package repositories:

```bash
# Core utilities
curl
wget
git
openssl

# Python
python3 (3.8+)
python3-pip
python3-venv

# Docker
docker.io (or Docker CE)
docker-compose

# Web Server
nginx

# SSL/TLS
certbot
python3-certbot-nginx

# Database tools
postgresql-client

# Cache tools
redis-tools

# Firewall
ufw (Uncomplicated Firewall)
```

### Python Requirements

All Python dependencies are listed in `requirements.txt`:

```
Django>=5.2.8
psycopg2-binary>=2.9.9
gunicorn>=21.2.0
paramiko>=3.4.0
psutil>=5.9.0
pandas>=2.0.0
numpy>=1.24.0
scipy>=1.11.0
scikit-learn>=1.3.0
adtk>=0.2.2
requests>=2.31.0
redis>=5.0.0
django-redis>=5.4.0
django-jazzmin>=3.0.0
whitenoise>=6.6.0
```

---

## Network Requirements

### Ports That Must Be Open

| Port | Protocol | Purpose | Required |
|------|----------|---------|----------|
| **22** | TCP | SSH access | Yes |
| **80** | TCP | HTTP (for Let's Encrypt) | Optional |
| **443** | TCP | HTTPS (standard) | Optional |
| **8005** | TCP | HTTPS (custom port) | Yes |
| **8000** | TCP | Django app (internal) | No (localhost only) |
| **5433** | TCP | PostgreSQL (internal) | No (localhost only) |
| **6379** | TCP | Redis (internal) | No (localhost only) |

### Firewall Configuration

- **Inbound**: Allow ports 22, 80, 443, and 8005
- **Outbound**: Allow all (for package installation and updates)

---

## DNS Requirements

### Domain Configuration

1. **A Record**: Point your domain to the server's public IP address
   ```
   stacksense.assistanz.com → [Server IP Address]
   ```

2. **Optional - CNAME**: For www subdomain
   ```
   www.stacksense.assistanz.com → stacksense.assistanz.com
   ```

### DNS Propagation

- Allow 24-48 hours for DNS propagation
- Verify DNS resolution before deployment:
  ```bash
  dig stacksense.assistanz.com
  nslookup stacksense.assistanz.com
  ```

---

## SSL/TLS Certificate Options

### Option 1: Self-Signed Certificate (Default)

- **Pros**: Quick setup, no external dependencies
- **Cons**: Browser security warnings, not trusted by default
- **Use Case**: Development, internal networks, testing

### Option 2: Let's Encrypt Certificate (Recommended for Production)

- **Pros**: Free, trusted by browsers, auto-renewal
- **Cons**: Requires valid domain and port 80 access
- **Requirements**:
  - Valid domain name
  - Port 80 accessible from internet
  - Email address for notifications

**To use Let's Encrypt after deployment:**
```bash
sudo certbot --nginx -d stacksense.assistanz.com \
  --non-interactive --agree-tos \
  --email admin@example.com --redirect
```

---

## Access Requirements

### SSH Access

- **User**: Root or user with sudo privileges
- **Authentication**: SSH key (recommended) or password
- **Port**: 22 (default)

### Repository Access

- **Git Repository**: Access to clone the application code
- **Options**:
  - Public repository (no authentication needed)
  - Private repository (SSH key or token required)
  - Local files (copy files to server manually)

---

## Pre-Deployment Checklist

Before running the deployment script, ensure:

- [ ] Server meets minimum hardware requirements
- [ ] Operating system is supported (Ubuntu 20.04+ or Debian 11+)
- [ ] Root or sudo access is available
- [ ] Domain DNS A record points to server IP
- [ ] Firewall allows required ports (22, 80, 443, 8005)
- [ ] At least 10GB free disk space
- [ ] Internet connection is available (for package installation)
- [ ] Git repository is accessible (if using Git deployment)

---

## Deployment Steps

### Quick Start

1. **Clone or copy the repository to the server**
   ```bash
   git clone https://github.com/your-org/stacksense-repo.git /opt/stacksense
   # OR copy files manually
   ```

2. **Make deployment script executable**
   ```bash
   chmod +x /opt/stacksense/deploy.sh
   ```

3. **Run deployment script**
   ```bash
   sudo /opt/stacksense/deploy.sh stacksense.assistanz.com admin@example.com
   ```

4. **Access the application**
   ```
   https://stacksense.assistanz.com:8005
   ```

### Manual Deployment Steps

If you prefer to deploy manually:

1. Install system dependencies
2. Install Docker and Docker Compose
3. Clone/copy application code
4. Create Docker network
5. Start PostgreSQL container
6. Start Redis container
7. Build and start Django application container
8. Generate SSL certificate
9. Configure Nginx
10. Configure firewall

---

## Post-Deployment

### Verify Deployment

1. **Check containers are running**
   ```bash
   docker ps
   ```

2. **Check application logs**
   ```bash
   docker logs monitoring_web
   ```

3. **Test application**
   ```bash
   curl -k https://stacksense.assistanz.com:8005
   ```

4. **Access admin panel**
   ```
   https://stacksense.assistanz.com:8005/admin/
   ```

### Important Files Created

- **Credentials**: `/opt/stacksense/deployment_credentials.txt`
- **Environment**: `/opt/stacksense/.env`
- **Nginx Config**: `/etc/nginx/sites-available/stacksense.assistanz.com`
- **SSL Certificates**: `/etc/nginx/ssl/`

### Default Credentials

After deployment, check `/opt/stacksense/deployment_credentials.txt` for:
- Django admin username and password
- Database credentials
- Redis password

**⚠️ Important**: Change default passwords after first login!

---

## Troubleshooting

### Common Issues

1. **Port already in use**
   - Check what's using the port: `sudo netstat -tlnp | grep 8005`
   - Stop conflicting service or change port in deployment script

2. **Docker permission denied**
   - Add user to docker group: `sudo usermod -aG docker $USER`
   - Log out and log back in

3. **Database connection failed**
   - Wait for database to be ready: `docker exec monitoring_db pg_isready`
   - Check database logs: `docker logs monitoring_db`

4. **SSL certificate errors**
   - For self-signed: Accept browser warning
   - For Let's Encrypt: Ensure port 80 is accessible

5. **Nginx configuration errors**
   - Test config: `sudo nginx -t`
   - Check logs: `sudo tail -f /var/log/nginx/error.log`

---

## Maintenance

### Updating the Application

```bash
cd /opt/stacksense
git pull  # If using Git
docker restart monitoring_web
docker exec monitoring_web python manage.py migrate
docker exec monitoring_web python manage.py collectstatic --noinput
```

### Backup

```bash
# Database backup
docker exec monitoring_db pg_dump -U monitoring_user monitoring_db > backup.sql

# Application files
tar -czf stacksense_backup_$(date +%Y%m%d).tar.gz /opt/stacksense
```

### Monitoring

- **Application logs**: `docker logs -f monitoring_web`
- **Nginx logs**: `sudo tail -f /var/log/nginx/stacksense.assistanz.com_access.log`
- **System resources**: `htop` or `docker stats`

---

## Security Considerations

1. **Change default passwords** immediately after deployment
2. **Use Let's Encrypt** for production (not self-signed certificates)
3. **Configure firewall** to only allow necessary ports
4. **Keep system updated**: `sudo apt update && sudo apt upgrade`
5. **Regular backups** of database and application files
6. **Monitor logs** for suspicious activity
7. **Use SSH keys** instead of passwords for SSH access

---

## Support

For issues or questions:
- Check logs: `docker logs monitoring_web`
- Review deployment script output
- Verify all requirements are met
- Check firewall and DNS configuration

---

## Version Information

- **Deployment Script Version**: 1.0
- **Last Updated**: December 2025
- **Compatible with**: StackSense v1.0+








