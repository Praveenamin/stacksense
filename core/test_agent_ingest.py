"""
Phase 4 (ingestion / agent API) -- the security front door.

Every monitored VM POSTs here with a per-server bearer token. This is the only place
untrusted, network-supplied input enters StackSense, so these tests focus on the
boundary: token auth (accept/reject/revoke/isolation), input hardening (auth, size,
shape), and integrity (server-stamped time, field whitelist, suspension), plus that a
valid push actually persists.

The token is the server's identity: AgentCredential stores only a SHA-256 hash, and a
token authenticates exactly one server.
"""
import json

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (Server, AgentCredential, SystemMetric, ServerHeartbeat,
                         Service, Container, SSHAuthEvent, MonitoringConfig,
                         BusinessMonitorConfig, BusinessKPI)


# A minimal VALID metrics payload (the five REQUIRED_FIELDS).
VALID = {
    "cpu_percent": 12.5, "memory_total": 8_000_000_000, "memory_available": 4_000_000_000,
    "memory_percent": 50.0, "memory_used": 4_000_000_000,
}


class _Base(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name="vm-a", ip_address="10.4.4.1", username="agent")
        _, self.token = AgentCredential.generate_for_server(self.server)
        self.client = Client()
        self.metrics_url = reverse("agent_ingest_metrics")

    def _post(self, url, body, token=None):
        kw = {"content_type": "application/json"}
        if token is not None:
            kw["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        data = json.dumps(body) if isinstance(body, (dict, list)) else body
        return self.client.post(url, data=data, **kw)


class AuthBoundaryTests(_Base):
    def test_valid_token_accepted(self):
        r = self._post(self.metrics_url, VALID, token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["stored"])

    def test_missing_auth_header_rejected(self):
        r = self._post(self.metrics_url, VALID)            # no Authorization
        self.assertEqual(r.status_code, 401)
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_malformed_auth_header_rejected(self):
        r = self.client.post(self.metrics_url, data=json.dumps(VALID),
                             content_type="application/json",
                             HTTP_AUTHORIZATION="Token abc")   # not "Bearer ..."
        self.assertEqual(r.status_code, 401)

    def test_unknown_token_rejected(self):
        r = self._post(self.metrics_url, VALID, token="definitely-not-a-real-token")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_revoked_token_rejected(self):
        cred = AgentCredential.objects.get(server=self.server)
        cred.enabled = False
        cred.save(update_fields=["enabled"])
        r = self._post(self.metrics_url, VALID, token=self.token)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_token_binds_data_to_its_own_server(self):
        # Server B exists; pushing with A's token -- even with a payload naming B --
        # must store for A only. The token is the identity, not the payload.
        server_b = Server.objects.create(name="vm-b", ip_address="10.4.4.2", username="agent")
        AgentCredential.generate_for_server(server_b)
        r = self._post(self.metrics_url,
                       dict(VALID, server_id=server_b.id, server="vm-b"),
                       token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SystemMetric.objects.filter(server=self.server).count(), 1)
        self.assertEqual(SystemMetric.objects.filter(server=server_b).count(), 0)


class InputHardeningTests(_Base):
    def test_oversized_body_rejected_413(self):
        big = dict(VALID, junk="x" * (260 * 1024))         # > 256 KB
        r = self._post(self.metrics_url, big, token=self.token)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_malformed_json_rejected_400(self):
        r = self._post(self.metrics_url, "{not valid json", token=self.token)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_non_object_json_rejected_400(self):
        r = self._post(self.metrics_url, [1, 2, 3], token=self.token)   # a JSON array
        self.assertEqual(r.status_code, 400)
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_missing_required_field_rejected_400(self):
        payload = {k: v for k, v in VALID.items() if k != "cpu_percent"}
        r = self._post(self.metrics_url, payload, token=self.token)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_non_numeric_required_field_rejected_400(self):
        r = self._post(self.metrics_url, dict(VALID, cpu_percent="not-a-number"),
                       token=self.token)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(SystemMetric.objects.count(), 0)

    def test_get_method_not_allowed_405(self):
        self.assertEqual(self.client.get(self.metrics_url).status_code, 405)


class MetricsIntegrityTests(_Base):
    def test_valid_push_stores_exactly_one_metric(self):
        r = self._post(self.metrics_url, VALID, token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SystemMetric.objects.filter(server=self.server).count(), 1)
        self.assertAlmostEqual(SystemMetric.objects.get().cpu_percent, 12.5)

    def test_timestamp_is_stamped_server_side(self):
        # An agent-supplied timestamp must be ignored -- no back/forward-dating.
        r = self._post(self.metrics_url, dict(VALID, timestamp="2000-01-01T00:00:00Z"),
                       token=self.token)
        self.assertEqual(r.status_code, 200)
        m = SystemMetric.objects.get()
        self.assertGreater(m.timestamp.year, 2020)             # server time, not 2000
        self.assertLess((timezone.now() - m.timestamp).total_seconds(), 60)

    def test_unknown_fields_are_ignored_whitelist(self):
        # The field whitelist means an agent can't write arbitrary columns -- not even
        # the primary key.
        r = self._post(self.metrics_url, dict(VALID, id=999999, evil_field="pwn"),
                       token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(SystemMetric.objects.filter(id=999999).exists())   # PK not hijacked
        self.assertFalse(hasattr(SystemMetric.objects.get(), "evil_field"))

    def test_suspended_server_acks_without_storing(self):
        MonitoringConfig.objects.create(server=self.server, enabled=True,
                                        monitoring_suspended=True)
        r = self._post(self.metrics_url, VALID, token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["stored"])
        self.assertEqual(SystemMetric.objects.count(), 0)
        # ...but the token is still a sign of life -> heartbeat updates.
        self.assertTrue(ServerHeartbeat.objects.filter(server=self.server).exists())

    def test_push_updates_heartbeat(self):
        self._post(self.metrics_url, VALID, token=self.token)
        self.assertTrue(ServerHeartbeat.objects.filter(server=self.server).exists())


class SyncEndpointsTests(_Base):
    def test_services_ingest_syncs_rows(self):
        r = self._post(reverse("agent_ingest_services"),
                       {"services": [{"name": "nginx", "status": "running",
                                      "service_type": "systemd"}]}, token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Service.objects.filter(server=self.server, name="nginx").exists())

    def test_containers_ingest_syncs_rows(self):
        r = self._post(reverse("agent_ingest_containers"),
                       {"containers": [{"name": "web", "image": "nginx:latest",
                                        "state": "running"}]}, token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Container.objects.filter(server=self.server, name="web").exists())

    def test_ssh_auth_ingest_stores_events(self):
        r = self._post(reverse("agent_ingest_ssh_auth"),
                       {"events": [{"source_ip": "1.2.3.4", "username": "root",
                                    "success": False, "raw": "Failed password"}]},
                       token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            SSHAuthEvent.objects.filter(server=self.server, source_ip="1.2.3.4").count(), 1)

    def test_ssh_auth_caps_events_per_push(self):
        events = [{"source_ip": f"10.0.0.{i % 256}", "username": "x", "success": False}
                  for i in range(1500)]                     # over the 1000 hard cap
        r = self._post(reverse("agent_ingest_ssh_auth"), {"events": events}, token=self.token)
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(SSHAuthEvent.objects.count(), 1000)

    def test_every_ingest_endpoint_requires_auth(self):
        # The shared _authenticate guards every endpoint, not just metrics.
        for name in ("agent_ingest_services", "agent_ingest_containers",
                     "agent_ingest_ssh_auth", "agent_heartbeat", "agent_ingest_metrics"):
            r = self._post(reverse(name), {"x": 1})         # no token
            self.assertEqual(r.status_code, 401, msg=name)


class PortServiceIngestTests(_Base):
    """Port-detected services carry an optional friendly label (display_name) +
    provenance (detected_via). Identity stays in `name` so a banner flap never
    duplicates rows or detaches the monitoring toggle."""

    def _svc(self, **over):
        base = {"name": "port-80", "status": "running", "service_type": "port",
                "port": 80, "bind_address": "0.0.0.0"}
        base.update(over)
        return self._post(reverse("agent_ingest_services"),
                          {"services": [base], "agent_version": "push-1.6.0"},
                          token=self.token)

    def test_persists_display_name_and_detected_via(self):
        r = self._svc(display_name="nginx (:80)", detected_via="port-banner")
        self.assertEqual(r.status_code, 200)
        s = Service.objects.get(server=self.server, name="port-80")
        self.assertEqual(s.display_name, "nginx (:80)")
        self.assertEqual(s.detected_via, "port-banner")
        self.assertEqual(s.label, "nginx (:80)")

    def test_old_agent_payload_without_new_fields_ok(self):
        # push-1.5.0 shape: no display_name/detected_via -> 200, fields stay NULL,
        # and the label falls back to the server-side role map.
        r = self._post(reverse("agent_ingest_services"),
                       {"services": [{"name": "port-3306", "status": "running",
                                      "service_type": "port", "port": 3306}]},
                       token=self.token)
        self.assertEqual(r.status_code, 200)
        s = Service.objects.get(server=self.server, name="port-3306")
        self.assertIsNone(s.display_name)
        self.assertIsNone(s.detected_via)
        self.assertEqual(s.label, "MySQL (:3306)")

    def test_stable_name_survives_banner_flap(self):
        # First push identifies nginx; operator turns monitoring on.
        self._svc(display_name="nginx (:80)", detected_via="port-banner")
        Service.objects.filter(server=self.server, name="port-80").update(monitoring_enabled=True)
        # Next cycle the banner read fails -> falls back to the role label.
        self._svc(display_name="HTTP (:80)", detected_via="port-map")
        rows = Service.objects.filter(server=self.server, name="port-80")
        self.assertEqual(rows.count(), 1)                      # NOT duplicated
        s = rows.get()
        self.assertTrue(s.monitoring_enabled)                  # toggle preserved
        self.assertEqual(s.display_name, "HTTP (:80)")         # label updated in place


class KpiIngestTests(TestCase):
    """The business KPI ingest endpoint uses a SEPARATE hashed token
    (BusinessMonitorConfig), but the same boundary discipline applies."""

    def setUp(self):
        self.cfg = BusinessMonitorConfig.get_config()
        self.token = self.cfg.generate_token()
        self.kpi = BusinessKPI.objects.create(name="Signups", key="signups", enabled=True)
        self.client = Client()
        self.url = reverse("kpi_ingest")

    def _post(self, body, token=None):
        kw = {"content_type": "application/json"}
        if token is not None:
            kw["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        data = json.dumps(body) if isinstance(body, (dict, list)) else body
        return self.client.post(self.url, data=data, **kw)

    def test_missing_token_rejected_401(self):
        self.assertEqual(self._post({"key": "signups", "value": 1}).status_code, 401)

    def test_wrong_token_rejected_401(self):
        self.assertEqual(self._post({"key": "signups", "value": 1}, token="wrong").status_code, 401)

    def test_valid_push_records_the_value(self):
        r = self._post({"key": "signups", "value": 42}, token=self.token)
        self.assertEqual(r.status_code, 200)
        self.kpi.refresh_from_db()
        self.assertEqual(self.kpi.last_value, 42)

    def test_unknown_key_rejected_404(self):
        self.assertEqual(
            self._post({"key": "nope", "value": 1}, token=self.token).status_code, 404)

    def test_missing_value_rejected_400(self):
        self.assertEqual(self._post({"key": "signups"}, token=self.token).status_code, 400)

    def test_oversized_body_rejected_413(self):
        big = {"key": "signups", "value": 1, "note": "x" * (260 * 1024)}
        self.assertEqual(self._post(big, token=self.token).status_code, 413)


class EveryEndpointBoundaryTests(TestCase):
    """Closes the matrix: prove EVERY push endpoint enforces auth, the 256 KB cap, and
    JSON validity -- directly, per endpoint, not by 'same shared code' equivalence."""

    # (url_name, token-kind) for the body-handling POST endpoints.
    BODY_ENDPOINTS = [
        ("agent_ingest_metrics", "agent"),
        ("agent_ingest_services", "agent"),
        ("agent_ingest_containers", "agent"),
        ("agent_ingest_ssh_auth", "agent"),
        ("kpi_ingest", "biz"),
    ]

    def setUp(self):
        self.server = Server.objects.create(name="vm-a", ip_address="10.4.4.9", username="agent")
        _, self.agent_token = AgentCredential.generate_for_server(self.server)
        self.biz_token = BusinessMonitorConfig.get_config().generate_token()
        self.client = Client()

    def _token(self, kind):
        return self.agent_token if kind == "agent" else self.biz_token

    def test_every_post_endpoint_requires_auth(self):
        for name, _ in self.BODY_ENDPOINTS + [("agent_heartbeat", "agent")]:
            r = self.client.post(reverse(name), data="{}", content_type="application/json")
            self.assertEqual(r.status_code, 401, msg=f"{name} accepted a no-token request")

    def test_ping_requires_auth(self):
        self.assertEqual(self.client.get(reverse("agent_ping")).status_code, 401)

    def test_every_body_endpoint_rejects_oversized_413(self):
        big = "x" * (260 * 1024)                              # > 256 KB, with a valid token
        for name, kind in self.BODY_ENDPOINTS:
            r = self.client.post(reverse(name), data=big, content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {self._token(kind)}")
            self.assertEqual(r.status_code, 413, msg=f"{name} accepted a >256KB body")

    def test_every_body_endpoint_rejects_malformed_json_400(self):
        for name, kind in self.BODY_ENDPOINTS:
            r = self.client.post(reverse(name), data="{not valid json",
                                 content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {self._token(kind)}")
            self.assertEqual(r.status_code, 400, msg=f"{name} accepted malformed JSON")
