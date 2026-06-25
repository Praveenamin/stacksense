#!/usr/bin/env python3
"""VENDOR-ONLY, run ONCE. Generate the Ed25519 signing keypair for StackSense licensing.

- Prints the PUBLIC key (base64) to embed in core/licensing.py (LICENSE_PUBLIC_KEY_B64).
- Writes the PRIVATE key to tools/licensing/license_private_key.pem (gitignored via *.pem).

SECURITY: the private key mints licenses. Keep it OFF customer machines and OUT of git
(move it into a password manager / vault). Anyone with it can issue licenses. This script
is never shipped to customers.
"""
import base64
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
PRIV_PATH = os.path.join(HERE, "license_private_key.pem")


def main():
    if os.path.exists(PRIV_PATH):
        sys.exit(f"Refusing to overwrite existing private key at {PRIV_PATH}")

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    # Private key -> PEM file (gitignored).
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(PRIV_PATH, "wb") as f:
        f.write(pem)
    os.chmod(PRIV_PATH, 0o600)

    # Public key -> raw 32 bytes -> base64 (the constant to embed in the app).
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")

    print("Private key written to:", PRIV_PATH, "(gitignored — keep it secret)")
    print()
    print("Embed this in core/licensing.py as LICENSE_PUBLIC_KEY_B64:")
    print(pub_b64)


if __name__ == "__main__":
    main()
