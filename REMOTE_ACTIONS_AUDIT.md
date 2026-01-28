# Remote Actions Audit: Read-Only + Agent Install

**Purpose:** Verify the app **does not change client servers** except for (1) **reading information** and (2) **installing the agent / access** (SSH key, psutil, and the metrics collection script in `/tmp`).

**Conclusion:** All remote actions fall into **READ**, **AGENT_ACCESS**, or **AGENT_DEPS** (psutil, `/tmp` script for metrics). There are **no** `systemctl start/stop/restart`, `iptables`, `ufw`, or any BLOCK_IP implementation. **BLOCK_IP** is only an AnalysisRule choice; no code runs firewall/block commands.

---

## 1. Audit Table

| Component | Location | Command / Action | Classification | Notes |
|-----------|----------|------------------|----------------|-------|
| **SSH key deploy** | `core/views.py` ~1397, 1402 | `mkdir -p ~/.ssh`, `chmod 700 ~/.ssh`, `grep` in `authorized_keys`; if missing: `echo "key" >> ~/.ssh/authorized_keys`, `chmod 600` | **AGENT_ACCESS** | One-time setup for passwordless SSH. |
| **SSH key deploy** | `core/admin.py` 133, 138 | Same as above | **AGENT_ACCESS** | Admin path for key deploy. |
| **SSH test** | `core/admin.py` 200 | `echo "Connection successful"` | **READ** | Connectivity check only. |
| **psutil install** | `core/views.py` 1468, 1512, 1546 | `python3 -c "import psutil"`; then pip/apt/yum install psutil (--user or system) | **AGENT_DEPS** | Needed for `collect_metrics`; user/system package install only. |
| **collect_metrics** | `core/management/commands/collect_metrics.py` 591–594, 599 | SFTP write `/tmp/collect_metrics.py`, `chmod 0o755`; `python3 /tmp/collect_metrics.py` | **AGENT_DEPS** + **READ** | Script: psutil, `/proc`, `/sys`, `lscpu`, `lsblk`, `mdadm` — all read. Only remote writes: create/overwrite file in `/tmp`. File is not deleted after run. |
| **_detect_listening_ports** | `collect_metrics.py` 666 | `ss -tlnp` or `netstat -tlnp` | **READ** | Listener info only. |
| **scan_logs** | `core/management/commands/scan_logs.py` 98, 108 | `tail -n 1000 {path}` or `tail -c +{off} {path}`; `wc -c < {path}` | **READ** | Read log content and size only. |
| **discover_services** | `core/management/commands/discover_services.py` 84 | `systemctl list-units --type=service --all --no-pager --no-legend` | **READ** | List units only. |
| **service_scanner** | `core/service_scanner.py` 57, 64, 69 | `systemctl list-units --type=service --state=running --no-pager --no-legend`; `ss -tuln \| grep LISTEN`; `ps aux --sort=-%cpu \| head -11` | **READ** | List services, ports, processes. |
| **Service status check** | `core/views.py` 2863, 2884 | `systemctl is-active {name}`; `systemctl is-failed {name}` | **READ** | Status queries only. |
| **get_top_cpu_processes** | `core/views.py` 4478 | `ps aux --sort=-%cpu \| head -4 \| tail -3 \| awk ...` | **READ** | Process list by CPU. |
| **get_top_ram_processes** | `core/views.py` 4569 | `ps aux --sort=-%mem \| head -4 \| tail -3 \| awk ...` | **READ** | Process list by RAM. |
| **get_active_services** | `core/views.py` 4659, 4702 | `systemctl list-units --type=service --all --no-pager --no-legend`; `systemctl show {unit} --property=ActiveEnterTimestamp --value` | **READ** | List units and one property. |
| **get_server_services** | `core/views.py` 4874 | `systemctl list-units --type=service --all --no-pager --no-legend` | **READ** | Same as discover. |
| **measure_ssh_local_latency** | `core/service_latency.py` 156 | `python3 -c 'socket.connect(("127.0.0.1",port)); print(json.dumps(...))'` | **READ** | TCP connect to localhost and print JSON; no files, no service changes. |
| **get_top_processes (utils)** | `core/utils.py` 294, 327 | `ps aux --sort=-%cpu` or `-%mem` with `head`/`tail`/`awk` | **READ** | Same as views’ top-CPU/RAM. |

---

## 2. Classification

- **READ:** Query only (systemctl, ps, ss/netstat, tail, wc, /proc, /sys, lscpu, lsblk, mdadm, Python socket connect + print). No writes, no restarts, no blocks.
- **AGENT_ACCESS:** `~/.ssh`, `authorized_keys` — used for SSH access only.
- **AGENT_DEPS:** Install psutil; write and run `/tmp/collect_metrics.py`. Script logic is read-only; the only persistent change on the server is a file in `/tmp` (and package/psutil if not already present).

---

## 3. Explicitly Not Performed

- **BLOCK_IP:** Exists only as `AnalysisRule.recommendation` choice. No `iptables`, `ufw`, or other block logic.
- **systemctl start / stop / restart:** Not used. `agent/deploy_agent.sh` does `systemctl enable/start` for the agent; that script is **not** invoked by the Django app.
- **Killing processes:** Not used.
- **Editing configs (e.g. Apache/nginx):** Not used.

---

## 4. Acceptable Writes (Agent / Agent-Deps)

| Action | Rationale |
|--------|-----------|
| `~/.ssh`, `authorized_keys` | Required for agent/SSH access. |
| `pip/apt/yum install psutil` | Dependency for remote metrics script. |
| `/tmp/collect_metrics.py` + `chmod` | Standard pattern for one-off remote scripts. Script is read-only; file can remain in `/tmp` until cleared by OS. Optional improvement: delete after run. |

---

## 5. Recommendation

The application is **compliant** with: *no changes to client servers other than reading and installing agent/access/dependencies*. For stricter hygiene, consider deleting `/tmp/collect_metrics.py` after a successful run in `collect_metrics`.
