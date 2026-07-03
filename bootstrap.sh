#!/usr/bin/env bash
#
# StackSense -- one-line bootstrap for a fresh Ubuntu/Debian server.
#
# Installs anything MISSING (git, Docker, Docker Compose) -- skipping whatever is already
# present -- clones/updates the repo, then deploys via setup.sh.
#
# INTERACTIVE (default): it ASKS you the requirements and you choose --
#   IP vs domain  ->  default vs custom port  ->  TLS mode (narrowed to what's valid).
# Works even when piped, by reading your answers from the terminal:
#   curl -fsSL https://raw.githubusercontent.com/Praveenamin/stacksense/main/bootstrap.sh | sudo bash
#
# SCRIPTED: pass setup.sh flags to skip the wizard:
#   curl -fsSL .../bootstrap.sh | sudo bash -s -- --host 203.0.113.10 --ssl self-signed --ext-port 8443
#
# Env overrides: STACKSENSE_REPO=<git-url>  STACKSENSE_DIR=<path>  STACKSENSE_REF=<branch>
#
set -euo pipefail

REPO_URL="${STACKSENSE_REPO:-https://github.com/Praveenamin/stacksense.git}"
DIR="${STACKSENSE_DIR:-/opt/stacksense}"
REF="${STACKSENSE_REF:-main}"
TTY="${STACKSENSE_TTY:-/dev/tty}"

log()  { echo "[bootstrap] $*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
ask()  { local p="$1" a=""; printf "%s" "$p" >&2; IFS= read -r a < "$TTY" || true; printf '%s' "$a"; }

require_root() {
  [ "$(id -u)" -eq 0 ] || { echo "Please run as root -- e.g. pipe into 'sudo bash'." >&2; exit 1; }
}
require_apt() {
  have apt-get || { echo "Auto-install supports Debian/Ubuntu (apt). Install Docker + Compose + git" >&2
                    echo "yourself, then run:  cd ${DIR} && ./setup.sh" >&2; exit 1; }
}
install_prereqs() {
  if have git; then log "git already installed -- skipping."
  else log "Installing git..."; apt-get update -y >/dev/null; apt-get install -y git >/dev/null; fi

  if have docker; then log "Docker already installed ($(docker --version 2>/dev/null | cut -d, -f1)) -- skipping."
  else log "Installing Docker (get.docker.com)..."; curl -fsSL https://get.docker.com | sh; fi

  if docker compose version >/dev/null 2>&1; then log "Docker Compose v2 already installed -- skipping."
  else log "Installing Docker Compose plugin..."; apt-get update -y >/dev/null; apt-get install -y docker-compose-plugin >/dev/null; fi

  systemctl enable --now docker >/dev/null 2>&1 || true
}
clone_repo() {
  if [ -d "${DIR}/.git" ]; then
    log "Repo already at ${DIR} -- pulling latest (${REF})."
    git -C "${DIR}" checkout "${REF}" >/dev/null 2>&1 || true
    git -C "${DIR}" pull --ff-only origin "${REF}" || log "  (couldn't fast-forward; keeping current checkout)"
  else
    log "Cloning ${REPO_URL} -> ${DIR} (${REF})."
    git clone --branch "${REF}" "${REPO_URL}" "${DIR}"
  fi
}

# Interactive requirement wizard -> fills DEPLOY_ARGS for setup.sh.
gather_requirements() {
  [ -e "$TTY" ] || { echo "No terminal for interactive mode -- pass flags instead, e.g." >&2
                     echo "  --host <ip-or-domain> --ssl self-signed [--ext-port 8443]" >&2; exit 1; }
  echo >&2; echo "  === StackSense deployment ===" >&2; echo >&2

  # 1) host type
  local htype
  while :; do
    htype="$(ask '  Reach this server by   [1] IP address   [2] Domain name : ')"
    case "$htype" in 1|2) break;; *) echo "     please enter 1 or 2." >&2;; esac
  done

  # 2) host value
  local host
  if [ "$htype" = 1 ]; then host="$(ask '  Server public IP: ')"
  else host="$(ask '  Domain (e.g. monitor.example.com): ')"; fi
  [ -n "$host" ] || { echo "  A host is required." >&2; exit 1; }

  # 3) port
  local extport="443" ptype
  ptype="$(ask '  Port visitors use      [1] Default 443   [2] Custom port : ')"
  if [ "$ptype" = 2 ]; then extport="$(ask '  Custom external port: ')"; fi
  [ -n "$extport" ] || extport="443"

  # 4) TLS -- narrowed to what's valid for the chosen host+port
  local ssl email="" cert="" key="" c
  if [ "$htype" = 2 ] && [ "$extport" = "443" ]; then
    c="$(ask '  TLS certificate        [1] Lets Encrypt (auto, recommended)   [2] Self-signed   [3] Upload cert : ')"
    case "$c" in 1) ssl="letsencrypt";; 3) ssl="upload";; *) ssl="self-signed";; esac
  else
    if [ "$htype" = 1 ]; then echo "  (Lets Encrypt cannot issue for a bare IP.)" >&2; fi
    if [ "$htype" = 2 ] && [ "$extport" != "443" ]; then echo "  (Lets Encrypt needs the standard port 443; not available on a custom port.)" >&2; fi
    c="$(ask '  TLS certificate        [1] Self-signed (quick, testing)   [2] Upload cert : ')"
    case "$c" in 2) ssl="upload";; *) ssl="self-signed";; esac
  fi
  if [ "$ssl" = "letsencrypt" ]; then email="$(ask '  Email for Lets Encrypt renewal notices: ')"; fi
  if [ "$ssl" = "upload" ]; then
    cert="$(ask '  Path to fullchain cert (.pem): ')"
    key="$(ask '  Path to private key (.pem): ')"
  fi

  DEPLOY_ARGS=(--host "$host" --ssl "$ssl" --ext-port "$extport" -y)
  if [ -n "$email" ]; then DEPLOY_ARGS+=(--email "$email"); fi
  if [ -n "$cert" ];  then DEPLOY_ARGS+=(--cert "$cert"); fi
  if [ -n "$key" ];   then DEPLOY_ARGS+=(--key "$key"); fi

  echo >&2
  echo "  Summary:   host=${host}   port=${extport}   tls=${ssl}${email:+   email=${email}}" >&2
  local ok; ok="$(ask '  Proceed with deployment? [Y/n]: ')"
  case "$ok" in [nN]*) echo "  Cancelled." >&2; exit 1;; esac
}

main() {
  if [ -z "${STACKSENSE_PLAN_ONLY:-}" ]; then
    require_root; require_apt; install_prereqs; clone_repo; cd "${DIR}"
  fi

  DEPLOY_ARGS=()
  if [ "$#" -gt 0 ]; then
    DEPLOY_ARGS=("$@")
    [ -t 0 ] || DEPLOY_ARGS+=(-y)     # scripted + piped -> non-interactive
  else
    gather_requirements               # no flags -> ask the client
  fi

  if [ -n "${STACKSENSE_PLAN_ONLY:-}" ]; then
    echo "PLAN: ./setup.sh ${DEPLOY_ARGS[*]}"; return 0
  fi
  log "Running: ./setup.sh ${DEPLOY_ARGS[*]}"
  exec ./setup.sh "${DEPLOY_ARGS[@]}"
}

main "$@"
