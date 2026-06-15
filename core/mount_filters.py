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
