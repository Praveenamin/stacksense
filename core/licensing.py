"""StackSense licensing — offline Ed25519-signed license verification + status.

The vendor signs a license with a private key (tools/licensing/); the app verifies it
OFFLINE with the embedded public key below (no phone-home → air-gap safe). The signed
token is `b64url(payload_json) + "." + b64url(signature)`; tampering breaks the signature.

No license installed = EVALUATION mode (permissive, all features) so existing installs and
the test suite are unaffected; real enforcement applies once a valid license is installed.
See the plan: editions (standard/pro) + per-license VM cap + subscription expiry + node-lock.
"""
import base64
import json
from dataclasses import dataclass, field
from datetime import date

from django.conf import settings
from django.core.cache import cache

# Embedded vendor PUBLIC key (base64 of the raw Ed25519 public key). Verify-only; it can
# never mint a license. Overridable via settings.LICENSE_PUBLIC_KEY_B64 (used by tests).
_DEFAULT_PUBLIC_KEY_B64 = "XNZ9kBW/DEW4wwxp4+0YzLPxirueoBg662lE1Z6Clg0="

_VERIFIED_CACHE_KEY = "license_verified_v2"
_VERIFIED_TTL = 300  # 5 min


def _public_key_b64():
    return getattr(settings, "LICENSE_PUBLIC_KEY_B64", _DEFAULT_PUBLIC_KEY_B64)


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class LicenseInfo:
    license_id: str
    licensee: str
    edition: str
    max_servers: int | None
    features: list
    issued: str
    expires: str       # ISO date
    install_id: str
    grace_days: int = 14

    @property
    def expires_date(self):
        try:
            return date.fromisoformat(self.expires)
        except (ValueError, TypeError):
            return None


def verify_blob(blob: str):
    """Verify a signed license token and return LicenseInfo, or None if invalid
    (bad format / bad signature / bad payload). Pure — no DB access."""
    if not blob or "." not in blob:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        payload_b64, sig_b64 = blob.strip().split(".", 1)
        payload = _b64u_decode(payload_b64)
        sig = _b64u_decode(sig_b64)
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(_public_key_b64()))
        try:
            pub.verify(sig, payload)
        except InvalidSignature:
            return None
        data = json.loads(payload.decode("utf-8"))
        return LicenseInfo(
            license_id=str(data.get("license_id", "")),
            licensee=str(data.get("licensee", "")),
            edition=str(data.get("edition", "")).lower(),
            max_servers=data.get("max_servers"),
            features=list(data.get("features") or []),
            issued=str(data.get("issued", "")),
            expires=str(data.get("expires", "")),
            install_id=str(data.get("install_id", "")),
            grace_days=int(data.get("grace_days", 14) or 14),
        )
    except Exception:
        return None


def _stored_blob():
    # Read-only (no get_or_create) so reading the license never issues a write — keeps
    # request query counts stable and avoids a write on the hot per-request path.
    from .models import License
    return (License.objects.filter(id=1).values_list("blob", flat=True).first() or "")


def install_id() -> str:
    """Stable per-install fingerprint (node-lock). Stored once in AppConfig."""
    from .models import AppConfig
    return str(AppConfig.get_config().install_id)


def _verified_info():
    """The installed, signature-verified LicenseInfo (or None), cached briefly."""
    blob = _stored_blob()
    if not blob:
        return None
    cached = cache.get(_VERIFIED_CACHE_KEY)
    if cached is not None and cached.get("blob") == blob:
        return cached.get("info")
    info = verify_blob(blob)
    try:
        cache.set(_VERIFIED_CACHE_KEY, {"blob": blob, "info": info}, _VERIFIED_TTL)
    except Exception:
        pass
    return info


@dataclass
class LicenseStatus:
    state: str                 # none|invalid|valid|expiring|expired_grace|expired
    info: LicenseInfo | None
    server_count: int
    max_servers: int | None    # effective cap (license or eval default; None = unlimited)
    over_limit: bool
    days_left: int | None      # +ve before expiry, -ve after
    node_mismatch: bool
    grace_left: int | None = None   # days remaining in the grace window when expired_grace

    @property
    def is_eval(self):
        return self.state == "none"

    @property
    def read_only(self):
        """True once the app should block config/UI mutations (expired past grace)."""
        return self.state == "expired"


def current_license(server_count: int | None = None) -> LicenseStatus:
    """Compute the live license status (cheap; verification is cached)."""
    from .models import Server
    if server_count is None:
        server_count = Server.objects.count()

    info = _verified_info()
    blob = _stored_blob()

    if not blob:
        # Unlicensed -> evaluation mode.
        cap = getattr(settings, "LICENSE_EVAL_MAX_SERVERS", None)
        return LicenseStatus(
            state="none", info=None, server_count=server_count, max_servers=cap,
            over_limit=(cap is not None and server_count > cap),
            days_left=None, node_mismatch=False)
    if info is None:
        # A blob is stored but doesn't verify (tampered/corrupt) -> treat as invalid.
        return LicenseStatus(
            state="invalid", info=None, server_count=server_count, max_servers=0,
            over_limit=True, days_left=None, node_mismatch=False)

    today = date.today()
    exp = info.expires_date
    days_left = (exp - today).days if exp else None
    state, grace_left = "valid", None
    warn_days = int(getattr(settings, "LICENSE_EXPIRY_WARN_DAYS", 14))
    if days_left is not None:
        if days_left < 0:
            past = -days_left
            if past <= info.grace_days:
                state, grace_left = "expired_grace", info.grace_days - past
            else:
                state = "expired"
        elif days_left <= warn_days:
            state = "expiring"

    over_limit = info.max_servers is not None and server_count > info.max_servers
    node_mismatch = bool(info.install_id) and info.install_id != install_id()
    return LicenseStatus(
        state=state, info=info, server_count=server_count,
        max_servers=info.max_servers, over_limit=over_limit,
        days_left=days_left, node_mismatch=node_mismatch, grace_left=grace_left)


def has_feature(name: str) -> bool:
    """Is a (Pro-gated) feature available under the current license? Eval = all on."""
    st = current_license()
    if st.is_eval:
        return bool(getattr(settings, "LICENSE_EVAL_ALL_FEATURES", True))
    if st.info is None:        # invalid license -> nothing
        return False
    return name in st.info.features


def require_feature(name, message=None, redirect_to="monitoring_dashboard"):
    """View decorator: deny access to a Pro-gated feature unless the license grants it
    (eval mode grants all). Redirects with a message for page views."""
    from functools import wraps

    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not has_feature(name):
                from django.contrib import messages
                from django.shortcuts import redirect
                messages.error(request, message or
                               "This feature requires the Pro edition. Upgrade your license.")
                return redirect(redirect_to)
            return view(request, *args, **kwargs)
        return wrapped
    return deco


def can_add_server():
    """(allowed, reason) for creating a new monitored server under the current license."""
    st = current_license()
    if st.state == "expired":
        return False, "License expired — renew to add servers."
    if st.state == "invalid":
        return False, "License invalid — install a valid license to add servers."
    cap = st.max_servers
    if cap is None:
        return True, ""
    if st.server_count >= cap:
        return False, (f"Server limit reached ({st.server_count}/{cap}) for this license. "
                       f"Remove a server or upgrade your plan.")
    return True, ""
