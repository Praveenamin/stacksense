"""
Unit tests for the right-sizing recommendation engine (pure logic, no DB).

Run:  python manage.py test core.test_rightsizing
"""
from django.test import SimpleTestCase

from core.utils import rightsizing_constants as C
from core.utils.rightsizing_engine import (
    DimStats, VMWindowStats, Pricing,
    percentile, snap_up, next_step_up,
    confidence_for_days, classify, recommend_size, cost_savings, assess_vm,
)
from core.utils.rightsizing_report import build_report


def dim(avg, p95=None, peak=None):
    p95 = avg if p95 is None else p95
    peak = p95 if peak is None else peak
    return DimStats(avg=avg, p95=p95, peak=peak)


def vm(cpu, memory, *, days=120, vcpu=4, gb=8.0, disk=None,
       name="vm", sid=1, samples=1000):
    return VMWindowStats(
        server_id=sid, name=name, data_days=days, sample_count=samples,
        cpu=cpu, memory=memory, disk=disk, current_vcpu=vcpu, current_gb=gb,
    )


# ---------------------------------------------------------------------------
# Helpers: percentile / snapping
# ---------------------------------------------------------------------------
class HelperTests(SimpleTestCase):
    def test_percentile_empty(self):
        self.assertEqual(percentile([], 95), 0.0)

    def test_percentile_single(self):
        self.assertEqual(percentile([42.0], 95), 42.0)

    def test_percentile_interpolates(self):
        # p95 of 1..100 ~ 95.05
        self.assertAlmostEqual(percentile(list(range(1, 101)), 95), 95.05, places=2)
        self.assertEqual(percentile([0, 10], 50), 5.0)

    def test_snap_up(self):
        self.assertEqual(snap_up(1.33, C.VCPU_STEPS), 2)
        self.assertEqual(snap_up(2, C.VCPU_STEPS), 2)          # exact match
        self.assertEqual(snap_up(2.4, C.RAM_GB_STEPS), 4)
        self.assertEqual(snap_up(99999, C.VCPU_STEPS), 128)    # clamp

    def test_next_step_up(self):
        self.assertEqual(next_step_up(4, C.VCPU_STEPS), 8)
        self.assertEqual(next_step_up(2, C.VCPU_STEPS), 4)
        self.assertEqual(next_step_up(128, C.VCPU_STEPS), 128)  # clamp at top


# ---------------------------------------------------------------------------
# Confidence boundaries
# ---------------------------------------------------------------------------
class ConfidenceTests(SimpleTestCase):
    def test_boundaries(self):
        cases = {
            0: "NONE", 6: "NONE", 6.9: "NONE",
            7: "LOW", 7.0: "LOW", 29: "LOW", 29.9: "LOW",
            30: "MEDIUM", 30.0: "MEDIUM", 89: "MEDIUM", 89.9: "MEDIUM",
            90: "HIGH", 90.0: "HIGH", 91: "HIGH", 365: "HIGH",
        }
        for days, expected in cases.items():
            tier, _msg = confidence_for_days(days)
            self.assertEqual(tier, expected, f"days={days} -> {tier}, expected {expected}")

    def test_messages_are_verbatim(self):
        self.assertEqual(confidence_for_days(6)[1], C.MSG_INSUFFICIENT)
        self.assertEqual(confidence_for_days(7)[1], C.MSG_LOW)
        self.assertEqual(confidence_for_days(30)[1], C.MSG_MEDIUM)
        self.assertEqual(confidence_for_days(90)[1], C.MSG_HIGH)
        # exact spec strings
        self.assertEqual(
            C.MSG_INSUFFICIENT,
            "Insufficient data available. Recommendations will be generated "
            "after a minimum of 7 days of usage metrics.")
        self.assertEqual(
            C.MSG_LOW,
            "These recommendations are based on limited historical data and "
            "should be considered preliminary. Additional monitoring is "
            "recommended before taking action.")
        self.assertEqual(
            C.MSG_MEDIUM,
            "Recommendations are based on sustained usage patterns and can "
            "generally be considered for planning and optimization.")
        self.assertEqual(
            C.MSG_HIGH,
            "Recommendations are based on long-term usage trends and can be "
            "used for capacity planning and resource allocation decisions.")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
