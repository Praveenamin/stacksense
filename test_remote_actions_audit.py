#!/usr/bin/env python3
"""
Remote actions audit tests: ensure the app does not change client servers
except for reading and agent/access install.

Run:
  python test_remote_actions_audit.py
  # or
  python -m pytest test_remote_actions_audit.py -v
"""

import os
import re
from pathlib import Path

# Repo root: same dir as this script
REPO_ROOT = Path(__file__).resolve().parent

# Directories to scan for .py (app code). Excludes agent/, migrations (optional), and __pycache__.
APP_DIRS = ["core", "log_analyzer"]
# Forbidden substrings in app .py (runtime code that would modify client state)
FORBIDDEN = [
    ("systemctl start", "must not start services on remote"),
    ("systemctl stop", "must not stop services on remote"),
    ("systemctl restart", "must not restart services on remote"),
    ("iptables", "must not run iptables on remote (BLOCK_IP is recommendation-only)"),
    ("ufw ", "must not run ufw on remote (BLOCK_IP is recommendation-only)"),
    ("ufw\n", "must not run ufw on remote"),
]


def get_app_py_files():
    files = []
    for d in APP_DIRS:
        p = REPO_ROOT / d
        if not p.exists():
            continue
        for f in p.rglob("*.py"):
            if "__pycache__" in str(f) or "/migrations/" in str(f):
                continue
            files.append(f)
    return files


def test_no_forbidden_remote_commands():
    """App must not run systemctl start/stop/restart or iptables/ufw on remotes."""
    failures = []
    for path in get_app_py_files():
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            failures.append((str(path), f"read error: {e}"))
            continue
        rel = path.relative_to(REPO_ROOT)
        for sub, reason in FORBIDDEN:
            for line in content.splitlines():
                if sub in line and not line.strip().startswith("#"):
                    failures.append((str(rel), f"contains '{sub}': {reason}"))
                    break
    assert not failures, (
        "Forbidden remote command patterns found:\n  "
        + "\n  ".join(f"{p}: {r}" for p, r in failures)
    )


def test_block_ip_not_implemented():
    """BLOCK_IP must remain recommendation-only; no iptables/ufw with exec/subprocess."""
    failures = []
    for path in get_app_py_files():
        if "/migrations/" in str(path):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        has_block_cmd = "iptables" in content or "ufw " in content
        has_exec = "exec_command" in content or "subprocess" in content or "os.system" in content or "Popen" in content
        if has_block_cmd and has_exec:
            failures.append((str(path.relative_to(REPO_ROOT)), "contains both iptables/ufw and exec/subprocess"))
    assert not failures, (
        "BLOCK_IP might be implemented (iptables/ufw with exec/subprocess):\n  "
        + "\n  ".join(f"{p}: {r}" for p, r in failures)
    )


def run():
    err = 0
    for name, fn in [("no_forbidden_remote_commands", test_no_forbidden_remote_commands), ("block_ip_not_implemented", test_block_ip_not_implemented)]:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            err = 1
        except Exception as e:
            print(f"ERROR {name}: {e}")
            err = 1
    return err


if __name__ == "__main__":
    exit(run())
