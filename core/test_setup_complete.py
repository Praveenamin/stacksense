"""First-run setup completion screen: after creating the admin it guides the operator to
licensing (Install ID + trial status + where to install the license), then to sign in."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core import licensing

User = get_user_model()


class SetupCompleteTests(TestCase):
    def _post(self):
        return self.client.post(reverse("setup"), {
            "username": "admin1", "email": "a@x.test",
            "password1": "Zx9-kwPq-72mn", "password2": "Zx9-kwPq-72mn",
            "base_url": "https://monitor.example.com",
        })

    @override_settings(LICENSE_TRIAL_DAYS=7)
    def test_completion_guides_to_license_with_trial(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)                 # completion screen, not a redirect
        html = r.content.decode()
        self.assertIn("Setup complete", html)
        self.assertIn("Install ID", html)
        self.assertIn(licensing.install_id(), html)          # the real Install ID is shown
        self.assertIn("Settings", html)                      # points to Settings -> License
        self.assertIn("trial", html.lower())                 # trial note (LICENSE_TRIAL_DAYS=7)
        self.assertTrue(User.objects.filter(username="admin1", is_superuser=True).exists())

    @override_settings(LICENSE_TRIAL_DAYS=0)
    def test_completion_shows_eval_when_no_trial(self):
        html = self._post().content.decode()
        self.assertIn("Setup complete", html)
        self.assertIn("evaluation mode", html.lower())       # no trial -> eval note
