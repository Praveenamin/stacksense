"""Mounts excluded from disk-capacity alerting.

These are ephemeral / pseudo / bind-duplicate mounts whose "fullness" is not an
actionable disk-capacity incident:
  - tmpfs-backed runtime dirs (/run, /dev/shm, /run/user/*) -- volatile, RAM-sized
  - bind mounts of / re-exposed under another name (/tmp, /var/tmp on many images,
    which re-count the root filesystem and so mirror its usage exactly)
  - kernel pseudo filesystems (/proc, /sys, /dev)
  - the EFI system partition (/boot/efi) -- tiny, only written by firmware updates

Real data partitions are still monitored: /, /var, /home, /data, /opt, /srv, ...
The root filesystem (/) is NEVER excluded.

This list is mirrored in agent/stacksense_agent.py (EPHEMERAL_MOUNTS) so the agent
can avoid sending these at the source; this server-side copy also protects servers
whose deployed agent predates that change.
"""

# A mount is excluded if it equals one of these prefixes or sits beneath it.
EPHEMERAL_MOUNT_PREFIXES = (
    "/tmp",
    "/var/tmp",
    "/dev/shm",
    "/dev",
    "/run",
    "/var/run",
    "/var/lock",
    "/snap",
    "/boot/efi",
    "/proc",
    "/sys",
)


def is_ephemeral_mount(mount):
    """True if `mount` is an ephemeral/pseudo mount we should not raise disk alerts for.

    The root filesystem ("/") always returns False -- it is real and monitored.
    """
    if not mount:
        return False
    # Normalize: "tmp" / "/tmp/" -> "/tmp"; "" / "/" -> "/".
    m = "/" + str(mount).strip().strip("/")
    if m == "/":
        return False
    for p in EPHEMERAL_MOUNT_PREFIXES:
        if m == p or m.startswith(p + "/"):
            return True
    return False


def primary_mount(disk_usage):
    """The mount that best represents "the disk" for a server: the Linux root "/", else the
    Windows system drive "C:\\", else any drive letter (D:\\, ...), else the first key.
    Returns None for empty/invalid input. Use this instead of hard-coding "/" so Windows
    servers (whose disk_usage is keyed by drive letter) aren't treated as having no disk."""
    if not isinstance(disk_usage, dict) or not disk_usage:
        return None
    for key in ("/", "C:\\", "C:"):
        if key in disk_usage:
            return key
    for k in disk_usage:
        if isinstance(k, str) and len(k) >= 2 and k[1] == ":":
            return k
    return next(iter(disk_usage), None)


def primary_disk_percent(disk_usage):
    """Disk-usage percent of the primary mount (0.0 if unavailable)."""
    m = primary_mount(disk_usage)
    v = disk_usage.get(m) if (m is not None and isinstance(disk_usage, dict)) else None
    if isinstance(v, dict):
        try:
            return float(v.get("percent", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return 0.0
