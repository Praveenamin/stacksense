"""Reliability SLIs — Phase 1: the calculators are built on REAL data (synthetic-probe
success/latency for availability/error/response-time; resource '% of samples under threshold'),
and never fabricate a value — they return None when there's no data.

Phase 2: the compliance job populates SLIMeasurement against the realigned SLO defaults, the
compliance API reports a correct compliant-server count, MTTR reuses the anomaly window logic,
and the reliability timeseries is real availability + check-failure (no alert×10 proxy)."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Server, SyntheticCheck, SyntheticCheckResult, SystemMetric,
    Anomaly, SLIMeasurement, SLOConfig,
)
from core import sli_utils

# SystemMetric non-null fields without defaults.
_MEM = dict(memory_total=8_000_000_000, memory_available=4_000_000_000, memory_used=4_000_000_000)


class SliCalculatorTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="rel-vm", ip_address="10.9.9.1", username="agent")
        self.start = timezone.now() - timedelta(hours=2)
        self.end = timezone.now() + timedelta(minutes=1)

    def _check(self):
        return SyntheticCheck.objects.create(name="c", check_type="HTTP",
                                             url="https://x.test", server=self.server)

    def _result(self, check, success, ms=None):
        return SyntheticCheckResult.objects.create(
            synthetic_check=check, success=success, response_time_ms=ms,
            timestamp=timezone.now() - timedelta(minutes=10))

    def _metric(self, cpu=0.0, mem=0.0, disk_percent=None):
        du = {"/": {"percent": disk_percent}} if disk_percent is not None else {}
        return SystemMetric.objects.create(
            server=self.server, cpu_percent=cpu, memory_percent=mem, disk_usage=du,
            timestamp=timezone.now() - timedelta(minutes=5), **_MEM)

    # --- synthetic-based SLIs (availability / check-failure / response time) ---
    def test_uptime_is_success_ratio_of_probes(self):
        c = self._check()
        for _ in range(19):
            self._result(c, True, ms=100)
        self._result(c, False)                       # 19 ok / 20 total
        self.assertEqual(sli_utils.calculate_uptime_sli(self.server, self.start, self.end), 95.0)

    def test_error_rate_is_probe_failure_ratio(self):
        c = self._check()
        for _ in range(19):
            self._result(c, True)
        self._result(c, False)
        self.assertEqual(sli_utils.calculate_error_rate_sli(self.server, self.start, self.end), 5.0)

    def test_response_time_is_avg_of_successful_probes(self):
        c = self._check()
        self._result(c, True, ms=100)
        self._result(c, True, ms=300)
        self._result(c, False, ms=None)              # failed -> excluded
        self.assertEqual(sli_utils.calculate_response_time_sli(self.server, self.start, self.end), 200.0)

    def test_no_synthetic_data_returns_none_not_fabricated(self):
        self._metric(cpu=10, mem=10)                 # metrics exist, but NO probe data
        self.assertIsNone(sli_utils.calculate_uptime_sli(self.server, self.start, self.end))
        self.assertIsNone(sli_utils.calculate_error_rate_sli(self.server, self.start, self.end))
        self.assertIsNone(sli_utils.calculate_response_time_sli(self.server, self.start, self.end))

    # --- resource SLIs: % of samples at/under the reliability threshold ---
    def test_cpu_sli_is_percent_under_threshold(self):
        for v in [10, 20, 30, 40, 50, 60, 70, 80]:   # <= 85
            self._metric(cpu=v)
        for v in [90, 95]:                           # > 85
            self._metric(cpu=v)
        self.assertEqual(sli_utils.calculate_cpu_sli(self.server, self.start, self.end), 80.0)

    def test_disk_sli_reads_json_percent(self):
        for p in [10, 20, 30, 40]:                   # <= 90
            self._metric(disk_percent=p)
        self._metric(disk_percent=95)                # > 90  -> 4/5 = 80
        self.assertEqual(sli_utils.calculate_disk_sli(self.server, self.start, self.end), 80.0)

    def test_resource_sli_none_when_no_metrics(self):
        self.assertIsNone(sli_utils.calculate_cpu_sli(self.server, self.start, self.end))
        self.assertIsNone(sli_utils.calculate_memory_sli(self.server, self.start, self.end))


class MttrTests(TestCase):
    """MTTR reuses the anomaly window-end preference (recovered_at, else admin resolved_at),
    averaged over ENDED anomalies; None when nothing has ended."""

    def setUp(self):
        self.server = Server.objects.create(name="mttr-vm", ip_address="10.9.9.2", username="agent")
        self.start = timezone.now() - timedelta(hours=3)
        self.end = timezone.now() + timedelta(minutes=1)

    def _metric(self):
        return SystemMetric.objects.create(
            server=self.server, cpu_percent=0.0, memory_percent=0.0, disk_usage={},
            timestamp=timezone.now() - timedelta(minutes=40), **_MEM)

    def _anomaly(self, start_offset_min, duration_sec=None, resolved=False):
        m = self._metric()
        ts = timezone.now() - timedelta(minutes=start_offset_min)
        a = Anomaly.objects.create(
            server=self.server, metric=m, timestamp=ts,
            metric_type="cpu", metric_name="cpu_percent", metric_value=99.0,
            anomaly_score=0.9, severity="HIGH")
        if duration_sec is not None:
            a.recovered_at = ts + timedelta(seconds=duration_sec)
            a.save(update_fields=["recovered_at"])
        return a

    def test_mttr_is_average_of_ended_anomaly_durations(self):
        self._anomaly(60, duration_sec=60)      # 60s
        self._anomaly(50, duration_sec=120)     # 120s -> avg 90s
        self.assertEqual(
            sli_utils.calculate_mttr_seconds(self.server, self.start, self.end), 90.0)

    def test_ongoing_anomaly_is_excluded(self):
        self._anomaly(60, duration_sec=60)      # ended, 60s
        self._anomaly(30, duration_sec=None)    # ongoing -> excluded
        self.assertEqual(
            sli_utils.calculate_mttr_seconds(self.server, self.start, self.end), 60.0)

    def test_mttr_none_when_nothing_ended(self):
        self._anomaly(30, duration_sec=None)
        self.assertIsNone(
            sli_utils.calculate_mttr_seconds(self.server, self.start, self.end))


class ComplianceJobTests(TestCase):
    """The scheduled command writes SLIMeasurement rows against the realigned SLO defaults
    (resource SLIs are now 'gte' % under threshold), and skips metrics with no data."""

    def setUp(self):
        self.server = Server.objects.create(name="job-vm", ip_address="10.9.9.3", username="agent")
        c = SyntheticCheck.objects.create(name="c", check_type="HTTP",
                                          url="https://x.test", server=self.server)
        # 95/100 probe success -> availability 95 (< 99.9 SLO), check-failure 5 (> 1.0 SLO),
        # latency 100ms (< 200 SLO).
        for _ in range(95):
            SyntheticCheckResult.objects.create(synthetic_check=c, success=True,
                response_time_ms=100, timestamp=timezone.now() - timedelta(minutes=10))
        for _ in range(5):
            SyntheticCheckResult.objects.create(synthetic_check=c, success=False,
                response_time_ms=None, timestamp=timezone.now() - timedelta(minutes=10))
        # CPU all under threshold -> CPU SLI 100 (>= 95 SLO -> compliant).
        for _ in range(10):
            SystemMetric.objects.create(server=self.server, cpu_percent=20.0, memory_percent=10.0,
                disk_usage={}, timestamp=timezone.now() - timedelta(minutes=5), **_MEM)

    def _latest(self, metric_type):
        return (SLIMeasurement.objects.filter(server=self.server, metric_type=metric_type)
                .order_by("-time_window_end").first())

    def test_job_populates_measurements_with_realigned_semantics(self):
        # sanity: the realign migration flipped the global CPU SLO to gte/95.
        cpu_slo = SLOConfig.objects.get(server=None, metric_type="CPU")
        self.assertEqual((cpu_slo.target_operator, cpu_slo.target_value), ("gte", 95.0))

        call_command("calculate_sli_compliance", verbosity=0)

        # Availability measured but below the 99.9 target.
        up = self._latest("UPTIME")
        self.assertIsNotNone(up)
        self.assertEqual(up.sli_value, 95.0)
        self.assertFalse(up.is_compliant)

        # CPU % under threshold = 100 -> meets the gte 95 target.
        cpu = self._latest("CPU")
        self.assertIsNotNone(cpu)
        self.assertEqual(cpu.sli_value, 100.0)
        self.assertTrue(cpu.is_compliant)

        # Check-failure 5% breaches the lte 1.0 target.
        er = self._latest("ERROR_RATE")
        self.assertIsNotNone(er)
        self.assertEqual(er.sli_value, 5.0)
        self.assertFalse(er.is_compliant)

        # DISK has no per-sample data -> skipped, not fabricated.
        self.assertIsNone(self._latest("DISK"))


class ComplianceApiTests(TestCase):
    """dashboard_sli_compliance_api returns a real compliant-server count (not the old
    hardcoded 0) and an honest denominator (servers we actually measured)."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))

    def _measure(self, server, metric_type, compliant):
        SLIMeasurement.objects.create(
            server=server, metric_type=metric_type,
            time_window_start=timezone.now() - timedelta(days=7),
            time_window_end=timezone.now(), sli_value=99.0, slo_target=99.9,
            is_compliant=compliant, compliance_percentage=100.0 if compliant else 0.0,
            calculated_at=timezone.now())

    def test_compliant_servers_counted(self):
        good = Server.objects.create(name="good", ip_address="10.1.1.1", username="a")
        bad = Server.objects.create(name="bad", ip_address="10.1.1.2", username="a")
        # good: all metrics compliant; bad: one metric non-compliant -> not fully compliant.
        self._measure(good, "UPTIME", True)
        self._measure(good, "CPU", True)
        self._measure(bad, "UPTIME", True)
        self._measure(bad, "CPU", False)

        resp = self.client.get(reverse("dashboard_sli_compliance_api"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["compliant_servers"], 1)      # only 'good'
        self.assertEqual(data["total_servers"], 2)          # both were measured
        self.assertEqual(data["compliance_percentage"], 50.0)
        self.assertEqual(data["by_metric"]["CPU"]["compliant_servers"], 1)
        self.assertEqual(data["by_metric"]["CPU"]["total_servers"], 2)


class ReliabilityTimeseriesTests(TestCase):
    """The reliability payload exposes a REAL availability series + check-failure % from
    synthetic probes (no alert×10 proxy), plus MTTR."""

    def setUp(self):
        self.server = Server.objects.create(name="ts-vm", ip_address="10.9.9.4", username="agent")
        c = SyntheticCheck.objects.create(name="c", check_type="HTTP",
                                          url="https://x.test", server=self.server)
        for _ in range(9):
            SyntheticCheckResult.objects.create(synthetic_check=c, success=True,
                response_time_ms=50, timestamp=timezone.now() - timedelta(minutes=15))
        SyntheticCheckResult.objects.create(synthetic_check=c, success=False,
            response_time_ms=None, timestamp=timezone.now() - timedelta(minutes=15))

    def test_availability_and_check_failure_series_are_real(self):
        data = sli_utils.get_reliability_metrics_timeseries(self.server.id, "24h")
        # 9 ok / 10 total in a single hour bucket -> availability 90, check-failure 10.
        self.assertTrue(data["availability"])
        self.assertEqual(data["availability"][-1]["value"], 90.0)
        self.assertEqual(data["error_rate"][-1]["value"], 10.0)

    def test_no_synthetic_data_gives_empty_series_not_fabricated(self):
        empty = Server.objects.create(name="empty", ip_address="10.9.9.5", username="a")
        data = sli_utils.get_reliability_metrics_timeseries(empty.id, "24h")
        self.assertEqual(data["availability"], [])
        self.assertEqual(data["error_rate"], [])


class ReliabilityPageTests(TestCase):
    """Phase 3: the /reliability/ page renders and un-orphans the reused components, and the
    reliability API exposes availability stats + a humanized MTTR through the view."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("boss", "b@x.test", "pw"))

    def test_reliability_page_renders_with_reused_components(self):
        resp = self.client.get(reverse("reliability_dashboard"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # The two orphaned components are now wired via their container ids...
        self.assertIn('id="sli-compliance-cards-container"', body)
        self.assertIn('id="slo-compliance-gauges-container"', body)
        # ...and the components' scripts are actually included.
        self.assertIn('SLIComplianceCards.js', body)
        self.assertIn('SLOComplianceGauges.js', body)
        # Honest headline labels, not "throughput"/"requests per second".
        self.assertIn('Availability', body)
        self.assertIn('MTTR', body)
        self.assertIn('Check-failure', body)

        # Honest-label guard: none of the vocabulary we deliberately do NOT support (req/s
        # throughput isn't collected; "error rate" is really a check-failure %) leaks to the UI.
        lowered = body.lower()
        for banned in ('requests/sec', 'req/s', 'requests per second', 'throughput', 'error rate'):
            self.assertNotIn(banned, lowered, f"forbidden reliability term on the page: {banned!r}")

    def test_reliability_api_exposes_availability_stats_and_mttr_text(self):
        server = Server.objects.create(name="api-vm", ip_address="10.7.7.1", username="a")
        c = SyntheticCheck.objects.create(name="c", check_type="HTTP",
                                          url="https://x.test", server=server)
        for _ in range(10):
            SyntheticCheckResult.objects.create(synthetic_check=c, success=True,
                response_time_ms=40, timestamp=timezone.now() - timedelta(minutes=15))

        resp = self.client.get(reverse("dashboard_reliability_metrics_api"),
                               {"period": "24h", "server_id": server.id})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("availability", data)
        self.assertIn("availability", data["stats"])
        self.assertEqual(data["availability"][-1]["value"], 100.0)
        self.assertIn("mttr_text", data)               # "—" when no ended anomalies
        self.assertEqual(data["mttr_text"], "—")
        # Performance = avg successful-probe latency (ms); 10 probes at 40ms -> 40.0.
        self.assertEqual(data["response_time_ms"], 40.0)


class DashboardReliabilityRowTests(TestCase):
    """The main dashboard surfaces the reliability KPI row (Availability / Performance / MTTR /
    Error rate) for users who can view operations."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("boss2", "b2@x.test", "pw"))

    def test_dashboard_shows_reliability_kpi_row(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        for tile in ('dash-rel-availability', 'dash-rel-performance',
                     'dash-rel-mttr', 'dash-rel-errorrate'):
            self.assertIn('id="%s"' % tile, body)
        self.assertIn('Reliability &amp; SLOs', body)
        self.assertIn('Performance', body)
        self.assertIn('Error rate', body)
