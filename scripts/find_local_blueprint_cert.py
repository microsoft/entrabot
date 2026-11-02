"""Find the registered Blueprint cert thumbprint for this local private key.

This is how setup.sh decides whether to reuse a certificate. It never trusts the
thumbprint cached in the state file: that can be stale, or can belong to an earlier key
(after an unconfirmed ``--remove-all-existing-certs``, or a failed state write). Only a
match between the local private key and a certificate registered on the Blueprint counts,
and the thumbprint printed is that certificate's, so setup.sh can reuse it instead of
registering another one and correct a stale cache at the same time.

A private key waiting under ``blueprint-private-key.pending`` is checked for first. It
means an earlier run didn't finish, and register_blueprint_cert.py is the one place that
reconciles it, so this script exits 1 without asking Graph anything and setup.sh goes there.

Usage:
    python scripts/find_local_blueprint_cert.py <BLUEPRINT_OBJECT_ID>

Output:
    stdout: SHA-256 base64url thumbprint when a matching cert is found
    stderr: human-readable diagnostics

Exit codes:
    0 — a registered cert matches the local key; its thumbprint is on stdout
    1 — checked: a pending private key exists, there is no usable local key, or the local
        key matches no registered cert
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
        PENDING_PRIVATE_KEY_ACCOUNT,
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
    # First, before the real key or Graph: a pending key is unfinished business that only
    # register_blueprint_cert.py reconciles. Failing to read the entry is not the same as
    # there being none.
    try:
        pending = keyring.get_password(KEYRING_SERVICE, PENDING_PRIVATE_KEY_ACCOUNT)
    except Exception as exc:
        print(
            f"  Could not check for a pending private key ({type(exc).__name__}: {exc}).",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_CHECK
    if pending is not None:
        print(
            f"  A private key is waiting under '{PENDING_PRIVATE_KEY_ACCOUNT}' from a run that "
            "didn't finish; register_blueprint_cert.py reconciles it.",
            file=sys.stderr,
        )
        return EXIT_NOT_REGISTERED

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
