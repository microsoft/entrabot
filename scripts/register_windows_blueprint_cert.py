"""Register this machine's Windows Blueprint certificate on the Blueprint.

setup-windows.ps1 Step 6 passes the public DER of a certificate in Cert:\\CurrentUser\\My.
The certificate is ADDED and every other machine's is kept (#124); the write counts only
once it has been read back. Graph calls use the Provisioner token, never an az CLI token
(Learning #1). Everything is written to stderr; stdout stays empty.

Usage: python scripts/register_windows_blueprint_cert.py --blueprint-object-id <ID> --der <PATH>

Exit codes:
    0 - registered and confirmed
    1 - not registered, and nothing was changed: a token or read failure, or Graph refused
    2 - usage error, including a DER file that is missing, unreadable, or not a certificate
    3 - not confirmed: the Blueprint may have changed; re-run setup
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from blueprint_certs import (
    KeyCredentialsReadError,
    RegistrationRejectedError,
    build_key_credential,
    device_certificate_display_name,
    register_key_credential,
)
from entra_provisioning import get_bootstrap_graph_token

EXIT_REGISTERED, EXIT_NOT_REGISTERED, EXIT_USAGE, EXIT_UNCONFIRMED = 0, 1, 2, 3


def _say(message: str) -> None:
    print(message, file=sys.stderr)


def main(argv: Sequence[str] | None = None, *, sleep: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(description="Add this machine's certificate to the Blueprint.")
    parser.add_argument("--blueprint-object-id", required=True)
    parser.add_argument("--der", required=True, type=Path, help="the certificate's public DER")
    args = parser.parse_args(argv)

    try:
        new_entry = build_key_credential(args.der.read_bytes(), device_certificate_display_name())
    except (OSError, ValueError) as exc:
        _say(f"  ERROR: {args.der} is not a readable DER certificate ({type(exc).__name__}).")
        return EXIT_USAGE

    with contextlib.redirect_stdout(sys.stderr):  # the token helper prints progress to stdout
        try:
            token = get_bootstrap_graph_token(wait_for_propagation=False)
        except Exception as exc:
            _say(f"  ERROR: could not get a Graph token ({exc}). Nothing was changed.")
            return EXIT_NOT_REGISTERED
        try:
            result = register_key_credential(
                object_id=args.blueprint_object_id, token=token, new_entry=new_entry, sleep=sleep
            )
        except (KeyCredentialsReadError, RegistrationRejectedError) as exc:
            _say(f"  ERROR: {exc}. Nothing was changed.")
            return EXIT_NOT_REGISTERED
        except Exception as exc:  # the write may have landed, so don't claim otherwise
            _say(f"  ERROR: {exc}")
            _say("  The Blueprint may have changed. Re-run setup: it checks the Blueprint again.")
            return EXIT_UNCONFIRMED

    _say(f"  Certificate added. {len(result.prior)} existing certificate(s) kept.")
    return EXIT_REGISTERED


if __name__ == "__main__":
    sys.exit(main())
