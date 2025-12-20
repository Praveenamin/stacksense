# Heartbeat System - Lightweight Analysis & Port Requirements

## ✅ Lightweight Confirmation

### Current System (SSH-based Metric Collection)
- **Heavy**: Full SSH connection, remote script execution, data transfer
- **Resources**: SSH client, paramiko library, network overhead
- **Frequency**: Based on collection interval (typically 60+ seconds)
- **Dependencies**: SSH keys, paramiko, remote Python/psutil installation

### New System (HTTP Heartbeat)
- **Ultra Lightweight**: Simple HTTP POST request
- **Size**: ~50 lines of Python code
- **Resources**: 
  - Memory: < 5MB (just Python + requests library)
  - CPU: Negligible (one HTTP request every 30 seconds)
  - Network: ~200 bytes per request (tiny JSON payload)
- **Dependencies**: Only `requests` library (or use built-in `urllib` for zero dependencies)
- **Frequency**: 30 seconds (as requested)

### Comparison
```
Current SSH Collection:
- Establishes SSH connection (handshake overhead)
- Executes remote Python script (CPU intensive)
- Transfers metric data (KB of data)
- Closes connection
- Total: ~500KB-2MB per collection

New Heartbeat:
- Single HTTP POST request
- ~200 bytes payload
- No remote execution
- Total: ~200 bytes per heartbeat
```

**Result**: Heartbeat is **~1000x lighter** than current metric collection.

---

## 🔌 Port Requirements

### ✅ NO Separate Ports Needed!

The heartbeat system uses the **same HTTP/HTTPS port** as your Django web application:

- **Same Port**: Uses whatever port your Django app runs on (typically 80/443 or 8000 in development)
- **Same Protocol**: HTTP/HTTPS (standard web traffic)
- **Same Firewall Rules**: If your Django app is accessible, heartbeats will work

### Network Requirements

#### On Client Servers (Monitored Servers)
- **Outbound HTTP/HTTPS**: Client servers need to make outbound HTTP/HTTPS requests
- **No Inbound Ports**: No need to open any inbound ports on client servers
- **Firewall**: Only outbound traffic to Django server's IP/domain

#### On Django Server (Monitoring Server)
- **Inbound HTTP/HTTPS**: Same as your web interface (already configured)
- **No Additional Ports**: Uses existing web server port

### Example Configuration

```
Django Server: 192.168.1.100:8000 (or 80/443 in production)
Client Server: 192.168.1.101

Client Server Firewall Rules:
✅ OUTBOUND: Allow HTTP/HTTPS to 192.168.1.100:8000
❌ INBOUND: Nothing needed (agent makes outbound requests)

Django Server Firewall Rules:
✅ INBOUND: Already configured for web access
✅ Same port handles both web UI and heartbeat API
```

### Security Considerations

1. **API Endpoint**: `/api/heartbeat/<server_id>/`
2. **Authentication**: Can add simple token-based auth if needed
3. **HTTPS**: Recommended for production (same as web interface)
4. **No SSH Required**: Heartbeat doesn't need SSH access

---

## 📊 Resource Usage Estimate

### Agent Script (per server)
- **Memory**: 2-5 MB
- **CPU**: < 0.1% (one request every 30 seconds)
- **Network**: ~200 bytes every 30 seconds = ~6.4 KB/hour = ~150 KB/day
- **Disk**: < 10 KB (script file)

### Django Server (heartbeat handling)
- **Memory**: Negligible (simple database write)
- **CPU**: < 0.01% per heartbeat
- **Database**: One row per server (timestamp updates)

**Total Impact**: Minimal - heartbeat system uses less resources than a single SSH connection.

---

## 🚀 Deployment Simplicity

### Agent Deployment Options

1. **Systemd Service** (Recommended)
   - Runs as background service
   - Auto-restarts on failure
   - Logs to systemd journal

2. **Cron Job**
   - Simple: `*/30 * * * * /usr/bin/python3 /opt/heartbeat_agent.py`
   - No service management needed

3. **Python Script with Loop**
   - Runs continuously with sleep(30)
   - Can be managed with supervisor/systemd

### Configuration
- Single config file or environment variables
- Server ID + Django server URL
- No SSH keys, no complex setup

---

## ✅ Summary

| Aspect | Current (SSH Metrics) | New (HTTP Heartbeat) |
|--------|----------------------|---------------------|
| **Weight** | Heavy (SSH + script execution) | Ultra Light (HTTP POST) |
| **Ports** | SSH (22) - Inbound required | HTTP/HTTPS - Same as web |
| **Network** | KB per collection | Bytes per heartbeat |
| **Dependencies** | SSH keys, paramiko, remote Python | Just `requests` library |
| **Setup** | Complex (SSH key deployment) | Simple (config file) |
| **Resources** | High (SSH overhead) | Minimal (< 5MB RAM) |

**Conclusion**: The heartbeat system is significantly lighter and requires no additional ports beyond your existing web server configuration.

