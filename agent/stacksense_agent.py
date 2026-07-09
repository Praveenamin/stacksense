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

import concurrent.futures
import json
import os
import platform
import re
import shutil
import socket
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

AGENT_VERSION = "push-1.10.0"

# The agent is cross-platform (Linux + Windows). Core metrics come from psutil, which
# is OS-portable; a few collectors are Linux-only (systemd services, SSH auth log, SysV
# IPC, lsblk disk hardware) and are guarded by these flags. On Windows those are skipped
# and we rely on psutil + listening-port/banner detection.
_PLATFORM = platform.system()           # 'Linux' | 'Windows' | 'Darwin'
_IS_WINDOWS = _PLATFORM == "Windows"
_IS_LINUX = _PLATFORM == "Linux"


def _os_info():
    """Platform identity reported to the server so it knows which OS-specific detectors
    to run. Persisted server-side (write-on-change) as Server.os_type / os_version."""
    return {
        "os_type": "windows" if _IS_WINDOWS else ("linux" if _IS_LINUX else "other"),
        "os_version": (platform.platform() or "")[:200],
        "hostname": (platform.node() or "")[:200],
    }
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

# Mount paths we never report as disks: ephemeral runtime dirs and bind mounts of /
# re-exposed under another name (/tmp, /var/tmp on many images). They are not
# actionable capacity incidents and only re-count the root filesystem. The root
# filesystem ("/") is always reported. Mirrored server-side in core/mount_filters.py.
EPHEMERAL_MOUNT_PREFIXES = (
    "/tmp", "/var/tmp", "/dev/shm", "/dev", "/run", "/var/run",
    "/var/lock", "/snap", "/boot/efi", "/proc", "/sys",
)


def _is_ephemeral_mount(mountpoint):
    if _IS_WINDOWS:
        return False                       # Windows drives (C:\, D:\) are all real volumes
    m = "/" + str(mountpoint or "").strip().strip("/")
    if m == "/":
        return False
    return any(m == p or m.startswith(p + "/") for p in EPHEMERAL_MOUNT_PREFIXES)


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


# ---------------------------------------------------------------------------
# Service identification via privilege-free loopback banner grab
# ---------------------------------------------------------------------------
# A listening port tells you the protocol/role, not the product. To tell nginx
# from Apache from LiteSpeed we connect to 127.0.0.1:<port> and read the service's
# own banner (HTTP Server header, SSH/SMTP/IMAP greeting, MySQL handshake). This
# needs no root/sudo/shell change -- loopback TCP is allowed under the agent's
# hardened systemd unit. Best-effort: any failure -> None (fall back to the role).
PROBE_TIMEOUT = 1.0  # seconds; keep tight so a slow port never stalls the cycle

# Mirror of core/port_roles.PORT_ROLES (the agent can't import Django). Keep in sync.
_PORT_ROLES = {
    80: "HTTP", 443: "HTTPS", 81: "HTTP-Alt", 8080: "HTTP-Alt", 8443: "HTTPS-Alt",
    2082: "cPanel", 2083: "cPanel (SSL)", 2086: "WHM", 2087: "WHM (SSL)",
    2095: "Webmail", 2096: "Webmail (SSL)",
    2077: "cpdavd", 2078: "cpdavd (SSL)", 2079: "cpdavd", 2080: "cpdavd (SSL)",
    25: "SMTP", 465: "SMTP (SSL)", 587: "SMTP (submission)",
    110: "POP3", 995: "POP3 (SSL)", 143: "IMAP", 993: "IMAP (SSL)", 4190: "Sieve",
    53: "DNS", 953: "DNS (rndc)", 21: "FTP", 22: "SSH",
    3306: "MySQL", 5432: "PostgreSQL", 6379: "Redis", 27017: "MongoDB",
}

# Which probe to run per port.
_HTTP_PORTS = {80, 81, 8080, 2082, 2086, 2095, 2077, 2079}
_HTTPS_PORTS = {443, 8443, 2083, 2087, 2096, 2078, 2080}
_SMTP_PLAIN = {25, 587}
_SMTP_TLS = {465}
_IMAP_POP_PLAIN = {143, 110}
_IMAP_POP_TLS = {993, 995}


