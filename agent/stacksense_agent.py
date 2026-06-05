#!/usr/bin/env python3
"""
StackSense Push Agent

Runs on a monitored VM and PUSHES system metrics out to the StackSense
monitoring server over HTTPS. The agent only makes outbound calls -- it opens
no ports and listens for nothing -- and authenticates with a per-server bearer
token. The monitoring server never holds any credential that can log in to this
VM.

Dependencies:
    - Python 3.6+
    - psutil           (the only third-party package; for reading system stats)
    HTTP is done with the standard library (urllib), so 'requests' is NOT needed.

Configuration (environment variables, or ~/.stacksense_agent.conf as JSON):
    STACKSENSE_URL          Base URL of the monitoring server, e.g. https://mon.example.com:8000
    STACKSENSE_TOKEN        Per-server bearer token (from `manage.py create_agent_token`)
    STACKSENSE_INTERVAL     Seconds between pushes (default: 30)
    STACKSENSE_VERIFY_TLS   "true"/"false" -- verify the server's TLS cert (default: true)

Usage:
    python3 stacksense_agent.py            # run forever (intended for systemd)
    python3 stacksense_agent.py --once     # collect & push a single sample, then exit
    python3 stacksense_agent.py --dry-run  # collect & print one sample, push nothing
"""

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import psutil
except ImportError:
    sys.stderr.write(
        "ERROR: psutil is not installed. Install it with:\n"
        "    python3 -m pip install --user psutil\n"
    )
    sys.exit(1)

AGENT_VERSION = "push-1.1.0"
CONFIG_FILE = Path.home() / ".stacksense_agent.conf"
DEFAULT_INTERVAL = 30
HTTP_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_DELAY = 5

# Virtual filesystems we never report as disks.
IGNORED_FSTYPES = {
    "squashfs", "tmpfs", "devtmpfs", "proc", "sysfs",
    "cgroup", "cgroup2", "ramfs", "overlay", "udev", "virtfs",
}


def load_config():
    """Load config from environment variables, falling back to the config file."""
    config = {
        "url": os.environ.get("STACKSENSE_URL"),
        "token": os.environ.get("STACKSENSE_TOKEN"),
        "interval": int(os.environ.get("STACKSENSE_INTERVAL", DEFAULT_INTERVAL)),
        "verify_tls": os.environ.get("STACKSENSE_VERIFY_TLS", "true").lower() != "false",
    }
    if (not config["url"] or not config["token"]) and CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                file_config = json.load(f)
            for key in ("url", "token", "interval", "verify_tls"):
                if config.get(key) in (None, "") and key in file_config:
                    config[key] = file_config[key]
        except Exception as e:
            sys.stderr.write(f"Warning: could not read {CONFIG_FILE}: {e}\n")

    if not config.get("url") or not config.get("token"):
        sys.stderr.write(
            "ERROR: missing configuration. Set STACKSENSE_URL and STACKSENSE_TOKEN "
            f"(env vars) or populate {CONFIG_FILE}.\n"
        )
        sys.exit(1)

    config["url"] = config["url"].rstrip("/")
    return config


def _safe(fn, default=None):
    """Call fn() and swallow errors (some psutil calls need root or aren't portable)."""
    try:
        return fn()
    except Exception:
        return default


def collect_top_processes(limit=5):
    """Return {'cpu': [...], 'memory': [...]} of the heaviest processes."""
    procs = []
    for p in psutil.process_iter(["pid", "name", "username"]):
        try:
            procs.append(p)
        except Exception:
            continue
    # Prime cpu_percent (first call returns 0.0), then sample.
    for p in procs:
        _safe(lambda: p.cpu_percent(None))
    time.sleep(0.3)

    rows = []
    for p in procs:
        try:
            rows.append({
                "pid": p.pid,
                "name": p.info.get("name") or "",
                "user": p.info.get("username") or "",
                "cpu_percent": round(p.cpu_percent(None), 1),
                "memory_percent": round(p.memory_percent(), 1),
                # Absolute resident memory (bytes) + process start time. These let the
                # server track one process's memory growth over time (leak detection).
                "rss": _safe(lambda: p.memory_info().rss, 0),
                "start_time": _safe(lambda: int(p.create_time()), 0),
            })
        except Exception:
            continue

    by_cpu = sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:limit]
    by_mem = sorted(rows, key=lambda r: r["memory_percent"], reverse=True)[:limit]
    return {"cpu": by_cpu, "memory": by_mem}


