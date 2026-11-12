#!/usr/bin/env python3
"""
Shared helpers for Entra Graph provisioning.

Centralizes the dedicated "EntraBot Agent ID Provisioner" app registration
used for Agent Identity Blueprint and Agent Identity creation via the
Graph beta API.

Pattern borrowed from a related internal project — the provisioner is a dedicated app
with client_credentials flow, because Azure CLI tokens include
Directory.AccessAsUser.All which the Agent Identity APIs explicitly reject
(hard 403).

The Provisioner authenticates via an X.509 cert. The public cert is
registered on the app registration in Entra; the private key never
touches the disk:

* macOS / Linux: a PEM in the OS keystore (Keychain / Secret Service, via
  `keyring`).
* Windows: a non-exportable CNG key in ``Cert:\\CurrentUser\\My`` (TPM-backed
  when the machine has a usable TPM), created by
  ``entrabot.platform.wincert``. Tokens are requested with a client
  assertion signed by that key, so it never leaves the key store.

No client_secret anywhere. Matches the Blueprint-cert pattern already
used for agent-body auth (ADR-003).

Usage:
    # As a library:
    from entra_provisioning import get_existing_graph_token, get_bootstrap_graph_token

    # As a standalone bootstrap:
    python3 scripts/entra_provisioning.py
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MS_GRAPH_API_ID = "00000003-0000-0000-c000-000000000000"
PROVISIONER_APP_DISPLAY_NAME = "EntraBot Agent ID Provisioner"

# Application.ReadWrite.All — required for Blueprint CRUD
APP_READWRITE_ALL_ID = "1bfefb4e-e0b5-418b-a88f-73c46d2cc8e9"

BASE_PERMISSION_VALUES = [
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "DelegatedPermissionGrant.ReadWrite.All",
    "LicenseAssignment.ReadWrite.All",
    "Organization.Read.All",
    "User.ReadWrite.All",
]

AGENT_ID_PERMISSION_MATCHERS = [
    "AgentIdentity",
    "AgentIdUser",
]


class ProvisionerBootstrapError(RuntimeError):
    """Raised when the provisioner app cannot be created or consented."""


# ---------------------------------------------------------------------------
# Cert-auth secret storage — OS KEY STORE ONLY, never on disk
# ---------------------------------------------------------------------------
# CLAUDE.md non-negotiable: "Never write secrets to logs or memory files".
# The Provisioner app is authenticated via an X.509 cert. On macOS and
# Linux its private key is a PEM in the OS keystore (via `keyring`); on
# Windows it is a non-exportable CNG key in the certificate store. The
# public cert is registered on the app registration in Entra. No
# client_secret anywhere. Matches the Blueprint-cert pattern already used
# for agent-body auth.
_KEYCHAIN_SERVICE_CERT = "entrabot-provisioner-cert"

# Windows: the certificate is found by this friendly name, one per tenant.
_PROVISIONER_SUBJECT = "CN=entrabot-provisioner"
_PROVISIONER_FRIENDLY_NAME = "Entrabot Provisioner {tenant_id}"
_WINDOWS_SETUP_COMMAND = r"scripts\setup-windows.cmd"


def _uses_cng() -> bool:
    """True where the Provisioner key is a CNG key in the Windows certificate store."""
    return sys.platform == "win32"


def _mask(thumbprint: str | None) -> str:
    """The last four characters of a thumbprint: enough to tell two certs apart."""
    return "…" + thumbprint[-4:].upper() if thumbprint else "(none)"


@dataclass(frozen=True)
class CngProvisionerCert:
    """The Windows Provisioner credential: a pointer to a key that can't be exported.

    Holds no secret. ``sha1_hex`` finds the certificate (and through it the key) in
    ``Cert:\\CurrentUser\\My``; ``x5t_s256`` goes in the assertion header.
    """

    sha1_hex: str
    x5t_s256: str

    def __repr__(self) -> str:
        return f"CngProvisionerCert(sha1={_mask(self.sha1_hex)})"


def _keyring_module():
    """Import keyring lazily so missing dep produces a clear error."""
    try:
        import keyring as _kr

        return _kr
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "keyring is required for cert-auth. Install with: pip install -e '.[provisioning]'"
        ) from exc


def _assert_secure_keyring_backend() -> None:
    """Fail closed if the active ``keyring`` backend is not OS-native.

    Without this guard, the Provisioner cert + private key would be written
    in cleartext on Linux hosts that have ``keyrings.alt`` installed and no
    D-Bus session (containers, headless CI, sudo'd shells). The runtime
    CredentialStore already enforces this for the agent body; mirroring the
    check here closes the provisioning-time gap.
    """
    if _uses_cng():
        # Windows keeps the key in CNG and never calls keyring (which isn't even
        # reachable from an SSH or scheduled-task logon session).
        return
    from entrabot.platform.keyring_backend import assert_allowed_keyring_backend

    assert_allowed_keyring_backend()


def _keychain_get_cert(account: str) -> str | None:
    """Return the PEM (cert+key) bundle from the OS keystore, or None if absent.

    On Windows this only ever finds the plaintext PEM an older setup left behind;
    it is read so that install keeps working until it is migrated, never written.
    """
    if _uses_cng():
        return _windows_legacy_pem_read(account)
    _assert_secure_keyring_backend()
    kr = _keyring_module()
    return kr.get_password(_KEYCHAIN_SERVICE_CERT, account)


def _keychain_store_cert(account: str, pem_bundle: str) -> None:
    """Store the PEM (cert+key) bundle in the OS keystore — overwrites if present.

    Refuses on Windows: Credential Manager can't hold a PEM (2560-byte limit), and
    the fallback used to be a plaintext file. Windows uses a non-exportable CNG key
    instead (see ``_windows_ensure_provisioner_cert``).
    """
    if _uses_cng():
        raise ProvisionerBootstrapError(
            "The Provisioner private key is never written to disk on Windows; "
            "it lives in the certificate store."
        )
    _assert_secure_keyring_backend()
    kr = _keyring_module()
    kr.set_password(_KEYCHAIN_SERVICE_CERT, account, pem_bundle)


def _windows_local_appdata() -> Path:
    """%LOCALAPPDATA%, or its default location when the variable is empty."""
    value = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(value) if value else Path.home() / "AppData" / "Local"


def _windows_legacy_pem_path(account: str) -> Path:
    """Where setups before the CNG key kept the Provisioner PEM, in plaintext."""
    return _windows_local_appdata() / "entrabot" / f"provisioner-cert-{account}.pem"


def _windows_legacy_pem_read(account: str) -> str | None:
    path = _windows_legacy_pem_path(account)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def _windows_legacy_pem_remove(account: str) -> bool:
    """Delete the plaintext PEM from an older setup. True if there was one."""
    path = _windows_legacy_pem_path(account)
    if not path.is_file():
        return False
    path.unlink()
    return True


def _keychain_delete_cert(account: str) -> None:
    """Remove the keystore entry (or the legacy Windows PEM). No-op if absent."""
    if _uses_cng():
        _windows_legacy_pem_remove(account)
        return
    _assert_secure_keyring_backend()
    kr = _keyring_module()
    with contextlib.suppress(kr.errors.PasswordDeleteError):
        kr.delete_password(_KEYCHAIN_SERVICE_CERT, account)


def _der_to_pem(der: bytes) -> str:
    """A public certificate as PEM, the form ``az ad app credential reset`` takes."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "cryptography is required for cert-auth. Install with: pip install -e '.[provisioning]'"
        ) from exc
    try:
        certificate = x509.load_der_x509_certificate(der)
    except ValueError:
        # Not parseable here; base64-wrap it and let Entra be the judge.
        import base64
        import textwrap

        body = "\n".join(textwrap.wrap(base64.b64encode(der).decode(), 64))
        return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def _windows_usable_provisioner_certs(tenant_id: str) -> list[Any]:
    """This tenant's Provisioner certificates that are in date, keyed, and can sign."""
    from entrabot.platform import wincert

    now = datetime.now(UTC)
    usable = []
    for cert in wincert.list_certs(
        friendly_name=_PROVISIONER_FRIENDLY_NAME.format(tenant_id=tenant_id)
    ):
        if not cert.has_private_key or not (cert.not_before <= now < cert.not_after):
            continue
        try:
            wincert.test_sign(cert.sha1_hex)
        except wincert.WinCertError:
            continue
        usable.append(cert)
    return usable