class ClassifyTests(SimpleTestCase):
    def test_underutilized(self):
        self.assertEqual(classify(dim(12, 22), dim(15, 25)), C.CAT_UNDER)

    def test_overutilized_cpu_only(self):
        self.assertEqual(classify(dim(85, 95), dim(20, 30)), C.CAT_OVER)

    def test_overutilized_memory_only(self):
        self.assertEqual(classify(dim(20, 30), dim(85, 95)), C.CAT_OVER)

    def test_overutilized_by_p95_spike(self):
        # avg fine but p95 over 90 -> still over (safety)
        self.assertEqual(classify(dim(40, 92), dim(40, 50)), C.CAT_OVER)

    def test_optimized(self):
        self.assertEqual(classify(dim(55, 70), dim(60, 75)), C.CAT_OPTIMIZED)

    def test_neutral_between_bands(self):
        # not under (p95 >= 50), not optimized (avg < 40), not over
        self.assertEqual(classify(dim(35, 55), dim(38, 58)), C.CAT_NEUTRAL)

    def test_under_threshold_edges(self):
        # avg exactly 30 / p95 exactly 50 -> NOT under (requires strictly below)
        self.assertNotEqual(classify(dim(30, 50), dim(30, 50)), C.CAT_UNDER)
        # just below -> under
        self.assertEqual(classify(dim(29.9, 49.9), dim(29.9, 49.9)), C.CAT_UNDER)

    def test_over_threshold_edges(self):
        # avg exactly 80 / p95 exactly 90 -> NOT over (requires strictly above)
        self.assertNotEqual(classify(dim(80, 90), dim(80, 90)), C.CAT_OVER)
        self.assertEqual(classify(dim(80.1, 80), dim(20, 30)), C.CAT_OVER)
        self.assertEqual(classify(dim(50, 90.1), dim(20, 30)), C.CAT_OVER)


# ---------------------------------------------------------------------------
# Suggested size
# ---------------------------------------------------------------------------
class SizeTests(SimpleTestCase):
    def test_downsize_both_dims(self):
        s = vm(dim(12, 15), dim(14, 18), vcpu=4, gb=8.0)
        sv, sg = recommend_size(s, C.CAT_UNDER)
        # cpu: 4*15/60=1 -> 1 ; mem: 8*18/60=2.4 -> snap 4
        self.assertEqual(sv, 1)
        self.assertEqual(sg, 4.0)

    def test_downsize_floor_no_change_at_minimum(self):
        s = vm(dim(10, 20), dim(10, 20), vcpu=1, gb=1.0)
        sv, sg = recommend_size(s, C.CAT_UNDER)
        self.assertIsNone(sv)   # already 1 vCPU
        self.assertIsNone(sg)   # already 1 GB

    def test_downsize_only_one_dimension(self):
        # cpu low enough to shrink, memory low but rounds back to current
        s = vm(dim(12, 15), dim(40, 49), vcpu=4, gb=8.0)
        sv, sg = recommend_size(s, C.CAT_UNDER)
        self.assertEqual(sv, 1)
        # mem: 8*49/60 = 6.53 -> snap 8 == current -> no change
        self.assertIsNone(sg)

    def test_upgrade_hot_cpu_only(self):
        s = vm(dim(88, 95), dim(30, 40), vcpu=2, gb=8.0)
        sv, sg = recommend_size(s, C.CAT_OVER)
        # cpu: 2*95/60=3.17 -> snap 4 ; next_step_up(2)=4 -> 4
        self.assertEqual(sv, 4)
        self.assertIsNone(sg)   # memory not hot -> untouched

    def test_upgrade_guarantees_step_up(self):
        # p95 just over 90 on a large box; must still grow at least one step
        s = vm(dim(70, 90.5), dim(20, 30), vcpu=8, gb=16.0)
        sv, _sg = recommend_size(s, C.CAT_OVER)
        self.assertGreater(sv, 8)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
class CostTests(SimpleTestCase):
    def test_savings_unpriced(self):
        s = vm(dim(12, 15), dim(14, 18))
        self.assertIsNone(cost_savings(s, 1, 4.0, Pricing()))

    def test_savings_downsize_positive(self):
        s = vm(dim(12, 15), dim(14, 18), vcpu=4, gb=8.0)
        p = Pricing(price_per_vcpu_month=10, price_per_gb_month=5)
        # current 4*10+8*5=80 ; suggested 1*10+4*5=30 ; save 50
        self.assertEqual(cost_savings(s, 1, 4.0, p), 50.0)

    def test_savings_upgrade_negative(self):
        s = vm(dim(88, 95), dim(30, 40), vcpu=2, gb=8.0)
        p = Pricing(price_per_vcpu_month=10, price_per_gb_month=5)
        # current 2*10+8*5=60 ; suggested 4*10+8*5=80 ; -20
        self.assertEqual(cost_savings(s, 4, None, p), -20.0)


