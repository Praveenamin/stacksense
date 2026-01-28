# Production-Grade Test Plan

**Goals:**
1. **Read-only + agent-only:** No remote changes except reading and agent/access/deps install.
2. **Production readiness:** Core flows work; timeouts and errors are handled.

---

## 1. Remote-Actions Compliance (No Unauthorized Writes)

### 1.1 Automated checks

Run:

```bash
cd /home/ubuntu/stacksense-repo
python -m pytest tests/test_remote_actions_audit.py -v
```

These tests:
- Fail if `systemctl start`, `systemctl stop`, `systemctl restart` appear in app Python (excluding `agent/` and `*.md`).
- Fail if `iptables` or `ufw ` (with space) appear in app Python.
- Fail if any code path implements BLOCK_IP (iptables/ufw/block) — currently BLOCK_IP is only a model choice.

### 1.2 Manual audit

- Keep `REMOTE_ACTIONS_AUDIT.md` in sync when adding new `exec_command`, `open_sftp`, or any remote command execution.
- After adding such code: re-run the audit table and the automated tests.

---

## 2. Core Flows (Manual / Staging)

Use a **single test server** with SSH key and (optional) agent. Prefer a throwaway or staging host.

### 2.1 Metrics collection

| Step | Action | Expected |
|------|--------|----------|
| 2.1.1 | Add server (SSH key or password→key deploy) | Server added; key works if deployed. |
| 2.1.2 | Run `python manage.py collect_metrics <server_id>` | Exit 0; SystemMetric rows for that server. |
| 2.1.3 | On server: `ls -la /tmp/collect_metrics.py` (optional) | File may exist; it is read-only in behavior. |
| 2.1.4 | If psutil was missing: after first run, `python3 -c "import psutil"` on server | Succeeds. |

### 2.2 Log scanning

| Step | Action | Expected |
|------|--------|----------|
| 2.2.1 | Add MonitoredLog for an Apache/nginx (or test) log path. | MonitoredLog created. |
| 2.2.2 | Run `python manage.py scan_logs` | Exit 0; LogEvents if the path has matching lines. |
| 2.2.3 | On server: only `tail` and `wc -c` used on the path | No writes to the log file. |

### 2.3 Heartbeats

| Step | Action | Expected |
|------|--------|----------|
| 2.3.1 | With agent or SSH heartbeat: run `check_heartbeats` / `check_heartbeats_ssh` (per your setup). | No errors; last_heartbeat or equivalent updated. |
| 2.3.2 | If using agent: ensure `deploy_agent.sh` is **not** called by Django. | Confirmed: only manual or external automation. |

### 2.4 Service discovery and status

| Step | Action | Expected |
|------|--------|----------|
| 2.4.1 | Run `python manage.py discover_services <server_id>` or `--all` | Services discovered; only `systemctl list-units` used (read). |
| 2.4.2 | Trigger service check (e.g. check_services cron or UI that calls it) | Only `systemctl is-active`, `is-failed` (read). |

### 2.5 Service latency

| Step | Action | Expected |
|------|--------|----------|
| 2.5.1 | For a service with a port: run `collect_service_latency` or trigger from UI. | Latency stored; for localhost-bound: only `python3 -c 'socket.connect...'` (read). |

### 2.6 Log troubleshooting and AI

| Step | Action | Expected |
|------|--------|----------|
| 2.6.1 | Open Log Troubleshooting, run analysis. | Results from LogEvents; no remote writes. |
| 2.6.2 | “Ask AI for solution” with Ollama (or mock) configured. | No timeout with 120s Ollama timeout and 180s gunicorn; or clear error. |

---

## 3. Timeouts and Errors

| Area | Check | Expected |
|------|-------|----------|
| Ollama | OLLAMA_TIMEOUT=120; gunicorn --timeout 180; nginx proxy_read 180s | “Ask AI” completes or returns a clear timeout/connection error. |
| SSH | collect_metrics, scan_logs, discover_services, service checks | 10–30s timeouts; no indefinite hang. |
| collect_metrics | Script run: 90s timeout | Script finishes or fails with error; no silent hang. |

---

## 4. Production Readiness Checklist

- [ ] **Migrations** applied; no pending.
- [ ] **Cron (or scheduler):** `collect_metrics`, `scan_logs`, `check_heartbeats` / `check_heartbeats_ssh`, `check_services`, `detect_anomalies` as needed.
- [ ] **Log retention:** App config `log_retention_days` (7/15/30) and scan_logs purge in place.
- [ ] **Ollama:** If used: OLLAMA_API_URL and OLLAMA_TIMEOUT; healthcheck/logging for failures.
- [ ] **SSH:** Key path, permissions; no password in code.
- [ ] **BLOCK_IP:** Documented as “recommendation only”; no automatic blocking.

---

## 5. Running the Automated Audit Tests

From repo root:

```bash
python3 test_remote_actions_audit.py
```

Or with pytest: `python3 -m pytest test_remote_actions_audit.py -v`
