# Cron Job Setup Complete

## Status

✅ **Cron job has been successfully configured in root's crontab**

The heartbeat checker will run every 30 seconds automatically.

## Current Configuration

The cron job is set up for the **root** user with two entries:
1. Runs at the start of every minute
2. Runs 30 seconds into every minute

This ensures the heartbeat checker runs every 30 seconds.

## Verify Cron Job

```bash
# View root's crontab
sudo crontab -l -u root | grep check_heartbeats_ssh
```

You should see two entries.

## Test Manually

Test the command manually to verify it works:

```bash
cd /home/ubuntu/stacksense-repo
POSTGRES_HOST=localhost POSTGRES_PORT=5433 python3 manage.py check_heartbeats_ssh --verbose
```

## Check Heartbeat Status

```bash
cd /home/ubuntu/stacksense-repo
POSTGRES_HOST=localhost POSTGRES_PORT=5433 python3 manage.py check_heartbeats --verbose
```

## Note About SSH Authentication

If you see "No authentication methods available" errors, this means:
- SSH keys need to be deployed to client servers
- This is the same SSH setup used for metric collection
- Once SSH keys are configured, heartbeats will work automatically

## Monitor Cron Execution

Check cron logs:
```bash
sudo grep CRON /var/log/syslog | tail -20
```

Or check if the command is running:
```bash
ps aux | grep check_heartbeats_ssh
```

## Next Steps

1. **Configure SSH keys** (if not already done) - same as for metric collection
2. **Wait 30-60 seconds** after SSH is configured
3. **Check dashboard** - servers should show as "ONLINE" once SSH connections succeed

The system is now fully automated and will check heartbeats every 30 seconds!