def _read_socket(port, tls=False, send=None, read_bytes=4096):
    """Open a short loopback connection (optionally TLS), optionally send bytes, and
    return up to read_bytes of the response as text. Best-effort -> None on any error."""
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT)
        sock.settimeout(PROBE_TIMEOUT)
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname="localhost")
        if send:
            sock.sendall(send)
        return sock.recv(read_bytes).decode("latin-1", "replace")
    except Exception:
        return None
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def _probe_http(port, tls=False):
    """Return the HTTP `Server:` header value, or None."""
    resp = _read_socket(port, tls, send=b"HEAD / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    if not resp:
        return None
    for line in resp.split("\r\n"):
        if line.lower().startswith("server:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _probe_line(port, tls=False):
    """Return the first response line of a banner-first protocol (SSH/SMTP/IMAP/POP/FTP)."""
    resp = _read_socket(port, tls, send=None)
    if not resp:
        return None
    return resp.replace("\r", "").split("\n", 1)[0].strip() or None


def _probe_mysql(port):
    """Return the MySQL/MariaDB version string from the initial handshake, or None."""
    resp = _read_socket(port, tls=False, send=None, read_bytes=128)
    if not resp or len(resp) < 6:
        return None
    body = resp[5:]              # [3B length][1B seq][1B protocol=10][version NUL-terminated]
    end = body.find("\x00")
    return body[:end].strip() if end > 0 else None


def _identify_server_header(value):
    """Map an HTTP Server header to a canonical web-server product, or None."""
    if not value:
        return None
    v = value.lower()
    if "nginx" in v:
        return "nginx"
    if "litespeed" in v or "openlitespeed" in v or "lsws" in v:
        return "LiteSpeed"
    if "apache" in v:
        return "Apache"
    if "cpsrvd" in v:
        return "cpsrvd"
    if "openresty" in v:
        return "OpenResty"
    if "caddy" in v:
        return "Caddy"
    if "microsoft-iis" in v or v.startswith("iis"):
        return "IIS"
    return value.split("/")[0].strip() or None   # else report what it actually said


def _identify_banner(value):
    """Map a protocol greeting line to a canonical product, or None."""
    if not value:
        return None
    v = value.lower()
    if "openssh" in v:
        return "OpenSSH"
    if v.startswith("ssh-"):
        return "SSH"
    if "exim" in v:
        return "Exim"
    if "postfix" in v:
        return "Postfix"
    if "dovecot" in v:
        return "Dovecot"
    if "proftpd" in v:
        return "ProFTPD"
    if "pure-ftpd" in v:
        return "Pure-FTPd"
    if "vsftpd" in v:
        return "vsftpd"
    if "courier" in v:
        return "Courier"
    return None


def _identify_port(port):
    """Best-effort privilege-free product ID for a loopback listening port, or None."""
    if port in _HTTP_PORTS:
        return _identify_server_header(_probe_http(port, tls=False))
    if port in _HTTPS_PORTS:
        return _identify_server_header(_probe_http(port, tls=True))
    if port == 22:
        return _identify_banner(_probe_line(port))
    if port in _SMTP_PLAIN:
        return _identify_banner(_probe_line(port))
    if port in _SMTP_TLS:
        return _identify_banner(_probe_line(port, tls=True))
    if port in _IMAP_POP_PLAIN:
        return _identify_banner(_probe_line(port))
    if port in _IMAP_POP_TLS:
        return _identify_banner(_probe_line(port, tls=True))
    if port == 21:
        return _identify_banner(_probe_line(port))
    if port == 3306:
        ver = _probe_mysql(port)
        if ver:
            return "MariaDB" if "mariadb" in ver.lower() else "MySQL"
    return None


def _name_for_port(port, product, pname=""):
    """Return (display_name, detected_via) for a listening port.

    Precedence: verified banner product > the real process name (when psutil could
    see it) > well-known-port role > raw port. `name` (the upsert identity) is set
    separately and stays stable; this is only the cosmetic label.
    """
    if product:
        return f"{product} (:{port})", "port-banner"
    if pname:
        return f"{pname} (:{port})", "port-process"
    role = _PORT_ROLES.get(port)
    if role:
        return f"{role} (:{port})", "port-map"
    return f"port-{port}", "port-unknown"


# --- Per-service response time (agent-side TCP-connect latency) ------------------------
# We time a plain TCP connect to each ported service, locally on the box, so localhost-bound
# services (which the StackSense server can't reach in the outbound-only push model) are
# measurable. A completed handshake only -- no application request is ever sent, so there are
# no side effects on the monitored service. Mirrors core/service_latency.py, run agent-side.
_LATENCY_TIMEOUT = 1.5        # seconds per connect (short: bounds the whole batch)
_LATENCY_MAX_TARGETS = 50     # cap distinct (target, port) probes per push
_WILDCARD_BINDS = ("0.0.0.0", "::", "*", "")
_LOOPBACK_BINDS = ("::1", "localhost")


def _target_for_service(bind_address):
    """Where to connect to time a service: a concrete external bind IP as-is; a wildcard,
    empty, or loopback bind -> 127.0.0.1 (mirrors is_externally_accessible/is_localhost_bound
    in core/service_latency.py)."""
    addr = (bind_address or "").strip()
    if addr in _WILDCARD_BINDS or addr in _LOOPBACK_BINDS:
        return "127.0.0.1"
    return addr


def measure_tcp_latency(host, port, timeout=_LATENCY_TIMEOUT):
    """Time a TCP connect to host:port. Returns {latency_ms, success, error_message}.
    A completed handshake only -- nothing is sent. latency_ms is None on failure."""
    try:
        start = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return {"latency_ms": round((time.monotonic() - start) * 1000.0, 1),
                "success": True, "error_message": None}
    except Exception as e:
        return {"latency_ms": None, "success": False, "error_message": str(e)[:200]}


def _attach_service_latency(services):
    """Measure TCP-connect latency for each ported service and attach latency_ms/
    latency_success/latency_type/latency_error to its dict. Deduped by (target, port) and
    capped; each connect is bounded by _LATENCY_TIMEOUT so the whole batch stays short."""
    work = {}  # (target, port) -> [service dicts sharing it]
    for svc in services:
        port = svc.get("port")
        if not port:
            continue
        target = _target_for_service(svc.get("bind_address"))
        if not target:
            continue
        work.setdefault((target, int(port)), []).append(svc)
    if not work:
        return
    keys = list(work.keys())[:_LATENCY_MAX_TARGETS]
    results = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(keys))) as ex:
            fut_to_key = {ex.submit(measure_tcp_latency, t, p): (t, p) for (t, p) in keys}
            for fut, key in fut_to_key.items():
                results[key] = _safe(fut.result) or {}
    except Exception as e:
        print(f"[services] latency measurement skipped ({e!r})", file=sys.stderr)
        return
    for key, svcs in work.items():
        r = results.get(key)
        if not r:
            continue
        for svc in svcs:
            svc["latency_ms"] = r.get("latency_ms")
            svc["latency_success"] = r.get("success", False)
            svc["latency_type"] = "TCP"
            if r.get("error_message"):
                svc["latency_error"] = r["error_message"]


