# Nginx Architecture & Flow Diagram

## System Architecture Overview

This document describes how Nginx works as a reverse proxy for the StackSense monitoring application, including SSL/TLS configuration with self-signed certificates.

---

## Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           INTERNET / USERS                               │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
                                │ HTTPS Request
                                │ Domain: stacksense.assistanz.com
                                │ Port: 8005
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         NGINX REVERSE PROXY                             │
│                         (Host: Ubuntu Server)                           │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Server Block Configuration:                                      │  │
│  │  - Listen: 8005 (HTTPS/SSL)                                      │  │
│  │  - Server Name: stacksense.assistanz.com                         │  │
│  │  - SSL Certificate: /etc/nginx/ssl/stacksense.assistanz.com.crt  │  │
│  │  - SSL Key: /etc/nginx/ssl/stacksense.assistanz.com.key          │  │
│  │  - SSL Protocols: TLSv1.2, TLSv1.3                            │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  Functions:                                                             │
│  ✓ SSL/TLS Termination (Self-signed certificate)                       │
│  ✓ HTTP/2 Support                                                        │
│  ✓ Security Headers (X-Frame-Options, X-Content-Type-Options, etc.)    │
│  ✓ Request Forwarding to Django App                                    │
│  ✓ WebSocket Support                                                    │
│  ✓ Logging (Access & Error logs)                                       │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
                                │ HTTP Proxy Pass
                                │ http://127.0.0.1:8000
                                │ (Internal localhost connection)
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    DOCKER CONTAINER: monitoring_web                      │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Port Mapping:                                                    │  │
│  │  - Host Port: 8000 → Container Port: 8000                         │  │
│  │  - Protocol: TCP                                                  │  │
│  │  - Binding: 0.0.0.0:8000 (all interfaces)                         │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Django Application (StackSense)                                   │  │
│  │  - Framework: Django                                              │  │
│  │  - WSGI Server: Gunicorn/uWSGI (or Django dev server)            │  │
│  │  - Listens on: 0.0.0.0:8000 inside container                      │  │
│  │  - Processes: HTTP requests, API calls, WebSocket connections     │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Background Services:                                             │  │
│  │  - metrics_scheduler.py (runs every 30 seconds)                  │  │
│  │  - Log scanning (every 5 minutes)                                │  │
│  │  - Anomaly detection (every 5 minutes)                           │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
                                │ Database Connections
                                │
                ┌───────────────┴───────────────┐
                │                               │
                ▼                               ▼
