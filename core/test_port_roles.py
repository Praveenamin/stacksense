"""Closed-set tests for the well-known-port -> role map and the honest-naming invariant.

A port tells you the protocol/role, never the product. These tests lock the curated
map (so a future edit that drops 2087/4190 fails loudly), prove unknown ports return
None (so they get suppressed, not shown as key services), and guard that the map never
asserts a product name (nginx/apache/litespeed) -- product identity only ever comes
from a verified banner.
"""
from django.test import TestCase

from core.port_roles import PORT_ROLES, role_for_port

# The full set the product depends on. If you change PORT_ROLES, change this too --
# that's the point: the map is a contract, not an incidental dict.
EXPECTED = {
    80: "HTTP", 443: "HTTPS", 81: "HTTP-Alt", 8080: "HTTP-Alt", 8443: "HTTPS-Alt",
    2082: "cPanel", 2083: "cPanel (SSL)", 2086: "WHM", 2087: "WHM (SSL)",
    2095: "Webmail", 2096: "Webmail (SSL)",
    2077: "cpdavd", 2078: "cpdavd (SSL)", 2079: "cpdavd", 2080: "cpdavd (SSL)",
    25: "SMTP", 465: "SMTP (SSL)", 587: "SMTP (submission)",
    110: "POP3", 995: "POP3 (SSL)", 143: "IMAP", 993: "IMAP (SSL)", 4190: "Sieve",
    53: "DNS", 953: "DNS (rndc)", 21: "FTP", 22: "SSH",
    3306: "MySQL", 5432: "PostgreSQL", 6379: "Redis", 27017: "MongoDB",
}

# Ports seen on the real cPanel box that are ephemeral / unrecognized -> must be None.
UNKNOWN_PORTS = [52227, 52228, 52229, 44222, 11234, 579, 199, 2091, 0, 65535]


class PortRolesTests(TestCase):
    def test_map_matches_expected_closed_set(self):
        self.assertEqual(PORT_ROLES, EXPECTED)

    def test_every_known_port_resolves(self):
        for port, role in EXPECTED.items():
            self.assertEqual(role_for_port(port), role, f"port {port}")

    def test_unknown_and_ephemeral_ports_return_none(self):
        for port in UNKNOWN_PORTS:
            self.assertIsNone(role_for_port(port), f"port {port} should be unknown")

    def test_bad_input_returns_none(self):
        for bad in (None, "", "abc", "80x", object()):
            self.assertIsNone(role_for_port(bad))

    def test_string_port_is_accepted(self):
        self.assertEqual(role_for_port("3306"), "MySQL")

    def test_map_never_names_a_product(self):
        # Honest-naming invariant: the role map must not assert a web-server product.
        banned = ("nginx", "apache", "litespeed", "httpd", "openlitespeed")
        for port, role in PORT_ROLES.items():
            low = role.lower()
            for b in banned:
                self.assertNotIn(b, low, f"port {port} role {role!r} names a product")
