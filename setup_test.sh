#!/usr/bin/env bash
#
# Smoke tests for setup.sh -- syntax + per-SSL-mode dry-run output assertions.
# No Docker required (exercises only the --dry-run generation logic).
#
#   ./setup_test.sh
#
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }
run()  { ./setup.sh "$@" --dry-run -y 2>&1; }

echo "== syntax =="
bash -n setup.sh || fail "setup.sh has a syntax error"
ok "bash -n"

echo "== self-signed =="
out="$(run --host 10.0.0.5 --ssl self-signed)"
grep -q "USE_TLS=True"                              <<<"$out" || fail "USE_TLS should be True"
grep -q "OLLAMA_API_URL=http://ollama:11434"        <<<"$out" || fail "Ollama URL must be the service name"
if grep -q "DJANGO_SUPERUSER" <<<"$out"; then fail "no weak admin creds should appear (form-only)"; fi
grep -q "ALLOWED_HOSTS=10.0.0.5,"                   <<<"$out" || fail "ALLOWED_HOSTS missing the host"
grep -q "CSRF_TRUSTED_ORIGINS=https://10.0.0.5"     <<<"$out" || fail "CSRF origins missing https host"
grep -q "selfsigned/fullchain.pem"                  <<<"$out" || fail "self-signed cert path missing"
grep -q 'X-Forwarded-Proto \$scheme'                <<<"$out" || fail "X-Forwarded-Proto header missing"
grep -q 'proxy_set_header Host \$host'              <<<"$out" || fail "Host header missing"
grep -q "return 301 https"                          <<<"$out" || fail "http->https redirect missing"
ok "self-signed env + nginx"

echo "== letsencrypt =="
out="$(run --host mon.example.com --ssl letsencrypt --email a@b.c)"
grep -q "live/mon.example.com/fullchain.pem"        <<<"$out" || fail "LE cert path missing"
grep -q "certbot certonly --webroot"                <<<"$out" || fail "LE issuance step missing"
grep -q "acme-challenge"                            <<<"$out" || fail "ACME challenge location missing"
ok "letsencrypt cert path + issuance"

echo "== upload =="
out="$(run --host mon.example.com --ssl upload --cert fullchain.pem --key privkey.pem)"
grep -q "uploaded/fullchain.pem"                    <<<"$out" || fail "uploaded cert path missing"
ok "upload cert path"

echo "== rejects a bad SSL mode =="
if ./setup.sh --host x --ssl bogus --dry-run -y >/dev/null 2>&1; then fail "bad --ssl was accepted"; fi
ok "bad --ssl rejected"

echo "== dry-run has no side effects =="
before="$( [ -f deploy/nginx/app.conf ] && echo y || echo n )"
run --host 10.0.0.5 --ssl self-signed >/dev/null
after="$(  [ -f deploy/nginx/app.conf ] && echo y || echo n )"
[ "$before" = "$after" ] || fail "dry-run created/modified deploy/nginx/app.conf"
ok "no files written"

echo "== update.sh =="
bash -n update.sh || fail "update.sh has a syntax error"
ok "bash -n"
out="$(./update.sh --dry-run 2>&1)"
grep -q "DRY RUN"      <<<"$out" || fail "update.sh dry-run banner missing"
grep -q "health-check" <<<"$out" || fail "update.sh plan missing health-check"
grep -q "rollback"     <<<"$out" || fail "update.sh plan missing rollback"
if ./update.sh --bogus >/dev/null 2>&1; then fail "update.sh accepted an unknown flag"; fi
ok "dry-run plan + rejects unknown flag"

echo "ALL DEPLOY-SCRIPT SMOKE TESTS PASSED"
