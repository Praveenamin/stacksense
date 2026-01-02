# StackSense Deployment Guide - Step by Step

Complete guide to deploy StackSense monitoring application on a new server.

---

## Prerequisites Checklist

Before starting, ensure you have:

- [ ] **Server**: Ubuntu 20.04+ or Debian 11+ with root/sudo access
- [ ] **Domain**: Domain name pointing to server IP (e.g., `stacksense.assistanz.com`)
- [ ] **SSH Access**: SSH key or password access to the server
- [ ] **Minimum Resources**: 2GB RAM, 10GB disk space
- [ ] **Ports Open**: 22 (SSH), 80, 443, 8005 (HTTPS)
- [ ] **Internet**: Active internet connection on server

---

## Step 1: Prepare Your Local Machine

### 1.1 Download/Copy Application Files

**Option A: If you have Git repository**
```bash
# Clone the repository
git clone https://github.com/your-org/stacksense-repo.git
cd stacksense-repo
```

**Option B: If you have files locally**
```bash
# Navigate to your application directory
cd /path/to/stacksense-repo
```

### 1.2 Verify Files

Ensure these files exist:
- `deploy.sh` (deployment script)
- `requirements.txt` (Python dependencies)
- `core/` (Django application)
- `manage.py` (Django management script)

---

## Step 2: Connect to New Server

### 2.1 SSH into the Server

```bash
# Replace with your server details
ssh user@your-server-ip

# Or with domain
ssh user@stacksense.assistanz.com
```

### 2.2 Verify Server Access

```bash
# Check OS version
cat /etc/os-release

# Check available disk space
df -h

# Check available memory
free -h

# Check if you have sudo access
sudo whoami
```

---

## Step 3: Transfer Application Files to Server

### 3.1 Create Application Directory

```bash
# On the server
sudo mkdir -p /opt/stacksense
sudo chown $USER:$USER /opt/stacksense
```

### 3.2 Copy Files from Local Machine

**From your local machine**, run:

```bash
# Replace 'user@server-ip' with your server details
scp -r stacksense-repo/* user@your-server-ip:/opt/stacksense/

# Or use rsync (more efficient)
rsync -avz --progress stacksense-repo/ user@your-server-ip:/opt/stacksense/
```

**Alternative: If you have Git on server**
```bash
# On the server
cd /opt/stacksense
git clone https://github.com/your-org/stacksense-repo.git .
```

---

## Step 4: Run Deployment Script

### 4.1 Make Script Executable

```bash
# On the server
cd /opt/stacksense
chmod +x deploy.sh
```

### 4.2 Run Deployment

```bash
# Replace with your domain and email
sudo ./deploy.sh stacksense.assistanz.com admin@example.com
```

**What the script does:**
1. ✅ Installs system dependencies (Docker, Nginx, Python, etc.)
2. ✅ Sets up PostgreSQL database container
3. ✅ Sets up Redis cache container
4. ✅ Builds and starts Django application
5. ✅ Generates self-signed SSL certificate
6. ✅ Configures Nginx reverse proxy
7. ✅ Sets up firewall rules
8. ✅ Creates admin user

**Expected output:**
```
========================================
StackSense Deployment Script
========================================

Domain: stacksense.assistanz.com
Email: admin@example.com
App Directory: /opt/stacksense

[1/10] Checking system requirements...
[2/10] Installing system dependencies...
[3/10] Setting up application directory...
...
[10/10] Configuring firewall...

========================================
Deployment Completed Successfully!
========================================
```

### 4.3 Note the Credentials

The script will display and save credentials:
- Database password
- Django admin username and password
- Redis password

**Save these credentials!** They're also saved to:
```
/opt/stacksense/deployment_credentials.txt
```

---

## Step 5: Verify Deployment

### 5.1 Check Docker Containers

```bash
# Check all containers are running
docker ps

# Should show:
# - monitoring_web (Django app)
# - monitoring_db (PostgreSQL)
# - monitoring_redis (Redis)
```

### 5.2 Check Application Status

```bash
# Test Django application
docker exec monitoring_web python manage.py check

# View application logs
docker logs monitoring_web

# Check metrics scheduler
docker exec monitoring_web ps aux | grep metrics_scheduler
```

### 5.3 Test Web Access

```bash
# Test from server
curl -k https://localhost:8005

# Or test from browser
# https://stacksense.assistanz.com:8005
```

**Note**: Browser will show SSL warning (self-signed certificate). Click "Advanced" → "Proceed" to continue.

---

## Step 6: Access the Application

### 6.1 Open in Browser

```
https://stacksense.assistanz.com:8005
```

### 6.2 Login to Admin Panel

```
https://stacksense.assistanz.com:8005/admin/
```

**Default credentials** (from deployment output):
- Username: `admin` (or as specified)
- Password: (check `/opt/stacksense/deployment_credentials.txt`)

### 6.3 Change Default Password

**Important**: Change the default admin password immediately after first login!

---

## Step 7: Configure Your First Server

### 7.1 Add a Server to Monitor

1. Go to **Instances** → **Add Server**
2. Fill in server details:
   - **Server Name**: e.g., "Web Server 1"
   - **IP Address**: Server's IP
   - **Port**: 22 (SSH)
   - **Username**: SSH username
   - **SSH Key**: Upload or paste SSH private key

3. Click **Save**

### 7.2 Verify Server Connection

- Server should appear in the Instances list
- Status should show "Online" after a few seconds
- Metrics should start collecting automatically

---

## Step 8: (Optional) Setup Let's Encrypt SSL

If you want to use a trusted SSL certificate instead of self-signed:

### 8.1 Install Certbot (if not already installed)

```bash
sudo apt-get update
sudo apt-get install -y certbot python3-certbot-nginx
```

