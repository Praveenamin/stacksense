"""
First-run setup wizard.

A fresh install comes up with NO admin. Until one exists, SetupGateMiddleware funnels
every request to /setup/, where this view creates the initial administrator (+ the site
URL) and then permanently locks the wizard. Existing installs (which already have a
superuser) skip it entirely, so this never appears on a server that's already running.
"""
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

User = get_user_model()


def setup_required():
    """True when the first-run wizard still needs to run.

    Cheap and stateless (test-safe): for a normal running install the first EXISTS
    short-circuits to False. Setup is 'done' once either a flag is set OR any active
    superuser exists -- so it can never re-open on an install that already has an admin.
    """
    if User.objects.filter(is_superuser=True, is_active=True).exists():
        return False
    from .models import AppConfig
    return not AppConfig.objects.filter(id=1, setup_completed=True).exists()


class SetupForm(forms.Form):
    username = forms.CharField(max_length=150, label="Admin username")
    email = forms.EmailField(label="Admin email")
    password1 = forms.CharField(widget=forms.PasswordInput, label="Password", strip=False)
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirm password", strip=False)
    base_url = forms.URLField(
        label="Public URL",
        help_text="The address people use to reach this instance, e.g. https://monitor.example.com",
    )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords don't match.")
        if p1:
            try:
                validate_password(p1)            # Django's strength validators
            except ValidationError as e:
                self.add_error("password1", e)
        return cleaned


@require_http_methods(["GET", "POST"])
def setup_view(request):
    # Locked once setup is done -- never expose the wizard on a configured instance.
    if not setup_required():
        return redirect(settings.LOGIN_URL)

    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from .models import AppConfig, UserACL
        with transaction.atomic():
            user = User.objects.create_superuser(
                username=form.cleaned_data["username"],
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password1"],
            )
            UserACL.get_or_create_for_user(user)   # superuser -> Admin role
            cfg = AppConfig.get_config()
            cfg.base_url = form.cleaned_data["base_url"]
            cfg.setup_completed = True
            cfg.save()
        messages.success(request, "Setup complete — please sign in with your new admin account.")
        return redirect(settings.LOGIN_URL)

    return render(request, "core/setup.html", {"form": form})
