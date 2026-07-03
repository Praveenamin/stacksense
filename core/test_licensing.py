"""Licensing: offline verification + enforcement (server cap, edition feature gates,
License admin page). Uses a TEST Ed25519 keypair (settings override) so it never depends
on the real vendor private key."""
import base64
import json
from datetime import date, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Server, License, UserACL, AppConfig
from core import licensing

User = get_user_model()
_PRO = ["windows", "executive", "ai", "security", "business"]


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class LicensingTests(TestCase):
    def setUp(self):
        self.priv = Ed25519PrivateKey.generate()
        pub_raw = self.priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ov = override_settings(LICENSE_PUBLIC_KEY_B64=base64.b64encode(pub_raw).decode(),
                               LICENSE_EVAL_MAX_SERVERS=None)
        ov.enable()
        self.addCleanup(ov.disable)
        cache.delete("license_verified_v2")
        self.addCleanup(cache.delete, "license_verified_v2")
        self.admin = User.objects.create_superuser("licadm", "l@x.test", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.iid = licensing.install_id()

    def _mint(self, edition="pro", max_servers=100, expires="2030-01-01",
              install_id=None, features=None):
        payload = {
            "license_id": "t", "licensee": "Test Co", "edition": edition,
            "max_servers": max_servers,
            "features": _PRO if (features is None and edition == "pro") else (features or []),
            "issued": "2024-01-01", "expires": expires,
            "install_id": self.iid if install_id is None else install_id, "grace_days": 14,
        }
        pj = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return _b64u(pj) + "." + _b64u(self.priv.sign(pj))

    def _install(self, blob):
        lic = License.get(); lic.blob = blob; lic.save()

    def _mk_servers(self, n):
        for i in range(n):
            Server.objects.create(name=f"s{i}", ip_address=f"10.0.0.{i + 1}", username="a")

    # --- verification ---
    def test_valid_license_parses(self):
        info = licensing.verify_blob(self._mint(edition="pro", max_servers=50))
        self.assertIsNotNone(info)
        self.assertEqual(info.edition, "pro")
        self.assertEqual(info.max_servers, 50)

    def test_tampered_rejected(self):
        self.assertIsNone(licensing.verify_blob(self._mint()[:-3] + "xxx"))

    # --- eval (no license) ---
    def test_eval_allows_and_all_features(self):
        self.assertEqual(licensing.current_license().state, "none")
        self.assertTrue(licensing.can_add_server()[0])
        self.assertTrue(licensing.has_feature("windows"))

    # --- server cap ---
    def test_cap_blocks_over_limit(self):
        self._install(self._mint(edition="standard", max_servers=2))
        self._mk_servers(2)
        self.assertFalse(licensing.can_add_server()[0])
        self.client.post(reverse("add_server_agent"),
                         {"name": "extra", "ip_address": "10.9.9.9", "os_type": "linux"})
        self.assertEqual(Server.objects.count(), 2)        # 3rd was blocked

    def test_under_cap_allows(self):
        self._install(self._mint(edition="standard", max_servers=5))
        self._mk_servers(2)
        self.assertTrue(licensing.can_add_server()[0])

    # --- edition feature gates ---
    def test_windows_blocked_on_standard(self):
        self._install(self._mint(edition="standard", max_servers=10))
        self.assertFalse(licensing.has_feature("windows"))
        self.client.post(reverse("add_server_agent"),
                         {"name": "win1", "ip_address": "10.0.0.50", "os_type": "windows"})
        self.assertFalse(Server.objects.filter(name="win1").exists())

    def test_windows_allowed_on_pro(self):
        self._install(self._mint(edition="pro", max_servers=10))
        self.assertTrue(licensing.has_feature("windows"))
        self.client.post(reverse("add_server_agent"),
                         {"name": "win1", "ip_address": "10.0.0.50", "os_type": "windows"})
        self.assertTrue(Server.objects.filter(name="win1").exists())

    def test_executive_feature_by_edition(self):
        self._install(self._mint(edition="standard", max_servers=10))
        self.assertFalse(licensing.has_feature("executive"))
        self._install(self._mint(edition="pro", max_servers=10))
        self.assertTrue(licensing.has_feature("executive"))

    # --- License admin page ---
    def test_page_installs_valid_and_rejects_garbage(self):
        self.client.post(reverse("license_admin"),
                         {"license_blob": self._mint(edition="pro", max_servers=42)})
        self.assertEqual(License.get().edition, "pro")
        self.assertEqual(licensing.current_license().max_servers, 42)
        self.client.post(reverse("license_admin"), {"license_blob": "not-a-license"})
        self.assertEqual(License.get().edition, "pro")     # unchanged by garbage

    def test_page_admin_only(self):
        op = User.objects.create_user("op", "o@x.test", "pw", is_staff=True)
        UserACL.get_or_create_for_user(op)                 # role=None -> no capabilities
        c = Client(); c.force_login(op)
        self.assertNotEqual(c.get(reverse("license_admin")).status_code, 200)

    # --- expiry lifecycle: read-only degrade (Phase 3) ---
    def test_expired_past_grace_is_read_only(self):
        exp = (date.today() - timedelta(days=40)).isoformat()   # past 14-day grace
        self._install(self._mint(edition="pro", max_servers=10, expires=exp))
        st = licensing.current_license()
        self.assertEqual(st.state, "expired")
        self.assertTrue(st.read_only)
        # A mutating UI POST is blocked (read-only) and changes nothing.
        r = self.client.post(reverse("add_server_agent"),
                             {"name": "ro", "ip_address": "10.7.7.7", "os_type": "linux"})
        self.assertEqual(r.status_code, 302)               # redirected, not processed
        self.assertFalse(Server.objects.filter(name="ro").exists())
        # Reads still work.
        self.assertEqual(self.client.get(reverse("monitoring_dashboard")).status_code, 200)
        # The License page still accepts a renewed license (recovery path stays open).
        self.client.post(reverse("license_admin"),
                         {"license_blob": self._mint(edition="pro", max_servers=10,
                                                     expires="2030-01-01")})
        self.assertEqual(licensing.current_license().state, "valid")

    def test_grace_period_is_not_read_only(self):
        exp = (date.today() - timedelta(days=3)).isoformat()    # within 14-day grace
        self._install(self._mint(edition="pro", max_servers=10, expires=exp))
        st = licensing.current_license()
        self.assertEqual(st.state, "expired_grace")
        self.assertFalse(st.read_only)
        # Not read-only: a mutating POST is NOT blocked (cap/edition allow it).
        self.client.post(reverse("add_server_agent"),
                         {"name": "grace", "ip_address": "10.7.7.8", "os_type": "linux"})
        self.assertTrue(Server.objects.filter(name="grace").exists())

    def test_ingest_not_blocked_while_read_only(self):
        exp = (date.today() - timedelta(days=40)).isoformat()
        self._install(self._mint(edition="pro", max_servers=10, expires=exp))
        self.assertTrue(licensing.current_license().read_only)
        # Agent ingest must NOT hit the read-only block (data keeps flowing). It fails
        # its own token auth, but must not be the license read-only 403.
        r = self.client.post(reverse("agent_ingest_metrics"),
                             data="{}", content_type="application/json")
        self.assertNotEqual(r.status_code, 302)            # not the read-only redirect
        try:
            self.assertNotIn("read-only", (r.json().get("error") or "").lower())
        except ValueError:
            pass


@override_settings(LICENSE_TRIAL_DAYS=7)
class TrialTests(TestCase):
    """A fresh, unlicensed install with LICENSE_TRIAL_DAYS>0 gets an N-day trial (fully
    permissive) that degrades to read-only when it lapses. Trial anchor = AppConfig.created_at.
    LICENSE_TRIAL_DAYS=0 (the default) keeps the old unlimited evaluation."""

    def setUp(self):
        cache.delete("license_verified_v2")
        self.addCleanup(cache.delete, "license_verified_v2")
        self.admin = User.objects.create_superuser("trialadm", "tr@x.test", "pw")
        self.client = Client(); self.client.force_login(self.admin)

    def _set_install_age(self, days):
        cfg = AppConfig.get_config()
        AppConfig.objects.filter(pk=cfg.pk).update(
            created_at=timezone.now() - timedelta(days=days))

    def test_active_trial_is_permissive(self):
        self._set_install_age(0)
        st = licensing.current_license()
        self.assertEqual(st.state, "trial")
        self.assertEqual(st.days_left, 7)
        self.assertFalse(st.read_only)
        self.assertTrue(licensing.can_add_server()[0])
        self.assertTrue(licensing.has_feature("windows"))     # trial unlocks everything

    def test_trial_counts_down(self):
        self._set_install_age(5)
        st = licensing.current_license()
        self.assertEqual(st.state, "trial")
        self.assertEqual(st.days_left, 2)

    def test_expired_trial_is_read_only(self):
        self._set_install_age(10)                              # past the 7-day trial
        st = licensing.current_license()
        self.assertEqual(st.state, "trial_expired")
        self.assertTrue(st.read_only)
        self.assertFalse(licensing.can_add_server()[0])
        self.assertFalse(licensing.has_feature("windows"))
        # a mutating UI POST is blocked; reads still work
        r = self.client.post(reverse("add_server_agent"),
                             {"name": "tx", "ip_address": "10.7.7.7", "os_type": "linux"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Server.objects.filter(name="tx").exists())
        self.assertEqual(self.client.get(reverse("monitoring_dashboard")).status_code, 200)

    @override_settings(LICENSE_TRIAL_DAYS=0)
    def test_disabled_is_unlimited_eval(self):
        self._set_install_age(999)                            # old install, but trial off
        st = licensing.current_license()
        self.assertEqual(st.state, "none")                    # unlimited evaluation, not read-only
        self.assertFalse(st.read_only)
        self.assertTrue(licensing.can_add_server()[0])
        self.assertTrue(licensing.has_feature("windows"))
