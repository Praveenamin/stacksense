"""
Phase 6 (security detection) -- SSH brute-force -> SecurityEvent.

The agent ships SSH auth-log events (Phase 4 ingestion); detect_ssh_brute_force
correlates recent FAILED SSHAuthEvents per (server, source IP) within the look-back
window and raises a SSH_BRUTE_FORCE SecurityEvent once the failed-attempt threshold is
reached. _upsert_event dedups: the same IP attacking the same server updates one open
event; the same IP on two servers yields two. The detection brain is what these tests
exercise (the delivery/routing is covered in Phase 2).
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import Server, SSHAuthEvent, SecurityEvent, SecurityMonitorConfig
from core.security_monitor import detect_ssh_brute_force, detect_security_events


class SshBruteForceDetectionTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="vm-a", ip_address="10.2.2.1", username="agent")
        self.cfg = SecurityMonitorConfig.get_config()
        self.cfg.enabled = True
        self.cfg.window_minutes = 10
        self.cfg.brute_force_ip_threshold = 5
        self.cfg.save()
        self.now = timezone.now()

    def _ssh(self, ip, n=1, server=None, success=False, ago_minutes=1):
        server = server or self.server
        SSHAuthEvent.objects.bulk_create([
            SSHAuthEvent(server=server, timestamp=self.now - timedelta(minutes=ago_minutes),
                         source_ip=ip, username="root", success=success, raw="x")
            for _ in range(n)])

    def _detect(self):
        return detect_ssh_brute_force(self.cfg, timezone.now())

    # --- threshold -----------------------------------------------------------------
    def test_below_threshold_raises_nothing(self):
        self._ssh("1.2.3.4", n=4)                       # threshold is 5
        self.assertEqual(self._detect(), [])
        self.assertEqual(SecurityEvent.objects.count(), 0)

    def test_at_threshold_raises_one_high_event(self):
        self._ssh("1.2.3.4", n=5)
        events = self._detect()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.event_type, SecurityEvent.EventType.SSH_BRUTE_FORCE)
        self.assertEqual(ev.severity, SecurityEvent.Severity.HIGH)
        self.assertEqual(ev.source_ip, "1.2.3.4")
        self.assertEqual(ev.server_id, self.server.id)
        self.assertEqual(ev.event_count, 5)

    def test_threshold_value_is_honored(self):
        self.cfg.brute_force_ip_threshold = 20
        self.cfg.save()
        self._ssh("1.2.3.4", n=10)                       # below the new threshold
        self.assertEqual(self._detect(), [])

    # --- only failures, only in-window ---------------------------------------------
    def test_successful_logins_never_count(self):
        self._ssh("1.2.3.4", n=20, success=True)
        self.assertEqual(self._detect(), [])

    def test_only_failures_count_when_mixed(self):
        self._ssh("1.2.3.4", n=4, success=False)
        self._ssh("1.2.3.4", n=20, success=True)
        self.assertEqual(self._detect(), [])            # 4 fails < 5

    def test_attempts_outside_the_window_are_ignored(self):
        self._ssh("1.2.3.4", n=10, ago_minutes=60)      # window is 10 minutes
        self.assertEqual(self._detect(), [])

    # --- noise filtering -----------------------------------------------------------
    def test_ignored_ip_excluded(self):
        self._ssh("0.0.0.0", n=10)                       # 0.0.0.0 is in _IGNORED_IPS
        self.assertEqual(self._detect(), [])

    def test_empty_source_ip_excluded(self):
        self._ssh("", n=10)
        self.assertEqual(self._detect(), [])

    # --- grouping ------------------------------------------------------------------
    def test_same_ip_on_two_servers_yields_two_events(self):
        server_b = Server.objects.create(name="vm-b", ip_address="10.2.2.2", username="agent")
        self._ssh("1.2.3.4", n=5, server=self.server)
        self._ssh("1.2.3.4", n=5, server=server_b)
        events = self._detect()
        self.assertEqual(len(events), 2)
        self.assertEqual({e.server_id for e in events}, {self.server.id, server_b.id})

    def test_different_ips_on_one_server_yield_separate_events(self):
        self._ssh("1.2.3.4", n=5)
        self._ssh("5.6.7.8", n=5)
        events = self._detect()
        self.assertEqual({e.source_ip for e in events}, {"1.2.3.4", "5.6.7.8"})

    # --- dedup ---------------------------------------------------------------------
    def test_rerun_updates_the_open_event_not_a_duplicate(self):
        self._ssh("1.2.3.4", n=5)
        self.assertEqual(len(self._detect()), 1)
        self._ssh("1.2.3.4", n=3)                        # more attempts, same IP+server
        self.assertEqual(self._detect(), [])            # NO new event
        evs = SecurityEvent.objects.filter(event_type=SecurityEvent.EventType.SSH_BRUTE_FORCE)
        self.assertEqual(evs.count(), 1)
        self.assertEqual(evs.first().event_count, 8)    # updated count

    def test_resolved_event_does_not_suppress_a_fresh_attack(self):
        self._ssh("1.2.3.4", n=5)
        ev = self._detect()[0]
        ev.status = SecurityEvent.Status.RESOLVED
        ev.save(update_fields=["status"])
        fresh = self._detect()                          # dedup only matches OPEN events
        self.assertEqual(len(fresh), 1)
        self.assertEqual(SecurityEvent.objects.count(), 2)


class DetectionGateTests(TestCase):
    """The top-level detect_security_events() honors the enabled flag."""

    def setUp(self):
        self.server = Server.objects.create(name="vm-a", ip_address="10.2.2.3", username="agent")
        self.cfg = SecurityMonitorConfig.get_config()
        self.cfg.brute_force_ip_threshold = 5
        self.cfg.alert_enabled = False                  # don't fan out to email/Slack in tests
        self.cfg.save()
        SSHAuthEvent.objects.bulk_create([
            SSHAuthEvent(server=self.server, timestamp=timezone.now(), source_ip="9.9.9.9",
                         username="root", success=False, raw="x") for _ in range(6)])

    def test_enabled_runs_ssh_detection(self):
        self.cfg.enabled = True
        self.cfg.save()
        events = detect_security_events()
        self.assertTrue(any(e.event_type == SecurityEvent.EventType.SSH_BRUTE_FORCE
                            for e in events))

    def test_disabled_skips_detection_entirely(self):
        self.cfg.enabled = False
        self.cfg.save()
        self.assertEqual(detect_security_events(), [])
        self.assertEqual(SecurityEvent.objects.count(), 0)
