"""Ephemeral-mount exclusion for disk alerting.

The agent on a box where /tmp and /var/tmp are bind mounts of / re-counts the root
filesystem three times, so a transient root-fill fires three identical disk
anomalies (/, /tmp, /var/tmp). We exclude ephemeral / pseudo / bind-duplicate
mounts from disk alerting -- but NEVER the root filesystem or real data partitions.

Closed-set enumeration: every excluded prefix (and a path beneath it) is excluded;
a fixed roster of real partitions is kept. Plus a check that the live anomaly
chokepoint (_disk_percents) actually drops them.
"""
from types import SimpleNamespace

from django.test import TestCase

from core.mount_filters import is_ephemeral_mount, EPHEMERAL_MOUNT_PREFIXES
from core.anomaly_detector import AnomalyDetector

# Mounts that must NOT raise disk alerts (ephemeral / pseudo / bind-dup of /).
EXCLUDED = [
    "/tmp", "/tmp/", "tmp", "/tmp/sub",
    "/var/tmp", "/var/tmp/x",
    "/dev/shm", "/dev",
    "/run", "/run/user/0", "/run/user/1000", "/run/lock",
    "/var/run", "/var/lock",
    "/snap", "/snap/core/12345",
    "/boot/efi",
    "/proc", "/sys",
]

# Real partitions an operator genuinely wants watched -- must be KEPT.
KEPT = [
    "/", "",                       # root is always real; empty normalizes to root
    "/var", "/var/log", "/var/lib",
    "/home", "/data", "/opt", "/srv", "/usr",
    "/boot",                       # /boot is real; only /boot/efi is excluded
    "/development",                # must not be caught by the "/dev" prefix
    "/mnt/data", "/data1",
]


class IsEphemeralMountTests(TestCase):
    def test_excluded_mounts_are_ephemeral(self):
        for m in EXCLUDED:
            self.assertTrue(is_ephemeral_mount(m), f"{m!r} should be excluded")

    def test_real_partitions_are_kept(self):
        for m in KEPT:
            self.assertFalse(is_ephemeral_mount(m), f"{m!r} should be kept")

    def test_root_is_never_excluded(self):
        self.assertFalse(is_ephemeral_mount("/"))
        self.assertFalse(is_ephemeral_mount(""))
        self.assertFalse(is_ephemeral_mount(None))

    def test_every_prefix_itself_is_excluded(self):
        # Closed set: each declared prefix (and a child of it) is excluded.
        for p in EPHEMERAL_MOUNT_PREFIXES:
            self.assertTrue(is_ephemeral_mount(p), f"prefix {p!r} not excluded")
            self.assertTrue(is_ephemeral_mount(p + "/child"), f"{p}/child not excluded")


class DiskPercentsExclusionTests(TestCase):
    """The live anomaly chokepoint must drop ephemeral mounts but keep real ones."""

    def _detector(self):
        # __init__ only stashes server/config; no DB needed for _disk_percents.
        return AnomalyDetector(server=SimpleNamespace(name="t"), config=SimpleNamespace())

    def test_disk_percents_drops_ephemeral_keeps_real(self):
        det = self._detector()
        metric = SimpleNamespace(disk_usage={
            "/":         {"percent": 99.9},   # transient root fill -- real, kept
            "/tmp":      {"percent": 99.9},   # bind-dup of / -- dropped
            "/var/tmp":  {"percent": 99.9},   # bind-dup of / -- dropped
            "/run":      {"percent": 80.0},   # tmpfs -- dropped
            "/home":     {"percent": 42.0},   # real data partition -- kept
        })
        out = det._disk_percents(metric)
        self.assertEqual(set(out), {"/", "/home"})
        self.assertEqual(out["/"], 99.9)
        self.assertEqual(out["/home"], 42.0)
