"""2b: conservative systemd<->port merge at ingest.

A listening port that clearly belongs to a reported systemd unit is folded into that unit's
row (so the service is ONE row that has a port => Response/SLO), instead of a unit row plus a
separate port row. Ambiguous cases are never merged. Driven through the real token-authed
ingest endpoint (push shape), so it also proves no agent change is required.
"""
import json

from django.test import Client, TestCase
from django.urls import reverse

from core.models import AgentCredential, Server, Service


class _MergeBase(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="box1", ip_address="10.0.0.9", username="agent")
        _, self.token = AgentCredential.generate_for_server(self.server)
        self.url = reverse("agent_ingest_services")
        self.client = Client()

    def _push(self, services):
        return self.client.post(
            self.url,
            data=json.dumps({"agent_version": "push-1.10.0", "services": services}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    @staticmethod
    def _systemd(name):
        return {"name": name, "status": "running", "service_type": "systemd"}

    @staticmethod
    def _port(name, port, **over):
        item = {"name": name, "status": "running", "service_type": "port",
                "port": port, "bind_address": "0.0.0.0"}
        item.update(over)
        return item


class ServiceMergeTests(_MergeBase):
    def test_wellknown_port_folds_into_unit_when_names_differ(self):
        # unit "mariadb" + process "mysqld" on :3306 -> ONE row (mariadb, port 3306), no mysqld row.
        r = self._push([self._systemd("mariadb"), self._port("mysqld", 3306)])
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Service.objects.filter(server=self.server, name="mysqld").exists())
        maria = Service.objects.get(server=self.server, name="mariadb")
        self.assertEqual(maria.port, 3306)
        self.assertEqual(maria.service_type, "systemd")   # identity stays the unit

    def test_portN_fallback_folds_into_unique_unit(self):
        # /proc fallback names ports "port-N" (no process). Still folds when exactly one candidate.
        r = self._push([self._systemd("mariadb"), self._port("port-3306", 3306)])
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Service.objects.filter(server=self.server, name="port-3306").exists())
        self.assertEqual(Service.objects.get(server=self.server, name="mariadb").port, 3306)

    def test_ambiguous_wellknown_port_is_not_merged(self):
        # apache2 AND nginx both plausibly own :80 -> refuse to guess; port stays its own row.
        r = self._push([self._systemd("apache2"), self._systemd("nginx"), self._port("port-80", 80)])
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Service.objects.filter(server=self.server, name="port-80", port=80).exists())
        self.assertIsNone(Service.objects.get(server=self.server, name="apache2").port)
        self.assertIsNone(Service.objects.get(server=self.server, name="nginx").port)

    def test_exact_process_name_match_merges_even_if_not_wellknown(self):
        # A custom app on an odd port: unit "myapp" + process "myapp" on :9100 -> one row.
        r = self._push([self._systemd("myapp"), self._port("myapp", 9100)])
        self.assertEqual(r.status_code, 200)
        rows = Service.objects.filter(server=self.server, name="myapp")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().port, 9100)

    def test_unmatched_port_stays_its_own_row(self):
        # No systemd unit to attach to -> the port keeps its own row (unchanged behaviour).
        r = self._push([self._systemd("cron"), self._port("port-9999", 9999)])
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Service.objects.filter(server=self.server, name="port-9999", port=9999).exists())

    def test_existing_duplicate_row_self_heals_to_stopped(self):
        # A stale "mysqld" port row from before the merge existed...
        Service.objects.create(server=self.server, name="mysqld", service_type="port",
                               port=3306, status="running", auto_detected=True)
        # ...now folds away: no longer reported -> stopped sweep marks it stopped; mariadb owns 3306.
        r = self._push([self._systemd("mariadb"), self._port("mysqld", 3306)])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Service.objects.get(server=self.server, name="mysqld").status, "stopped")
        self.assertEqual(Service.objects.get(server=self.server, name="mariadb").port, 3306)

    def test_latency_is_carried_onto_the_merged_unit(self):
        # The agent measured latency on the port entry; after folding, the unit row shows it.
        Service.objects.create(server=self.server, name="mariadb", service_type="systemd",
                               status="running", monitoring_enabled=True)
        r = self._push([self._systemd("mariadb"),
                        self._port("mysqld", 3306, latency_ms=12.5, latency_success=True,
                                   latency_type="TCP")])
        self.assertEqual(r.status_code, 200)
        maria = Service.objects.get(server=self.server, name="mariadb")
        self.assertEqual(maria.port, 3306)
        self.assertTrue(maria.last_latency_success)
        self.assertEqual(maria.last_latency_ms, 12.5)

    def test_multiport_service_shows_single_row_lowest_port(self):
        # apache2 on :80 and :443 -> one row, primary (lowest) port; no duplicate rows.
        r = self._push([self._systemd("apache2"),
                        self._port("apache2", 443), self._port("apache2", 80)])
        self.assertEqual(r.status_code, 200)
        rows = Service.objects.filter(server=self.server, name="apache2")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().port, 80)
