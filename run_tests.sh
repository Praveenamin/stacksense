#!/usr/bin/env bash
#
# StackSense test suite -- the single entrypoint for local runs and CI.
#
# Runs BOTH the Django test suite (the `core` app) and the standalone agent
# resilience suite. The agent is a standalone script, not a Django app, so
# `manage.py test` does NOT discover agent/test_agent_resilience.py -- this script
# makes sure it always runs alongside the rest.
#
# Usage (inside the web container, e.g. `docker compose exec web ./run_tests.sh`):
#   ./run_tests.sh                       # everything (Django core + agent suite)
#   ./run_tests.sh core.test_alert_routing   # targeted: just these Django tests
#
# CI: this is the entrypoint. A GitHub Actions job would bring up the compose stack
# and run `docker compose exec -T web ./run_tests.sh`.
#
set -euo pipefail
cd "$(dirname "$0")"

# Targeted run: pass-through to manage.py test, skip the agent suite.
if [ "$#" -gt 0 ]; then
    exec python manage.py test "$@"
fi

echo "=================================================="
echo " 1/2  Django test suite (core)"
echo "=================================================="
python manage.py test core

echo
echo "=================================================="
echo " 2/2  Agent resilience suite (standalone)"
echo "=================================================="
python agent/test_agent_resilience.py

echo
echo "All test suites passed."
