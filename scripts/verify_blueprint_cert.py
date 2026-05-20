"""Verify a locally-cached Blueprint cert thumbprint is still registered.

setup.sh's cached-thumbprint fast path (Step 6) skips cert regeneration
when BLUEPRINT_CERT_THUMBPRINT is in the state file. But if a teammate
or another machine ran setup.sh since, that thumbprint may have been
replaced on the Blueprint app, and the local Keychain private key no
longer has a matching public key on the Entra side. Result: cryptic
``invalid_client`` at Hop 1 instead of a clear "re-run setup.sh" signal.

This script checks the claim before we trust the cache.

Usage:
    python scripts/verify_blueprint_cert.py <BLUEPRINT_OBJECT_ID> <EXPECTED_THUMBPRINT>

Exit codes:
    0 — thumbprint is present on the Blueprint's keyCredentials (cache is valid)
    1 — thumbprint NOT present (cache is stale; setup.sh should regenerate)
    2 — usage error
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import sys

import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from entra_provisioning import get_existing_graph_token


def _thumbprint_of(der_key_b64: str) -> str:
    """Compute SHA-256 base64url-no-pad thumbprint from a base64-DER cert."""
    der = base64.b64decode(der_key_b64)
    cert = x509.load_der_x509_certificate(der)
    der_cert = cert.public_bytes(serialization.Encoding.DER)
    return base64.urlsafe_b64encode(hashlib.sha256(der_cert).digest()).rstrip(b"=").decode()


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: verify_blueprint_cert.py <BLUEPRINT_OBJECT_ID> <EXPECTED_THUMBPRINT>",
            file=sys.stderr,
        )
        return 2

    blueprint_obj_id, expected = sys.argv[1], sys.argv[2]

    with contextlib.redirect_stdout(sys.stderr):
        token = get_existing_graph_token()

    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/applications/{blueprint_obj_id}?$select=keyCredentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    if not resp.ok:
        print(
            f"  [warn] Blueprint fetch failed ({resp.status_code}); assuming cache stale",
            file=sys.stderr,
        )
        return 1

    creds = resp.json().get("keyCredentials", []) or []
    for c in creds:
        der_b64 = c.get("key")
        if not der_b64:
            continue
        try:
            if _thumbprint_of(der_b64) == expected:
                return 0
        except Exception:
            continue

    print(
        f"  [cache-desync] cached thumbprint {expected[:16]}... is NOT on the Blueprint "
        f"(found {len(creds)} other cert(s) — someone else re-ran setup.sh since).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
