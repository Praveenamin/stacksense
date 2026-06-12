"""
First-run setup wizard tests.

Covers: setup-required detection, the gate middleware (redirect-all-then-lock, with
machine/health endpoints exempt), the wizard form (valid creates admin + Admin role +
locks; invalid is rejected), one-time lockout, the existing-install safety (a server
with an admin is never gated), and CSRF.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from core.models import AppConfig
from core.permissions import ROLE_ADMIN
from core.setup_views import setup_required

User = get_user_model()

SETUP_URL = reverse("setup")          # /setup/
LOGIN_URL = settings.LOGIN_URL        # /admin/login/

VALID = {
    "username": "owner",
    "email": "owner@x.test",
    "password1": "Str0ng-Passw0rd!42",
    "password2": "Str0ng-Passw0rd!42",
    "base_url": "https://monitor.example.com",
}


class SetupRequiredDetectionTests(TestCase):
    def test_required_on_fresh_install(self):
        self.assertTrue(setup_required())

    def test_not_required_when_active_superuser_exists(self):
        User.objects.create_superuser("a", "a@x.test", "pw")
        self.assertFalse(setup_required())

    def test_not_required_when_flag_set(self):
        AppConfig.objects.update_or_create(id=1, defaults={"setup_completed": True})
        self.assertFalse(setup_required())

    def test_inactive_superuser_does_not_satisfy_setup(self):
        u = User.objects.create_superuser("a", "a@x.test", "pw")
        u.is_active = False
        u.save(update_fields=["is_active"])
        self.assertTrue(setup_required())


class GateMiddlewareTests(TestCase):
    def test_fresh_install_redirects_web_pages_to_setup(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], SETUP_URL)

    def test_setup_page_reachable_when_required(self):
        self.assertEqual(self.client.get(SETUP_URL).status_code, 200)

    def test_health_passes_through_when_required(self):
        self.assertEqual(self.client.get("/health/").status_code, 200)

    def test_machine_api_is_not_gated(self):
        # The agent endpoint must hit its own auth (401), never a 302 to the web wizard.
        r = self.client.post("/api/agent/metrics/", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_wizard_locked_after_setup(self):
        User.objects.create_superuser("a", "a@x.test", "pw")
        r = self.client.get(SETUP_URL)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], LOGIN_URL)

    def test_existing_install_is_never_forced_to_setup(self):
        admin = User.objects.create_superuser("a", "a@x.test", "pw")
        self.client.force_login(admin)
        r = self.client.get("/")
        self.assertNotEqual(r.get("Location"), SETUP_URL)   # not funneled to the wizard


class SetupViewTests(TestCase):
    def test_get_renders_the_form(self):
        r = self.client.get(SETUP_URL)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="username"')
        self.assertContains(r, 'name="base_url"')

    def test_valid_post_creates_admin_assigns_role_and_locks(self):
        r = self.client.post(SETUP_URL, VALID)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], LOGIN_URL)

        u = User.objects.get(username="owner")
        self.assertTrue(u.is_superuser and u.is_active)
        self.assertEqual(u.email, "owner@x.test")
        self.assertEqual(u.acl.role.name, ROLE_ADMIN)        # superuser -> Admin role

        cfg = AppConfig.get_config()
        self.assertTrue(cfg.setup_completed)
        self.assertEqual(cfg.base_url, "https://monitor.example.com")
        self.assertFalse(setup_required())                  # locked now
        self.assertEqual(self.client.get(SETUP_URL).status_code, 302)

    def test_password_mismatch_rejected(self):
        r = self.client.post(SETUP_URL, dict(VALID, password2="Different-Pass!99"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="owner").exists())

    def test_weak_password_rejected(self):
        r = self.client.post(SETUP_URL, dict(VALID, password1="123", password2="123"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="owner").exists())

    def test_invalid_email_rejected(self):
        r = self.client.post(SETUP_URL, dict(VALID, email="not-an-email"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="owner").exists())

    def test_missing_base_url_rejected(self):
        data = dict(VALID)
        data.pop("base_url")
        r = self.client.post(SETUP_URL, data)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="owner").exists())

    def test_duplicate_username_rejected(self):
        User.objects.create_user("owner", "x@x.test", "pw")   # a non-superuser already named owner
        r = self.client.post(SETUP_URL, VALID)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="owner", is_superuser=True).exists())

    def test_one_time_lockout_blocks_a_second_admin(self):
        self.client.post(SETUP_URL, VALID)                    # first admin created
        r = self.client.post(SETUP_URL, dict(VALID, username="intruder", email="i@x.test"))
        self.assertEqual(r.status_code, 302)                  # wizard locked
        self.assertEqual(r["Location"], LOGIN_URL)
        self.assertFalse(User.objects.filter(username="intruder").exists())


class CsrfTests(TestCase):
    def test_post_without_csrf_is_forbidden(self):
        c = Client(enforce_csrf_checks=True)
        r = c.post(SETUP_URL, VALID)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.filter(username="owner").exists())
