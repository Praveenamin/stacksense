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

- StackSense no longer executes any remote commands on monitored servers (push-agent only). If that ever changes, update `REMOTE_ACTIONS_AUDIT.md`.
- After adding any outbound/remote behavior: re-run the audit table and the automated tests.

---

## 2. Core Flows (Manual / Staging)

Use a **single test server** with the push agent installed. Prefer a throwaway or staging host.

### 2.1 Metrics ingestion (push agent)

| Step | Action | Expected |
|------|--------|----------|
| 2.1.1 | Add server (name + IP) in the UI | Server added; per-server token + install command shown. |
| 2.1.2 | Run the one-line `curl … | sudo bash` installer on the target server | Agent installed; `stacksense-agent` service running. |
| 2.1.3 | Wait for the first push | SystemMetric rows appear for that server; status flips to online. |
| 2.1.4 | Send a bad/empty token push (manual curl) | Rejected with 401/403; no metric written. |

### 2.2 Log scanning

| Step | Action | Expected |
|------|--------|----------|
| 2.2.1 | Add MonitoredLog for an Apache/nginx (or test) log path. | MonitoredLog created. |
| 2.2.2 | Run `python manage.py scan_logs` | Exit 0; old LogEvents purged (housekeeping only — no longer collects logs over SSH; live log push is paused). |
| 2.2.3 | Open Log Troubleshooting pages | Existing LogEvents render; feature is kept. |

### 2.3 Heartbeats

| Step | Action | Expected |
|------|--------|----------|
| 2.3.1 | Run `check_heartbeats` / `check_server_connectivity` (reads pushed heartbeats; no SSH). | No errors; status reflects last push freshness. |
| 2.3.2 | Stop the agent on the test server, wait past the threshold. | Server flips to offline; alert raised on status change. |

### 2.4 Services and containers (agent-pushed)

| Step | Action | Expected |
|------|--------|----------|
| 2.4.1 | Confirm the agent pushes its service list. | Services appear for the server; no StackSense→server call made. |
| 2.4.2 | Confirm the agent pushes container inventory (Docker/Podman/containerd). | Containers listed; data is push-sourced. |

### 2.5 Service latency

| Step | Action | Expected |
|------|--------|----------|
| 2.5.1 | For a service with a port: run `collect_service_latency` or trigger from UI. | Latency stored via TCP/HTTP probe; localhost-only services are skipped. |

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
| Ingest endpoint | Agent push handler | Handles malformed/oversized payloads without hanging; rejects bad tokens fast. |
| Service latency | `collect_service_latency` TCP/HTTP probe | 10–30s timeout per probe; no indefinite hang. |

---

## 4. Production Readiness Checklist

- [ ] **Migrations** applied; no pending.
- [ ] **Scheduler:** `metrics_scheduler.py` running; `check_heartbeats` / `check_server_connectivity`, `detect_anomalies`, `detect_memory_leaks`, `detect_security_events`, `run_synthetic_checks`, `collect_service_latency`, `scan_logs` (housekeeping), `aggregate_metrics`, `cleanup_metrics` as needed.
- [ ] **Agents:** Each monitored server has the agent installed and pushing; per-server tokens valid.
- [ ] **Log retention:** App config `log_retention_days` (7/15/30) and scan_logs purge in place.
- [ ] **Ollama:** If used: OLLAMA_API_URL and OLLAMA_TIMEOUT; healthcheck/logging for failures.
- [ ] **Tokens:** Per-server bearer tokens stored securely; rotation/revocation works; no SSH credentials held.
- [ ] **BLOCK_IP:** Documented as “recommendation only”; no automatic blocking.

---

## 5. Running the Automated Audit Tests

From repo root:

```bash
python3 test_remote_actions_audit.py
```

Or with pytest: `python3 -m pytest test_remote_actions_audit.py -v`