def _windows_find_provisioner_cert(tenant_id: str, *, recorded_only: bool = False) -> Any | None:
    """The Provisioner certificate to use: the one recorded in state, else the newest."""
    usable = _windows_usable_provisioner_certs(tenant_id)
    recorded = (get_state("PROVISIONER_CERT_THUMBPRINT") or "").upper()
    for cert in usable:
        if cert.sha1_hex == recorded:
            return cert
    if recorded_only or not usable:
        return None
    return max(usable, key=lambda cert: cert.not_after)


def _windows_ensure_provisioner_cert(client_id: str, tenant_id: str) -> str | CngProvisionerCert:
    """Make sure a non-exportable Provisioner key exists and its cert is on the app.

    Order matters for a machine that still has the plaintext PEM of an older setup:

    1. the new key is created and its certificate uploaded (if that fails, the new key
       is removed again and the PEM is left alone);
    2. the new key gets a token, proving Entra accepts it (if not, the PEM is kept and
       returned: it is still this machine's working credential);
    3. the PEM's certificate is removed from the app, by thumbprint, so a copy of the
       file stops working (if that fails, the operator is told exactly what to remove
       and the next run tries again);
    4. the PEM is deleted.

    The machine never ends up with no working credential.
    """
    from entrabot.platform import wincert

    try:
        cert = _windows_find_provisioner_cert(tenant_id)
        recorded = (get_state("PROVISIONER_CERT_THUMBPRINT") or "").upper()
        if cert is not None and cert.sha1_hex == recorded:
            print(f"  Using existing Provisioner certificate ({cert.ksp} key store)")
        elif cert is not None:
            print(
                "  Local Provisioner certificate exists but is not recorded on this "
                "app; uploading its public certificate..."
            )
            _upload_cert_to_app(client_id, _der_to_pem(cert.der))
            set_state("PROVISIONER_CERT_THUMBPRINT", cert.sha1_hex.lower())
        else:
            print("  Generating Provisioner certificate (RSA 2048, 365 days, non-exportable)...")
            cert, tpm_reason = wincert.create_signing_cert(
                subject=_PROVISIONER_SUBJECT,
                friendly_name=_PROVISIONER_FRIENDLY_NAME.format(tenant_id=tenant_id),
                days=365,
            )
            print("  Uploading public certificate to the Provisioner app...")
            try:
                _upload_cert_to_app(client_id, _der_to_pem(cert.der))
            except ProvisionerBootstrapError:
                # Nothing was registered, so this key authenticates nothing.
                try:
                    wincert.delete_cert(cert.sha1_hex, delete_key=True)
                except wincert.WinCertError as cleanup:
                    print(f"  WARNING: Could not remove the unused new certificate: {cleanup}")
                raise
            set_state("PROVISIONER_CERT_THUMBPRINT", cert.sha1_hex.lower())
            where = "the TPM" if tpm_reason is None else f"the software key store ({tpm_reason})"
            print(
                "  Provisioner private key is in the Windows certificate store, "
                f"non-exportable, protected by {where}."
            )
    except wincert.WinCertError as exc:
        raise ProvisionerBootstrapError(str(exc)) from exc

    credential = CngProvisionerCert(sha1_hex=cert.sha1_hex, x5t_s256=cert.x5t_s256)
    legacy_pem = _windows_legacy_pem_read(tenant_id)
    if legacy_pem is None:
        _retire_old_certificates(client_id, tenant_id, cert, [])
        return credential

    legacy = _legacy_cert_identity(legacy_pem)
    if legacy is None:
        path = _windows_legacy_pem_path(tenant_id)
        print(f"  WARNING: The Provisioner key file left by an earlier setup is unreadable: {path}")
        print("  Its certificate can't be identified, so it can't be removed from the app")
        print("  automatically. In the Entra admin center, open App registrations >")
        print(f"  '{PROVISIONER_APP_DISPLAY_NAME}' (application ID {client_id}) >")
        print("  Certificates & secrets > Certificates, and remove any certificate you can't")
        print("  account for.")
        mine = _mask(cert.sha1_hex)
        print(f"  This machine's certificate is the one whose thumbprint ends in {mine}.")
        _retire_old_certificates(client_id, tenant_id, cert, [])
        return credential

    # Until Entra accepts the key-store certificate, the old one is this machine's
    # only working credential: prove the new one before retiring the old.
    try:
        _acquire_graph_token_cng(tenant_id, client_id, credential)
    except ProvisionerBootstrapError as exc:
        print(f"  WARNING: Entra does not accept the new Provisioner certificate yet: {exc}")
        print("  The old key file is kept, and still works, until it does. Run setup again")
        print("  in a few minutes to finish moving off it.")
        return legacy_pem

    _retire_old_certificates(client_id, tenant_id, cert, [legacy])
    return credential


# Old Provisioner certificates whose removal from the app failed, retried on every run.
# Comma-separated lowercase SHA-1 thumbprints, and their expiry dates in the same order.
_REVOKE_PENDING = "PROVISIONER_CERT_REVOKE_PENDING"
_REVOKE_PENDING_EXPIRES = "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES"

_SHA1_HEX_RE = re.compile(r"^[0-9A-Fa-f]{40}$")


