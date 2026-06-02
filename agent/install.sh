#!/usr/bin/env bash
#
# StackSense push-agent installer.
#
# Run this ON THE MONITORED VM (as root / via sudo). It installs the agent into
# its own isolated Python environment, registers a hardened systemd service that
# runs as a dedicated non-root user, verifies it can authenticate to the
# monitoring server, and starts it (also on every boot).
#
# One-liner:
#   curl -fsSL <MON_URL>/agent/install.sh | sudo bash -s -- --url <MON_URL> --token <TOKEN>
#
# Options:
#   --url <URL>         Monitoring server base URL (required), e.g. https://mon.example.com:8000
#   --token <TOKEN>     Per-server agent token (required), from `manage.py create_agent_token`
#   --interval <SECS>   Seconds between pushes (default: 30)
#   --insecure          Skip TLS certificate verification (self-signed servers only)
#   --uninstall         Stop and remove the agent, then exit
#
set -euo pipefail

INSTALL_DIR=/opt/stacksense-agent
CONF_DIR=/etc/stacksense-agent
SERVICE_USER=stacksense
SERVICE_NAME=stacksense-agent
INTERVAL=30
VERIFY_TLS=true
URL=""
TOKEN=""
UNINSTALL=false

while [ $# -gt 0 ]; do
  case "$1" in
    --url)      URL="${2:-}"; shift 2;;
    --token)    TOKEN="${2:-}"; shift 2;;
    --interval) INTERVAL="${2:-30}"; shift 2;;
    --insecure) VERIFY_TLS=false; shift;;
    --uninstall) UNINSTALL=true; shift;;
    *) echo "Unknown argument: $1" >&2; exit 1;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: please run as root (use sudo)." >&2
  exit 1
fi

if [ "$UNINSTALL" = "true" ]; then
  echo "Removing StackSense agent..."
  systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$SERVICE_NAME.service"
  systemctl daemon-reload || true
  rm -rf "$INSTALL_DIR" "$CONF_DIR"
  echo "Done. (The '$SERVICE_USER' system user was left in place.)"
  exit 0
fi

# Allow env vars as a fallback for url/token.
URL="${URL:-${STACKSENSE_URL:-}}"
TOKEN="${TOKEN:-${STACKSENSE_TOKEN:-}}"
[ -n "$URL" ]   || { echo "ERROR: --url is required." >&2; exit 1; }
[ -n "$TOKEN" ] || { echo "ERROR: --token is required." >&2; exit 1; }
URL="${URL%/}"

CURL_OPTS="-fsSL"
[ "$VERIFY_TLS" = "false" ] && CURL_OPTS="$CURL_OPTS -k"

echo "[1/6] Installing prerequisites (python3, venv, curl)..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq python3 python3-venv curl ca-certificates >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 curl ca-certificates >/dev/null 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 curl ca-certificates >/dev/null 2>&1 || true
fi
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required but not available." >&2; exit 1; }
command -v curl    >/dev/null 2>&1 || { echo "ERROR: curl is required but not available." >&2; exit 1; }

echo "[2/6] Creating service user and directories..."
id -u "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$INSTALL_DIR" "$CONF_DIR"

# For container monitoring: if Docker is present, add the agent user to the
# 'docker' group so it can run 'docker ps'. NOTE: docker group membership is
# effectively root-equivalent on this host -- skip this if you don't want the
# agent to have Docker access (containers simply won't be reported).
if command -v docker >/dev/null 2>&1 && getent group docker >/dev/null 2>&1; then
  usermod -aG docker "$SERVICE_USER" 2>/dev/null && \
    echo "      Added '$SERVICE_USER' to the docker group (for container monitoring)."
fi

echo "[3/6] Downloading agent from $URL ..."
curl $CURL_OPTS "$URL/agent/stacksense_agent.py" -o "$INSTALL_DIR/stacksense_agent.py"

echo "[4/6] Setting up isolated Python environment (venv + psutil)..."
[ -d "$INSTALL_DIR/venv" ] || python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$INSTALL_DIR/venv/bin/pip" install --quiet psutil

echo "[5/6] Writing configuration and systemd service..."
umask 077
cat > "$CONF_DIR/agent.env" <<EOF
STACKSENSE_URL=$URL
STACKSENSE_TOKEN=$TOKEN
STACKSENSE_INTERVAL=$INTERVAL
STACKSENSE_VERIFY_TLS=$VERIFY_TLS
STACKSENSE_SERVICES_INTERVAL=60
EOF
chmod 0600 "$CONF_DIR/agent.env"
chown root:root "$CONF_DIR/agent.env"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=StackSense Push Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
EnvironmentFile=$CONF_DIR/agent.env
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/stacksense_agent.py
Restart=always
RestartSec=10
# Hardening: the agent only reads stats and dials out -- it needs no privileges
# and no write access to the filesystem.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

echo "[6/6] Verifying authentication, then starting service..."
set +e
PING_OUT=$("$INSTALL_DIR/venv/bin/python3" - "$URL" "$TOKEN" "$VERIFY_TLS" <<'PY'
import sys, ssl, urllib.request
url, token, verify = sys.argv[1], sys.argv[2], sys.argv[3]
ctx = ssl.create_default_context()
if verify == "false":
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(
    url.rstrip("/") + "/api/agent/ping/",
    headers={"Authorization": "Bearer " + token},
)
try:
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        print(r.read().decode("utf-8", "replace"))
except Exception as e:
    print("PING_FAILED: %s" % e)
    sys.exit(1)
PY
)
PING_RC=$?
set -e
echo "      server said: $PING_OUT"
if [ $PING_RC -ne 0 ]; then
  echo "ERROR: could not authenticate to $URL." >&2
  echo "       Check the --url and --token, and that this VM can reach the server." >&2
  echo "       The service was installed but NOT started." >&2
  exit 1
fi

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME" >/dev/null 2>&1
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo ""
  echo "Success - the StackSense agent is running and will start on boot."
  echo "  Status: systemctl status $SERVICE_NAME"
  echo "  Logs:   journalctl -u $SERVICE_NAME -f"
  echo "  Remove: curl -fsSL $URL/agent/install.sh | sudo bash -s -- --uninstall"
else
  echo "ERROR: the service did not start. Inspect: journalctl -u $SERVICE_NAME" >&2
  exit 1
fi