def collect_services():
    """Detect running services on this host.

    Primary source: systemd running units (works as a non-root user).
    Best-effort secondary: listening TCP/UDP ports (only what psutil can see).
    Returns a list of {name, status, service_type, port, bind_address, process_id}.
    """
    services = []
    seen = set()

    # systemd running services
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running",
             "--no-pager", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=10,
        )
        for line in out.stdout.splitlines():
            parts = line.split(None, 4)
            if not parts:
                continue
            unit = parts[0]
            if unit.endswith(".service"):
                name = unit[:-len(".service")]
                if name and name not in seen:
                    seen.add(name)
                    services.append({"name": name, "status": "running", "service_type": "systemd"})
    except Exception:
        pass

    # Listening ports (best-effort; full visibility needs root)
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != "LISTEN" or not conn.laddr:
                continue
            port = conn.laddr.port
            addr = conn.laddr.ip
            pname = ""
            if conn.pid:
                pname = _safe(lambda: psutil.Process(conn.pid).name(), "") or ""
            name = pname or f"port-{port}"
            key = ("port", name, port)
            if key in seen:
                continue
            seen.add(key)
            services.append({
                "name": name, "status": "running", "service_type": "port",
                "port": port, "bind_address": addr,
                "process_id": str(conn.pid) if conn.pid else "",
            })
    except Exception:
        pass

    return services


_SSH_LOG_PATHS = ["/var/log/auth.log", "/var/log/secure"]
_SSH_RE_ACCEPTED = re.compile(r"sshd\[\d+\]:\s+Accepted \w+ for (\S+) from (\d{1,3}(?:\.\d{1,3}){3})")
_SSH_RE_FAILED = re.compile(r"sshd\[\d+\]:\s+Failed password for (?:invalid user )?(\S+) from (\d{1,3}(?:\.\d{1,3}){3})")
_SSH_RE_INVALID = re.compile(r"sshd\[\d+\]:\s+Invalid user (\S+) from (\d{1,3}(?:\.\d{1,3}){3})")


def collect_ssh_auth(state, max_events=500):
    """Incrementally tail the SSH auth log and return new auth events.

    Lightweight: tracks a byte offset and reads only newly-appended lines each
    cycle (seeks to end on first run, so no historical backfill). Returns [] if
    no log is present or the agent user can't read it (needs the 'adm' group).
    """
    path = None
    for p in _SSH_LOG_PATHS:
        if os.path.exists(p):
            path = p
            break
    if not path:
        return []
    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    # First time we see this log: start from the end (don't reprocess history).
    if state.get("path") != path:
        state["path"] = path
        state["offset"] = size
        return []

    offset = state.get("offset", size)
    if offset > size:  # log was rotated/truncated
        offset = 0

    events = []
    try:
        with open(path, "r", errors="replace") as f:
            f.seek(offset)
            for line in f:
                if "sshd" not in line:
                    continue
                m = _SSH_RE_ACCEPTED.search(line)
                if m:
                    events.append({"username": m.group(1)[:150], "source_ip": m.group(2), "success": True, "raw": line.strip()[:300]})
                else:
                    m = _SSH_RE_FAILED.search(line) or _SSH_RE_INVALID.search(line)
                    if m:
                        events.append({"username": m.group(1)[:150], "source_ip": m.group(2), "success": False, "raw": line.strip()[:300]})
                if len(events) >= max_events:
                    break
            state["offset"] = f.tell()
    except (OSError, PermissionError):
        return []
    return events


def collect_containers():
    """Detect containers via the Docker CLI (best-effort).

    Returns [] if Docker isn't installed or the agent user can't access the
    Docker socket. Requires the agent user to be in the 'docker' group.
    """
    out = []
    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode != 0:
            return out
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
            except ValueError:
                continue
            status = c.get("Status") or ""
            state = (c.get("State") or "").lower()
            if not state:
                state = "running" if status.startswith("Up") else "exited"
            out.append({
                "container_id": (c.get("ID") or "")[:64],
                "name": c.get("Names") or "",
                "image": c.get("Image") or "",
                "state": state,
                "status_text": status[:200],
                "ports": (c.get("Ports") or "")[:300],
            })
    except Exception:
        pass
    return out