def _legacy_cert_identity(pem_bundle: str) -> tuple[str, str | None] | None:
    """(SHA-1 thumbprint, expiry date) of the certificate in a PEM bundle, or None."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "cryptography is required for cert-auth. Install with: pip install -e '.[provisioning]'"
        ) from exc
    try:
        certificate = x509.load_pem_x509_certificate(_cert_pem_from_bundle(pem_bundle).encode())
        der = certificate.public_bytes(serialization.Encoding.DER)
    except (ProvisionerBootstrapError, ValueError):
        return None
    sha1 = hashlib.sha1(der).hexdigest().upper()  # noqa: S324 — an identifier
    return sha1, certificate.not_valid_after_utc.date().isoformat()


def _pending_revocations() -> list[tuple[str, str | None]]:
    sha1s = [part.strip() for part in (get_state(_REVOKE_PENDING) or "").split(",")]
    expiries = [part.strip() for part in (get_state(_REVOKE_PENDING_EXPIRES) or "").split(",")]
    pending: list[tuple[str, str | None]] = []
    for index, sha1 in enumerate(sha1s):
        if not sha1:
            continue
        if not _SHA1_HEX_RE.match(sha1):
            print(f"  WARNING: ignoring a malformed entry in {_REVOKE_PENDING} in the state file.")
            continue
        expires = expiries[index] if index < len(expiries) else ""
        pending.append((sha1.upper(), expires or None))
    return pending


def _set_pending_revocations(pending: list[tuple[str, str | None]]) -> None:
    if not pending:
        clear_state(_REVOKE_PENDING)
        clear_state(_REVOKE_PENDING_EXPIRES)
        return
    set_state(_REVOKE_PENDING, ",".join(sha1.lower() for sha1, _ in pending))
    set_state(_REVOKE_PENDING_EXPIRES, ",".join(expires or "" for _, expires in pending))


def _print_revocation_failed(
    client_id: str, sha1: str, expires: str | None, exc: Exception
) -> None:
    when = f"expires {expires}" if expires else "expiry date unknown"
    print(f"  WARNING: The old Provisioner certificate ({_mask(sha1)}, {when}) could not be")
    print(f"  removed from the app: {exc}")
    print("  Its private key was kept in a plaintext file by an earlier setup, so a copy of")
    print("  that file can still get tokens for the Provisioner app until it is removed.")
    print("  Remove it in the Entra admin center: App registrations >")
    print(f"  '{PROVISIONER_APP_DISPLAY_NAME}' (application ID {client_id}) >")
    print("  Certificates & secrets > Certificates: the certificate whose thumbprint ends in")
    print(f"  {_mask(sha1)} ({when}). Setup tries again on its next run.")


def _retire_old_certificates(
    client_id: str,
    tenant_id: str,
    keep: Any,
    found: list[tuple[str, str | None]],
) -> None:
    """Remove old certificates from the app, then the plaintext key file.

    ``found`` is the certificate of the key file on disk, if it could be read; the
    certificates whose removal failed on an earlier run are added to it. ``keep`` is
    this machine's key-store certificate, which is never removed.
    """
    targets = list(found)
    for item in _pending_revocations():
        if item[0] not in {sha1 for sha1, _ in targets}:
            targets.append(item)

    still_pending: list[tuple[str, str | None]] = []
    lost: ProvisionerCertificateLostError | None = None
    for sha1, expires in targets:
        if sha1 == keep.sha1_hex.upper():
            continue  # the certificate this machine uses is never an "old" one
        try:
            outcome = _revoke_app_certificate(client_id, sha1, keep_sha1=keep.sha1_hex)
        except ProvisionerCertificateLostError as exc:
            # The old certificate is gone, but so is this machine's: put it back below.
            lost = exc
            continue
        except ProvisionerBootstrapError as exc:
            _print_revocation_failed(client_id, sha1, expires, exc)
            still_pending.append((sha1, expires))
            continue
        if outcome == "removed":
            print(f"  Removed the old Provisioner certificate ({_mask(sha1)}) from the app.")
        else:
            print(f"  The old Provisioner certificate ({_mask(sha1)}) is not on the app.")
    _set_pending_revocations(still_pending)

    # Removed or not, the plaintext key goes: what's left to do is in Entra, and the
    # warning above says what.
    try:
        if _windows_legacy_pem_remove(tenant_id):
            print("  Removed the plaintext Provisioner key file left by an earlier setup.")
    except OSError as exc:
        print(
            "  WARNING: Could not delete the plaintext Provisioner key file "
            f"{_windows_legacy_pem_path(tenant_id)} ({exc.strerror or type(exc).__name__}). "
            "Delete it by hand; setup also tries again on its next run."
        )

    if lost is not None:
        print(f"  ERROR: {lost}")
        print("  Uploading this machine's Provisioner certificate to the app again...")
        try:
            _upload_cert_to_app(client_id, _der_to_pem(keep.der))
        except ProvisionerBootstrapError:
            # Unrecorded, so the next run uploads it again.
            clear_state("PROVISIONER_CERT_THUMBPRINT")
            raise
        print("  This machine's Provisioner certificate is on the app again.")


# ---------------------------------------------------------------------------
# Removing one certificate from the Provisioner app
# ---------------------------------------------------------------------------

_REVOKE_CONFIRM_ATTEMPTS = 6
_REVOKE_CONFIRM_DELAY_SECONDS = 5.0


class ProvisionerCertificateLostError(ProvisionerBootstrapError):
    """The old certificate was removed, and this machine's is no longer on the app."""


def _normalize_sha1_hex(value: str) -> str:
    if not isinstance(value, str) or not _SHA1_HEX_RE.match(value):
        raise ProvisionerBootstrapError(
            "A certificate thumbprint must be 40 hexadecimal characters."
        )
    return value.upper()


def _key_credential_thumbprints(entry: dict[str, Any]) -> set[str] | None:
    """The SHA-1 thumbprints one ``keyCredentials`` entry can be identified by.

    The certificate itself (``key``) decides when Graph returns it. Otherwise
    ``customKeyIdentifier`` is read in the forms it comes in: hex, base64 of the raw
    hash, or base64 of the hex text. None when the entry can't be identified.
    """
    key = entry.get("key")
    if isinstance(key, str) and key.strip():
        try:
            der = base64.b64decode(key, validate=True)
        except (binascii.Error, ValueError):
            der = b""
        if der.lstrip().startswith(b"-----BEGIN"):
            try:
                from cryptography import x509
                from cryptography.hazmat.primitives import serialization

                der = x509.load_pem_x509_certificate(der).public_bytes(serialization.Encoding.DER)
            except ValueError:
                der = b""
        if der:
            return {hashlib.sha1(der).hexdigest().upper()}  # noqa: S324 — an identifier

    identifier = entry.get("customKeyIdentifier")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    text = identifier.strip()
    found: set[str] = set()
    if _SHA1_HEX_RE.match(text):
        found.add(text.upper())
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) == 20:
        found.add(raw.hex().upper())
    elif len(raw) == 40:
        decoded = raw.decode("ascii", errors="replace")
        if _SHA1_HEX_RE.match(decoded):
            found.add(decoded.upper())
    return found or None


def _app_object_id(client_id: str) -> str:
    rc, out, err = run_az(["ad", "app", "show", "--id", client_id, "--query", "id", "-o", "json"])
    object_id = _az_scalar(out) if rc == 0 else ""
    if not object_id:
        raise ProvisionerBootstrapError(
            f"Could not look up the Provisioner app {client_id}: {err or 'no object ID returned'}"
        )
    return object_id


def _read_app_key_credentials(object_id: str) -> list[dict[str, Any]]:
    """The app's certificates, with their key material (Graph returns ``key`` only
    when ``keyCredentials`` is selected on a single application)."""
    rc, out, err = run_az(
        [
            "rest",
            "--method",
            "GET",
            "--url",
            f"https://graph.microsoft.com/v1.0/applications/{object_id}?$select=keyCredentials",
            "-o",
            "json",
        ]
    )
    if rc != 0:
        raise ProvisionerBootstrapError(
            f"Could not read the Provisioner app's certificates: {err or f'az exited {rc}'}"
        )
    try:
        data = json.loads(out) if out else None
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(
            f"Could not parse the Provisioner app's certificates: {exc}"
        ) from exc
    entries = data.get("keyCredentials") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ProvisionerBootstrapError(
            "The Provisioner app's certificates came back in an unexpected shape."
        )
    return entries


