#!/usr/bin/env bash
#
# StackSense -- first-time deploy on a Docker host.
#
#   ./setup.sh --host monitor.example.com --ssl letsencrypt --email you@example.com
#   ./setup.sh --host 203.0.113.10        --ssl self-signed
#   ./setup.sh --host monitor.example.com --ssl upload --cert fullchain.pem --key privkey.pem
#
# --ext-port PORT : public HTTPS port visitors use, when it is NOT 443 (e.g. behind a
#   shared IP / CloudStack port-forward where you reach it as https://domain:8443).
#   In that case nginx serves HTTPS-only and the port is baked into the CSRF origin.
#   Forward your external port to the VM's port 443 (nginx listens on 443 internally).
#
# It generates .env (with strong random secrets), writes the nginx TLS config for the
# chosen mode, provisions the certificate, brings the stack up, waits for health, and
# then prints the URL of the one-time web setup form (/setup) where you create the admin.
#
# Re-running is safe: an existing .env is preserved (secrets are never regenerated).
# Use --dry-run to see exactly what it would write/do without touching anything.
#
set -euo pipefail
cd "$(dirname "$0")"

# ---- args ------------------------------------------------------------------------
HOST=""; SSL=""; EMAIL=""; CERT=""; KEY=""; EXT_PORT=""; DRY_RUN=false; NONINTERACTIVE=false
NGINX_CONF="deploy/nginx/app.conf"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

usage() { grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//' | head -20; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --ssl) SSL="$2"; shift 2;;
    --email) EMAIL="$2"; shift 2;;
    --cert) CERT="$2"; shift 2;;
    --key) KEY="$2"; shift 2;;
    --ext-port) EXT_PORT="$2"; shift 2;;
    --dry-run) DRY_RUN=true; shift;;
    --non-interactive|-y) NONINTERACTIVE=true; shift;;
    -h|--help) usage 0;;
    *) echo "Unknown option: $1" >&2; usage 1;;
  esac
done

info() { echo "  $*"; }
step() { echo; echo "==> $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# ---- prompts for anything missing (unless non-interactive) -----------------------
prompt() {  # prompt VAR "question" "default"
  local var="$1" q="$2" def="${3:-}" ans
  [ -n "${!var}" ] && return 0
  if $NONINTERACTIVE; then [ -n "$def" ] && eval "$var=\$def" && return 0; die "--$var is required"; fi
  read -r -p "$q${def:+ [$def]}: " ans || true
  eval "$var=\${ans:-\$def}"
}

prompt HOST "Public host / domain (or server IP)"
if [ -z "$SSL" ] && ! $NONINTERACTIVE; then
  echo "SSL options:  1) letsencrypt (free, auto-renew, needs a real domain + port 80)"
  echo "              2) upload     (provide your own cert + key)"
  echo "              3) self-signed (for an IP / internal / testing)"
  read -r -p "Choose 1/2/3 [3]: " s || true
  case "${s:-3}" in 1) SSL=letsencrypt;; 2) SSL=upload;; *) SSL=self-signed;; esac
fi
SSL="${SSL:-self-signed}"
case "$SSL" in letsencrypt|upload|self-signed) ;; *) die "--ssl must be letsencrypt|upload|self-signed";; esac
[ "$SSL" = letsencrypt ] && prompt EMAIL "Email for Let's Encrypt notices"
if [ "$SSL" = upload ]; then
  prompt CERT "Path to fullchain.pem"; prompt KEY "Path to privkey.pem"
  $DRY_RUN || { [ -f "$CERT" ] || die "cert not found: $CERT"; [ -f "$KEY" ] || die "key not found: $KEY"; }
fi

# Public HTTPS port. Default 443 (standard). A non-443 port means we're behind a
# forward / shared IP -> serve HTTPS-only and bake the port into the CSRF origin.
if [ -z "$EXT_PORT" ] && [ "$SSL" != letsencrypt ] && ! $NONINTERACTIVE; then
  read -r -p "Public HTTPS port visitors use [443]: " p || true; EXT_PORT="${p:-443}"
fi
EXT_PORT="${EXT_PORT:-443}"
if [ "$SSL" = letsencrypt ] && [ "$EXT_PORT" != 443 ]; then
  die "Let's Encrypt needs the standard ports; for a forwarded/non-443 port use --ssl upload or self-signed."
