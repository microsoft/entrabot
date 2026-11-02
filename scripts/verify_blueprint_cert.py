"""Verify a locally-cached Blueprint cert thumbprint is still registered.

setup.sh's cached-thumbprint fast path (Step 6) skips cert regeneration
when BLUEPRINT_CERT_THUMBPRINT is in the state file. But that certificate
may have been removed from the Blueprint since (by an admin, or by
``setup.sh --remove-all-existing-certs`` on another machine), and then the
local private key has no matching public key on the Entra side. Result:
cryptic ``invalid_client`` at Hop 1 instead of a clear "re-run setup.sh"
signal.

This script checks the claim before we trust the cache.

Usage:
    python scripts/verify_blueprint_cert.py <BLUEPRINT_OBJECT_ID> <EXPECTED_THUMBPRINT>

Exit codes:
    0 — thumbprint is present on the Blueprint's keyCredentials (cache is valid)
    1 — checked: thumbprint is NOT present (cache is stale; setup.sh adds a new cert)
    2 — usage error
    3 — could not check (Graph or token failure, or a certificate whose key material
        couldn't be read). This says nothing about whether the thumbprint is
        registered, so setup.sh must stop rather than regenerate.
"""

from __future__ import annotations

import contextlib
import sys

try:
    from blueprint_certs import fetch_key_credentials, unreadable_certificates
    from entra_provisioning import get_existing_graph_token
except ImportError as exc:
    # A traceback would exit 1, which means "checked: not registered".
    print(f"  [could-not-check] Missing dependency: {exc}", file=sys.stderr)
    sys.exit(3)

EXIT_REGISTERED = 0
EXIT_NOT_REGISTERED = 1
EXIT_USAGE = 2
EXIT_COULD_NOT_CHECK = 3


def _check(blueprint_obj_id: str, expected: str) -> int:
    with contextlib.redirect_stdout(sys.stderr):
        token = get_existing_graph_token()

    credentials = fetch_key_credentials(blueprint_obj_id, token)
    if any(credential.thumbprint == expected for credential in credentials):
        return EXIT_REGISTERED

    unreadable = unreadable_certificates(credentials)
    if unreadable:
        print(
            f"  [could-not-check] Thumbprint {expected[:16]}... wasn't found, but the key "
            f"material of {len(unreadable)} certificate(s) couldn't be read, so it may "
            "still be registered.",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_CHECK

    print(
        f"  [cache-desync] cached thumbprint {expected[:16]}... is NOT on the Blueprint "
        f"(found {len(credentials)} other cert(s)).",
        file=sys.stderr,
    )
    return EXIT_NOT_REGISTERED


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: verify_blueprint_cert.py <BLUEPRINT_OBJECT_ID> <EXPECTED_THUMBPRINT>",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        return _check(sys.argv[1], sys.argv[2])
    except Exception as exc:  # a traceback would exit 1, "checked: not registered"
        print(f"  [could-not-check] Could not read the Blueprint: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_CHECK


if __name__ == "__main__":
    sys.exit(main())
