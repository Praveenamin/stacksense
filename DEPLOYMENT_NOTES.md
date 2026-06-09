# StackSense Deployment Notes

## Important: Fresh Installation vs. Data Migration

### What the `deploy.sh` Script Does

The `deploy.sh` script creates a **FRESH INSTALLATION** with:
- ✅ Application code (from Git repository or files)
- ✅ Empty database (new PostgreSQL instance)
- ✅ Default configurations
- ✅ New admin user (with generated password)
- ✅ No existing servers, metrics, or configurations

### What the `deploy.sh` Script Does NOT Do

The deployment script does **NOT** copy:
- ❌ Existing configured servers
- ❌ Historical metrics data
- ❌ User accounts and permissions
- ❌ Alert configurations
- ❌ Email alert settings
- ❌ Log troubleshooting configurations
- ❌ Per-server agent tokens / registered servers
- ❌ Any data from the old database

---

## Two Deployment Scenarios

### Scenario 1: Fresh Installation (New Server)

**Use Case**: Setting up StackSense for the first time or on a new server

**Steps**:
```bash
# 1. Copy application files to server
scp -r stacksense-repo/ user@new-server:/opt/stacksense

# 2. Run deployment script
ssh user@new-server
sudo /opt/stacksense/deploy.sh stacksense.assistanz.com admin@example.com

# 3. Access application and configure servers manually
```

**Result**: Clean installation, you'll need to:
- Add servers manually
- Configure alerts
- Set up users
- Configure email alerts

---

### Scenario 2: Migration (Moving from Old Server to New Server)

**Use Case**: Moving existing StackSense installation with all data to a new server

**Steps**:
```bash
# 1. Deploy fresh installation on new server
sudo /opt/stacksense/deploy.sh stacksense.assistanz.com admin@example.com

# 2. Run migration script to copy all data
./migrate_to_new_server.sh user@old-server.com user@new-server.com
```

**Result**: Complete migration including:
- ✅ All configured servers
- ✅ All historical metrics
- ✅ All user accounts
- ✅ All alert configurations
- ✅ Registered servers and per-server agent tokens
- ✅ SSL certificates

---

## Migration Script: `migrate_to_new_server.sh`

### What It Does

1. **Creates backup on source server**:
   - Database dump (PostgreSQL) — includes registered servers and agent tokens
   - Application configuration files (.env)
   - Media files
   - SSL certificates

2. **Transfers to target server**:
   - Downloads backup to local machine
   - Uploads to target server

3. **Restores on target server**:
   - Restores database
   - Restores application files
   - Restores SSL certificates
   - Restarts services

### Usage

```bash
# Make script executable
chmod +x migrate_to_new_server.sh

# Run migration
./migrate_to_new_server.sh user@old-server.com user@new-server.com
```

### Prerequisites

- SSH access to both servers
- Docker running on both servers
- StackSense deployed on target server (fresh installation)
- SSH key-based authentication (recommended)

---

## Complete Migration Workflow

### Step-by-Step Process

1. **Prepare New Server**
   ```bash
   # On new server
   sudo /opt/stacksense/deploy.sh stacksense.assistanz.com admin@example.com
   ```

2. **Run Migration Script**
   ```bash
   # On local machine or any server with SSH access to both
   ./migrate_to_new_server.sh user@old-server.com user@new-server.com
   ```

3. **Verify Migration**
   ```bash
   # On new server
   docker exec monitoring_web python manage.py shell -c "
   from core.models import Server;
   print(f'Servers migrated: {Server.objects.count()}');
   [print(f'  - {s.name}') for s in Server.objects.all()]
   "
   ```

4. **Test Application**
   - Access: `https://stacksense.assistanz.com:8005`
   - Verify all servers are listed
   - Check metrics are being collected
   - Test alert configurations

---

## Manual Migration (Alternative Method)

If you prefer to migrate manually:

### 1. Backup Database on Old Server
```bash
# On old server
docker exec monitoring_db pg_dump -U monitoring_user monitoring_db > backup.sql
```

### 2. Backup Application Files
```bash
# On old server
tar -czf app_backup.tar.gz \
    /opt/stacksense/.env \
    /opt/stacksense/media/
```

### 3. Copy to New Server
```bash
# From local machine
scp user@old-server:backup.sql ./
scp user@old-server:app_backup.tar.gz ./
scp backup.sql user@new-server:/tmp/
scp app_backup.tar.gz user@new-server:/tmp/
```

### 4. Restore on New Server
```bash
# On new server
# Restore database
docker exec -i monitoring_db psql -U monitoring_user monitoring_db < /tmp/backup.sql

# Restore files
cd /opt/stacksense
tar -xzf /tmp/app_backup.tar.gz

# Restart
docker restart monitoring_web
```

---

## What Gets Migrated

### ✅ Migrated Data

- **Servers**: All configured servers with their settings
- **Metrics**: Historical system metrics data
- **Users**: All user accounts and permissions
- **Alerts**: Alert configurations and history
- **Email Config**: Email alert settings
- **Monitoring Config**: Per-server monitoring settings
- **Log Config**: Log troubleshooting configurations
- **Agent Tokens**: Per-server bearer tokens the agents use to push data
- **SSL Certificates**: Nginx SSL certificates

### ❌ Not Migrated (Server-Specific)

- **Docker volumes**: Need to be recreated
- **System packages**: Installed separately
- **Nginx configuration**: Recreated by deploy script
- **Firewall rules**: Configured separately
- **Cron jobs**: Need to be set up separately

---

## Post-Migration Checklist

After migration, verify:

- [ ] All servers are listed in the application
- [ ] Agents are still pushing (servers show online; tokens carried over)
- [ ] Metrics are being collected
- [ ] Alerts are configured correctly
- [ ] Email alerts are working
- [ ] Users can log in
- [ ] Historical data is visible
- [ ] Application is accessible via domain
- [ ] SSL certificate is working

---

## Troubleshooting Migration

### Issue: Database restore fails

**Solution**:
```bash
# Check database is ready
docker exec monitoring_db pg_isready -U monitoring_user

# Check database exists
docker exec monitoring_db psql -U monitoring_user -l

# Try restore again
docker exec -i monitoring_db psql -U monitoring_user monitoring_db < backup.sql
```

### Issue: Servers not connecting

**Solution**:
- Confirm the agent is running on each monitored server: `systemctl status stacksense-agent`
- Check the agent logs for auth errors: `sudo journalctl -u stacksense-agent -n 50`
- Verify the per-server token carried over in the migrated database (re-issue and re-run the installer if needed)
- Confirm the agent's StackSense API URL points at the new host

### Issue: Metrics not collecting

**Solution**:
- Check metrics scheduler is running: `docker exec monitoring_web ps aux | grep metrics_scheduler`
- Check logs: `docker logs monitoring_web`
- Verify database connection
- Restart scheduler if needed

---

## Summary

- **`deploy.sh`**: Fresh installation only (no data migration)
- **`migrate_to_new_server.sh`**: Migrates all data from old to new server
- **Use both**: Deploy fresh, then migrate data

For a complete migration, you need to:
1. Run `deploy.sh` on new server (fresh install)
2. Run `migrate_to_new_server.sh` to copy all data