fi
if [ "$EXT_PORT" = 443 ]; then
  EXT_ORIGIN="https://${HOST}"; HTTPS_ONLY=false
else
  EXT_ORIGIN="https://${HOST}:${EXT_PORT}"; HTTPS_ONLY=true
fi

# USE_TLS=True for every HTTPS mode (so secure cookies turn on); only a bare HTTP
# deploy would set it False -- which this script never does (all 3 modes serve TLS).
USE_TLS=True

# ---- secret generation -----------------------------------------------------------
gen_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else python3 -c "import secrets;print(secrets.token_hex(32))"; fi
}

# ---- .env (generate once; never clobber existing secrets) ------------------------
render_env() {
  cat <<EOF
DEBUG=False
SECRET_KEY=${SECRET_KEY}
ALLOWED_HOSTS=${HOST},localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=${EXT_ORIGIN}
USE_TLS=${USE_TLS}

POSTGRES_DB=monitoring_db
POSTGRES_USER=monitoring_user
POSTGRES_PASSWORD=${DB_PASSWORD}
POSTGRES_HOST=db
POSTGRES_PORT=5432

REDIS_URL=redis://redis:6379/0

# Ollama runs as a Compose service -- use its service name, not localhost.
OLLAMA_API_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2
OLLAMA_TIMEOUT=120
LLM_ENABLED=True

# The initial admin is created via the first-run web form (/setup).
EOF
}

# ---- nginx config per SSL mode ---------------------------------------------------
cert_paths() {
  case "$SSL" in
    letsencrypt) CRT="/etc/letsencrypt/live/${HOST}/fullchain.pem"; CKEY="/etc/letsencrypt/live/${HOST}/privkey.pem";;
    upload)      CRT="/etc/letsencrypt/uploaded/fullchain.pem";     CKEY="/etc/letsencrypt/uploaded/privkey.pem";;
    self-signed) CRT="/etc/letsencrypt/selfsigned/fullchain.pem";   CKEY="/etc/letsencrypt/selfsigned/privkey.pem";;
  esac
}

render_nginx() {  # $1 = "http-only" (LE bootstrap) | "tls"
  local mode="$1"
  cert_paths
  # Forwarded headers (literal nginx vars) that make Django's SECURE_PROXY_SSL_HEADER
  # and Host checks work behind the proxy. Single-quoted so bash keeps $host etc. literal.
  local proxy='        proxy_pass http://web:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;'
  echo "# Generated by setup.sh -- SSL mode: ${SSL}${HTTPS_ONLY:+ (HTTPS-only, ext port ${EXT_PORT})}"
  # HTTPS-only (behind a forward, no external :80): emit just the TLS server, no :80 block.
  if [ "$mode" = "tls" ] && [ "${HTTPS_ONLY:-false}" = true ]; then
    cat <<EOF
server {
    listen 443 ssl;
    http2 on;
    server_name ${HOST};
    ssl_certificate     ${CRT};
    ssl_certificate_key ${CKEY};
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 25m;
    location / {
${proxy}
    }
}
EOF
    return 0
  fi
  cat <<EOF
server {
    listen 80;
    server_name ${HOST};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
EOF
  if [ "$mode" = "http-only" ]; then
    cat <<EOF
    location / {
${proxy}
    }
}
EOF
  else
    cat <<EOF
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    http2 on;
    server_name ${HOST};
    ssl_certificate     ${CRT};
    ssl_certificate_key ${CKEY};
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 25m;
    location / {
${proxy}
    }
}
EOF
  fi
}

# ---- DRY RUN ---------------------------------------------------------------------
if $DRY_RUN; then
  SECRET_KEY="<generated-32-byte-hex>"; DB_PASSWORD="<generated-32-byte-hex>"
  step "DRY RUN -- nothing will be written or started"
  info "host=$HOST  ssl=$SSL  ext_port=$EXT_PORT  https_only=$HTTPS_ONLY  email=${EMAIL:-n/a}"
  echo; echo "----- .env (secrets shown as placeholders) -----"; render_env
  echo; echo "----- $NGINX_CONF (TLS) -----"; render_nginx tls
  echo; echo "----- planned -----"
  info "$COMPOSE up -d --build${SSL:+ }$([ "$SSL" = letsencrypt ] && echo '--profile letsencrypt')"
  [ "$SSL" = letsencrypt ] && info "certbot certonly --webroot -w /var/www/certbot -d $HOST --email $EMAIL --agree-tos -n"
  info "wait for http://127.0.0.1:8000/health/  ->  print  ${EXT_ORIGIN}/setup"
  exit 0
