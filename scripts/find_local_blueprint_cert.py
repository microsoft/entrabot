"""Recover the registered Blueprint cert thumbprint for this local private key.

Worktree-local setup state can miss BLUEPRINT_CERT_THUMBPRINT even when the
machine already has the private key in the OS credential store and the matching
public cert is still registered on the Blueprint app. In that case setup.sh
should reuse the cert instead of prompting to rotate it.

Usage:
    python scripts/find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>

Output:
    stdout: SHA-256 base64url thumbprint when a matching cert is found
    stderr: human-readable diagnostics
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import sys

import keyring
import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from entra_provisioning import get_graph_token


def _thumbprint_of(cert: x509.Certificate) -> str:
    der = cert.public_bytes(serialization.Encoding.DER)
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()


def _same_public_key(private_key_pem: str, cert: x509.Certificate) -> bool:
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    private_public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    cert_public_der = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_public_der == cert_public_der


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>", file=sys.stderr)
        return 2

    blueprint_obj_id = sys.argv[1]
    private_key_pem = keyring.get_password("entraclaw", "blueprint-private-key")
    if not private_key_pem:
        print("  No local blueprint-private-key found in OS credential store.", file=sys.stderr)
        return 1

    with contextlib.redirect_stdout(sys.stderr):
        token = get_graph_token(wait_for_propagation=False)

    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/applications/{blueprint_obj_id}?$select=keyCredentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    if not resp.ok:
        print(
            f"  Blueprint cert fetch failed ({resp.status_code}); cannot recover thumbprint.",
            file=sys.stderr,
        )
        return 1

    for credential in resp.json().get("keyCredentials", []) or []:
        der_b64 = credential.get("key")
        if not der_b64:
            continue
        cert = x509.load_der_x509_certificate(base64.b64decode(der_b64))
        if _same_public_key(private_key_pem, cert):
            print(_thumbprint_of(cert))
            name = credential.get("displayName") or "unnamed cert"
            print(f"  Recovered local Blueprint cert from Entra: {name}", file=sys.stderr)
            return 0

    print(
        "  Local private key does not match any registered Blueprint cert.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
