#!/usr/bin/env bash
#
# StackSense -- safe in-place update for a deployed host.
#
#   ./update.sh               # test the new code -> migrate -> restart -> health-check -> rollback if broken
#   ./update.sh --skip-tests  # deploy without the test gate (only if you already tested on dev)
#   ./update.sh --dry-run     # show the plan; touch nothing
#
# Because the code is volume-mounted, an update is just: pull + migrate + restart
# (a rebuild only when requirements.txt/Dockerfile changed). The new code is tested
# BEFORE the running server is touched, and the deploy is rolled back if the new
# version fails its health check.
#
set -euo pipefail
cd "$(dirname "$0")"

SKIP_TESTS=false; DRY_RUN=false
for a in "$@"; do
  case "$a" in
    --skip-tests) SKIP_TESTS=true;;
    --dry-run) DRY_RUN=true;;
    -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//' | head -14; exit 0;;
    *) echo "Unknown option: $a" >&2; exit 1;;
  esac
done

say() { echo; echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# Match how this host was deployed: include the prod overlay ONLY if the containerized
# nginx is running (lean setup.sh deploy). A host-nginx deploy uses the base file alone,
# so we never clash on port 80.
COMPOSE="docker compose"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^monitoring_nginx$'; then
  COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
fi

deps_changed() { ! git diff --quiet "$PREV" "$NEW" -- requirements.txt Dockerfile; }

restart_web() {
  if deps_changed; then $COMPOSE up -d --build web; else $COMPOSE restart web; fi
}

rollback() {
  say "ROLLING BACK to $PREV"
  git reset --hard "$PREV"
  restart_web || true
  echo "Rolled back the code. NOTE: database migrations are NOT auto-reverted -- if a"
  echo "migration caused the failure, inspect it: $COMPOSE exec web python manage.py showmigrations"
}

PREV="$(git rev-parse HEAD)"

say "Fetching latest code"
$DRY_RUN || git fetch --quiet origin
NEW="$(git rev-parse '@{u}' 2>/dev/null || echo "$PREV")"

if $DRY_RUN; then
  $SKIP_TESTS && teststep="(tests skipped)" || teststep="test new code"
  say "DRY RUN -- plan only"
  echo "  compose:         $COMPOSE"
  echo "  current commit:  $PREV"
  echo "  upstream commit: $NEW"
  echo "  steps: pull --ff-only -> $teststep -> migrate -> collectstatic -> restart/rebuild web -> health-check(/health/) -> rollback on failure"
  exit 0
fi

git pull --ff-only || die "git pull failed (local changes or non-fast-forward). Resolve and retry."
NEW="$(git rev-parse HEAD)"
if [ "$PREV" = "$NEW" ]; then say "Already up to date — nothing to deploy."; exit 0; fi
say "Updating $PREV -> $NEW"

# 1) Test the NEW code first. The running server still serves the OLD code until restart,
#    so a failure here leaves production untouched.
if ! $SKIP_TESTS; then
  say "Testing the new code (deploy is blocked if anything fails)"
  if ! $COMPOSE exec -T web python manage.py test core; then
    git reset --hard "$PREV"
    die "Tests failed — NOT deploying. Reverted code to $PREV; the running server was never touched."
  fi
fi

# 2) Migrate + ship.
say "Applying migrations"
$COMPOSE exec -T web python manage.py migrate --noinput || { rollback; die "migrate failed"; }
say "Collecting static files"
$COMPOSE exec -T web python manage.py collectstatic --noinput >/dev/null 2>&1 || true
say "Restarting the app"
restart_web || { rollback; die "restart/rebuild failed"; }

# 3) Health-check; roll back if the new version isn't serving.
say "Health check"
ok=false
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health/ >/dev/null 2>&1; then ok=true; break; fi
  sleep 2
done
$ok || { rollback; die "New version failed its health check — rolled back to $PREV."; }

say "Deployed $NEW successfully."