def collect_ipc_stats():
    """System V IPC + POSIX /dev/shm summary (best-effort).

    Used by the server to spot shared-memory / semaphore leaks -- e.g. orphaned
    SysV segments (nattch=0) that hold RAM forever, or a growing number of
    semaphore arrays. Non-root can read system-wide IPC via /proc/sysvipc.
    Returns a dict, or None if nothing could be read (never raises).
    """
    stats = {}

    # SysV shared memory: `ipcs -m -b`  (cols: key shmid owner perms bytes nattch status)
    try:
        res = subprocess.run(["ipcs", "-m", "-b"], capture_output=True, text=True, timeout=10)
        seg = total = orphan = orphan_bytes = 0
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[1].isdigit():  # data row (numeric shmid)
                try:
                    nbytes, nattch = int(parts[4]), int(parts[5])
                except ValueError:
                    continue
                seg += 1
                total += nbytes
                if nattch == 0:           # orphaned: no process attached
                    orphan += 1
                    orphan_bytes += nbytes
        stats.update(shm_segments=seg, shm_bytes=total,
                     shm_orphaned=orphan, shm_orphaned_bytes=orphan_bytes)
    except Exception:
        pass

    # SysV semaphore arrays: `ipcs -s`  (count data rows)
    try:
        res = subprocess.run(["ipcs", "-s"], capture_output=True, text=True, timeout=10)
        stats["sem_arrays"] = sum(
            1 for ln in res.stdout.splitlines()
            if len(ln.split()) >= 2 and ln.split()[1].isdigit()
        )
    except Exception:
        pass

    # SysV message queues: `ipcs -q -b`  (cols: key msqid owner perms used-bytes messages)
    try:
        res = subprocess.run(["ipcs", "-q", "-b"], capture_output=True, text=True, timeout=10)
        cnt = qbytes = 0
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].isdigit():
                cnt += 1
                qbytes += int(parts[4]) if parts[4].isdigit() else 0
        stats.update(msg_queues=cnt, msg_bytes=qbytes)
    except Exception:
        pass

    # POSIX shared memory: total bytes under /dev/shm
    try:
        total = 0
        with os.scandir("/dev/shm") as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                except Exception:
                    continue
        stats["devshm_bytes"] = total
    except Exception:
        pass

    return stats or None


def collect_metrics(prev):
    """Collect one metrics sample. `prev` carries counters from the last sample
    so we can compute per-second I/O rates. Returns (metrics_dict, new_prev)."""
    now = time.time()
    metrics = {"agent_version": AGENT_VERSION}

    # CPU
    metrics["cpu_percent"] = psutil.cpu_percent(interval=1)
    metrics["cpu_count"] = psutil.cpu_count()
    metrics["physical_cpu_count"] = _safe(lambda: psutil.cpu_count(logical=False))
    load = _safe(lambda: psutil.getloadavg())
    if load:
        metrics["cpu_load_avg_1m"], metrics["cpu_load_avg_5m"], metrics["cpu_load_avg_15m"] = load

    # Memory
    mem = psutil.virtual_memory()
    metrics.update({
        "memory_total": mem.total,
        "memory_available": mem.available,
        "memory_percent": mem.percent,
        "memory_used": mem.used,
        "memory_buffers": getattr(mem, "buffers", 0),
        "memory_cached": getattr(mem, "cached", 0),
        "memory_shared": getattr(mem, "shared", 0),
    })
    swap = psutil.swap_memory()
    metrics.update({
        "swap_total": swap.total or None,
        "swap_used": swap.used or None,
        "swap_percent": swap.percent if swap.total else None,
    })

    # Disk usage per real partition
    disk_usage = {}
    for part in psutil.disk_partitions(all=False):
        if part.fstype.lower() in IGNORED_FSTYPES:
            continue
        if "/virtfs/" in part.mountpoint or "virtfs" in part.device.lower():
            continue
        usage = _safe(lambda: psutil.disk_usage(part.mountpoint))
        if usage is None:
            continue
        disk_usage[part.mountpoint] = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
        }
    metrics["disk_usage"] = disk_usage

    # Network counters per interface
    net_per_nic = _safe(lambda: psutil.net_io_counters(pernic=True), {}) or {}
    metrics["network_io"] = {
        nic: {
            "bytes_sent": c.bytes_sent,
            "bytes_recv": c.bytes_recv,
            "packets_sent": c.packets_sent,
            "packets_recv": c.packets_recv,
        }
        for nic, c in net_per_nic.items()
    }
    metrics["network_connections"] = _safe(lambda: len(psutil.net_connections()))

    # Cumulative I/O counters (for rate calculation)
    disk_io = _safe(lambda: psutil.disk_io_counters())
    net_io = _safe(lambda: psutil.net_io_counters())
    cur = {
        "ts": now,
        "disk_read": disk_io.read_bytes if disk_io else None,
        "disk_write": disk_io.write_bytes if disk_io else None,
        "net_sent": net_io.bytes_sent if net_io else None,
        "net_recv": net_io.bytes_recv if net_io else None,
    }
    if disk_io:
        metrics["disk_read_bytes_total"] = disk_io.read_bytes
        metrics["disk_write_bytes_total"] = disk_io.write_bytes

    # Per-second rates vs the previous sample
    if prev and prev.get("ts"):
        dt = now - prev["ts"]
        if dt > 0:
            def rate(cur_v, prev_v):
                if cur_v is None or prev_v is None or cur_v < prev_v:
                    return None
                return int((cur_v - prev_v) / dt)
            metrics["disk_io_read"] = rate(cur["disk_read"], prev.get("disk_read"))
            metrics["disk_io_write"] = rate(cur["disk_write"], prev.get("disk_write"))
            metrics["net_io_sent"] = rate(cur["net_sent"], prev.get("net_sent"))
            metrics["net_io_recv"] = rate(cur["net_recv"], prev.get("net_recv"))

    # Uptime + top processes
    metrics["system_uptime_seconds"] = int(now - psutil.boot_time())
    metrics["top_processes"] = _safe(lambda: collect_top_processes(), {})

    return metrics, cur


