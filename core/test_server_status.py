"""Server online/offline threshold.

A healthy agent pushes ~every 30s, but one FAILED push cycle (3 retries) leaves no
heartbeat for ~55s. The old 60s offline threshold therefore flapped a running server
"offline" on a single transient blip. The threshold is now tolerant (default 180s):
a few missed pushes are absorbed; a real, sustained gap still goes offline.
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Server, ServerHeartbeat
from core.views import _calculate_server_status


class OfflineThresholdTests(TestCase):
    def setUp(self):
        # A recent app heartbeat => the app is NOT "down", so the normal (base) threshold
        # applies rather than the 600s app-restart grace.
        cache.set("monitoring_app_heartbeat", timezone.now().isoformat())
        self.server = Server.objects.create(name="s1", ip_address="10.0.0.1", username="agent")

    def _heartbeat(self, age_seconds):
        ServerHeartbeat.objects.update_or_create(
            server=self.server,
            defaults={"last_heartbeat": timezone.now() - timedelta(seconds=age_seconds)})

    def test_recent_heartbeat_is_online(self):
        self._heartbeat(20)
        self.assertEqual(_calculate_server_status(self.server), "online")

    def test_transient_gap_does_not_flip_offline(self):
        # ~90s without a push (one failed retry cycle) -- used to be offline at 60s.
        self._heartbeat(90)
        self.assertNotEqual(_calculate_server_status(self.server), "offline")

    def test_just_under_default_threshold_not_offline(self):
        self._heartbeat(170)
        self.assertNotEqual(_calculate_server_status(self.server), "offline")

    def test_sustained_gap_is_offline(self):
        self._heartbeat(300)                      # 5 min -> a real outage
        self.assertEqual(_calculate_server_status(self.server), "offline")

    def test_no_heartbeat_is_offline(self):
        self.assertEqual(_calculate_server_status(self.server), "offline")

    @override_settings(OFFLINE_THRESHOLD_SECONDS=90)
    def test_threshold_is_operator_tunable(self):
        self._heartbeat(120)                      # beyond the tuned 90s
        self.assertEqual(_calculate_server_status(self.server), "offline")