### 8.2 Obtain SSL Certificate

```bash
# Replace with your domain and email
sudo certbot --nginx -d stacksense.assistanz.com \
  --non-interactive --agree-tos \
  --email admin@example.com --redirect
```

### 8.3 Update Nginx Configuration

Certbot will automatically update Nginx configuration. If using custom port (8005), you may need to manually update:

```bash
sudo nano /etc/nginx/sites-available/stacksense.assistanz.com
# Change listen port from 8005 to 443
```

---

## Step 9: Post-Deployment Tasks

### 9.1 Setup Cron Jobs (Optional)

For automated tasks, you can set up cron jobs:

```bash
# Setup service monitoring cron
sudo /opt/stacksense/setup_services_cron.sh

# Setup heartbeat monitoring cron
sudo /opt/stacksense/setup_heartbeat_cron.sh
```

### 9.2 Configure Email Alerts

1. Go to **Alerts Configure** → **Alert Configuration**
2. Enter SMTP settings:
   - SMTP Host
   - Port
   - Username/Password
   - From/To email addresses
3. Click **Save Configuration**
4. Test email alert

### 9.3 Setup Log Monitoring

1. Go to **Logs Analysis** → **Log Troubleshooting Configuration**
2. Select server and service type
3. Configure log path
4. Click **Begin Troubleshooting**

---

## Troubleshooting

### Issue: Deployment Script Fails

**Check logs:**
```bash
# View script output
sudo ./deploy.sh 2>&1 | tee deployment.log

# Check system logs
sudo journalctl -xe
```

**Common fixes:**
- Ensure you have sudo/root access
- Check internet connection
- Verify disk space: `df -h`
- Check if ports are available: `sudo netstat -tlnp | grep -E '8000|8005|5433'`

### Issue: Containers Not Starting

```bash
# Check container logs
docker logs monitoring_web
docker logs monitoring_db
docker logs monitoring_redis

# Check Docker status
sudo systemctl status docker

# Restart containers
docker restart monitoring_web monitoring_db monitoring_redis
```

### Issue: Cannot Access Application

```bash
# Check Nginx status
sudo systemctl status nginx
sudo nginx -t

# Check firewall
sudo ufw status

# Test from server
curl -k https://localhost:8005

# Check port is listening
sudo netstat -tlnp | grep 8005
```

### Issue: Database Connection Failed

```bash
# Check database is ready
docker exec monitoring_db pg_isready -U monitoring_user

# Check database logs
docker logs monitoring_db

# Test connection
docker exec monitoring_db psql -U monitoring_user -d monitoring_db -c "SELECT 1;"
```

---

## Quick Reference Commands

### View Logs
```bash
# Application logs
docker logs -f monitoring_web

# Nginx logs
sudo tail -f /var/log/nginx/stacksense.assistanz.com_access.log
sudo tail -f /var/log/nginx/stacksense.assistanz.com_error.log

# Metrics scheduler logs
docker exec monitoring_web tail -f /tmp/metrics_scheduler.log
```

### Restart Services
```bash
# Restart all containers
docker restart monitoring_web monitoring_db monitoring_redis

# Restart Nginx
sudo systemctl restart nginx

# Restart Docker
sudo systemctl restart docker
```

### Backup
```bash
# Backup database
docker exec monitoring_db pg_dump -U monitoring_user monitoring_db > backup_$(date +%Y%m%d).sql

# Backup application files
tar -czf app_backup_$(date +%Y%m%d).tar.gz /opt/stacksense/.env /opt/stacksense/ssh_keys/
```

### Update Application
```bash
cd /opt/stacksense
git pull  # If using Git
docker restart monitoring_web
docker exec monitoring_web python manage.py migrate
docker exec monitoring_web python manage.py collectstatic --noinput
```

---

## Deployment Summary

### What Gets Installed

✅ **System Packages**: Docker, Nginx, Python, PostgreSQL client, Redis tools  
✅ **Docker Containers**: PostgreSQL, Redis, Django application  
✅ **SSL Certificate**: Self-signed (can upgrade to Let's Encrypt)  
✅ **Nginx Configuration**: Reverse proxy on port 8005  
✅ **Firewall Rules**: Ports 22, 80, 443, 8005  
✅ **Admin User**: Default admin account created  

### What You Need to Configure

⚠️ **Servers**: Add servers to monitor manually  
⚠️ **Email Alerts**: Configure SMTP settings  
⚠️ **Users**: Create additional users if needed  
⚠️ **Alerts**: Configure alert thresholds  
⚠️ **Log Monitoring**: Setup log troubleshooting  

---

## Next Steps After Deployment

1. ✅ **Change default admin password**
2. ✅ **Add servers to monitor**
3. ✅ **Configure email alerts**
4. ✅ **Setup log monitoring**
5. ✅ **Configure alert thresholds**
6. ✅ **Create additional users (if needed)**
7. ✅ **Test all features**

---

## Support

If you encounter issues:

1. Check the logs (see Troubleshooting section)
2. Verify all prerequisites are met
3. Review deployment script output
4. Check firewall and DNS configuration
5. Ensure Docker containers are running

---

## Files Created During Deployment

- `/opt/stacksense/` - Application directory
- `/opt/stacksense/.env` - Environment configuration
- `/opt/stacksense/deployment_credentials.txt` - Saved credentials
- `/etc/nginx/sites-available/stacksense.assistanz.com` - Nginx config
- `/etc/nginx/ssl/` - SSL certificates
- Docker volumes: `stacksense_postgres_data`, `stacksense_redis_data`, etc.

---

**Deployment Complete!** 🎉

Your StackSense monitoring application is now running at:
**https://stacksense.assistanz.com:8005**