def _revoke_app_certificate(client_id: str, sha1_hex: str, *, keep_sha1: str | None = None) -> str:
    """Remove the certificate with this SHA-1 thumbprint from the Provisioner app.

    Identified by thumbprint only, never by display name. Uses the az session that
    uploaded the new certificate, and reads the app back to confirm.

    Returns:
        ``"removed"`` once a read no longer lists it, or ``"not_registered"`` when the
        app doesn't have it (nothing is changed).

    Raises:
        ProvisionerBootstrapError: it is the certificate this machine uses
            (``keep_sha1``), which is never removed; the app could not be read, or one
            of its certificates could not be identified (nothing is changed); this
            machine's certificate is not listed (nothing is changed); Entra refused
            the removal; or it is still listed after every read.
        ProvisionerCertificateLostError: it was removed, but this machine's
            certificate is no longer listed either.
    """
    target = _normalize_sha1_hex(sha1_hex)
    keep = _normalize_sha1_hex(keep_sha1) if keep_sha1 else None
    if target == keep:
        raise ProvisionerBootstrapError(
            f"Certificate {_mask(target)} is the one this machine has in use; not removing it."
        )

    object_id = _app_object_id(client_id)
    entries = _read_app_key_credentials(object_id)
    identified = [(entry, _key_credential_thumbprints(entry)) for entry in entries]
    matches = [entry for entry, ids in identified if ids and target in ids]
    unidentified = sum(1 for _, ids in identified if ids is None)

    if not matches:
        if unidentified:
            raise ProvisionerBootstrapError(
                f"{unidentified} certificate(s) on the Provisioner app could not be identified "
                f"by thumbprint, so whether {_mask(target)} is among them is unknown. Nothing "
                "was removed."
            )
        return "not_registered"
    if keep is not None and not any(ids and keep in ids for _, ids in identified):
        raise ProvisionerBootstrapError(
            f"This machine's certificate ({_mask(keep)}) is not listed on the Provisioner app, "
            f"so {_mask(target)} was not removed: it may be the app's only working certificate."
        )

    for entry in matches:
        key_id = entry.get("keyId")
        if not isinstance(key_id, str) or not key_id:
            raise ProvisionerBootstrapError(
                f"Certificate {_mask(target)} is on the app without a key ID; nothing was removed."
            )
        rc, _, err = run_az(
            ["ad", "app", "credential", "delete", "--id", client_id, "--key-id", key_id, "--cert"]
        )
        if rc != 0:
            raise ProvisionerBootstrapError(
                f"Entra did not remove certificate {_mask(target)}: {err or f'az exited {rc}'}"
            )

    for attempt in range(_REVOKE_CONFIRM_ATTEMPTS):
        if attempt:
            time.sleep(_REVOKE_CONFIRM_DELAY_SECONDS)
        listed = [
            _key_credential_thumbprints(entry) or set()
            for entry in _read_app_key_credentials(object_id)
        ]
        if any(target in ids for ids in listed):
            continue  # removals take a moment to show up in reads
        if keep is not None and not any(keep in ids for ids in listed):
            raise ProvisionerCertificateLostError(
                f"Removing the old certificate ({_mask(target)}) left this machine's "
                f"certificate ({_mask(keep)}) no longer listed on the Provisioner app."
            )
        return "removed"
    raise ProvisionerBootstrapError(
        f"Certificate {_mask(target)} is still listed on the Provisioner app after its "
        f"removal was accepted ({_REVOKE_CONFIRM_ATTEMPTS} reads)."
    )


