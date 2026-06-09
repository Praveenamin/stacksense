# Remote Actions Audit: Push-Agent Model (No Outbound Connections)

**Purpose:** Verify that StackSense **does not connect to or change monitored
servers** in any way.

**Conclusion:** StackSense is now **push-agent only**. The StackSense server
**never initiates a connection** to a monitored server — no SSH, no SFTP, no
remote command execution. All data (metrics, services, containers, heartbeats,
the server's own SSH auth events) is collected **locally by the agent** on each
monitored VM and **pushed** to StackSense over HTTPS with a per-server bearer
token. There are therefore **no** remote reads or writes performed by StackSense
to audit, and **no** `systemctl start/stop/restart`, `iptables`, `ufw`, or
BLOCK_IP execution anywhere. **BLOCK_IP** remains only an `AnalysisRule`
recommendation choice; no code runs firewall/block commands.

---

## 1. What StackSense Does Toward Monitored Servers

| Direction | Behavior |
|-----------|----------|
| **Inbound (server → StackSense)** | The agent POSTs metrics/services/containers/heartbeats/SSH-auth-events over HTTPS. StackSense authenticates the per-server bearer token and stores the data. |
| **Outbound (StackSense → server)** | **None.** StackSense does not SSH, SFTP, or run any command on monitored servers. |
| **Outbound to services** | `collect_service_latency` may probe a reachable service over plain TCP/HTTP for latency; localhost-only services are skipped. This is a network probe, not a login or command execution. |

The agent install is performed by an admin running a one-line
`curl … | sudo bash` command on the target server; StackSense does not deploy
anything itself.

---

## 2. Removed (was SSH-based, no longer exists)

The following SSH/SFTP behaviors were **removed** when StackSense moved to the
push-agent model:

- SSH public-key auto-deployment to monitored servers.
- The `paramiko` dependency and the `ssh_keys/` directory.
- Remote `psutil` install and the `/tmp/collect_metrics.py` SFTP-write-and-run pattern.
- The `collect_metrics`, `discover_services`, `check_heartbeats_ssh`, and
  `check_services` management commands (all SSH-based).
- Remote `systemctl`/`ss`/`ps`/`tail` execution over SSH for status, services,
  and logs — this data is now agent-pushed.

---

## 3. Explicitly Not Performed

- **Outbound SSH/SFTP/remote exec:** Not used. StackSense never connects out.
- **BLOCK_IP:** Exists only as an `AnalysisRule.recommendation` choice. No
  `iptables`, `ufw`, or other block logic.
- **systemctl start / stop / restart on monitored servers:** Not used.
- **Killing processes / editing configs on monitored servers:** Not used.

> Note: The monitoring host's **own** firewall (e.g. `ufw allow 22` so admins can
> SSH into the StackSense box itself) and the agent's report of each monitored
> server's **own** SSH auth events (`SSHAuthEvent`, which feeds brute-force /
> security detection) are unrelated to StackSense making outbound connections —
> the latter is agent-pushed data, not a StackSense SSH session.

---

## 4. Recommendation

The application is **compliant**: it makes **no changes to, and no logins into,**
monitored servers. The only outbound network behavior is optional TCP/HTTP
latency probes to reachable service ports.
