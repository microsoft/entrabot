"""Register this machine's Blueprint certificate on the Blueprint.

The setup wizard and certificate rotation use this on Windows, macOS and Linux (the file
keeps the name it had when only Windows used it). The certificate is named by its SHA-1
thumbprint (``--sha1``) and its public half is read in-process from this account's
certificate store: Cert:\\CurrentUser\\My on Windows, the Keychain or keyring item on macOS
and Linux. No file is written. ``--der`` takes a DER file instead, for callers that already
have one.
The certificate is ADDED and every other machine's is kept (#124); the write counts only
once it has been read back. ``--remove-all-existing-certs`` is the one exception: the new
certificate REPLACES every certificate on the Blueprint, other machines' included. It never
asks: the caller (setup) has asked the person, and lists here what goes.
Graph calls use the Provisioner token, never an az CLI token (Learning #1). Everything is
written to stderr; stdout stays empty, and the thumbprint is never echoed.

Usage:
    python scripts/register_windows_blueprint_cert.py --blueprint-object-id <ID> --sha1 <HEX>
    python scripts/register_windows_blueprint_cert.py --blueprint-object-id <ID> --der <PATH>
        [--remove-all-existing-certs]

Exit codes:
    0 - registered and confirmed
    1 - not registered, and nothing was changed: a token or read failure, or Graph refused
    2 - usage error, including a DER file that is missing, unreadable, or not a certificate,
        and a thumbprint that isn't in this user's certificate store
    3 - not confirmed: the Blueprint may have changed; re-run setup
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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


def _cert_store() -> Any:
    """This system's certificate store: the same functions on every one."""
    if sys.platform == "win32":
        from entrabot.platform import wincert

        return wincert
    from entrabot.platform import keycert

    return keycert


def _der_from_store(sha1: str) -> bytes | None:
    """The public DER of a certificate in this user's store, or None after saying why."""
    wincert = _cert_store()
    where = "Cert:\\CurrentUser\\My" if sys.platform == "win32" else "the Keychain or keyring"

    try:
        cert = wincert.find_cert(sha1)
    except wincert.WinCertError as exc:
        _say(f"  ERROR: could not read the certificate store ({exc}).")
        return None
    if cert is None:
        _say(
            "  ERROR: that certificate is not in this user's certificate store "
            f"({where})."
        )
        return None
    return cert.der


def main(argv: Sequence[str] | None = None, *, sleep: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(description="Add this machine's certificate to the Blueprint.")
    parser.add_argument("--blueprint-object-id", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--der", type=Path, help="the certificate's public DER")
    source.add_argument(
        "--sha1",
        help="SHA-1 thumbprint of a certificate in this account's certificate store "
        "(Cert:\\CurrentUser\\My on Windows, the Keychain or keyring elsewhere)",
    )
    parser.add_argument(
        "--remove-all-existing-certs",
        action="store_true",
        help="REPLACE every certificate on the Blueprint, other machines' included, with "
        "this one. Never asks: the caller has.",
    )
    args = parser.parse_args(argv)
    replace_all = args.remove_all_existing_certs

    if args.sha1:
        der_bytes = _der_from_store(args.sha1)
        if der_bytes is None:
            return EXIT_USAGE
        described = "The certificate in the certificate store"
    else:
        try:
            der_bytes = args.der.read_bytes()
        except OSError as exc:
            _say(f"  ERROR: {args.der} is not a readable DER certificate ({type(exc).__name__}).")
            return EXIT_USAGE
        described = str(args.der)

    try:
        new_entry = build_key_credential(der_bytes, device_certificate_display_name())
    except ValueError as exc:
        _say(f"  ERROR: {described} is not a readable DER certificate ({type(exc).__name__}).")
        return EXIT_USAGE

    with contextlib.redirect_stdout(sys.stderr):  # the token helper prints progress to stdout
        try:
            token = get_bootstrap_graph_token(wait_for_propagation=False)
        except Exception as exc:
            _say(f"  ERROR: could not get a Graph token ({exc}). Nothing was changed.")
            return EXIT_NOT_REGISTERED
        try:
            result = register_key_credential(
                object_id=args.blueprint_object_id,
                token=token,
                new_entry=new_entry,
                remove_all_existing=replace_all,
                sleep=sleep,
            )
        except (KeyCredentialsReadError, RegistrationRejectedError) as exc:
            _say(f"  ERROR: {exc}. Nothing was changed.")
            return EXIT_NOT_REGISTERED
        except Exception as exc:  # the write may have landed, so don't claim otherwise
            _say(f"  ERROR: {exc}")
            _say("  The Blueprint may have changed. Re-run setup: it checks the Blueprint again.")
            return EXIT_UNCONFIRMED

    if replace_all:
        _say(f"  Certificate added. {len(result.prior)} existing certificate(s) removed:")
        for credential in result.prior:
            _say(credential.summary())  # the name and expiry only
    else:
        _say(f"  Certificate added. {len(result.prior)} existing certificate(s) kept.")
    return EXIT_REGISTERED


if __name__ == "__main__":
    sys.exit(main())
