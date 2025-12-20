# Database Migration Instructions

## ✅ django-redis Issue Resolved

The `django-redis` module has been installed successfully (version 6.0.0).

## ⚠️ Database Connection Issue

The migration cannot run because the database host "db" cannot be resolved. This indicates a Docker setup where the database container needs to be started.

## Solutions

### Option 1: Start Docker Database Container

If you're using Docker Compose:

```bash
cd /home/ubuntu/stacksense-repo
docker-compose up -d db
# Wait a few seconds for database to start
python3 manage.py migrate
```

### Option 2: Check Docker Container Status

```bash
# Check if database container exists
docker ps -a | grep db

# If container exists but stopped, start it
docker start <container_name>

# Then run migration
python3 manage.py migrate
```

### Option 3: Update Database Settings (if not using Docker)

If your database is on a different host, update `log_analyzer/settings.py`:

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "monitoring_db",
        "USER": "monitoring_user",
        "PASSWORD": "monitoring_pass",
        "HOST": "localhost",  # or your actual database host
        "PORT": "5432",
    }
}
```

## Migration File Ready

The migration file `core/migrations/0016_add_server_heartbeat.py` is ready and will create the `ServerHeartbeat` table once the database is accessible.

## After Migration

Once the migration runs successfully, you can:

1. **Verify the migration**:
   ```bash
   python3 manage.py check_heartbeats --verbose
   ```

2. **Deploy agent scripts** to your monitored servers (see `agent/README.md`)

3. **Test the heartbeat API**:
   ```bash
   curl -X POST http://your-server/api/heartbeat/1/ \
        -H "Content-Type: application/json" \
        -d '{"agent_version": "1.0.0"}'
   ```

## Summary

- ✅ django-redis installed
- ✅ Migration file created
- ⚠️ Waiting for database connection
- ✅ All code implementation complete

Once the database is accessible, run `python3 manage.py migrate` to complete the setup.

