#!/bin/bash

# Quick fix for CSRF verification failed error
# Usage: sudo ./fix-csrf.sh [domain] [nginx_port]
# Example: sudo ./fix-csrf.sh dev-stacksense.assistanz.com 8005

DOMAIN="${1:-dev-stacksense.assistanz.com}"
NGINX_PORT="${2:-8005}"
APP_DIR="${APP_DIR:-/opt/stacksense}"

echo "Fixing CSRF configuration for domain: $DOMAIN"

# Update .env file
if [ -f "$APP_DIR/.env" ]; then
    # Backup existing .env
    cp "$APP_DIR/.env" "$APP_DIR/.env.backup.$(date +%Y%m%d_%H%M%S)"
    
    # Add CSRF settings if not present
    if ! grep -q "CSRF_TRUSTED_ORIGINS" "$APP_DIR/.env"; then
        echo "" >> "$APP_DIR/.env"
        echo "# CSRF Configuration" >> "$APP_DIR/.env"
        echo "CSRF_TRUSTED_ORIGINS=https://$DOMAIN:$NGINX_PORT,https://$DOMAIN,http://$DOMAIN:$NGINX_PORT,http://$DOMAIN,http://localhost:8000,https://localhost:8000" >> "$APP_DIR/.env"
        echo "USE_TLS=True" >> "$APP_DIR/.env"
        echo "BEHIND_PROXY=True" >> "$APP_DIR/.env"
        echo "✓ Added CSRF_TRUSTED_ORIGINS to .env"
    else
        # Update existing CSRF_TRUSTED_ORIGINS
        sed -i "s|CSRF_TRUSTED_ORIGINS=.*|CSRF_TRUSTED_ORIGINS=https://$DOMAIN:$NGINX_PORT,https://$DOMAIN,http://$DOMAIN:$NGINX_PORT,http://$DOMAIN,http://localhost:8000,https://localhost:8000|" "$APP_DIR/.env"
        echo "✓ Updated CSRF_TRUSTED_ORIGINS in .env"
    fi
    
    # Ensure USE_TLS and BEHIND_PROXY are set
    if ! grep -q "USE_TLS" "$APP_DIR/.env"; then
        echo "USE_TLS=True" >> "$APP_DIR/.env"
    else
        sed -i "s|USE_TLS=.*|USE_TLS=True|" "$APP_DIR/.env"
    fi
    
    if ! grep -q "BEHIND_PROXY" "$APP_DIR/.env"; then
        echo "BEHIND_PROXY=True" >> "$APP_DIR/.env"
    else
        sed -i "s|BEHIND_PROXY=.*|BEHIND_PROXY=True|" "$APP_DIR/.env"
    fi
    
    echo "✓ Configuration updated"
else
    echo "Error: .env file not found at $APP_DIR/.env"
    exit 1
fi

# Restart the web container
echo "Restarting web container..."
docker restart monitoring_web

echo ""
echo "✓ CSRF fix applied! Container restarted."
echo "Please try logging in again."
echo ""
echo "If you still see CSRF errors, check:"
echo "  1. Domain matches: $DOMAIN"
echo "  2. Port matches: $NGINX_PORT"
echo "  3. Check logs: docker logs monitoring_web --tail=50"