def build_opener(verify_tls):
    """Return a urllib opener; optionally skip TLS verification (self-signed)."""
    if verify_tls:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def push(config, opener, path, payload):
    """POST JSON to the monitoring server with the bearer token. Returns dict or None."""
    url = f"{config['url']}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {config['token']}")

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except ValueError:
                    return {"status": resp.status, "raw": body}
        except urllib.error.HTTPError as e:
            # 4xx (e.g. 401 bad token) won't fix itself -- don't retry.
            detail = e.read().decode("utf-8", "replace")
            sys.stderr.write(f"Push failed: HTTP {e.code} {detail}\n")
            if 400 <= e.code < 500:
                return None
            last_err = e
        except Exception as e:
            last_err = e
            sys.stderr.write(f"Push attempt {attempt} failed: {e}\n")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    sys.stderr.write(f"Giving up after {MAX_RETRIES} attempts: {last_err}\n")
    return None


def main():
    args = set(sys.argv[1:])
    once = "--once" in args
    dry_run = "--dry-run" in args

    config = load_config()
    opener = build_opener(config["verify_tls"])

    if not config["verify_tls"]:
        sys.stderr.write(
            "Warning: TLS verification is DISABLED (STACKSENSE_VERIFY_TLS=false). "
            "Use only with a trusted self-signed setup.\n"
        )

    print(f"StackSense agent {AGENT_VERSION} -> {config['url']} every {config['interval']}s")

    services_interval = int(os.environ.get("STACKSENSE_SERVICES_INTERVAL", 60))
    last_services_push = 0  # force a services push on the first loop
    last_ipc = 0           # refresh IPC stats on the same (cheap) ~60s cadence
    ipc_cache = None
    ssh_state = {}  # incremental SSH auth-log tail state

    prev = None
    while True:
        try:
            metrics, prev = collect_metrics(prev)
        except Exception as e:
            sys.stderr.write(f"Metric collection error: {e}\n")
            metrics = None

        # Refresh SysV IPC / shared-memory summary every ~60s and attach the latest
        # snapshot to each metrics push (so it lands on every SystemMetric row).
        if time.monotonic() - last_ipc >= services_interval:
            ipc_cache = collect_ipc_stats()
            last_ipc = time.monotonic()
        if metrics is not None and ipc_cache is not None:
            metrics["ipc_stats"] = ipc_cache

        if dry_run:
            print(json.dumps({"metrics": metrics, "services": collect_services(), "containers": collect_containers()}, indent=2, default=str))
            return

        if metrics is not None:
            result = push(config, opener, "/api/agent/metrics/", metrics)
            if result and result.get("status") == "ok":
                stored = result.get("stored")
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] pushed (stored={stored})")
            else:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] push not accepted")

        # Push new SSH auth events incrementally (only when there are any)
        try:
            ssh_events = collect_ssh_auth(ssh_state)
            if ssh_events:
                push(config, opener, "/api/agent/ssh-auth/", {"events": ssh_events, "agent_version": AGENT_VERSION})
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ssh-auth pushed ({len(ssh_events)})")
        except Exception as e:
            sys.stderr.write(f"SSH auth push error: {e}\n")

        # Push the detected services + containers periodically (they change rarely)
        if time.monotonic() - last_services_push >= services_interval:
            try:
                services = collect_services()
                res = push(config, opener, "/api/agent/services/", {"services": services, "agent_version": AGENT_VERSION})
                if res and res.get("status") == "ok":
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] services pushed ({len(services)})")
            except Exception as e:
                sys.stderr.write(f"Service push error: {e}\n")
            try:
                containers = collect_containers()
                res = push(config, opener, "/api/agent/containers/", {"containers": containers, "agent_version": AGENT_VERSION})
                if res and res.get("status") == "ok":
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] containers pushed ({len(containers)})")
            except Exception as e:
                sys.stderr.write(f"Container push error: {e}\n")
            last_services_push = time.monotonic()

        if once:
            return
        time.sleep(config["interval"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("\nStopped.\n")