fi

# ---- preconditions ---------------------------------------------------------------
step "Checking Docker"
command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker + Compose first."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 not available ('docker compose')."

# ---- write .env (preserve existing) ----------------------------------------------
step "Configuration (.env)"
if [ -f .env ]; then
  info ".env exists -- keeping it (secrets preserved). Delete it to regenerate."
else
  SECRET_KEY="$(gen_secret)"; DB_PASSWORD="$(gen_secret)"
  render_env > .env
  chmod 600 .env
  info "Wrote .env with fresh random SECRET_KEY + DB password (chmod 600)."
fi

# ---- nginx config dir ------------------------------------------------------------
step "nginx config ($SSL)"
mkdir -p deploy/nginx

# ---- SSL provisioning ------------------------------------------------------------
# Certs live in the named volume 'stacksense_certs' (pinned in docker-compose.prod.yml
# so this standalone `docker run` mounts the SAME volume nginx uses).
if [ "$SSL" = self-signed ]; then
  step "Generating self-signed certificate"
  docker run --rm -v stacksense_certs:/etc/letsencrypt --entrypoint sh alpine/openssl -c \
    "mkdir -p /etc/letsencrypt/selfsigned && openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
       -keyout /etc/letsencrypt/selfsigned/privkey.pem \
       -out    /etc/letsencrypt/selfsigned/fullchain.pem -subj '/CN=${HOST}'" \
    || die "self-signed cert generation failed"
  render_nginx tls > "$NGINX_CONF"

elif [ "$SSL" = upload ]; then
  step "Installing uploaded certificate"
  docker run --rm -v stacksense_certs:/etc/letsencrypt -v "$PWD:/in:ro" --entrypoint sh alpine/openssl -c \
    "mkdir -p /etc/letsencrypt/uploaded && cp '/in/$(basename "$CERT")' /etc/letsencrypt/uploaded/fullchain.pem && cp '/in/$(basename "$KEY")' /etc/letsencrypt/uploaded/privkey.pem" \
    || die "failed to install uploaded cert (cert/key must be in this directory)"
  render_nginx tls > "$NGINX_CONF"

else  # letsencrypt
  step "Let's Encrypt -- HTTP-01 issuance for $HOST"
  render_nginx http-only > "$NGINX_CONF"          # bootstrap: serve :80 + ACME path
  $COMPOSE up -d --build
  info "Requesting certificate (port 80 must be reachable from the internet)..."
  $COMPOSE --profile letsencrypt run --rm certbot certonly --webroot -w /var/www/certbot \
    -d "$HOST" --email "$EMAIL" --agree-tos -n \
    || die "Let's Encrypt issuance failed (check DNS for $HOST and that port 80 is open)."
  render_nginx tls > "$NGINX_CONF"                # switch to TLS conf + start renew loop
  $COMPOSE --profile letsencrypt up -d
fi

# ---- bring up + reload -----------------------------------------------------------
step "Starting the stack"
$COMPOSE up -d --build
docker exec monitoring_nginx nginx -s reload 2>/dev/null || true

# ---- wait for health -------------------------------------------------------------
step "Waiting for the app to become healthy"
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health/ >/dev/null 2>&1; then info "App is up."; break; fi
  [ "$i" = 60 ] && die "App did not become healthy in time -- check: $COMPOSE logs web"
  sleep 2
done

# ---- done ------------------------------------------------------------------------
step "Done"
echo
echo "  Open your browser to finish setup (create your admin):"
echo "      ${EXT_ORIGIN}/setup"
[ "$HTTPS_ONLY" = true ] && echo "      (forward your external port ${EXT_PORT} -> this VM's port 443 in CloudStack)"
[ "$SSL" = self-signed ] && echo "      (self-signed cert -> expect a browser warning; safe for IP/internal use)"
echo
