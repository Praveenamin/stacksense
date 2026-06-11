"""
Alert taxonomy tests: every alert source maps to exactly one of the five categories
(no KeyErrors / surprises), and the default-severity helper grades severity-less
AlertHistory alerts as specified (connection down -> CRITICAL, service/container/
threshold -> HIGH, anything resolved -> LOW).
"""
from django.test import SimpleTestCase

from core import alert_categories as ac
from core.models import AlertHistory, Anomaly


class CategoryMappingTests(SimpleTestCase):
    def test_every_alerthistory_type_maps_to_a_category(self):
        # Threshold breaches -> RESOURCE; connection/service/container -> AVAILABILITY.
        expected = {
            "CPU": ac.AlertCategory.RESOURCE,
            "Memory": ac.AlertCategory.RESOURCE,
            "Disk": ac.AlertCategory.RESOURCE,
            "CONNECTION": ac.AlertCategory.AVAILABILITY,
            "SERVICE": ac.AlertCategory.AVAILABILITY,
            "CONTAINER": ac.AlertCategory.AVAILABILITY,
        }
        for type_code, _ in AlertHistory.AlertType.choices:
            cat = ac.for_alert_type(type_code)
            self.assertIn(cat, ac.AlertCategory.values)
            if type_code in expected:
                self.assertEqual(cat, expected[type_code])

    def test_io_variants_map_to_resource(self):
        for t in ("diskio", "disk_io", "networkio", "network_io", "network"):
            self.assertEqual(ac.for_alert_type(t), ac.AlertCategory.RESOURCE)

    def test_unknown_alert_type_defaults_to_resource(self):
        self.assertEqual(ac.for_alert_type("something_new"), ac.AlertCategory.RESOURCE)
        self.assertEqual(ac.for_alert_type(None), ac.AlertCategory.RESOURCE)

    def test_anomaly_metric_types(self):
        for m in ("cpu", "memory", "disk", "network"):
            self.assertEqual(ac.for_anomaly(m), ac.AlertCategory.RESOURCE)
        for leak in ("shm_leak", "ipc_leak", "process_rss_leak"):
            self.assertEqual(ac.for_anomaly(leak), ac.AlertCategory.CAPACITY)

    def test_constant_sources(self):
        self.assertEqual(ac.SECURITY, ac.AlertCategory.SECURITY)
        self.assertEqual(ac.SYNTHETIC, ac.AlertCategory.AVAILABILITY)
        self.assertEqual(ac.BUSINESS, ac.AlertCategory.BUSINESS)

    def test_label_round_trips(self):
        self.assertEqual(ac.label(ac.AlertCategory.RESOURCE), "Resource / Performance")
        self.assertEqual(ac.label("capacity"), "Capacity & Health")
        self.assertEqual(ac.label("bogus"), "bogus")  # unknown -> echoed back


class DefaultSeverityTests(SimpleTestCase):
    def test_connection_down_is_critical(self):
        self.assertEqual(
            ac.default_severity_for_alert_type("connection", "triggered"), ac.SEV_CRITICAL)
        self.assertEqual(
            ac.default_severity_for_alert_type("CONNECTION", "triggered"), ac.SEV_CRITICAL)

    def test_service_container_threshold_are_high(self):
        for t in ("service", "container", "CPU", "Memory", "Disk"):
            self.assertEqual(
                ac.default_severity_for_alert_type(t, "triggered"), ac.SEV_HIGH)

    def test_resolved_is_low_regardless_of_type(self):
        for t in ("connection", "service", "CPU", "container"):
            self.assertEqual(
                ac.default_severity_for_alert_type(t, "resolved"), ac.SEV_LOW)

    def test_severity_values_match_anomaly_choices(self):
        # Severity constants must line up with the persisted choices on the model.
        model_values = set(Anomaly.Severity.values)
        self.assertEqual(
            {ac.SEV_LOW, ac.SEV_MEDIUM, ac.SEV_HIGH, ac.SEV_CRITICAL},
            model_values)