# ---------------------------------------------------------------------------
# assess_vm — end to end incl. the <7-day gate
# ---------------------------------------------------------------------------
class AssessTests(SimpleTestCase):
    def test_insufficient_gate(self):
        s = vm(dim(12, 15), dim(14, 18), days=6)
        a = assess_vm(s)
        self.assertEqual(a.category, C.CAT_INSUFFICIENT)
        self.assertEqual(a.confidence, "NONE")
        self.assertEqual(a.message, C.MSG_INSUFFICIENT)
        self.assertIsNone(a.suggested_vcpu)
        self.assertIsNone(a.suggested_gb)
        self.assertEqual(a.recommendation_text, "")
        self.assertEqual(a.data_period_label, "")

    def test_insufficient_gate_exact_boundary(self):
        # 6.99 days still insufficient, 7.0 produces a recommendation
        self.assertEqual(assess_vm(vm(dim(12, 15), dim(14, 18), days=6.99)).category,
                         C.CAT_INSUFFICIENT)
        self.assertNotEqual(assess_vm(vm(dim(12, 15), dim(14, 18), days=7.0)).category,
                            C.CAT_INSUFFICIENT)

    def test_underutilized_end_to_end_low_confidence(self):
        s = vm(dim(12, 15), dim(14, 18), days=10, vcpu=4, gb=8.0)
        p = Pricing(price_per_vcpu_month=10, price_per_gb_month=5)
        a = assess_vm(s, pricing=p)
        self.assertEqual(a.category, C.CAT_UNDER)
        self.assertEqual(a.confidence, "LOW")
        self.assertEqual(a.data_period_label, "7")
        self.assertEqual(a.suggested_vcpu, 1)
        self.assertEqual(a.suggested_gb, 4.0)
        self.assertEqual(a.delta_vcpu, 3)        # 4 - 1
        self.assertEqual(a.delta_gb, 4.0)        # 8 - 4
        self.assertEqual(a.monthly_savings, 50.0)
        self.assertIn("Downsize", a.recommendation_text)

    def test_optimized_no_change(self):
        s = vm(dim(55, 70), dim(60, 75), days=200)
        a = assess_vm(s)
        self.assertEqual(a.category, C.CAT_OPTIMIZED)
        self.assertEqual(a.confidence, "HIGH")
        self.assertEqual(a.data_period_label, "90+")
        self.assertIsNone(a.suggested_vcpu)
        self.assertIsNone(a.suggested_gb)
        self.assertIn("benchmark", a.recommendation_text)

    def test_overutilized_end_to_end_medium_confidence(self):
        s = vm(dim(88, 95), dim(30, 40), days=45, vcpu=2, gb=8.0)
        a = assess_vm(s)
        self.assertEqual(a.category, C.CAT_OVER)
        self.assertEqual(a.confidence, "MEDIUM")
        self.assertEqual(a.data_period_label, "30")
        self.assertEqual(a.suggested_vcpu, 4)
        self.assertIn("Upgrade", a.recommendation_text)


# ---------------------------------------------------------------------------
# Report builder — grouping, caps, sorting, totals
# ---------------------------------------------------------------------------
class ReportTests(SimpleTestCase):
    PRICE = Pricing(price_per_vcpu_month=10, price_per_gb_month=5)

    def _fleet(self):
        a = []
        # 12 underutilized (to test the cap at 10), varying load
        for i in range(12):
            a.append(assess_vm(
                vm(dim(5 + i, 10 + i), dim(8 + i, 12 + i),
                   days=120, vcpu=4, gb=8.0, name=f"under{i}", sid=100 + i),
                pricing=self.PRICE))
        # 3 overloaded
        for i in range(3):
            a.append(assess_vm(
                vm(dim(85 + i, 95), dim(40, 50),
                   days=120, vcpu=2, gb=8.0, name=f"over{i}", sid=200 + i),
                pricing=self.PRICE))
        # 2 optimized
        for i in range(2):
            a.append(assess_vm(
                vm(dim(55, 70), dim(58, 72),
                   days=120, vcpu=4, gb=8.0, name=f"opt{i}", sid=300 + i),
                pricing=self.PRICE))
        # 4 insufficient (<7d)
        for i in range(4):
            a.append(assess_vm(
                vm(dim(20, 30), dim(20, 30),
                   days=3, name=f"new{i}", sid=400 + i)))
        return a

    def test_caps_and_counts(self):
        r = build_report(self._fleet(), pricing_configured=True)
        self.assertEqual(r["pending_count"], 4)
        self.assertEqual(r["eligible_count"], 17)            # 12+3+2
        self.assertEqual(r["counts"]["underutilized"], 12)
        self.assertEqual(len(r["top_underutilized"]), 10)    # capped
        self.assertEqual(len(r["top_overloaded"]), 3)
        self.assertEqual(len(r["top_optimized"]), 2)

    def test_underutilized_sorted_lowest_first(self):
        r = build_report(self._fleet(), pricing_configured=True)
        loads = [(a.cpu.avg + a.memory.avg) for a in r["top_underutilized"]]
        self.assertEqual(loads, sorted(loads))               # ascending

    def test_overloaded_sorted_highest_peak_first(self):
        r = build_report(self._fleet(), pricing_configured=True)
        peaks = [max(a.cpu.p95, a.memory.p95) for a in r["top_overloaded"]]
        self.assertEqual(peaks, sorted(peaks, reverse=True))

    def test_totals_priced(self):
        r = build_report(self._fleet(), pricing_configured=True)
        self.assertIsNotNone(r["total_monthly_savings"])
        self.assertGreater(r["total_monthly_savings"], 0)
        self.assertGreater(r["total_reclaim_vcpu"], 0)

    def test_cost_opportunities_unpriced_uses_reduction(self):
        # Same fleet but rebuilt without pricing
        fleet = []
        for i in range(3):
            fleet.append(assess_vm(
                vm(dim(8, 12), dim(10, 14), days=120, vcpu=4, gb=8.0,
                   name=f"u{i}", sid=500 + i)))   # no pricing -> savings None
        r = build_report(fleet, pricing_configured=False)
        self.assertIsNone(r["total_monthly_savings"])
        self.assertEqual(r["cost_opportunities"], r["reduction_eligible"])
