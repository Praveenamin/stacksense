"""A CPU/memory anomaly should name the heaviest process at that exact sample.

Mirrors the leak detector (which names the culprit process). The agent already ships
per-sample top_processes; the detector now appends "Top process at that time: ..." to
the CPU and memory anomaly explanations.
"""
from django.test import TestCase
from django.utils import timezone

from core.models import Server, SystemMetric, MonitoringConfig
from core.anomaly_detector import AnomalyDetector


class TopProcessAttributionTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="s1", ip_address="10.0.0.1", username="agent")
        self.config = MonitoringConfig.objects.create(
            server=self.server, cpu_threshold=80.0, memory_threshold=90.0,
            disk_threshold=90.0, network_io_threshold=0.0, monitored_disks=[])
        self.det = AnomalyDetector(self.server, self.config)

    def _metric(self, cpu=99.0, mem=30.0, top=None):
        return SystemMetric.objects.create(
            server=self.server, timestamp=timezone.now(), cpu_percent=cpu,
            memory_total=8_000_000_000, memory_available=4_000_000_000,
            memory_percent=mem, memory_used=4_000_000_000,
            top_processes=top if top is not None else {})

    def test_cpu_anomaly_names_top_cpu_process(self):
        m = self._metric(cpu=99.0, top={
            "cpu": [{"pid": 1234, "name": "stress-ng", "cpu_percent": 97.0},
                    {"pid": 5, "name": "idle", "cpu_percent": 1.0}],
            "memory": [{"pid": 9, "name": "redis", "memory_percent": 12.0}],
        })
        cpu = [a for a in self.det.detect_anomalies(m) if a["metric_type"] == "cpu"]
        self.assertEqual(len(cpu), 1)
        self.assertIn("Top process at that time: stress-ng (pid 1234) at 97% CPU.",
                      cpu[0]["explanation"])

    def test_memory_anomaly_names_top_memory_process(self):
        m = self._metric(cpu=10.0, mem=99.0, top={
            "cpu": [{"pid": 1, "name": "x", "cpu_percent": 2.0}],
            "memory": [{"pid": 4321, "name": "java", "memory_percent": 71.0}],
        })
        mem = [a for a in self.det.detect_anomalies(m) if a["metric_type"] == "memory"]
        self.assertEqual(len(mem), 1)
        self.assertIn("Top process at that time: java (pid 4321) at 71% memory.",
                      mem[0]["explanation"])

    def test_cpu_anomaly_still_fires_without_process_data(self):
        # No top_processes captured (e.g. older agent) -> anomaly fires, no suffix, no error.
        m = self._metric(cpu=99.0, top={})
        cpu = [a for a in self.det.detect_anomalies(m) if a["metric_type"] == "cpu"]
        self.assertEqual(len(cpu), 1)
        self.assertNotIn("Top process at that time", cpu[0]["explanation"])

    def test_suffix_handles_json_string_top_processes(self):
        # top_processes can arrive as a JSON string; the suffix must still parse it.
        import json
        m = self._metric(cpu=99.0, top=json.dumps({
            "cpu": [{"pid": 7, "name": "ffmpeg", "cpu_percent": 88.0}]}))
        cpu = [a for a in self.det.detect_anomalies(m) if a["metric_type"] == "cpu"]
        self.assertIn("ffmpeg (pid 7) at 88% CPU.", cpu[0]["explanation"])