def _decode_proc_addr(hexaddr, is_v6):
    """Decode the hex local-address field from /proc/net/tcp{,6} to a printable IP.
    Addresses are stored in host byte order (little-endian on the platforms we run on)."""
    try:
        raw = bytes.fromhex(hexaddr)
        if is_v6:
            # 16 bytes stored as four little-endian 32-bit words.
            packed = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
            return socket.inet_ntop(socket.AF_INET6, packed)
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    except Exception:
        return ""


def _parse_proc_net_tcp_listeners(text, is_v6):
    """Parse /proc/net/tcp{,6} content -> [(port, bind_address), ...] for LISTEN sockets.
    State column 0A is TCP_LISTEN. These files are world-readable, so this works without
    root -- there is no PID/owner here, so callers name the service by port/banner."""
    out = []
    for line in text.splitlines()[1:]:   # skip the header row
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A":
            continue
        hexaddr, _, hexport = parts[1].partition(":")
        try:
            port = int(hexport, 16)
        except ValueError:
            continue
        if port:
            out.append((port, _decode_proc_addr(hexaddr, is_v6)))
    return out


def _listening_ports_from_proc():
    """Fallback listening-port discovery for hosts where psutil.net_connections is denied
    (unprivileged Linux): read /proc/net/tcp{,6} directly. Returns [(port, bind_address), ...]
    deduped by port (first occurrence kept)."""
    seen_ports = set()
    results = []
    for path, is_v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            with open(path, "r") as fh:
                text = fh.read()
        except OSError:
            continue
        for port, addr in _parse_proc_net_tcp_listeners(text, is_v6):
            if port in seen_ports:
                continue
            seen_ports.add(port)
            results.append((port, addr))
    return results


