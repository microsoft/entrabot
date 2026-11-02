"""Recover the registered Blueprint cert thumbprint for this local private key.

Worktree-local setup state can miss BLUEPRINT_CERT_THUMBPRINT even when the
machine already has the private key in the OS credential store and the matching
public cert is still registered on the Blueprint app. In that case setup.sh
should reuse the cert instead of registering another one.

Usage:
    python scripts/find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>

Output:
    stdout: SHA-256 base64url thumbprint when a matching cert is found
    stderr: human-readable diagnostics

Exit codes:
    0 — a registered cert matches the local key; its thumbprint is on stdout
    1 — checked: no usable local key, or the local key matches no registered cert
    2 — usage error
    3 — could not check (keystore, Graph, or token failure, or a certificate whose
        key material couldn't be read). setup.sh must stop rather than regenerate.
"""

from __future__ import annotations

import contextlib
import sys

try:
    import keyring
    from blueprint_certs import (
        KEYRING_SERVICE,
        PRIVATE_KEY_ACCOUNT,
        fetch_key_credentials,
        find_credential_for_public_key,
        public_key_of_private_pem,
        unreadable_certificates,
    )
    from entra_provisioning import get_existing_graph_token
except ImportError as exc:
    # A traceback would exit 1, which means "checked: not registered".
    print(f"  Could not check: missing dependency: {exc}", file=sys.stderr)
    sys.exit(3)

EXIT_FOUND = 0
EXIT_NOT_REGISTERED = 1
EXIT_USAGE = 2
EXIT_COULD_NOT_CHECK = 3


def _find(blueprint_obj_id: str) -> int:
    private_key_pem = keyring.get_password(KEYRING_SERVICE, PRIVATE_KEY_ACCOUNT)
    if not private_key_pem:
        print("  No local blueprint-private-key found in OS credential store.", file=sys.stderr)
        return EXIT_NOT_REGISTERED

    try:
        public_key = public_key_of_private_pem(private_key_pem)
    except (ValueError, TypeError):
        # Nothing can authenticate with this key, so there is nothing to recover.
        print("  The local blueprint-private-key isn't a usable private key.", file=sys.stderr)
        return EXIT_NOT_REGISTERED

    with contextlib.redirect_stdout(sys.stderr):
        token = get_existing_graph_token()

    credentials = fetch_key_credentials(blueprint_obj_id, token)
    match = find_credential_for_public_key(credentials, public_key)
    if match is not None:
        print(match.thumbprint)
        print(f"  Recovered local Blueprint cert from Entra: {match.display_name}", file=sys.stderr)
        return EXIT_FOUND

    unreadable = unreadable_certificates(credentials)
    if unreadable:
        print(
            f"  Could not check: the key material of {len(unreadable)} certificate(s) "
            "couldn't be read, so the local key may still match one of them.",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_CHECK

    print("  Local private key does not match any registered Blueprint cert.", file=sys.stderr)
    return EXIT_NOT_REGISTERED


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>", file=sys.stderr)
        return EXIT_USAGE

    try:
        return _find(sys.argv[1])
    except Exception as exc:  # a traceback would exit 1, "checked: not registered"
        print(f"  Could not check the Blueprint's certificates: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_CHECK


if __name__ == "__main__":
    sys.exit(main())