def _generate_provisioner_cert() -> tuple[str, str, str]:
    """Generate a fresh self-signed cert for Provisioner auth.

    Returns (cert_pem, key_pem, thumbprint_hex). The PEMs are strings
    (UTF-8); thumbprint is lowercase hex (matches Entra's
    ``customKeyIdentifier`` / ``keyId`` representation).

    Private material is returned as strings in memory — caller must
    store it in Keychain, never to disk.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "cryptography is required for cert-auth. Install with: pip install -e '.[provisioning]'"
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "entrabot-provisioner"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "EntraBot"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Backdated: a machine whose clock runs a little fast must not mint a
        # certificate Entra considers not yet valid.
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=10))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(encoding=serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    thumbprint = cert.fingerprint(hashes.SHA1()).hex()
    return cert_pem, key_pem, thumbprint


def _cert_pem_from_bundle(pem_bundle: str) -> str:
    """Extract the public certificate PEM from a cert+key PEM bundle."""
    marker = "-----END CERTIFICATE-----"
    if marker not in pem_bundle:
        raise ProvisionerBootstrapError("stored Provisioner cert bundle has no certificate")
    return pem_bundle.split(marker, 1)[0] + marker + "\n"


def _thumbprint_from_cert_pem(cert_pem: str) -> str:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "cryptography is required for cert-auth. Install with: pip install -e '.[provisioning]'"
        ) from exc

    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return cert.fingerprint(hashes.SHA1()).hex()


def _upload_cert_to_app(app_id: str, cert_pem: str) -> None:
    """Register a public cert on the Provisioner app via az CLI.

    Uses ``--append`` for idempotent multi-cert support (matches the
    Blueprint pattern). The public cert is written to a mktemp'd file
    only for the duration of the az CLI call; private key is NEVER
    touched.
    """
    with tempfile.NamedTemporaryFile(suffix=".crt", mode="w", delete=False, encoding="utf-8") as tf:
        tf.write(cert_pem)
        cert_path = tf.name
    try:
        rc, out, err = run_az(
            [
                "ad",
                "app",
                "credential",
                "reset",
                "--id",
                app_id,
                "--cert",
                f"@{cert_path}",
                "--append",
                "-o",
                "json",
            ]
        )
        if rc != 0:
            raise ProvisionerBootstrapError(err or "failed to upload cert to Provisioner app")
        # az prints a JSON blob on success; we just care that rc == 0
        _ = out
    finally:
        with contextlib.suppress(OSError):
            os.unlink(cert_path)


def _remove_legacy_password_credentials(app_id: str) -> int:
    """Delete any password credentials from the Provisioner app.

    Returns the count of credentials deleted. Used during migration
    from the secret-auth era — once cert-auth is in place, a lingering
    password credential is a usable backdoor and MUST be removed.

    ``az ad app credential delete`` without ``--cert`` targets password
    credentials; with ``--cert`` it targets key credentials. We only
    delete passwords here.
    """
    rc, out, err = run_az(
        [
            "ad",
            "app",
            "show",
            "--id",
            app_id,
            "--query",
            "passwordCredentials[].keyId",
            "-o",
            "json",
        ]
    )
    if rc != 0:
        raise ProvisionerBootstrapError(err or "could not list Provisioner password credentials")
    try:
        key_ids = json.loads(out) if out else []
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(f"failed to parse passwordCredentials list: {exc}") from exc

    removed = 0
    for key_id in key_ids:
        rc, _, err = run_az(
            [
                "ad",
                "app",
                "credential",
                "delete",
                "--id",
                app_id,
                "--key-id",
                key_id,
            ]
        )
        if rc != 0:
            raise ProvisionerBootstrapError(err or f"failed to delete password credential {key_id}")
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

_STATE_FILE = Path(__file__).resolve().parent.parent / ".entrabot-state.json"


def _load_state() -> dict:
    if _STATE_FILE.is_file():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def get_state(key: str) -> str | None:
    """Read a value from .entrabot-state.json."""
    return _load_state().get(key)


def set_state(key: str, value: str) -> None:
    """Write a value to .entrabot-state.json."""
    state = _load_state()
    state[key] = value
    _save_state(state)


def clear_state(key: str) -> None:
    """Remove a key from .entrabot-state.json."""
    state = _load_state()
    state.pop(key, None)
    _save_state(state)


# ---------------------------------------------------------------------------
# az CLI helpers
# ---------------------------------------------------------------------------


# Characters cmd.exe gives a meaning to. An argument containing one must be quoted
# when it travels through a .cmd shim, or the batch file is re-parsed around it.
_CMD_METACHARACTERS = frozenset(' \t()&|^<>%!"')


def _quote_for_cmd(arg: str) -> str:
    quoted = subprocess.list2cmdline([arg])
    if not quoted.startswith('"') and any(ch in _CMD_METACHARACTERS for ch in arg):
        quoted = f'"{quoted}"'
    return quoted


def _az_command(args: list[str]) -> tuple[list[str] | str, dict[str, str] | None]:
    """The command (and environment) that runs ``az`` with these arguments.

    On Windows ``az`` is ``az.cmd``, a batch file that ends in ``python -m azure.cli
    %*`` inside an ``IF ( ... )`` block. Arguments are re-parsed by cmd.exe on the
    way through, so a ``)`` in a URL closes that block and ``&``, ``|``, ``^``, ``<``,
    ``>`` and ``%`` are interpreted. Running the CLI's own interpreter directly
    takes cmd.exe out of the path altogether; only when the layout isn't recognised
    does the call go through the shim, with such arguments quoted.
    """
    az_bin = shutil.which("az") or "az"
    if sys.platform != "win32" or not az_bin.lower().endswith((".cmd", ".bat")):
        return [az_bin, *args], None

    shim = Path(az_bin)
    msi_python = shim.parent.parent / "python.exe"  # ...\CLI2\wbin\az.cmd
    if msi_python.is_file():
        return (
            [str(msi_python), "-IBm", "azure.cli", *args],
            {**os.environ, "AZ_INSTALLER": "MSI"},
        )
    venv_python = shim.parent / "python.exe"  # pip install azure-cli: ...\Scripts\az.bat
    if venv_python.is_file():
        return [str(venv_python), "-m", "azure.cli", *args], None
    return " ".join(_quote_for_cmd(part) for part in [az_bin, *args]), None


def run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
    """Run an az CLI command, return (returncode, stdout, stderr)."""
    command, env = _az_command(args)
    result = subprocess.run(  # noqa: S603 — az with a fixed argument list
        command, capture_output=capture, text=True, errors="replace", env=env
    )
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def _az_scalar(out: str) -> str:
    """One value from ``az ... --query <scalar> -o json``.

    JSON is parsed where TSV was used before: a warning line or a locale-dependent
    console can corrupt TSV silently, while corrupted JSON fails to parse. Output
    that isn't JSON is returned as-is, stripped.
    """
    text = (out or "").strip()
    if not text:
        return ""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    return value.strip() if isinstance(value, str) else ""


def get_signed_in_user_id() -> str | None:
    """Get the object ID of the currently signed-in Azure CLI user."""
    rc, out, _ = run_az(["ad", "signed-in-user", "show", "--query", "id", "-o", "json"])
    user_id = _az_scalar(out) if rc == 0 else ""
    return user_id or None


def build_sponsors_bind() -> list[str]:
    """Build the sponsors@odata.bind array for Blueprint/Agent Identity creation."""
    user_id = get_signed_in_user_id()
    if user_id:
        return [f"https://graph.microsoft.com/v1.0/users/{user_id}"]
    return []


# ---------------------------------------------------------------------------
# Permission discovery
# ---------------------------------------------------------------------------


def _load_graph_app_roles() -> list[dict]:
    """Load all app roles from the Microsoft Graph service principal."""
    rc, out, err = run_az(
        [
            "ad",
            "sp",
            "show",
            "--id",
            MS_GRAPH_API_ID,
            "--query",
            "appRoles[].{id:id,value:value}",
            "-o",
            "json",
        ]
    )
    if rc != 0 or not out:
        raise ProvisionerBootstrapError(err or "could not query Microsoft Graph app roles")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(f"failed to parse Graph role list: {exc}") from exc


def resolve_graph_permissions() -> dict[str, str]:
    """Return a map of permission value -> role ID for all Graph app roles."""
    roles = _load_graph_app_roles()
    return {role["value"]: role["id"] for role in roles if role.get("value") and role.get("id")}


def build_required_permission_values() -> list[str]:
    """Build the list of permission values needed for Agent Identity provisioning.

    Dynamically discovers all AgentIdentity-related permissions from Graph,
    adds Application.ReadWrite.All for Blueprint CRUD.
    """
    required = list(BASE_PERMISSION_VALUES)
    graph_permissions = resolve_graph_permissions()
    for value in sorted(graph_permissions):
        if any(matcher in value for matcher in AGENT_ID_PERMISSION_MATCHERS):
            required.append(value)
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for v in required:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


# ---------------------------------------------------------------------------
# Permission management
# ---------------------------------------------------------------------------


def _resolve_permission_specs(required_values: list[str]) -> list[tuple[str, str]]:
    """Resolve permission value names to (value, "roleId=Role") specs."""
    permission_map = resolve_graph_permissions()
    specs = []
    missing = []
    for value in required_values:
        role_id = permission_map.get(value)
        if role_id:
            specs.append((value, f"{role_id}=Role"))
        else:
            missing.append(value)
    if missing:
        raise ProvisionerBootstrapError(
            "missing Microsoft Graph application permissions in tenant: "
            + ", ".join(sorted(missing))
        )
    return specs


def _get_existing_permission_role_ids(client_id: str) -> set[str]:
    """Get the set of role IDs already assigned to an app."""
    rc, out, err = run_az(
        [
            "ad",
            "app",
            "show",
            "--id",
            client_id,
            "--query",
            "requiredResourceAccess[?resourceAppId=='00000003-0000-0000-c000-000000000000']"
            ".resourceAccess[].id",
            "-o",
            "json",
        ]
    )
    if rc != 0 or not out:
        if err:
            raise ProvisionerBootstrapError(err)
        return set()
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(f"failed to parse existing app permissions: {exc}") from exc
    return {item for item in data if item}


def _get_consented_graph_role_ids(client_id: str) -> set[str]:
    """Return Microsoft Graph app-role IDs effectively admin-consented on the
    provisioner's service principal.

    Unlike :func:`_get_existing_permission_role_ids` (which reads
    ``requiredResourceAccess`` — the *declared* permissions on the app),
    this inspects the SP's ``appRoleAssignments`` — the *effective* grants
    produced by admin consent. Consent drift (a permission requested but
    never consented) is invisible to ``requiredResourceAccess``, so the only
    way to detect it is to read the actual grants.

    On a read failure we return an empty set, which makes the caller treat
    everything as unconsented and re-run admin consent. ``admin-consent`` is
    idempotent, so erring toward re-consent is safe.
    """
    # Two steps, by object ID: the one-call form addresses the service principal as
    # servicePrincipals(appId='...'), and those parentheses don't survive az.cmd on
    # Windows. Filtering happens here rather than in a JMESPath --query for the
    # same reason.
    rc, out, _ = run_az(["ad", "sp", "show", "--id", client_id, "--query", "id", "-o", "json"])
    sp_object_id = _az_scalar(out) if rc == 0 else ""
    if not sp_object_id:
        return set()

    rc, out, _ = run_az(
        [
            "rest",
            "--method",
            "GET",
            "--url",
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp_object_id}/appRoleAssignments",
            "-o",
            "json",
        ]
    )
    if rc != 0 or not out:
        return set()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return set()
    assignments = data.get("value") if isinstance(data, dict) else None
    if not isinstance(assignments, list):
        return set()
    return {
        item["appRoleId"]
        for item in assignments
        if isinstance(item, dict)
        and item.get("resourceDisplayName") == "Microsoft Graph"
        and item.get("appRoleId")
    }


# What az says when the signed-in account simply isn't allowed to do this. Retrying
# doesn't help, so these stop the consent loop at once.
_MISSING_PRIVILEGE_TERMS = (
    "insufficient privileges",
    "authorization_requestdenied",
    "forbidden",
    "access denied",
    "accessdenied",
    "status code 403",
)

_CONSENT_ROLES = "Global Administrator or Privileged Role Administrator"


def _is_missing_privilege(err: str) -> bool:
    lowered = (err or "").lower()
    return any(term in lowered for term in _MISSING_PRIVILEGE_TERMS)


def _print_consent_role_required(err: str = "") -> None:
    print("  ERROR: The signed-in account can't grant admin consent for the Microsoft Graph")
    print("  application permissions the provisioner app needs.")
    print(f"  That takes the {_CONSENT_ROLES} role in this tenant.")
    print("  Ask someone with one of those roles to run setup, or to grant admin consent to")
    print(f"  the '{PROVISIONER_APP_DISPLAY_NAME}' app in the Entra admin center, then rerun.")
    if err:
        print(f"  Details: {err}")


def _print_admin_required(action: str, err: str = "") -> None:
    print(f"  ERROR: The signed-in operator could not {action}.")
    print("  This step requires an Entra administrator with permission to")
    print("  create/update app registrations and grant Microsoft Graph admin consent.")
    print("  Ask an administrator to complete the setup, then rerun.")
    if err:
        print(f"  Details: {err}")


# What az says when the app registration demonstrably does not exist, as opposed to
# "the lookup failed" (not signed in, no network, MFA, throttling...). Only these
# count as "not found"; anything else is "could not check".
_APP_NOT_FOUND_TERMS = (
    "request_resourcenotfound",
    "does not exist or one of its queried reference-property objects are not present",
    "no application found matching",
)

APP_EXISTS = "exists"
APP_NOT_FOUND = "not_found"
APP_UNKNOWN = "unknown"


def _application_status(client_id: str) -> str:
    """Whether the app registration exists: ``exists``, ``not_found`` or ``unknown``.

    ``not_found`` only when Entra said so. A failed lookup is ``unknown``, and nothing
    may be discarded on the strength of it.
    """
    rc, _, err = run_az(["ad", "app", "show", "--id", client_id, "-o", "json"], capture=True)
    if rc == 0:
        return APP_EXISTS
    lowered = (err or "").lower()
    if any(term in lowered for term in _APP_NOT_FOUND_TERMS):
        return APP_NOT_FOUND
    print(f"  Could not look up the Provisioner app {client_id}: {err or f'az exited {rc}'}")
    return APP_UNKNOWN


def _application_exists(client_id: str) -> bool:
    return _application_status(client_id) == APP_EXISTS


def _ensure_service_principal(client_id: str) -> None:
    """Ensure a service principal exists for the given app."""
    rc, _, _ = run_az(["ad", "sp", "show", "--id", client_id], capture=True)
    if rc == 0:
        return
    rc, _, err = run_az(["ad", "sp", "create", "--id", client_id])
    if rc != 0 and "already exists" not in err.lower():
        raise ProvisionerBootstrapError(err or "service principal creation failed")


def _ensure_permissions_and_consent(client_id: str, required_values: list[str]) -> bool:
    """Add Graph permissions and grant admin consent if permissions changed.

    "Changed" means either (a) a required permission was missing from the
    app's ``requiredResourceAccess`` and had to be added, OR (b) a required
    permission is declared but not effectively admin-consented on the SP
    (consent drift). Either case warrants re-running admin consent; the
    caller uses the ``True`` return to decide whether a propagation wait is
    warranted.

    Checking *consented* permissions (not just *declared* ones) is what lets
    this self-heal consent drift: a permission added to the app's required
    list after the last admin-consent stays unconsented forever if we only
    compare the declared set.
    """
    _ensure_service_principal(client_id)

    permission_specs = _resolve_permission_specs(required_values)
    required_role_ids = {spec.split("=", 1)[0] for _, spec in permission_specs}
    declared_role_ids = _get_existing_permission_role_ids(client_id)
    consented_role_ids = _get_consented_graph_role_ids(client_id)

    missing_specs = [
        spec for _, spec in permission_specs if spec.split("=", 1)[0] not in declared_role_ids
    ]
    unconsented_role_ids = sorted(required_role_ids - consented_role_ids)
    print(f"  Ensuring {len(permission_specs)} Graph application permissions on provisioner app...")

    if not missing_specs and not unconsented_role_ids:
        print("  Provisioner app already has the required Graph permissions (declared + consented)")
        return False

    if missing_specs:
        cmd = [
            "ad",
            "app",
            "permission",
            "add",
            "--id",
            client_id,
            "--api",
            MS_GRAPH_API_ID,
            "--api-permissions",
            *missing_specs,
        ]
        rc, _, err = run_az(cmd)
        if rc != 0 and "already exists" not in err.lower():
            lowered = err.lower()
            if any(term in lowered for term in ["insufficient", "authorization", "privilege"]):
                _print_admin_required(
                    "add the required Microsoft Graph application permissions", err
                )
            raise ProvisionerBootstrapError(err or "permission add failed")
    elif unconsented_role_ids:
        # Declared but not effectively consented — classic consent drift,
        # usually because a permission was added to the required list after
        # the last admin consent. Re-run consent so it becomes effective.
        print(
            f"  Consent drift: {len(unconsented_role_ids)} required permission(s) declared "
            "but not admin-consented; re-running admin consent"
        )

    # Grant admin consent. Runs when permissions were newly added in this
    # invocation OR when declared permissions remain unconsented (drift). The
    # read/action path uses load_existing_app_registration (no permission
    # repair), so this does not re-consent on every token acquisition.
    print("  Granting admin consent for provisioner app...")
    consent_error = ""
    for attempt in range(4):
        if attempt:
            wait = 10 * (attempt + 1)
            print(f"  Retrying admin consent in {wait}s (attempt {attempt + 1}/4)...")
            time.sleep(wait)
        rc, _, err = run_az(["ad", "app", "permission", "admin-consent", "--id", client_id])
        if rc == 0:
            print("  Admin consent granted")
            return True
        consent_error = err
        if _is_missing_privilege(err):
            # Not a propagation delay: this account can't consent, now or in 90 seconds.
            _print_consent_role_required(err)
            raise ProvisionerBootstrapError(
                f"admin consent requires the {_CONSENT_ROLES} role: "
                f"{err or 'insufficient privileges'}"
            )

    lowered = consent_error.lower()
    if any(term in lowered for term in ["insufficient", "authorization", "privilege", "admin"]):
        _print_admin_required("grant admin consent for the provisioner app", consent_error)
    raise ProvisionerBootstrapError(consent_error or "admin consent failed")


# ---------------------------------------------------------------------------
# Provisioner app lifecycle
# ---------------------------------------------------------------------------


def ensure_app_registration(
    required_values: list[str],
    wait_for_propagation: bool = True,
) -> tuple[str, str | CngProvisionerCert, str]:
    """Ensure the dedicated provisioner app exists with cert-auth.

    Returns (client_id, credential, tenant_id).

    On macOS and Linux ``credential`` is the PEM-encoded cert+private-key. The
    private key ONLY exists in the OS keystore; the PEM in memory is transient —
    callers must not write it to disk.

    On Windows ``credential`` is a :class:`CngProvisionerCert`, which points at a
    non-exportable key in the certificate store and holds no secret.

    State file tracks only non-secret identifiers (app id, thumbprint).
    """
    tenant_id = os.environ.get("ENTRABOT_TENANT_ID") or get_state("TENANT_ID")
    if not tenant_id:
        rc, out, err = run_az(["account", "show", "--query", "tenantId", "-o", "json"])
        tenant_id = _az_scalar(out) if rc == 0 else ""
        if not tenant_id:
            raise ProvisionerBootstrapError(
                err or "cannot determine tenant ID; run 'az login' first"
            )
        set_state("TENANT_ID", tenant_id)

    # SECURITY: legacy migration path — if a prior (secret-auth) run
    # left PROVISIONER_CLIENT_SECRET in the state file, we purge it
    # BEFORE the cert-auth flow starts. This guarantees the state
    # file never carries a secret forward.
    if get_state("PROVISIONER_CLIENT_SECRET"):
        print(
            "  WARNING: legacy PROVISIONER_CLIENT_SECRET found in state file. "
            "Cert-auth supersedes secret-auth; purging the secret from disk."
        )
        clear_state("PROVISIONER_CLIENT_SECRET")

    client_id = get_state("PROVISIONER_CLIENT_ID")
    # Windows keeps the key in CNG; its certificate is looked up further down.
    pem_bundle = None if _uses_cng() else _keychain_get_cert(tenant_id)

    # Stale-app detection. Only Entra saying "no such app" makes the cached app stale;
    # a lookup that failed changes nothing, because acting on it would throw away a
    # working credential.
    if client_id:
        status = _application_status(client_id)
        if status == APP_UNKNOWN:
            raise ProvisionerBootstrapError(
                f"Could not check whether the Provisioner app {client_id} still exists in "
                "Entra (see the az error above). Nothing was changed. Fix that (for "
                "example, run 'az login') and run setup again."
            )
        if status == APP_NOT_FOUND:
            print(f"  Cached provisioner app no longer exists in Entra: {client_id}")
            client_id = None
            # Its certificate was registered on an app that is gone, so a new one is
            # made below. The old key is NOT deleted here: on macOS / Linux the new
            # bundle overwrites it once its certificate is on the app, and on Windows
            # the old key file is only retired after the replacement has worked.
            pem_bundle = None
            clear_state("PROVISIONER_CLIENT_ID")
            # Unrecorded, so a key-store certificate on Windows is uploaded to the new app.
            clear_state("PROVISIONER_CERT_THUMBPRINT")

    # Find-or-create the Provisioner app registration
    if not client_id:
        rc, out, _ = run_az(
            [
                "ad",
                "app",
                "list",
                "--display-name",
                PROVISIONER_APP_DISPLAY_NAME,
                "--query",
                "[0].appId",
                "-o",
                "json",
            ]
        )
        found_app_id = _az_scalar(out) if rc == 0 else ""
        if found_app_id:
            client_id = found_app_id
            print(f"  Found existing provisioner app: {client_id}")
            set_state("PROVISIONER_CLIENT_ID", client_id)
        else:
            print("  Creating dedicated EntraBot provisioner app...")
            rc, out, err = run_az(
                [
                    "ad",
                    "app",
                    "create",
                    "--display-name",
                    PROVISIONER_APP_DISPLAY_NAME,
                    "--sign-in-audience",
                    "AzureADMyOrg",
                    "--query",
                    "appId",
                    "-o",
                    "json",
                ]
            )
            created_app_id = _az_scalar(out) if rc == 0 else ""
            if not created_app_id:
                lowered = (err or "").lower()
                if any(
                    term in lowered
                    for term in ["insufficient", "authorization", "privilege", "permission"]
                ):
                    _print_admin_required("create the dedicated provisioner app registration", err)
                raise ProvisionerBootstrapError(err or "app registration creation failed")
            client_id = created_app_id
            print(f"  Created provisioner app: {client_id}")
            set_state("PROVISIONER_CLIENT_ID", client_id)

    permissions_changed = _ensure_permissions_and_consent(client_id, required_values)

    # SECURITY: any leftover password credentials on the app are a
    # backdoor — remove them unconditionally. This closes the window
    # opened by the secret-auth era.
    try:
        if _remove_legacy_password_credentials(client_id):
            print("  Removed legacy app credentials from Provisioner app.")
    except ProvisionerBootstrapError as exc:
        print(f"  WARN: could not enumerate/delete legacy app credentials: {type(exc).__name__}")

    credential: str | CngProvisionerCert
    if _uses_cng():
        # Windows: a non-exportable key in the certificate store; nothing on disk.
        credential = _windows_ensure_provisioner_cert(client_id, tenant_id)
    elif not pem_bundle:
        # Cert-auth path: generate + upload + keystore-store if absent
        print("  Generating cert for Provisioner (RSA 2048, 365 days)...")
        cert_pem, key_pem, thumbprint = _generate_provisioner_cert()
        print("  Uploading public cert to app...")
        _upload_cert_to_app(client_id, cert_pem)
        pem_bundle = cert_pem + key_pem
        _keychain_store_cert(tenant_id, pem_bundle)
        set_state("PROVISIONER_CERT_THUMBPRINT", thumbprint)
        keystore = "macOS Keychain" if sys.platform == "darwin" else "the OS keyring"
        print(
            f"  Cert private key stored in {keystore} "
            f"(service='{_KEYCHAIN_SERVICE_CERT}', account='{tenant_id}')."
        )
        credential = pem_bundle
    else:
        thumbprint = get_state("PROVISIONER_CERT_THUMBPRINT")
        if not thumbprint:
            cert_pem = _cert_pem_from_bundle(pem_bundle)
            thumbprint = _thumbprint_from_cert_pem(cert_pem)
            print(
                "  Local Provisioner cert exists but is not recorded on this "
                "app; uploading public cert..."
            )
            _upload_cert_to_app(client_id, cert_pem)
            set_state("PROVISIONER_CERT_THUMBPRINT", thumbprint)
        print("  Using existing Provisioner cert")
        credential = pem_bundle

    if wait_for_propagation and permissions_changed:
        print("  Waiting 30s for Graph permission propagation...")
        time.sleep(30)

    return client_id, credential, tenant_id


def load_existing_app_registration() -> tuple[str, str | CngProvisionerCert, str]:
    """Load an already-bootstrapped provisioner app without mutating Entra.

    Utility scripts use this path so read/status/action commands don't create
    app registrations, add permissions, grant consent, or wait for propagation.
    """
    tenant_id = os.environ.get("ENTRABOT_TENANT_ID") or get_state("TENANT_ID")
    client_id = get_state("PROVISIONER_CLIENT_ID")
    if not tenant_id or not client_id:
        raise ProvisionerBootstrapError(
            "Provisioner app is not bootstrapped. Run: python3 scripts/entra_provisioning.py"
        )

    status = _application_status(client_id)
    if status == APP_UNKNOWN:
        raise ProvisionerBootstrapError(
            f"Could not check whether the Provisioner app {client_id} exists in Entra "
            "(see the az error above). Check 'az login' and the network, then try again."
        )
    if status == APP_NOT_FOUND:
        raise ProvisionerBootstrapError(
            "Provisioner app from state was not found in Entra. "
            "Run: python3 scripts/entra_provisioning.py"
        )

    if _uses_cng():
        return client_id, _windows_load_provisioner_credential(tenant_id), tenant_id

    pem_bundle = _keychain_get_cert(tenant_id)
    if not pem_bundle:
        raise ProvisionerBootstrapError(
            "Provisioner certificate private key is missing locally. "
            "Run: python3 scripts/entra_provisioning.py"
        )
    return client_id, pem_bundle, tenant_id


def _windows_load_provisioner_credential(tenant_id: str) -> str | CngProvisionerCert:
    """The Windows credential for read paths: finds one, never creates or migrates.

    The certificate recorded in state wins. A machine set up before the key moved
    into the certificate store still has its PEM and keeps working from it until
    the next setup run migrates it.
    """
    from entrabot.platform import wincert

    try:
        cert = _windows_find_provisioner_cert(tenant_id, recorded_only=True)
        if cert is None:
            legacy_pem = _windows_legacy_pem_read(tenant_id)
            if legacy_pem:
                return legacy_pem
            cert = _windows_find_provisioner_cert(tenant_id)
    except wincert.WinCertError as exc:
        raise ProvisionerBootstrapError(str(exc)) from exc
    if cert is None:
        raise ProvisionerBootstrapError(
            "The Provisioner certificate is not in this user's certificate store. "
            f"Run {_WINDOWS_SETUP_COMMAND} as the user who runs Entrabot."
        )
    return CngProvisionerCert(sha1_hex=cert.sha1_hex, x5t_s256=cert.x5t_s256)


# A certificate uploaded seconds ago isn't always known to the token endpoint yet.
_TOKEN_PROPAGATION_ATTEMPTS = 7
_TOKEN_PROPAGATION_DELAY_SECONDS = 10.0
_TOKEN_TIMEOUT_SECONDS = 30.0
_GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def _new_token_client() -> Any:
    import httpx

    return httpx.Client(timeout=_TOKEN_TIMEOUT_SECONDS)


def _build_cng_assertion(**kwargs: Any) -> str:
    from entrabot.auth.certificate import build_client_assertion

    return build_client_assertion(**kwargs)


def _acquire_graph_token_cng(tenant_id: str, client_id: str, credential: CngProvisionerCert) -> str:
    """Client-credentials token for Graph, proving possession of the CNG key.

    The assertion is signed inside the key store (``cncrypt_signer``), the same way
    the agent body signs Hop 1, so the private key is never in this process.
    """
    import httpx

    from entrabot.auth.cncrypt_signer import SigningError

    endpoint = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    failure = ""
    for attempt in range(_TOKEN_PROPAGATION_ATTEMPTS):
        if attempt:
            print(
                "  The Provisioner certificate isn't recognised by Entra yet; retrying in "
                f"{_TOKEN_PROPAGATION_DELAY_SECONDS:.0f}s "
                f"(attempt {attempt + 1}/{_TOKEN_PROPAGATION_ATTEMPTS})..."
            )
            time.sleep(_TOKEN_PROPAGATION_DELAY_SECONDS)
        try:
            assertion = _build_cng_assertion(
                cert_thumbprint=credential.x5t_s256,
                cert_sha1=credential.sha1_hex,
                client_id=client_id,
                token_endpoint=endpoint,
            )
        except SigningError as exc:
            raise ProvisionerBootstrapError(
                f"The Provisioner certificate could not sign the token request: {exc}"
            ) from exc

        try:
            with _new_token_client() as client:
                response = client.post(
                    endpoint,
                    data={
                        "client_id": client_id,
                        "scope": _GRAPH_DEFAULT_SCOPE,
                        "grant_type": "client_credentials",
                        "client_assertion_type": _ASSERTION_TYPE,
                        "client_assertion": assertion,
                    },
                )
        except httpx.HTTPError as exc:
            raise ProvisionerBootstrapError(
                f"Could not reach the Entra token endpoint ({type(exc).__name__})."
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise ProvisionerBootstrapError(
                f"The Entra token endpoint answered HTTP {response.status_code} with a body "
                "that isn't JSON."
            ) from exc
        if not isinstance(data, dict):
            raise ProvisionerBootstrapError(
                f"The Entra token endpoint answered HTTP {response.status_code} with an "
                "unexpected body."
            )

        # Entra reports failures as an error document, not an exception: check for
        # it before looking at access_token.
        if "error" in data:
            description = str(data.get("error_description", ""))[:400]
            failure = f"{data['error']}: {description}"
            if data["error"] == "invalid_client" and "AADSTS700027" in description:
                continue  # the certificate hasn't reached the token endpoint yet
            raise ProvisionerBootstrapError(f"Provisioner token request failed — {failure}")

        token = data.get("access_token")
        if not token:
            raise ProvisionerBootstrapError(
                "The Entra token response did not contain an access_token."
            )
        return token

    raise ProvisionerBootstrapError(
        "Entra did not accept the Provisioner certificate after "
        f"{_TOKEN_PROPAGATION_ATTEMPTS} attempts — {failure}"
    )


def get_graph_token(
    required_values: list[str] | None = None,
    wait_for_propagation: bool = True,
    auto_provision: bool = True,
) -> str:
    """Get a Graph API access token via the Provisioner app's cert.

    macOS/Linux use ``CertificateCredential`` with the PEM from the OS keystore;
    Windows signs a client assertion with the non-exportable CNG key. Either way
    the private key never comes from disk. Auto-provisions the app + cert on first
    run unless ``auto_provision`` is false.
    """
    if auto_provision:
        if required_values is None:
            required_values = build_required_permission_values()
        client_id, credential, tenant_id = ensure_app_registration(
            required_values,
            wait_for_propagation=wait_for_propagation,
        )
    else:
        client_id, credential, tenant_id = load_existing_app_registration()

    if isinstance(credential, CngProvisionerCert):
        return _acquire_graph_token_cng(tenant_id, client_id, credential)
    pem_bundle = credential

    try:
        from azure.identity import CertificateCredential
    except ImportError as exc:
        raise ProvisionerBootstrapError(
            "azure-identity is required. Install with: pip install -e '.[provisioning]'"
        ) from exc

    certificate_credential = CertificateCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        certificate_data=pem_bundle.encode(),
    )
    return certificate_credential.get_token("https://graph.microsoft.com/.default").token


def get_bootstrap_graph_token(
    required_values: list[str] | None = None,
    wait_for_propagation: bool = True,
) -> str:
    """Get a Graph token, creating or repairing the provisioner app if needed."""
    return get_graph_token(
        required_values=required_values,
        wait_for_propagation=wait_for_propagation,
        auto_provision=True,
    )


def get_existing_graph_token() -> str:
    """Get a Graph token from an already-bootstrapped provisioner app."""
    return get_graph_token(wait_for_propagation=False, auto_provision=False)


# ---------------------------------------------------------------------------
# CLI entrypoint — standalone provisioner bootstrap
# ---------------------------------------------------------------------------


def bootstrap_cli() -> int:
    """Bootstrap the provisioner app and print summary."""
    try:
        required_values = build_required_permission_values()
        ensure_app_registration(required_values)
        print("")
        print("Provisioner bootstrap complete")
        print(f"  Permissions: {', '.join(required_values)}")
        return 0
    except ProvisionerBootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(bootstrap_cli())