def collect_services():
    """Detect running services on this host.

    Primary source: systemd running units (works as a non-root user).
    Best-effort secondary: listening TCP/UDP ports, with a privilege-free banner
    grab to identify the real product (nginx vs Apache vs LiteSpeed, Exim, ...).
    Returns a list of dicts {name, status, service_type, port, bind_address,
    process_id, display_name, detected_via}.
    """
    services = []
    seen = set()

    # systemd running services (Linux only; Windows relies on listening-port detection
    # below -- Windows-service-name classification is a later phase).
    sysd_count = 0
    if _IS_LINUX:
        try:
            out = subprocess.run(
                ["systemctl", "list-units", "--type=service", "--state=running",
                 "--no-pager", "--no-legend", "--plain"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode != 0:
                print(f"[services] systemctl exited {out.returncode}: "
                      f"{(out.stderr or '').strip()[:200]}", file=sys.stderr)
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
                        sysd_count += 1
        except FileNotFoundError:
            print("[services] systemctl not found -- not a systemd host; "
                  "using listening-port detection only", file=sys.stderr)
        except Exception as e:
            print(f"[services] systemd detection failed ({e!r}); "
                  f"using listening-port detection only", file=sys.stderr)
        if sysd_count == 0:
            print("[services] no systemd units detected; relying on listening-port "
                  "detection (services will be named by port/banner)", file=sys.stderr)

    # Windows services (the analog of systemd units). psutil.win_service_iter() lists
    # the Service Control Manager's services by name (W3SVC=IIS, MSSQLSERVER, ...). We
    # report the running ones with service_type="windows"; the server classifies which
    # are notable vs background. Listening-port detection below still runs too.
    if _IS_WINDOWS and hasattr(psutil, "win_service_iter"):
        win_count = 0
        try:
            for svc in psutil.win_service_iter():
                info = _safe(lambda: svc.as_dict())
                if not info or info.get("status") != "running":
                    continue
                name = info.get("name")
                if not name or name in seen:
                    continue
                seen.add(name)
                services.append({
                    "name": name, "status": "running", "service_type": "windows",
                    "display_name": info.get("display_name") or name,
                    "process_id": str(info.get("pid") or ""),
                })
                win_count += 1
        except Exception as e:
            print(f"[services] Windows service enumeration failed ({e!r}); "
                  f"relying on listening-port detection", file=sys.stderr)
        if win_count == 0:
            print("[services] no Windows services enumerated; relying on listening-port "
                  "detection", file=sys.stderr)

    # Listening ports (best-effort; full visibility needs root). For each port we
    # attempt a 1s loopback banner grab to name the real product; on failure we
    # fall back to the well-known-port role, then the raw port. `name` (the upsert
    # identity) stays stable (process name when visible, else "port-<n>"); the
    # friendly label rides in display_name so it can change without churning rows.
    probed = {}  # port -> product (a port can appear on both IPv4 and IPv6)
    discovered_ports = set()   # ports psutil already surfaced -> don't double-add via fallback
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
            discovered_ports.add(port)
            if port not in probed:
                probed[port] = _safe(lambda: _identify_port(port))
            display_name, detected_via = _name_for_port(port, probed[port], pname)
            services.append({
                "name": name, "status": "running", "service_type": "port",
                "port": port, "bind_address": addr,
                "process_id": str(conn.pid) if conn.pid else "",
                "display_name": display_name, "detected_via": detected_via,
            })
    except Exception:
        pass

    # Non-root fallback: on unprivileged Linux, psutil.net_connections raises AccessDenied
    # and yields nothing, so listening services never get a port row -> no Response/SLO.
    # Recover the ports we missed straight from /proc/net/tcp{,6} (world-readable). There is
    # no owning PID there, so these are named by port/banner only.
    if _IS_LINUX:
        try:
            for port, addr in _listening_ports_from_proc():
                if port in discovered_ports:
                    continue
                name = f"port-{port}"
                key = ("port", name, port)
                if key in seen:
                    continue
                seen.add(key)
                discovered_ports.add(port)
                if port not in probed:
                    probed[port] = _safe(lambda: _identify_port(port))
                display_name, detected_via = _name_for_port(port, probed[port], "")
                services.append({
                    "name": name, "status": "running", "service_type": "port",
                    "port": port, "bind_address": addr, "process_id": "",
                    "display_name": display_name, "detected_via": detected_via,
                })
        except Exception as e:
            print(f"[services] /proc port fallback failed ({e!r})", file=sys.stderr)

    # Time each ported service (TCP connect) so the server can show per-service response
    # time + flag "slow". Best-effort: never let it break service collection.
    try:
        _attach_service_latency(services)
    except Exception as e:
        print(f"[services] latency measurement error ({e!r})", file=sys.stderr)

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


def _have(binary):
    """True if `binary` is on PATH."""
    return shutil.which(binary) is not None


def _run(cmd):
    """Run an argv list (no shell), return stdout or None on any failure."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return res.stdout if res.returncode == 0 else None
    except Exception:
        return None


def _docker_like(out, runtime):
    """Parse docker/nerdctl `ps --format {{json .}}` (one JSON object per line)."""
    rows = []
    for line in (out or "").splitlines():
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
        rows.append({
            "container_id": (c.get("ID") or "")[:64],
            "name": c.get("Names") or c.get("Name") or "",
            "image": c.get("Image") or "",
            "state": state,
            "status_text": status[:200],
            "ports": (c.get("Ports") or "")[:300],
            "runtime": runtime,
        })
    return rows


def _podman(out):
    """Parse `podman ps -a --format json` (a single JSON array)."""
    rows = []
    try:
        data = json.loads(out) if out else []
    except ValueError:
        return rows
    for c in data if isinstance(data, list) else []:
        names = c.get("Names") or c.get("Name") or []
        name = names[0] if isinstance(names, list) and names else (names if isinstance(names, str) else "")
        state = (c.get("State") or "").lower() or "running"
        ports = c.get("Ports") or ""
        if isinstance(ports, list):
            ports = ", ".join(str(p) for p in ports)
        rows.append({
            "container_id": (c.get("Id") or c.get("ID") or "")[:64],
            "name": name,
            "image": c.get("Image") or "",
            "state": state,
            "status_text": (c.get("Status") or "")[:200],
            "ports": str(ports)[:300],
            "runtime": "podman",
        })
    return rows


def _crictl(out):
    """Parse `crictl ps -a -o json` (CRI / containerd in Kubernetes)."""
    rows = []
    try:
        data = json.loads(out) if out else {}
    except ValueError:
        return rows
    state_map = {"CONTAINER_RUNNING": "running", "CONTAINER_EXITED": "exited",
                 "CONTAINER_CREATED": "created", "CONTAINER_UNKNOWN": "unknown"}
    for c in data.get("containers", []):
        meta = c.get("metadata") or {}
        img = c.get("image") or {}
        rows.append({
            "container_id": (c.get("id") or "")[:64],
            "name": meta.get("name") or "",
            "image": (img.get("image") if isinstance(img, dict) else img) or "",
            "state": state_map.get(c.get("state") or "", "unknown"),
            "status_text": (c.get("state") or "")[:200],
            "ports": "",
            "runtime": "containerd",
        })
    return rows


_SECRET_ENV_RE = re.compile(r"(pass|secret|token|key|cred|auth|pwd)", re.I)
INSPECT_CAP = 50  # max containers inspected per inspect cycle (keeps it light)


def _redact_env(env_list):
    """[`K=V`, ...] -> [{k, v}], redacting secret-looking values (at the source)."""
    out = []
    for e in env_list or []:
        if not isinstance(e, str) or "=" not in e:
            continue
        k, v = e.split("=", 1)
        if _SECRET_ENV_RE.search(k):
            v = "***redacted***"
        out.append({"k": k[:80], "v": v[:200]})
    return out[:60]


def _summ_docker_inspect(obj):
    """Compact, sanitized summary from a docker/podman/nerdctl `inspect` object."""
    cfg = obj.get("Config") or {}
    host = obj.get("HostConfig") or {}
    state = obj.get("State") or {}
    netset = obj.get("NetworkSettings") or {}
    mem = host.get("Memory") or 0
    nanocpus = host.get("NanoCpus") or 0
    ports = []
    for cport, binds in (netset.get("Ports") or {}).items():
        if binds:
            for b in binds:
                ports.append(f"{b.get('HostPort','')}->{cport}")
        else:
            ports.append(cport)
    mounts = []
    for mnt in obj.get("Mounts") or []:
        src = mnt.get("Source") or mnt.get("Name") or ""
        dst = mnt.get("Destination") or ""
        rw = "rw" if mnt.get("RW", True) else "ro"
        mounts.append(f"{src}:{dst} ({rw})")
    cmd = cfg.get("Cmd") or obj.get("Args") or []
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    return {
        "id": (obj.get("Id") or "")[:64],
        "image": cfg.get("Image") or obj.get("Image") or "",
        "created": obj.get("Created") or "",
        "command": str(cmd)[:300],
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no",
        "cpus": round(nanocpus / 1e9, 2) if nanocpus else None,
        "mem_limit": int(mem) if mem else None,
        "status": state.get("Status") or "",
        "health": (state.get("Health") or {}).get("Status") or "",
        "ports": ports[:30],
        "networks": list((netset.get("Networks") or {}).keys())[:20],
        "mounts": mounts[:30],
        "env": _redact_env(cfg.get("Env")),
    }


def _inspect_one(runtime, cid):
    """Run the runtime's read-only `inspect` and return a compact summary, or None."""
    if not cid:
        return None
    try:
        if runtime == "docker":
            out = _run(["docker", "inspect", cid])
        elif runtime == "podman":
            out = _run(["sudo", "-n", "podman", "inspect", cid])
        elif runtime == "containerd":
            out = _run(["sudo", "-n", "nerdctl", "inspect", cid]) or _run(["sudo", "-n", "crictl", "inspect", cid])
        else:
            return None
        if not out:
            return None
        data = json.loads(out)
        if isinstance(data, list) and data:                 # docker / podman / nerdctl
            return _summ_docker_inspect(data[0])
        if isinstance(data, dict):                          # crictl (CRI) shape -- best-effort
            st = data.get("status") or {}
            cfg = (data.get("info") or {}).get("config") or {}
            return {
                "id": (st.get("id") or cid)[:64],
                "image": ((st.get("image") or {}).get("image")) or "",
                "created": st.get("createdAt") or "",
                "command": "", "restart_policy": "", "cpus": None, "mem_limit": None,
                "status": (st.get("state") or "").replace("CONTAINER_", "").lower(),
                "health": "",
                "ports": [],
                "networks": [],
                "mounts": [f"{m.get('hostPath','')}:{m.get('containerPath','')}" for m in (st.get("mounts") or [])][:30],
                "env": _redact_env([f"{e.get('key')}={e.get('value')}" for e in (cfg.get("envs") or [])]),
            }
    except Exception:
        return None
    return None


def collect_containers(inspect=False):
    """Detect containers across runtimes (best-effort, read-only).

    Docker is read directly (agent in the 'docker' group). Podman / containerd
    (nerdctl, crictl) are read via `sudo -n <cmd> ps ...` -- the installer adds a
    scoped, read-only sudoers entry for exactly those list commands. Any runtime
    that's absent or not permitted simply contributes nothing.
    """
    rows = []
    if _have("docker"):
        rows += _docker_like(_run(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"]), "docker")
    if _have("podman"):
        rows += _podman(_run(["sudo", "-n", "podman", "ps", "-a", "--format", "json"]))
    if _have("nerdctl"):
        rows += _docker_like(_run(["sudo", "-n", "nerdctl", "ps", "-a", "--format", "{{json .}}"]), "containerd")
    if _have("crictl"):
        rows += _crictl(_run(["sudo", "-n", "crictl", "ps", "-a", "-o", "json"]))

    # De-dupe by (runtime, name); keep the first non-empty-named entry.
    seen, out = set(), []
    for r in rows:
        if not r.get("name"):
            continue
        key = (r["runtime"], r["name"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)

    # Optionally enrich with a read-only `inspect` summary (slow cadence; capped).
    if inspect:
        for item in out[:INSPECT_CAP]:
            data = _inspect_one(item["runtime"], item.get("container_id") or item["name"])
            if data:
                item["inspect"] = data
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


def _disk_kind(name, tran, rota):
    """Classify a physical disk as NVMe / SSD / HDD from its name, transport and
    rotational flag. NVMe wins by name/transport; rotational==1 => HDD; else SSD."""
    n = (name or "").lower()
    t = (tran or "").lower()
    if t == "nvme" or n.startswith("nvme"):
        return "NVMe"
    try:
        if int(rota) == 1:
            return "HDD"
    except (TypeError, ValueError):
        if str(rota).strip().lower() in ("1", "true"):
            return "HDD"
    return "SSD"


def collect_disk_hardware():
    """Read-only inventory of physical disks (SSD/HDD/NVMe), RAID arrays, and a
    mount -> disk map. Prefers `lsblk` (read-only); falls back to /sys/block.

    Returns (summary, mount_map):
      summary   = {physical_disk_count, disks[...], raid_arrays[...], raid}
      mount_map = {mountpoint: {disk_type, physical_disk, raid}}
    NEVER writes to the host -- inspection only.
    """
    disks, raid_arrays, mount_map = [], [], {}

    # lsblk + /sys/block + /proc/mdstat are Linux-only. On Windows we report a minimal
    # summary (disk_usage per drive is still fully populated by psutil in collect_metrics);
    # SSD/HDD/RAID annotation is simply omitted.
    if _IS_WINDOWS:
        n = _safe(lambda: len(psutil.disk_partitions(all=False)), 0) or 0
        return ({"physical_disk_count": n, "disks": [], "raid_arrays": [], "raid": "none"}, {})

    out = _run(["lsblk", "-J", "-b", "-o",
                "NAME,TYPE,ROTA,TRAN,MODEL,SIZE,MOUNTPOINT"]) if _have("lsblk") else None
    if out:
        try:
            tree = json.loads(out).get("blockdevices", []) or []
        except Exception:
            tree = []

        def visit(node, disk_anc, raid_lvl):
            ntype = (node.get("type") or "").lower()
            da, rl = disk_anc, raid_lvl
            if ntype == "disk":
                da = node
                disks.append({
                    "name": node.get("name"),
                    "type": _disk_kind(node.get("name"), node.get("tran"), node.get("rota")),
                    "model": (node.get("model") or "").strip() or None,
                    "size": node.get("size"),
                    "transport": node.get("tran") or None,
                    "rotational": node.get("rota"),
                })
            if ntype.startswith("raid"):
                rl = ntype
                raid_arrays.append({"name": node.get("name"), "level": ntype,
                                    "size": node.get("size")})
            mp = node.get("mountpoint")
            if mp and da:
                mount_map[mp] = {
                    "disk_type": _disk_kind(da.get("name"), da.get("tran"), da.get("rota")),
                    "physical_disk": da.get("name"),
                    "raid": rl or "none",
                }
            for ch in (node.get("children") or []):
                visit(ch, da, rl)

        for n in tree:
            visit(n, None, None)
    else:
        # Fallback: enumerate /sys/block (no reliable mount mapping without lsblk).
        try:
            for dev in sorted(os.listdir("/sys/block")):
                if dev.startswith(("loop", "ram", "dm-", "md", "sr", "fd", "zram")):
                    continue
                base = "/sys/block/" + dev
                rota = _safe(lambda: open(base + "/queue/rotational").read().strip())
                sectors = _safe(lambda: int(open(base + "/size").read().strip()))
                model = _safe(lambda: open(base + "/device/model").read().strip())
                is_nvme = dev.startswith("nvme")
                disks.append({
                    "name": dev,
                    "type": _disk_kind(dev, "nvme" if is_nvme else None, rota),
                    "model": (model or "").strip() or None,
                    "size": (sectors * 512) if sectors else None,
                    "transport": "nvme" if is_nvme else None,
                    "rotational": rota,
                })
        except Exception:
            pass
        mdstat = _safe(lambda: open("/proc/mdstat").read())
        if mdstat:
            for line in mdstat.splitlines():
                m = re.match(r"(md\d+)\s*:\s*\w+\s+(raid\d+)", line)
                if m:
                    raid_arrays.append({"name": m.group(1), "level": m.group(2), "size": None})

    summary = {
        "physical_disk_count": len(disks),
        "disks": disks,
        "raid_arrays": raid_arrays,
        "raid": raid_arrays[0]["level"] if raid_arrays else "none",
    }
    return summary, mount_map


def collect_metrics(prev):
    """Collect one metrics sample. `prev` carries counters from the last sample
    so we can compute per-second I/O rates. Returns (metrics_dict, new_prev)."""
    now = time.time()
    metrics = {"agent_version": AGENT_VERSION}
    metrics.update(_os_info())             # os_type / os_version / hostname

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
        if _is_ephemeral_mount(part.mountpoint):
            continue  # /tmp, /var/tmp, /run, ... -- ephemeral / bind-dup of /
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
    # Physical disk inventory (SSD/HDD/NVMe, RAID, disk count) + per-mount tags.
    disk_hw, mount_map = _safe(collect_disk_hardware, ({}, {})) or ({}, {})
    for mp, info in disk_usage.items():
        ann = mount_map.get(mp)
        if ann:
            info.update(ann)
    metrics["disk_usage"] = disk_usage
    metrics["disk_hardware"] = disk_hw

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
    inspect_interval = int(os.environ.get("STACKSENSE_INSPECT_INTERVAL", 300))  # deep `inspect` every ~5 min
    last_inspect = 0
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
        if _IS_LINUX and time.monotonic() - last_ipc >= services_interval:
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

        # Push new SSH auth events incrementally (Linux only; Windows auth lives in the
        # Event Log -- a later phase). Only when there are any.
        try:
            ssh_events = collect_ssh_auth(ssh_state) if _IS_LINUX else []
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
                do_inspect = time.monotonic() - last_inspect >= inspect_interval
                containers = collect_containers(inspect=do_inspect)
                if do_inspect:
                    last_inspect = time.monotonic()
                res = push(config, opener, "/api/agent/containers/", {"containers": containers, "agent_version": AGENT_VERSION})
                if res and res.get("status") == "ok":
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] containers pushed ({len(containers)}, inspect={do_inspect})")
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
