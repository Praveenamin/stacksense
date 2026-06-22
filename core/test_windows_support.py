"""Windows-scope foundations (Phase 1): server OS awareness + gating Linux-only detectors.

These run entirely on the Linux/Django server. They prove:
  - the agent-reported os_type/os_version persists (write-on-change, backward-compatible),
  - the Linux-only detectors (SysV-IPC leaks, SSH brute-force) are skipped for non-Linux
    servers and unchanged for Linux (the default),
  - Windows drive letters are not treated as ephemeral mounts.
Existing servers default to os_type="linux", so nothing here changes Linux behavior.
"""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (Server, AgentCredential, SystemMetric, SSHAuthEvent,
                         SecurityEvent, SecurityMonitorConfig)
from core.mount_filters import is_ephemeral_mount
from core.utils.leak_detection import detect_leaks
from core.security_monitor import detect_ssh_brute_force

User = get_user_model()

VALID = {
    "cpu_percent": 12.5, "memory_total": 8_000_000_000, "memory_available": 4_000_000_000,
    "memory_percent": 50.0, "memory_used": 4_000_000_000,
}


class OSPersistenceTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="w1", ip_address="10.0.0.9", username="agent")
        _, self.token = AgentCredential.generate_for_server(self.server)
        self.client = Client()
        self.url = reverse("agent_ingest_metrics")

    def _push(self, **extra):
        return self.client.post(self.url, data=json.dumps(dict(VALID, **extra)),
                                content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_default_is_linux(self):
        self.assertEqual(self.server.os_type, "linux")

    def test_windows_os_persisted(self):
        r = self._push(os_type="windows", os_version="Windows-10-10.0.22631-SP0")
        self.assertEqual(r.status_code, 200)
        self.server.refresh_from_db()
        self.assertEqual(self.server.os_type, "windows")
        self.assertEqual(self.server.os_version, "Windows-10-10.0.22631-SP0")

    def test_old_agent_without_os_keeps_default(self):
        # push-1.6.0 and earlier send no os_type -> server stays linux (backward-compatible).
        self._push()
        self.server.refresh_from_db()
        self.assertEqual(self.server.os_type, "linux")

    def test_bogus_os_type_ignored(self):
        self._push(os_type="haiku")
        self.server.refresh_from_db()
        self.assertEqual(self.server.os_type, "linux")

    def test_missing_os_does_not_reset_previous(self):
        self._push(os_type="windows", os_version="Win11")
        self._push()                                   # later push omits os_type
        self.server.refresh_from_db()
        self.assertEqual(self.server.os_type, "windows")   # not reset to linux
        self.assertEqual(self.server.os_version, "Win11")


class _LeakBase(TestCase):
    def _server(self, os_type):
        return Server.objects.create(name=f"s-{os_type}", ip_address="10.0.0.1", username="a",
                                     os_type=os_type)

    def _seed_orphaned_shm(self, server, n=14):
        now = timezone.now()
        ipc = {"shm_orphaned_bytes": 1_000_000_000, "shm_orphaned": 3}   # huge -> triggers shm_leak
        for i in range(n):
            SystemMetric.objects.create(
                server=server, timestamp=now - timedelta(minutes=(n - i)),
                cpu_percent=5.0, memory_total=8_000_000_000, memory_available=6_000_000_000,
                memory_percent=25.0, memory_used=2_000_000_000, ipc_stats=ipc)


class IpcLeakGatingTests(_LeakBase):
    def test_linux_server_reports_shm_leak(self):
        srv = self._server("linux")
        self._seed_orphaned_shm(srv)
        names = [f["metric_name"] for f in detect_leaks(srv)]
        self.assertIn("shm_leak", names)

    def test_windows_server_skips_ipc_leak(self):
        srv = self._server("windows")
        self._seed_orphaned_shm(srv)               # identical data
        names = [f["metric_name"] for f in detect_leaks(srv)]
        self.assertNotIn("shm_leak", names)


class SshBruteForceGatingTests(TestCase):
    def setUp(self):
        self.cfg = SecurityMonitorConfig.get_config()
        self.now = timezone.now()

    def _seed_failures(self, server, ip="203.0.113.7", n=50):
        for _ in range(n):
            SSHAuthEvent.objects.create(server=server, source_ip=ip, username="root",
                                        success=False, raw="Failed password", timestamp=self.now)

    def test_linux_server_flags_brute_force(self):
        srv = Server.objects.create(name="lin", ip_address="10.0.0.2", username="a", os_type="linux")
        self._seed_failures(srv)
        detect_ssh_brute_force(self.cfg, self.now)
        self.assertTrue(SecurityEvent.objects.filter(
            server=srv, event_type=SecurityEvent.EventType.SSH_BRUTE_FORCE).exists())

    def test_windows_server_not_flagged(self):
        srv = Server.objects.create(name="win", ip_address="10.0.0.3", username="a", os_type="windows")
        self._seed_failures(srv)
        detect_ssh_brute_force(self.cfg, self.now)
        self.assertFalse(SecurityEvent.objects.filter(
            server=srv, event_type=SecurityEvent.EventType.SSH_BRUTE_FORCE).exists())


class WindowsServingAndCommandTests(TestCase):
    """Windows installer artifacts are served, and the onboarding command branches by OS."""
    def setUp(self):
        self.admin = User.objects.create_superuser("wadmin", "a@x.test", "pw")
        self.client = Client()

    def test_install_ps1_is_served_and_is_exe_based(self):
        r = self.client.get(reverse("agent_install_ps1"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("StackSenseAgent", body)              # scheduled-task name
        self.assertIn("stacksense-agent.exe", body)         # downloads the standalone exe
        # No Python on the host: the installer must NOT bootstrap pip / embeddable Python.
        self.assertNotIn("get-pip", body)
        self.assertNotIn("embed-amd64", body)

    def test_agent_exe_redirects_to_release_when_no_local_file(self):
        # No local agent/stacksense-agent.exe in the repo -> redirect to the published
        # GitHub Release asset so the installer works with nothing placed on the server.
        r = self.client.get(reverse("agent_exe"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("stacksense-agent.exe", r["Location"])
        self.assertIn("releases", r["Location"])

    def test_windows_picker_creates_windows_server_with_powershell_command(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("add_server_agent"),
                             {"name": "win-1", "ip_address": "10.0.0.50", "os_type": "windows"})
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("install.ps1", body)
        self.assertIn("powershell", body)
        self.assertEqual(Server.objects.get(name="win-1").os_type, "windows")

    def test_linux_picker_keeps_bash_command(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("add_server_agent"),
                             {"name": "lin-1", "ip_address": "10.0.0.51", "os_type": "linux"})
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("install.sh", body)
        self.assertIn("sudo bash", body)
        self.assertEqual(Server.objects.get(name="lin-1").os_type, "linux")


class WindowsMountFilterTests(TestCase):
    def test_windows_drives_are_not_ephemeral(self):
        for drive in ("C:\\", "D:\\", "C:\\Windows\\Temp", "E:"):
            self.assertFalse(is_ephemeral_mount(drive), f"{drive!r} should be monitored")

    def test_linux_ephemeral_still_excluded(self):
        self.assertTrue(is_ephemeral_mount("/tmp"))
        self.assertFalse(is_ephemeral_mount("/"))
