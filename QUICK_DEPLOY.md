# Quick Deployment Reference

## 🚀 Fast Deployment (5 Steps)

### Step 1: Copy Files to Server
```bash
# From your local machine
scp -r stacksense-repo/* user@server-ip:/opt/stacksense/
```

### Step 2: SSH into Server
```bash
ssh user@server-ip
```

### Step 3: Make Script Executable
```bash
cd /opt/stacksense
chmod +x deploy.sh
```

### Step 4: Run Deployment
```bash
sudo ./deploy.sh stacksense.assistanz.com admin@example.com
```

### Step 5: Access Application
```
https://stacksense.assistanz.com:8005
```

---

## 📋 One-Liner Deployment

```bash
# Copy files and deploy in one go (from local machine)
scp -r stacksense-repo/* user@server:/opt/stacksense/ && \
ssh user@server "cd /opt/stacksense && chmod +x deploy.sh && sudo ./deploy.sh stacksense.assistanz.com admin@example.com"
```

---

## ✅ Post-Deployment Checklist

- [ ] Access application: `https://your-domain:8005`
- [ ] Login with admin credentials (check `/opt/stacksense/deployment_credentials.txt`)
- [ ] Change default admin password
- [ ] Add servers to monitor
- [ ] Configure email alerts
- [ ] Test server connections

---

## 🔧 Quick Commands

```bash
# Check status
docker ps

# View logs
docker logs monitoring_web

# Restart app
docker restart monitoring_web

# Check credentials
cat /opt/stacksense/deployment_credentials.txt
```

---

For detailed instructions, see: `DEPLOYMENT_GUIDE.md`








