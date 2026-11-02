"""Generate this machine's Blueprint certificate and register it on the Blueprint.

setup.sh Step 6 calls this when the machine has no certificate that is still
registered. By default it ADDS to the Blueprint and never removes a certificate.
``--remove-all-existing-certs`` is the only way to replace the list.

The private key's route is what keeps a failure from locking anyone out:

1. It's stored under ``blueprint-private-key.pending`` and read back.
2. The certificate is registered on the Blueprint and confirmed by reading it back.
3. Only then is the key promoted to ``blueprint-private-key``, the account the agent
   signs with, and the pending entry removed.

If nothing was written (the read failed, or Graph refused the update), a key this
run generated is deleted and the real key and the Blueprint are unchanged. If a write
may have landed but can't be confirmed, the pending key is KEPT: after a remove-all it
could be the only key that still matches anything on the Blueprint. The next run
promotes it if its certificate has appeared, and otherwise registers a certificate for
that same key rather than overwriting it.

Only the public certificate leaves this machine. Graph calls use the Provisioner token,
not an az CLI token (Learning #1: those carry Directory.AccessAsUser.All, which Agent
Identity APIs reject).

Usage:
    python scripts/register_blueprint_cert.py \\
        --blueprint-object-id <ID> --blueprint-app-id <APP_ID> --state-file <PATH> \\
        [--remove-all-existing-certs]

Output:
    stdout: ONLY the SHA-256 base64url thumbprint, on success (setup.sh captures it)
    stderr: everything else

Exit codes:
    0 — registered, confirmed, key promoted, state file updated
    1 — not registered, and nothing was changed: a read or token failure, Graph refused
        the update, or the operator declined at the prompt
    2 — usage error
    3 — outcome not confirmed: the Blueprint may have changed; re-run setup
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import keyring
from blueprint_certs import (
    KEYRING_SERVICE,
    PENDING_PRIVATE_KEY_ACCOUNT,
    PRIVATE_KEY_ACCOUNT,
    KeyCredential,
    KeyCredentialsReadError,
    RegistrationRejectedError,
    RegistrationResult,
    build_key_credential,
    device_certificate_display_name,
    fetch_key_credentials,
    find_credential_for_public_key,
    public_key_of_private_pem,
    register_key_credential,
    thumbprint_of_der,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from entra_provisioning import get_bootstrap_graph_token

from entrabot.platform.keyring_backend import assert_allowed_keyring_backend

EXIT_REGISTERED = 0
EXIT_NOT_REGISTERED = 1
EXIT_USAGE = 2  # argparse's own exit code for bad arguments
EXIT_UNCONFIRMED = 3

CERTIFICATE_LIFETIME_DAYS = 365

_RECOVERY_ADVICE = (
    "  The Blueprint may have changed. Re-run setup: a private key still waiting under "
    f"'{PENDING_PRIVATE_KEY_ACCOUNT}' is kept, and is promoted if its certificate is on the "
    "Blueprint."
)


class _NotRegistered(Exception):
    """Stop here: the Blueprint and the real private key are unchanged."""


@dataclass(frozen=True)
class GeneratedCertificate:
    private_key_pem: str = field(repr=False)
    der_bytes: bytes
    thumbprint: str


def _say(message: str = "") -> None:
    print(message, file=sys.stderr)


def _pem_of(private_key: Any) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _issue_certificate(
    private_key: Any, private_key_pem: str, blueprint_app_id: str
) -> GeneratedCertificate:
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"entrabot-blueprint-{blueprint_app_id}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "EntraBot Device Agent"),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=CERTIFICATE_LIFETIME_DAYS))
        .sign(private_key, hashes.SHA256())
    )
    der_bytes = certificate.public_bytes(serialization.Encoding.DER)
    return GeneratedCertificate(
        private_key_pem=private_key_pem,
        der_bytes=der_bytes,
        thumbprint=thumbprint_of_der(der_bytes),
    )


def _delete_pending_key() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, PENDING_PRIVATE_KEY_ACCOUNT)
    except Exception as exc:
        _say(
            f"  Warning: could not remove the pending key entry '{PENDING_PRIVATE_KEY_ACCOUNT}' "
            f"({type(exc).__name__}). The next run deals with it."
        )


def _store_pending_key(private_key_pem: str) -> None:
    try:
        keyring.set_password(KEYRING_SERVICE, PENDING_PRIVATE_KEY_ACCOUNT, private_key_pem)
        stored = keyring.get_password(KEYRING_SERVICE, PENDING_PRIVATE_KEY_ACCOUNT)
    except Exception as exc:
        raise _NotRegistered(
            f"Could not store the new private key in the OS credential store "
            f"({type(exc).__name__})."
        ) from exc
    if stored != private_key_pem:
        _delete_pending_key()
        raise _NotRegistered("The OS credential store did not return the new private key intact.")


def _promote_pending_key(private_key_pem: str) -> None:
    """Make the pending key the one the agent signs with.

    Any failure leaves the pending entry in place for the next run.
    """
    try:
        keyring.set_password(KEYRING_SERVICE, PRIVATE_KEY_ACCOUNT, private_key_pem)
        stored = keyring.get_password(KEYRING_SERVICE, PRIVATE_KEY_ACCOUNT)
    except Exception as exc:
        raise RuntimeError(
            f"The certificate is registered, but its private key could not be stored as "
            f"'{PRIVATE_KEY_ACCOUNT}' ({type(exc).__name__})."
        ) from exc
    if stored != private_key_pem:
        raise RuntimeError(
            f"The certificate is registered, but the OS credential store did not return "
            f"'{PRIVATE_KEY_ACCOUNT}' intact."
        )
    _delete_pending_key()


def _write_state(state_file: Path, thumbprint: str) -> None:
    data = json.loads(state_file.read_text()) if state_file.is_file() else {}
    data["BLUEPRINT_CERT_THUMBPRINT"] = thumbprint
    data.pop("BLUEPRINT_SECRET", None)  # clean up old secret if present
    state_file.write_text(json.dumps(data, indent=2) + "\n")


def _usable_private_key(private_key_pem: str | None) -> Any | None:
    """Load a key left under the pending account by an earlier run, if there is one.

    An entry that isn't a usable private key can't match any certificate, so it can't
    protect anyone and is discarded.
    """
    if not private_key_pem:
        return None
    try:
        return serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except (ValueError, TypeError):
        _say("  Discarding an unusable entry left under the pending key account.")
        _delete_pending_key()
        return None


def _stdin_is_terminal() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except ValueError:  # closed stdin
        return False


def _confirm_remove_all(existing: Sequence[KeyCredential]) -> bool:
    """Show what --remove-all-existing-certs will remove and get consent."""
    _say()
    _say(
        "  --remove-all-existing-certs replaces every certificate on the Blueprint with a new "
        "one for this machine. It does not delete the Blueprint itself."
    )
    if not existing:
        _say("  The Blueprint has no certificates, so there is nothing to remove.")
        return True

    _say(f"  These {len(existing)} certificate(s) will be removed:")
    for credential in existing:
        _say(credential.summary())
    _say("  Every machine using one of them stops authenticating until it runs setup again.")

    if _stdin_is_terminal():
        sys.stderr.write(
            f"  Remove all {len(existing)} and replace them with this machine's new "
            "certificate? [y/N] "
        )
        sys.stderr.flush()
        return sys.stdin.readline().strip().lower() in ("y", "yes")

    _say()
    _say("  " + "=" * 72)
    _say(f"  REMOVING ALL {len(existing)} CERTIFICATE(S) FROM THIS BLUEPRINT.")
    _say("  There is no terminal to confirm on: the flag itself is explicit consent.")
    _say("  " + "=" * 72)
    _say()
    return True


def _report_success(result: RegistrationResult, remove_all: bool) -> None:
    if not remove_all:
        _say(f"  Certificate added. {len(result.prior)} existing certificate(s) kept.")
        return

    _say(f"  Removed {len(result.prior)} certificate(s). The Blueprint itself was not deleted.")
    prior_ids = {credential.key_id for credential in result.prior}
    lingering = [c.display_name for c in result.current if c.key_id in prior_ids]
    if lingering:
        _say(
            "  WARNING: still listed on the Blueprint after the update: "
            f"{', '.join(lingering)}. Check with scripts/list_blueprint_certs.py."
        )


def _run(args: argparse.Namespace, sleep: Callable[[float], None]) -> str:
    object_id = args.blueprint_object_id
    remove_all = args.remove_all_existing_certs

    # Nothing is generated or stored unless all of this works. The backend check comes
    # first: on a host without an OS-native keyring, set_password would otherwise write
    # the private key to disk in cleartext.
    try:
        assert_allowed_keyring_backend()
        token = get_bootstrap_graph_token(wait_for_propagation=False)
        existing = fetch_key_credentials(object_id, token)
        retained_pem = keyring.get_password(KEYRING_SERVICE, PENDING_PRIVATE_KEY_ACCOUNT)
    except Exception as exc:
        raise _NotRegistered(f"Could not prepare to register a certificate: {exc}") from exc

    retained_key = _usable_private_key(retained_pem)
    if retained_key is not None:
        match = find_credential_for_public_key(existing, public_key_of_private_pem(retained_pem))
        if match is not None:
            _say(f"  An earlier run's certificate is on the Blueprint: {match.display_name}")
            _promote_pending_key(retained_pem)
            _write_state(args.state_file, match.thumbprint)
            if not remove_all:
                return match.thumbprint
            retained_key = None

    if remove_all:
        if not _confirm_remove_all(existing):
            raise _NotRegistered("Declined at the prompt.")
    else:
        _say(
            f"  Registering this machine's certificate: adding to {len(existing)} existing "
            "certificate(s). None are removed."
        )

    key_is_new = retained_key is None
    if key_is_new:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_key_pem = _pem_of(private_key)
        _store_pending_key(private_key_pem)
    else:
        # An earlier write of this key's certificate could still appear, so it isn't
        # replaced with a new key until this run has succeeded.
        _say("  Reusing the private key an unconfirmed earlier run left pending.")
        private_key, private_key_pem = retained_key, retained_pem

    certificate = _issue_certificate(private_key, private_key_pem, args.blueprint_app_id)
    new_entry = build_key_credential(certificate.der_bytes, device_certificate_display_name())

    try:
        result = register_key_credential(
            object_id=object_id,
            token=token,
            new_entry=new_entry,
            remove_all_existing=remove_all,
            sleep=sleep,
        )
    except (KeyCredentialsReadError, RegistrationRejectedError) as exc:
        # Nothing was written, so a key generated by this run has no certificate.
        if key_is_new:
            _delete_pending_key()
        raise _NotRegistered(str(exc)) from exc
    # A RegistrationUnconfirmedError passes through to main() with the pending key still
    # in place: this write may have landed.

    _promote_pending_key(certificate.private_key_pem)
    _write_state(args.state_file, result.thumbprint)
    _report_success(result, remove_all)
    return result.thumbprint


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate this machine's Blueprint certificate and register it. "
        "By default this adds to the Blueprint and removes nothing.",
    )
    parser.add_argument("--blueprint-object-id", required=True)
    parser.add_argument("--blueprint-app-id", required=True)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument(
        "--remove-all-existing-certs",
        action="store_true",
        help="Remove EVERY certificate on the Blueprint, on every machine, and replace them "
        "with this machine's new one. Lists them first and asks y/N on a terminal; without a "
        "terminal the flag itself is consent. This does not delete the Blueprint itself.",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _parse_args(argv)
    stdout = sys.stdout

    # setup.sh captures stdout and writes it to .env, so nothing but the thumbprint may
    # reach it, whatever a helper prints (Learning #29).
    with contextlib.redirect_stdout(sys.stderr):
        try:
            thumbprint = _run(args, sleep)
        except _NotRegistered as exc:
            _say(f"  ERROR: {exc} Nothing was changed.")
            return EXIT_NOT_REGISTERED
        except Exception as exc:
            _say(f"  ERROR: {exc}")
            _say(_RECOVERY_ADVICE)
            return EXIT_UNCONFIRMED

    print(thumbprint, file=stdout)
    return EXIT_REGISTERED


if __name__ == "__main__":
    sys.exit(main())