┌───────────────────────────┐   ┌───────────────────────────┐
│  Docker: monitoring_db    │   │  Docker: monitoring_redis │
│  PostgreSQL Database      │   │  Redis Cache              │
│  - Host Port: 5433        │   │  - Host Port: 6379        │
│  - Container Port: 5432   │   │  - Container Port: 6379   │
│  - Binding: 0.0.0.0:5433  │   │  - Binding: 0.0.0.0:6379 │
└───────────────────────────┘   └───────────────────────────┘
```

---

## Port Mapping Details

| Service | Host Port | Container Port | Protocol | Purpose |
|---------|-----------|----------------|----------|---------|
| **Nginx** | **8005** | N/A | HTTPS/TLS | External access point with SSL |
| **Django App** | 8000 | 8000 | HTTP | Application server (internal) |
| **PostgreSQL** | 5433 | 5432 | TCP | Database |
| **Redis** | 6379 | 6379 | TCP | Cache/Session store |

---

## SSL/TLS Configuration (Self-Signed)

### Certificate Location
- **Certificate**: `/etc/nginx/ssl/stacksense.assistanz.com.crt`
- **Private Key**: `/etc/nginx/ssl/stacksense.assistanz.com.key`

### SSL Configuration
```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers HIGH:!aNULL:!MD5;
ssl_prefer_server_ciphers on;
```

### Self-Signed Certificate Details
- **Type**: Self-signed (not from a Certificate Authority)
- **Domain**: stacksense.assistanz.com
- **Port**: 8005
- **Warning**: Browsers will show a security warning because the certificate is not trusted by default

---

## Request Flow (Step by Step)

### 1. User Request
```
User Browser → https://stacksense.assistanz.com:8005/
```

### 2. DNS Resolution
```
stacksense.assistanz.com → Server IP Address
```

### 3. Nginx Receives Request
- Nginx listens on port **8005** for HTTPS connections
- Validates SSL/TLS handshake using self-signed certificate
- Browser may show security warning (user must accept)

### 4. SSL Termination
- Nginx terminates SSL connection
- Decrypts HTTPS request to HTTP
- Adds security headers

### 5. Proxy Pass to Django
```
Nginx → http://127.0.0.1:8000 (localhost)
```
- Forwards request to Django app running in Docker container
- Sets proxy headers (X-Real-IP, X-Forwarded-For, etc.)

### 6. Django Processing
- Django receives HTTP request on port 8000
- Processes request (views, templates, API calls)
- May query PostgreSQL (port 5433) or Redis (port 6379)

### 7. Response Flow
```
Django → Nginx → User Browser
```
- Django sends HTTP response
- Nginx encrypts response with SSL
- Sends HTTPS response back to user

---

## Domain Configuration

### DNS Setup
- **Domain**: `stacksense.assistanz.com`
- **Record Type**: A Record
- **Points to**: Server's public IP address
- **Port**: 8005 (non-standard HTTPS port)

### Why Port 8005?
- Standard HTTPS port 443 might be used by other services
- Port 8005 allows multiple services on the same server
- Access URL: `https://stacksense.assistanz.com:8005`

---

## Security Headers

Nginx adds the following security headers to all responses:

```nginx
X-Frame-Options: SAMEORIGIN
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
```

---

## Logging

### Nginx Logs
- **Access Log**: `/var/log/nginx/stacksense.assistanz.com_access.log`
- **Error Log**: `/var/log/nginx/stacksense.assistanz.com_error.log`

### Django Logs
- Application logs are handled by Django's logging configuration
- Metrics scheduler logs: `/tmp/metrics_scheduler.log`

---

## WebSocket Support

Nginx is configured to support WebSocket connections:

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

This allows real-time features to work through the reverse proxy.

---

## File Upload Limits

- **Maximum upload size**: 100MB
- Configured via: `client_max_body_size 100M;`

---

## Troubleshooting

### Check Nginx Status
```bash
sudo systemctl status nginx
sudo nginx -t  # Test configuration
```

### Check Ports
```bash
sudo netstat -tlnp | grep -E ":8005|:8000"
# or
sudo ss -tlnp | grep -E ":8005|:8000"
```

### View Nginx Configuration
```bash
sudo cat /etc/nginx/sites-enabled/stacksense.assistanz.com
```

### Test SSL Connection
```bash
curl -k https://stacksense.assistanz.com:8005
# -k flag ignores SSL certificate verification (for self-signed certs)
```

### Check Docker Containers
```bash
docker ps
docker logs monitoring_web
```

---

## Notes

1. **Self-Signed Certificate**: Users will see a browser warning. To avoid this, use Let's Encrypt or a commercial CA certificate.

2. **Port 8005**: Non-standard port requires users to specify it in the URL. Consider using port 443 with proper DNS/SSL setup.

3. **Internal Communication**: Nginx communicates with Django via localhost (127.0.0.1), which is secure and fast.

4. **Docker Networking**: The Django container exposes port 8000 to the host, allowing Nginx to connect via localhost.

---

## Future Improvements

1. **Let's Encrypt SSL**: Replace self-signed certificate with Let's Encrypt for trusted SSL
2. **Standard Port**: Move to port 443 for standard HTTPS access
3. **Load Balancing**: Add multiple Django instances behind Nginx
4. **Rate Limiting**: Implement rate limiting in Nginx
5. **Caching**: Add Nginx caching for static files








