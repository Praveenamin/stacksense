"""
Alerts-page volume / load test.

The Alerts page (alert_history) merges AlertHistory + Anomaly in Python and caps the
result at 500. The scale risk is an N+1 (a query per row) that only bites at volume.
These tests prove the page issues a CONSTANT number of queries regardless of row count,
caps the rendered list at 500, and renders within a budget at ~100k rows.
"""
import time

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Server, SystemMetric, AlertHistory, Anomaly


class AlertsPageVolumeTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="vol-vm", ip_address="10.5.5.1", username="agent")
        self.admin = User.objects.create_superuser("vol_admin", "vol@x.test", "pw")
        self.client.force_login(self.admin)
        self.metric = SystemMetric.objects.create(
            server=self.server, cpu_percent=95, memory_total=8_000_000_000,
            memory_available=1_000_000_000, memory_used=7_000_000_000, memory_percent=88)

    def _seed(self, n_alerts, n_anomalies):
        now = timezone.now()
        AlertHistory.objects.bulk_create([
            AlertHistory(server=self.server, alert_type="CPU", status="triggered",
                         severity="HIGH", value=95, threshold=80, message=f"cpu {i}",
                         recipients="", sent_at=now)
            for i in range(n_alerts)], batch_size=5000)
        Anomaly.objects.bulk_create([
            Anomaly(server=self.server, metric=self.metric, timestamp=now, metric_type="cpu",
                    metric_name="cpu_percent", metric_value=95, anomaly_score=0.9,
                    severity="HIGH", resolved=False)
            for i in range(n_anomalies)], batch_size=5000)

    def _get(self):
        r = self.client.get(reverse("alert_history") + "?time_range=all")
        self.assertEqual(r.status_code, 200)
        return r

    def _query_count(self):
        with CaptureQueriesContext(connection) as ctx:
            self._get()
        return len(ctx.captured_queries)

    def test_query_count_is_constant_regardless_of_volume(self):
        # The decisive N+1 guard: the page must issue the SAME number of queries whether
        # there are hundreds of rows or hundreds of thousands.
        self._seed(100, 100)
        baseline = self._query_count()
        self._seed(50_000, 50_000)            # ~100k rows total
        at_volume = self._query_count()
        self.assertEqual(at_volume, baseline,
                         f"query count grew with volume ({baseline} -> {at_volume}): likely an N+1")

    def test_rendered_list_is_capped_at_500(self):
        self._seed(2_000, 2_000)
        self.assertLessEqual(len(self._get().context["unified_items"]), 500)

    def test_renders_within_budget_at_volume(self):
        self._seed(50_000, 50_000)            # ~100k rows
        t0 = time.monotonic()
        self._get()
        elapsed = time.monotonic() - t0
        # Generous budget (CI/infra-dependent); the point is "seconds, not minutes".
        self.assertLess(elapsed, 5.0, f"Alerts page took {elapsed:.2f}s at ~100k rows")
