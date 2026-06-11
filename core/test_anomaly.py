"""
Incident-grade anomaly detector tests: a baseline deviation only fires when it is
large, genuinely high, AND sustained -- so idle-server spikes (CPU ~1% -> 25%) are
ignored, while sustained high load and hard-ceiling breaches still fire.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import Server, MonitoringConfig, SystemMetric, Anomaly
from core.anomaly_detector import AnomalyDetector


class IncidentGradeAnomalyTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="t-vm", ip_address="10.0.0.9", username="agent")
        self.config = MonitoringConfig.objects.create(
            server=self.server, enabled=True, cpu_threshold=80,
            memory_threshold=90, disk_threshold=90, anomaly_sensitivity="BALANCED")
        self.detector = AnomalyDetector(self.server, self.config)
        self.t0 = timezone.now() - timedelta(hours=1)

    def _metric(self, cpu, secs):
        return SystemMetric.objects.create(
            server=self.server, timestamp=self.t0 + timedelta(seconds=secs),
            cpu_percent=cpu, memory_total=8_000_000_000, memory_available=4_000_000_000,
            memory_used=4_000_000_000, memory_percent=20.0)

    def _history(self, values):
        """Create metrics for `values` (oldest->newest); return the latest as 'current'."""
        m = None
        for i, v in enumerate(values):
            m = self._metric(v, i * 30)
        return m

    def _cpu_anoms(self, metric):
        return [a for a in self.detector.detect_anomalies(metric) if a["metric_type"] == "cpu"]

    def test_idle_brief_spike_suppressed(self):
        # Idle baseline ~10%, one blip to 25% -> not an incident.
        m = self._history([9, 11] * 14 + [25])
        self.assertEqual(self._cpu_anoms(m), [])

    def test_brief_high_blip_not_sustained_suppressed(self):
        # A single high (55%) sample after an idle baseline -> transient, suppressed.
        m = self._history([9, 11] * 14 + [55])
        self.assertEqual(self._cpu_anoms(m), [])

    def test_sustained_high_fires_medium(self):
        # Idle baseline, then 3 sustained samples at 55% -> one MEDIUM anomaly.
        m = self._history([9, 11] * 13 + [55, 55, 55])
        anoms = self._cpu_anoms(m)
        self.assertEqual(len(anoms), 1)
        self.assertEqual(anoms[0]["severity"], Anomaly.Severity.MEDIUM)

    def test_ceiling_breach_fires_immediately(self):
        # A single sample over the hard ceiling fires regardless of history/sustain.
        m = self._metric(95, 0)
        anoms = self._cpu_anoms(m)
        self.assertEqual(len(anoms), 1)
        self.assertIn("alert limit", anoms[0]["explanation"])
