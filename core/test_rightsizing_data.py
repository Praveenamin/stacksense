"""
DB-backed tests for the right-sizing data layer (core.utils.rightsizing_data).

Covers edge cases: eligible VM, new VM (<7d), sparse/gapped history, a VM with
no metrics, and a mixed-age fleet. Run: python manage.py test core.test_rightsizing_data
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import Server, SystemMetric
from core.utils.rightsizing_data import gather_vm_window_stats, MIN_SAMPLES
from core.utils.rightsizing_engine import assess_vm
from core.utils import rightsizing_constants as C

GB = 1024 ** 3


def mk_metric(server, ts, cpu, mem, cpu_count=4, mem_total=8 * GB, disk=None):
    used = int(mem_total * mem / 100)
    return SystemMetric.objects.create(
        server=server, timestamp=ts,
        cpu_percent=cpu, cpu_count=cpu_count,
        memory_total=mem_total, memory_used=used,
        memory_available=mem_total - used, memory_percent=mem,
        disk_usage=disk or {"/": {"total": 100 * GB, "used": int(25 * GB), "percent": 25.0}},
    )


def seed(server, *, span_days, n, cpu, mem, cpu_count=4, mem_total=8 * GB):
    """Create n metrics evenly spread across the last span_days."""
    now = timezone.now()
    for i in range(n):
        ts = now - timedelta(days=span_days) + timedelta(
            seconds=(span_days * 86400) * i / max(1, n - 1))
        mk_metric(server, ts, cpu, mem, cpu_count=cpu_count, mem_total=mem_total)


class DataLayerTests(TestCase):
    def _stats_for(self, server_id):
        return next(s for s in gather_vm_window_stats() if s.server_id == server_id)

    def test_eligible_vm_stats_and_capacity(self):
        s = Server.objects.create(name="eligible", ip_address="10.0.0.1", username="x")
        seed(s, span_days=40, n=60, cpu=20.0, mem=22.0, cpu_count=4, mem_total=8 * GB)
        st = self._stats_for(s.id)
        self.assertGreaterEqual(st.data_days, 39)
        self.assertEqual(st.current_vcpu, 4)
        self.assertAlmostEqual(st.current_gb, 8.0, places=1)
        self.assertAlmostEqual(st.cpu.avg, 20.0, places=1)
        self.assertAlmostEqual(st.memory.avg, 22.0, places=1)
        # Both dimensions low -> UNDER classification end-to-end, Medium confidence.
        a = assess_vm(st)
        self.assertEqual(a.category, C.CAT_UNDER)
        self.assertEqual(a.confidence, "MEDIUM")

    def test_new_vm_is_insufficient(self):
        s = Server.objects.create(name="new", ip_address="10.0.0.2", username="x")
        seed(s, span_days=3, n=40, cpu=20.0, mem=30.0)
        st = self._stats_for(s.id)
        self.assertLess(st.data_days, 7)
        self.assertEqual(assess_vm(st).category, C.CAT_INSUFFICIENT)

    def test_sparse_history_forced_insufficient(self):
        # 40-day span but fewer than MIN_SAMPLES points -> not trusted.
        s = Server.objects.create(name="sparse", ip_address="10.0.0.3", username="x")
        seed(s, span_days=40, n=MIN_SAMPLES - 1, cpu=20.0, mem=30.0)
        st = self._stats_for(s.id)
        self.assertEqual(st.data_days, 0.0)
        self.assertEqual(assess_vm(st).category, C.CAT_INSUFFICIENT)

    def test_vm_with_no_metrics(self):
        s = Server.objects.create(name="empty", ip_address="10.0.0.4", username="x")
        st = self._stats_for(s.id)
        self.assertEqual(st.data_days, 0.0)
        self.assertEqual(st.sample_count, 0)
        self.assertEqual(assess_vm(st).category, C.CAT_INSUFFICIENT)

    def test_percentile_is_computed(self):
        s = Server.objects.create(name="pctl", ip_address="10.0.0.5", username="x")
        now = timezone.now()
        # 100 points, cpu 1..100 -> p95 ~ 95
        for i in range(100):
            mk_metric(s, now - timedelta(days=30) + timedelta(hours=i), float(i + 1), 50.0)
        st = self._stats_for(s.id)
        self.assertGreater(st.cpu.p95, 90)
        self.assertLessEqual(st.cpu.p95, 100)
        self.assertGreater(st.cpu.peak, st.cpu.avg)

    def test_mixed_age_fleet_each_independent(self):
        a = Server.objects.create(name="old", ip_address="10.0.1.1", username="x")
        b = Server.objects.create(name="young", ip_address="10.0.1.2", username="x")
        seed(a, span_days=120, n=60, cpu=90.0, mem=92.0, cpu_count=2, mem_total=8 * GB)
        seed(b, span_days=2, n=40, cpu=10.0, mem=10.0)
        stats = {s.server_id: s for s in gather_vm_window_stats()}
        # old VM -> high confidence + overutilized; young -> insufficient
        ass_a = assess_vm(stats[a.id])
        ass_b = assess_vm(stats[b.id])
        self.assertEqual(ass_a.confidence, "HIGH")
        self.assertEqual(ass_a.category, C.CAT_OVER)
        self.assertEqual(ass_b.category, C.CAT_INSUFFICIENT)
