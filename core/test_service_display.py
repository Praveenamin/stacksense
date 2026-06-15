"""Display-layer tests for port-detected services: the notable/background split and
the Service.label precedence. These operate on in-memory Service instances (no DB) --
both `label` and `_is_background_service` read only model attributes.
"""
from django.test import TestCase

from core.models import Service
from core.views import _is_background_service


def svc(**kw):
    return Service(**kw)


class NotableBackgroundSplitTests(TestCase):
    def test_mapped_well_known_port_is_notable(self):
        self.assertFalse(_is_background_service(
            svc(service_type="port", port=3306, detected_via="port-map")))

    def test_banner_identified_port_is_always_notable(self):
        # 8081 has no role mapping, but a confirmed banner makes it a key service.
        self.assertFalse(_is_background_service(
            svc(service_type="port", port=8081, detected_via="port-banner")))

    def test_unknown_ephemeral_port_is_background(self):
        self.assertTrue(_is_background_service(
            svc(service_type="port", port=52227, detected_via="port-unknown")))

    def test_legacy_port_row_without_detected_via_uses_role_map(self):
        # Old rows (detected_via=None): well-known -> notable, unknown -> background.
        self.assertFalse(_is_background_service(svc(service_type="port", port=443, name="port-443")))
        self.assertTrue(_is_background_service(svc(service_type="port", port=44222, name="port-44222")))

    def test_systemd_split_unchanged(self):
        self.assertTrue(_is_background_service(svc(service_type="systemd", name="dbus")))
        self.assertTrue(_is_background_service(svc(service_type="systemd", name="systemd-resolved")))
        self.assertFalse(_is_background_service(svc(service_type="systemd", name="nginx")))
        self.assertFalse(_is_background_service(svc(service_type="systemd", name="mariadb")))


class ServiceLabelTests(TestCase):
    def test_display_name_wins(self):
        s = svc(service_type="port", port=80, name="port-80", display_name="nginx (:80)")
        self.assertEqual(s.label, "nginx (:80)")

    def test_role_fallback_when_no_display_name(self):
        s = svc(service_type="port", port=80, name="port-80")
        self.assertEqual(s.label, "HTTP (:80)")

    def test_cpanel_role_fallback(self):
        self.assertEqual(svc(service_type="port", port=2083, name="port-2083").label, "cPanel (SSL) (:2083)")

    def test_raw_name_for_unknown_port(self):
        s = svc(service_type="port", port=52227, name="port-52227")
        self.assertEqual(s.label, "port-52227")

    def test_systemd_uses_name(self):
        self.assertEqual(svc(service_type="systemd", name="cron").label, "cron")
