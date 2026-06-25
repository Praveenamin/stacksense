#!/usr/bin/env python3
"""VENDOR-ONLY. Mint a signed StackSense license from the private key.

Usage:
  python tools/licensing/gen_license.py \
      --licensee "Acme Corp" --edition pro --max-servers 100 \
      --expires 2027-06-25 --install-id <customer-install-id>

Outputs the signed license string the customer pastes into the License page.
Edition feature defaults are defined below (easy to change). The customer's Install ID
comes from their License page (node-lock). Never run this on a customer machine.
"""
import argparse
import base64
import json
import os
import sys
from datetime import date, datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
PRIV_PATH = os.environ.get("STACKSENSE_LICENSE_KEY", os.path.join(HERE, "license_private_key.pem"))

# Edition -> feature set (the gated Pro-only capabilities). Adjust freely.
EDITION_FEATURES = {
    "standard": [],
    "pro": ["windows", "executive", "ai", "security", "business"],
}


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--licensee", required=True)
    ap.add_argument("--edition", required=True, choices=sorted(EDITION_FEATURES))
    ap.add_argument("--max-servers", type=int, required=True)
    ap.add_argument("--expires", required=True, help="YYYY-MM-DD")
    ap.add_argument("--install-id", required=True, help="customer's Install ID (node-lock)")
    ap.add_argument("--grace-days", type=int, default=14)
    ap.add_argument("--license-id", default=None)
    args = ap.parse_args()

    try:
        date.fromisoformat(args.expires)
    except ValueError:
        sys.exit("--expires must be YYYY-MM-DD")

    if not os.path.exists(PRIV_PATH):
        sys.exit(f"Private key not found at {PRIV_PATH}. Run gen_keypair.py first "
                 f"or set STACKSENSE_LICENSE_KEY.")
    with open(PRIV_PATH, "rb") as f:
        priv = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        sys.exit("Key is not an Ed25519 private key.")

    payload = {
        "license_id": args.license_id or _b64u(os.urandom(9)),
        "licensee": args.licensee,
        "edition": args.edition,
        "max_servers": args.max_servers,
        "features": EDITION_FEATURES[args.edition],
        "issued": datetime.now(timezone.utc).date().isoformat(),
        "expires": args.expires,
        "install_id": args.install_id,
        "grace_days": args.grace_days,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = priv.sign(payload_json)
    blob = _b64u(payload_json) + "." + _b64u(sig)

    print(blob)


if __name__ == "__main__":
    main()
