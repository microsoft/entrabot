"""Check whether a Blueprint certificate thumbprint is registered. An operator tool.

setup.sh does not call this. It used to, to vouch for the thumbprint cached in the state
file, but a thumbprint that is on the Blueprint says nothing about whether THIS machine's
private key matches it, and the cache can be stale or belong to an earlier key. setup.sh
relies on find_local_blueprint_cert.py instead, which proves the local key matches a
registered certificate.

Use this for the narrower question: is a particular thumbprint (the one in .env or in the
state file, say) still on the Blueprint? A certificate can be removed since by an admin,
or by ``setup.sh --remove-all-existing-certs`` on another machine, and then the key paired
with it fails at Hop 1 with a cryptic ``invalid_client``.

Usage:
    python scripts/verify_blueprint_cert.py <BLUEPRINT_OBJECT_ID> <EXPECTED_THUMBPRINT>

Exit codes:
    0 — thumbprint is present on the Blueprint's keyCredentials
    1 — checked: thumbprint is NOT present (re-run setup.sh to add a certificate)
    2 — usage error
    3 — could not check (Graph or token failure, or a certificate whose key material
        couldn't be read). This says nothing about whether the thumbprint is registered.
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
