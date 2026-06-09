# Deploy StackSense on a New Server

Step-by-step guide to deploy StackSense from scratch on a new Ubuntu/Debian server.

---

## Prerequisites

- **OS**: Ubuntu 20.04+ or Debian 11+
- **Resources**: ≥ 2GB RAM, ≥ 5GB disk
- **Access**: SSH + `sudo`
- **Domain**: A hostname pointing to the server (e.g. `monitor.example.com`), or use the server IP

---

## Option A: Automated deployment (recommended)

Uses `deploy.sh` to install Docker, PostgreSQL, Redis, Nginx, SSL, and the app.

### 1. Copy the app to the server

**From a Git clone:**

```bash
# On the new server
sudo mkdir -p /opt/stacksense
cd /opt/stacksense
sudo git clone https://github.com/YOUR_ORG/stacksense-repo.git .
```

Or **from your machine via SCP:**

```bash
scp -r /path/to/stacksense-repo/* user@NEW_SERVER_IP:/opt/stacksense/
```

### 2. Set Git repo URL (if using deploy.sh clone)

If you use `deploy.sh` to clone instead of copying files, set your repo **before** running it:

```bash
export REPO_URL="https://github.com/YOUR_ORG/stacksense-repo.git"
```

### 3. Run the deployment script

```bash
ssh user@NEW_SERVER_IP
cd /opt/stacksense
chmod +x deploy.sh
sudo ./deploy.sh YOUR_DOMAIN_OR_IP admin@example.com
```

**Examples:**

```bash
sudo ./deploy.sh monitor.example.com admin@example.com
sudo ./deploy.sh 203.0.113.50 admin@example.com
```

The script will:

- Install Docker, Docker Compose, Nginx, Certbot, and other dependencies
- Create PostgreSQL and Redis containers
- Build and run the Django app
- Create a self-signed SSL cert and Nginx config
- Open UFW ports 22, 80, 443, and **8005**
- Create an admin user and write credentials to `/opt/stacksense/deployment_credentials.txt`

### 4. Access the app

- **URL**: `https://YOUR_DOMAIN_OR_IP:8005`
- **Admin**: `https://YOUR_DOMAIN_OR_IP:8005/admin/`
- **Credentials**: `cat /opt/stacksense/deployment_credentials.txt`

Browsers will warn about the self-signed certificate; accept it to continue.

---

## Option B: Docker Compose only

Use this if Docker and Docker Compose are already installed and you prefer not to use Nginx/UFW via the script.

### 1. Copy app to server

Same as Option A (Git clone or SCP into `/opt/stacksense` or your chosen directory).

### 2. Create `.env`

```bash
cd /opt/stacksense
cp .env.example .env
```

Edit `.env` and set at least:

```bash
POSTGRES_PASSWORD=<strong-random-password>
SECRET_KEY=<django-secret-key>
ALLOWED_HOSTS=your-domain.com,localhost,127.0.0.1,0.0.0.0
CSRF_TRUSTED_ORIGINS=https://your-domain.com,http://localhost:8000
```

Use `openssl rand -base64 32` for passwords and `SECRET_KEY`.

### 3. Build and run

```bash
docker compose build --no-cache
docker compose up -d
```

### 4. Create admin user

```bash
docker compose exec web python manage.py createsuperuser
```

### 5. (Optional) Nginx + SSL in front

- Point Nginx at `http://127.0.0.1:8000`.
- Use a real certificate (e.g. Let’s Encrypt) or the same pattern as `deploy.sh` (self-signed + `listen 8005 ssl`).

---

## Post-deployment

### 1. Save credentials

```bash
cat /opt/stacksense/deployment_credentials.txt
```

Store them somewhere safe and **change the default admin password** after first login.

### 2. Add servers (push agent)

StackSense is push-agent only — there are no SSH keys to generate. To monitor a
server:

1. In the UI, go to **Instances → Add Server** and enter a name + IP.
2. Copy the generated one-line `curl … | sudo bash` agent install command.
3. Run it on the target server (as root/sudo). The agent installs, writes its
   config (StackSense URL + per-server token), and starts pushing metrics over
   HTTPS. StackSense never connects out to the server.

### 3. Scheduling (analysis & housekeeping)

The in-container scheduler (`metrics_scheduler.py`) runs the analysis and
housekeeping commands automatically — there is no metric-collection command
(metrics arrive via the agent push). If you prefer to run anything on cron
instead, these are the relevant commands (run as the user that owns the app):

```bash
# Anomaly detection
*/15 * * * * docker exec monitoring_web python manage.py detect_anomalies

# Heartbeat / connectivity evaluation (reads pushed heartbeats; no SSH)
*/1 * * * * docker exec monitoring_web python manage.py check_heartbeats
```

Adjust `monitoring_web` if your container name differs.

### 4. Optional: Let’s Encrypt SSL

If you use a real domain and Nginx:

```bash
sudo certbot --nginx -d YOUR_DOMAIN --non-interactive --agree-tos --email admin@example.com
```

Reconfigure Nginx to use the certbot-managed certs and, if desired, disable the self-signed setup.

---

## Migrating from an old server

To move existing data (servers, metrics, configs) to a new server:

1. Deploy a **fresh** StackSense on the new server (Option A or B).
2. Use the migration script from the repo:

   ```bash
   ./migrate_to_new_server.sh user@OLD_SERVER user@NEW_SERVER
   ```

See `DEPLOYMENT_NOTES.md` for details.

---

## Useful commands

| Task | Command |
|------|---------|
| Check containers | `docker ps` or `docker compose ps` |
| App logs | `docker logs monitoring_web` or `docker compose logs -f web` |
| Restart app | `docker restart monitoring_web` or `docker compose restart web` |
| Stop all | `docker compose down` |
| Run migrations | `docker exec monitoring_web python manage.py migrate --noinput` |
| Create superuser | `docker exec monitoring_web python manage.py createsuperuser` |

---

## Troubleshooting

- **502 Bad Gateway**: App not ready or crashed. Check `docker logs monitoring_web` and Nginx error logs.
- **DB connection errors**: Verify `POSTGRES_*` in `.env` match the DB container. Ensure DB is healthy: `docker exec monitoring_db pg_isready -U monitoring_user`.
- **CSRF / redirect issues**: Add your app URL (and Nginx URL if different) to `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` in `.env`.
- **Port 8005 blocked**: Open it in UFW: `sudo ufw allow 8005/tcp && sudo ufw reload`.

For more context, see `DEPLOYMENT_NOTES.md`, `QUICK_DEPLOY.md`, and `README.md`.
