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

An existing app registration with the Provisioner's name is only taken over when this run
would grant nobody anything: an administrator has already approved it for every permission
setup needs, or nobody else can use it. Otherwise setup stops and says why; set
ENTRABOT_PROVISIONER_APP_ID_VERIFIED to the application ID of the app you have checked to
use that one.
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
from dataclasses import dataclass, field
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
            # The thumbprint is the certificate's public identifier (what Windows and Entra
            # show), not a security digest. It is taken through the certificate object.
            try:
                from cryptography import x509
                from cryptography.hazmat.primitives import hashes

                thumbprint = x509.load_der_x509_certificate(der).fingerprint(
                    hashes.SHA1()  # noqa: S303 — an identifier, not a security hash
                )
            except ValueError:
                thumbprint = b""
            if thumbprint:
                return {thumbprint.hex().upper()}

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


# How ``az login`` signs in on Windows: the Windows account broker (the CLI's default
# there) or the browser. Same name as ``entrabot.winsetup.azcli.BROKER_SETTING``.
AZ_BROKER_SETTING = "AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"


def _az_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """The environment for ``az``. On Windows it is told to use the browser sign-in, which
    is the one the setup wizard makes, unless the person set the choice themselves: a
    script run by hand would otherwise look for a sign-in held by the Windows account
    broker and find none."""
    if sys.platform != "win32":
        return env
    result = dict(os.environ if env is None else env)
    if not any(name.upper() == AZ_BROKER_SETTING for name in result):
        result[AZ_BROKER_SETTING] = "false"
    return result


def run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
    """Run an az CLI command, return (returncode, stdout, stderr)."""
    command, env = _az_command(args)
    env = _az_env(env)
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

    Graph is the resource whose service principal has Graph's well-known application ID,
    matched by object ID: an app that only calls itself "Microsoft Graph" doesn't count.
    """
    # Two steps, by object ID: the one-call form addresses the service principal as
    # servicePrincipals(appId='...'), and those parentheses don't survive az.cmd on
    # Windows. Filtering happens here rather than in a JMESPath --query for the
    # same reason.
    rc, out, _ = run_az(["ad", "sp", "show", "--id", client_id, "--query", "id", "-o", "json"])
    sp_object_id = _az_scalar(out) if rc == 0 else ""
    if not sp_object_id:
        return set()
    try:
        graph_id = _graph_service_principal_id()
        assignments, _complete = _app_role_assignments(sp_object_id)
    except ProvisionerBootstrapError:
        return set()
    return _graph_role_ids(assignments, sp_object_id, graph_id)


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
# Which app registration is the Provisioner
# ---------------------------------------------------------------------------
#
# The Provisioner app is granted tenant-wide Microsoft Graph application permissions with
# admin consent. Display names are not unique, and anyone allowed to register applications
# can give an app this one. So a machine with no Provisioner app recorded takes over an
# existing app only when this run would grant nobody anything: an administrator has
# already approved it for every permission setup needs, or nobody else can use it (see
# _assess_candidate). Otherwise the operator removes the app they don't recognise, or
# names the right one in ENTRABOT_PROVISIONER_APP_ID_VERIFIED: a variable, because setup
# runs unattended, and not ENTRABOT_PROVISIONER_APP_ID, which setup itself writes to .env.

VERIFIED_APP_ID_ENV = "ENTRABOT_PROVISIONER_APP_ID_VERIFIED"

# Printed on a line of its own when an existing app is not taken over. The Windows setup
# wizard (src/entrabot/winsetup/wizard.py) looks for exactly this line to explain the
# refusal; tests on both sides pin it. It uses the wizard's ENTRABOT_RESULT= convention.
TAKEOVER_REFUSED_MARKER = "ENTRABOT_RESULT=provisioner-takeover-refused"
# Printed the same way, just before the error, when the app ENTRABOT_PROVISIONER_APP_ID_VERIFIED
# names is not accepted (not an application ID, no such app, or not the app this machine already
# uses), and when setup could not read from Entra what it needed to decide (a lookup of the app,
# by name or by ID, or the second read of its admin consent failed). Nothing in Entra changed.
APP_CHOICE_REJECTED_MARKER = "ENTRABOT_RESULT=provisioner-app-choice-rejected"
COULD_NOT_CHECK_MARKER = "ENTRABOT_RESULT=provisioner-could-not-check"

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_AZ_ERROR_LIMIT = 160


def _printable(value: object, limit: int = 120) -> str:
    """Directory text, safe to print: control characters (a terminal escape sequence in an
    owner's display name, say) become ``?``, and a long value is cut short."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _az_error(err: str, rc: int) -> str:
    """What az said on stderr, on one line and safe to print."""
    text = " ".join((err or "").split())
    return _printable(text, _AZ_ERROR_LIMIT) if text else f"az exited {rc}"


def _az_json(args: list[str]) -> Any:
    """Run a read-only az command and return its JSON output. Raises when it fails."""
    rc, out, err = run_az([*args, "-o", "json"])
    if rc != 0:
        raise ProvisionerBootstrapError(_az_error(err, rc))
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError as exc:
        raise ProvisionerBootstrapError(
            f"az printed something that isn't JSON ({exc.msg})"
        ) from exc


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProvisionerBootstrapError("az did not return a list of directory objects")
    return value


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _provisioner_app_override() -> str | None:
    """The application ID set in ENTRABOT_PROVISIONER_APP_ID_VERIFIED, lower case, or None.

    Raises when it is set to something that isn't an application ID. The value isn't
    repeated: whatever was pasted there by mistake must not end up in the setup log.
    """
    value = os.environ.get(VERIFIED_APP_ID_ENV, "").strip()
    if not value:
        return None
    if not _GUID_RE.match(value):
        print(APP_CHOICE_REJECTED_MARKER)
        raise ProvisionerBootstrapError(
            f"{VERIFIED_APP_ID_ENV} is set, but not to an application ID (a GUID such as "
            "00000000-0000-0000-0000-000000000000). Nothing was changed in Microsoft Entra. "
            "Set it to the Application (client) ID of the app registration you have checked, "
            "or unset it, then run setup again."
        )
    return value.lower()


def _signed_in_user_object_id() -> tuple[str | None, str]:
    """(object ID of the account az is signed in with, "") or (None, why it isn't known)."""
    rc, out, err = run_az(["ad", "signed-in-user", "show", "--query", "id", "-o", "json"])
    if rc != 0:
        return None, _az_error(err, rc)
    user_id = _az_scalar(out)
    if not _GUID_RE.match(user_id):
        return None, "az did not return an object ID"
    return user_id.lower(), ""


def _service_principal_object_id(app_id: str) -> str:
    """Object ID of this app's service principal. Raises when it can't be read."""
    rc, out, err = run_az(["ad", "sp", "show", "--id", app_id, "--query", "id", "-o", "json"])
    object_id = _az_scalar(out) if rc == 0 else ""
    if not _GUID_RE.match(object_id):
        raise ProvisionerBootstrapError(
            _az_error(err, rc) if rc != 0 else "az did not return an object ID"
        )
    return object_id.lower()


def _graph_service_principal_id() -> str:
    """Object ID of Microsoft Graph's service principal in this tenant, looked up by Graph's
    well-known application ID: an app that only calls itself "Microsoft Graph" is not it.
    Raises when it can't be read."""
    found = _az_json(["ad", "sp", "show", "--id", MS_GRAPH_API_ID, "--query", "[id,appId]"])
    if not (
        isinstance(found, list)
        and len(found) == 2
        and all(isinstance(value, str) for value in found)
        and _GUID_RE.match(found[0])
        and found[1].lower() == MS_GRAPH_API_ID
    ):
        raise ProvisionerBootstrapError(
            "az returned something that is not Microsoft Graph's service principal"
        )
    return found[0].lower()


def _app_role_assignments(principal_id: str) -> tuple[list[dict[str, Any]], bool]:
    """A service principal's appRoleAssignments, the application permissions admin consent
    gave it, and whether that is all of them. Raises when they can't be read."""
    url = f"https://graph.microsoft.com/v1.0/servicePrincipals/{principal_id}/appRoleAssignments"
    found = _az_json(["rest", "--method", "GET", "--url", url])
    entries = found.get("value") if isinstance(found, dict) else None
    if not isinstance(entries, list):
        raise ProvisionerBootstrapError("az did not return a list of app role assignments")
    complete = not found.get("@odata.nextLink")
    return [entry for entry in entries if isinstance(entry, dict)], complete


def _graph_role_ids(
    assignments: list[dict[str, Any]], principal_id: str, graph_id: str
) -> set[str]:
    """The Microsoft Graph application permissions among these assignments: granted to this
    principal, by Graph's own service principal (matched by object ID, never by name)."""
    return {
        str(entry["appRoleId"])
        for entry in assignments
        if entry.get("appRoleId")
        and str(entry.get("principalId", "")).lower() == principal_id.lower()
        and str(entry.get("resourceId", "")).lower() == graph_id.lower()
    }


def _local_provisioner_thumbprints(tenant_id: str, stored_pem: str | None) -> set[str]:
    """SHA-1 thumbprints (upper-case hex) of the Provisioner certificates whose private key
    is on this machine. Raises when the certificate store can't be read."""
    if not _uses_cng():
        if not stored_pem:
            return set()
        try:
            return {_thumbprint_from_cert_pem(_cert_pem_from_bundle(stored_pem)).upper()}
        except (ProvisionerBootstrapError, ValueError):
            return set()  # an unreadable bundle is no key this machine can sign with

    from entrabot.platform import wincert

    try:
        certs = wincert.list_certs(
            friendly_name=_PROVISIONER_FRIENDLY_NAME.format(tenant_id=tenant_id)
        )
        legacy_pem = _windows_legacy_pem_read(tenant_id)
    except (wincert.WinCertError, OSError) as exc:
        raise ProvisionerBootstrapError(str(exc)) from exc
    # A certificate without its private key proves nothing: anyone can import one.
    found = {cert.sha1_hex.upper() for cert in certs if cert.has_private_key}
    legacy = _legacy_cert_identity(legacy_pem) if legacy_pem else None
    if legacy is not None:
        found.add(legacy[0])
    return found


def _certificate_sha1(entry: dict[str, Any]) -> str | None:
    """SHA-1 of the certificate in a ``keyCredentials`` entry, from its key material only.

    ``customKeyIdentifier`` is whatever the uploader said it is, so it can't show whose
    certificate this is.
    """
    thumbprints = _key_credential_thumbprints({"key": entry.get("key")})
    return next(iter(thumbprints)) if thumbprints else None


def _created_on(app: dict[str, Any]) -> str:
    value = app.get("createdDateTime")
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return "on an unknown date"


def _owner_label(owner: dict[str, Any], me: str | None) -> str:
    """An owner's sign-in name (else display name), marked when it is the signed-in user."""
    name = owner.get("userPrincipalName") or owner.get("displayName") or owner.get("id")
    label = _printable(name or "an owner that could not be identified")
    if str(owner.get("@odata.type", "")).endswith("servicePrincipal"):
        label = f"{label} (an application)"
    if me is not None and str(owner.get("id", "")).lower() == me:
        label = f"you ({label})"
    return label


def _other_owners(owners: list[dict[str, Any]], me: str | None) -> list[str]:
    """Owners other than the signed-in user. None when who is signed in isn't known: that
    is reported once for every candidate, and blocks the takeover by itself."""
    if me is None:
        return []
    return [_owner_label(o, me) for o in owners if str(o.get("id", "")).lower() != me]


def _permissions_beyond(app: dict[str, Any], role_ids: set[str]) -> int | None:
    """How many permissions the app declares besides these Microsoft Graph application
    permissions (``role_ids`` in lower case), or None when what it declares can't be read."""
    declared = app.get("requiredResourceAccess")
    if not isinstance(declared, list):
        return None
    extra = 0
    for resource in declared:
        accesses = resource.get("resourceAccess") if isinstance(resource, dict) else None
        if not isinstance(accesses, list) or not all(isinstance(a, dict) for a in accesses):
            return None
        for access in accesses:
            if not (
                str(resource.get("resourceAppId", "")).lower() == MS_GRAPH_API_ID
                and access.get("type") == "Role"
                and str(access.get("id", "")).lower() in role_ids
            ):
                extra += 1
    return extra


@dataclass(frozen=True)
class _Facts:
    """What every candidate is judged against, read once."""

    me: str | None  # the signed-in user's object ID
    local: set[str] | None  # thumbprints of the certificates whose private key is here
    local_error: str
    role_ids: set[str] | None  # Graph app role IDs, lower case, of every permission needed
    graph_id: str | None  # object ID of Microsoft Graph's service principal in this tenant
    graph_error: str


@dataclass
class _Candidate:
    """An existing app registration with the Provisioner's name, as far as it was read."""

    app_id: str
    created: str
    owners: str = "could not be read"
    unsafe: list[str] = field(default_factory=list)  # who else could use it, or what it'd get
    unknown: list[str] = field(default_factory=list)  # what could not be checked
    approved: bool = False  # an administrator approved everything; taking it over grants nothing
    approval: str = ""  # what to say about that approval, if anything


def _assess_candidate(app: dict[str, Any], facts: _Facts) -> _Candidate:
    """Read what decides whether this app may become the Provisioner without being named.

    It may when this run would grant nobody anything by taking it over. That is so in two
    cases. The first is ``approved`` (see _approval): an administrator has already given it
    admin consent for every permission setup needs, so whoever can use it already could, and
    this run consents nothing. Otherwise everything below must hold (``unsafe`` and
    ``unknown`` stay empty): nobody but this machine and the tenant's administrators can use
    it or change it, now or later, and admin consent would grant it nothing setup doesn't
    ask for.

    * Every owner of the app, and of its service principal, is the signed-in user, or it
      has none. Owners can add credentials, so anyone else could let themselves in later.
      An app with no owners (an administrator can create one, with an app-only token for
      example, and owners can be removed) can only be changed by an administrator, who
      could change any app anyway. With the next point, nobody else can use it: that is
      the same guarantee as an app you alone own, so it is taken over the same way.
    * The app has no client secret and no federated credential, its service principal
      has no credential of its own, and every certificate on the app is one whose
      private key is on this machine. Owners alone don't show this: whoever registered
      the app can leave a way in, then make you its only owner or remove themselves.
    * It declares no permission but setup's Graph application permissions. Admin consent
      grants everything declared: a delegated scope, with a redirect URI someone else
      controls, would let them collect other users' tokens.

    Once taken over, only an administrator can change any of this before consent is given.
    """
    app_id = str(app.get("appId") or "")
    object_id = str(app.get("id") or "")
    candidate = _Candidate(app_id=_printable(app_id) or "(none)", created=_created_on(app))
    if not (_GUID_RE.match(app_id) and _GUID_RE.match(object_id)):
        candidate.unknown.append("the directory returned it without usable IDs")
        return candidate
    me, local, local_error, role_ids = facts.me, facts.local, facts.local_error, facts.role_ids

    try:
        owners = _objects(_az_json(["ad", "app", "owner", "list", "--id", app_id]))
    except ProvisionerBootstrapError as exc:
        candidate.unknown.append(f"its owners could not be read ({exc})")
    else:
        candidate.owners = ", ".join(_owner_label(owner, me) for owner in owners) or "none"
        others = _other_owners(owners, me)
        if others:
            candidate.unsafe.append(f"someone other than you owns it: {', '.join(others)}")

    secrets = app.get("passwordCredentials")
    if not isinstance(secrets, list):
        candidate.unknown.append("its client secrets could not be read")
    elif secrets:
        candidate.unsafe.append(f"it has {_count(len(secrets), 'client secret')}")

    # Graph returns a certificate's key material only when keyCredentials is selected on
    # a single application.
    url = f"https://graph.microsoft.com/v1.0/applications/{object_id}?$select=keyCredentials"
    try:
        found = _az_json(["rest", "--method", "GET", "--url", url])
        certificates = _objects(found.get("keyCredentials") if isinstance(found, dict) else None)
    except ProvisionerBootstrapError as exc:
        candidate.unknown.append(f"its certificates could not be read ({exc})")
    else:
        if certificates and local is None:
            candidate.unknown.append(
                f"whether its certificates are this machine's could not be checked ({local_error})"
            )
        elif certificates:
            foreign = [entry for entry in certificates if _certificate_sha1(entry) not in local]
            if foreign:
                candidate.unsafe.append(
                    f"it has {_count(len(foreign), 'certificate')} whose private key is not "
                    "on this machine"
                )

    try:
        federated = _objects(
            _az_json(["ad", "app", "federated-credential", "list", "--id", app_id])
        )
    except ProvisionerBootstrapError as exc:
        candidate.unknown.append(f"its federated credentials could not be read ({exc})")
    else:
        if federated:
            candidate.unsafe.append(
                f"it has {_count(len(federated), 'federated credential')}, so an identity "
                "from elsewhere can sign in as it"
            )

    if role_ids is not None:
        extra = _permissions_beyond(app, role_ids)
        if extra is None:
            candidate.unknown.append("the permissions it asks for could not be read")
        elif extra:
            candidate.unsafe.append(
                f"it asks for {_count(extra, 'permission')} setup does not use, which admin "
                "consent would grant as well"
            )

    _assess_service_principal(candidate, app, facts)
    return candidate


def _assess_service_principal(candidate: _Candidate, app: dict[str, Any], facts: _Facts) -> None:
    """The app's service principal, if it has one yet: the admin consent it holds, its own
    credentials, and its owners."""
    app_id = str(app.get("appId") or "")
    try:
        principals = [
            principal
            for principal in _objects(_az_json(["ad", "sp", "list", "--spn", app_id]))
            if str(principal.get("appId", "")).lower() == app_id.lower()
        ]
    except ProvisionerBootstrapError as exc:
        candidate.unknown.append(f"its service principal could not be read ({exc})")
        return
    if not principals:
        return  # nothing approved yet; setup creates it, as you
    if len(principals) > 1:
        candidate.unknown.append("it has more than one service principal")
        return
    principal = principals[0]
    principal_id = str(principal.get("id") or "")
    if not _GUID_RE.match(principal_id):
        candidate.unknown.append("its service principal could not be identified")
        return
    candidate.approved, candidate.approval = _approval(app, principal_id, facts)

    credentials = [principal.get("passwordCredentials"), principal.get("keyCredentials")]
    if not all(isinstance(entries, list) for entries in credentials):
        candidate.unknown.append("its service principal's credentials could not be read")
    elif any(credentials):
        total = sum(len(entries) for entries in credentials)
        candidate.unsafe.append(
            f"its service principal has {_count(total, 'credential')} of its own"
        )
    try:
        owners = _objects(_az_json(["ad", "sp", "owner", "list", "--id", principal_id]))
    except ProvisionerBootstrapError as exc:
        candidate.unknown.append(f"its service principal's owners could not be read ({exc})")
        return
    others = _other_owners(owners, facts.me)
    if others:
        candidate.unsafe.append(
            f"someone other than you owns its service principal: {', '.join(others)}"
        )


def _approval(app: dict[str, Any], principal_id: str, facts: _Facts) -> tuple[bool, str]:
    """Whether an administrator has already approved this app for every Microsoft Graph
    permission setup needs, so that taking it over grants nobody anything; and what to say.

    The approval read is the admin consent on its service principal: appRoleAssignments
    from Graph's own service principal, found by Graph's application ID and matched by
    object ID. It must cover every permission setup needs, or this run would consent the
    rest. Every other permission the app declares must be approved already too, or admin
    consent (never given in this run, but given by a later one) would grant it. Anything
    that can't be read means "not approved": the strict rule then decides.
    """
    unreadable = "whether an administrator has approved it could not be read"
    if facts.role_ids is None:
        return False, ""  # what setup needs is unknown, which is said once for all of them
    if facts.graph_id is None:
        return False, f"{unreadable} ({facts.graph_error})"
    try:
        assignments, complete = _app_role_assignments(principal_id)
    except ProvisionerBootstrapError as exc:
        return False, f"{unreadable} ({exc})"
    if not complete:
        return False, f"{unreadable} (there are more of them than one read returns)"
    granted = {role.lower() for role in _graph_role_ids(assignments, principal_id, facts.graph_id)}
    missing = facts.role_ids - granted
    if missing:
        return False, (
            f"not yet approved by an administrator for {len(missing)} of the permissions "
            "setup needs"
        )
    unapproved = _permissions_beyond(app, granted)
    if unapproved is None:
        return False, ""  # the strict rule says the permissions could not be read
    if unapproved:
        return False, (
            "approved by an administrator for the permissions setup needs, but it also asks "
            f"for {_count(unapproved, 'permission')} no administrator has approved"
        )
    return True, "already approved by an administrator for the permissions setup needs"


def _record_provisioner_app(app_id: str) -> None:
    set_state("PROVISIONER_CLIENT_ID", app_id)
    # A certificate recorded here was registered on whichever app was recorded before, so
    # it says nothing about this one. Unrecorded, this machine's certificate is uploaded.
    clear_state("PROVISIONER_CERT_THUMBPRINT")


def _create_provisioner_app() -> str:
    """Create a new Provisioner app registration and record it."""
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
            term in lowered for term in ["insufficient", "authorization", "privilege", "permission"]
        ):
            _print_admin_required("create the dedicated provisioner app registration", err)
        raise ProvisionerBootstrapError(err or "app registration creation failed")
    print(f"  Created provisioner app: {created_app_id}")
    _record_provisioner_app(created_app_id)
    return created_app_id


def _use_named_app(app_id: str) -> str:
    """The app ENTRABOT_PROVISIONER_APP_ID_VERIFIED names, once Entra shows it exists. Recorded.

    The operator has checked this app, so it is not examined the way an app found by name
    is; it only has to exist, under that application ID.
    """
    try:
        listed = _objects(_az_json(["ad", "app", "list", "--app-id", app_id]))
    except ProvisionerBootstrapError as exc:
        print(COULD_NOT_CHECK_MARKER)
        raise ProvisionerBootstrapError(
            f"Could not look up the app registration {VERIFIED_APP_ID_ENV} names "
            f"({app_id}): {exc}. Nothing was changed in Microsoft Entra. Fix that (for "
            "example, run 'az login') and run setup again."
        ) from exc
    apps = [app for app in listed if str(app.get("appId", "")).lower() == app_id]
    if not apps:
        print(APP_CHOICE_REJECTED_MARKER)
        raise ProvisionerBootstrapError(
            f"{VERIFIED_APP_ID_ENV} is set to {app_id}, but no app registration in this "
            "tenant has that application ID. Nothing was changed in Microsoft Entra. Set it "
            "to the Application (client) ID of the app registration you have checked, or "
            "unset it, then run setup again."
        )
    if len(apps) > 1:
        print(APP_CHOICE_REJECTED_MARKER)
        raise ProvisionerBootstrapError(
            f"Microsoft Entra returned {len(apps)} app registrations with the application ID "
            f"{app_id}. Nothing was changed in Microsoft Entra."
        )
    name = _printable(apps[0].get("displayName") or "")
    print(f"  Using the provisioner app {VERIFIED_APP_ID_ENV} names: {app_id} ('{name}')")
    _record_provisioner_app(app_id)
    return app_id


def _confirm_already_approved(client_id: str, required_values: list[str]) -> bool:
    """For an app taken over because an administrator already approved it: read its admin
    consent again, just before setup goes on, and consent nothing.

    This stands in for _ensure_permissions_and_consent, which would run admin consent
    (granting everything the app declares) if anything were missing. Raises, with nothing
    changed, when the approval no longer covers every permission setup needs: this run
    would then have to consent. Returns False: no permission changed, nothing to wait for.
    """
    required = {
        spec.split("=", 1)[0].lower() for _, spec in _resolve_permission_specs(required_values)
    }
    try:
        principal_id = _service_principal_object_id(client_id)
        assignments, complete = _app_role_assignments(principal_id)
        if not complete:
            raise ProvisionerBootstrapError("there are more of them than one read returns")
        graph_id = _graph_service_principal_id()
    except ProvisionerBootstrapError as exc:
        print(COULD_NOT_CHECK_MARKER)
        raise ProvisionerBootstrapError(
            f"Could not read the admin consent of the Provisioner app {client_id} again "
            f"({exc}), so setup did not take it over. Nothing was changed in Microsoft "
            "Entra. Run setup again."
        ) from exc
    granted = {role.lower() for role in _graph_role_ids(assignments, principal_id, graph_id)}
    missing = required - granted
    if missing:
        raise ProvisionerBootstrapError(
            f"The Provisioner app {client_id} is no longer approved by an administrator for "
            f"{len(missing)} of the permissions setup needs, so setup did not take it over: "
            "this run would have to consent. Nothing was changed in Microsoft Entra. Run "
            "setup again."
        )
    print("  Provisioner app already has the Graph permissions setup needs, approved by an")
    print("  administrator; nothing is consented for it in this run.")
    return False


def _print_takeover_refused(candidates: list[_Candidate], reasons: list[str]) -> None:
    name = PROVISIONER_APP_DISPLAY_NAME
    one = len(candidates) == 1
    print(TAKEOVER_REFUSED_MARKER)
    if one:
        print(f"  An app registration named '{name}' already exists, and this")
        print("  machine has no Provisioner app recorded. Setup did not take it over.")
    else:
        print(f"  {len(candidates)} app registrations are named '{name}', and this")
        print("  machine has no Provisioner app recorded. Setup did not take any of them over:")
        print("  it can't tell which one, if any, an earlier setup run created.")
    for line in (
        "The Provisioner app gets tenant-wide Microsoft Graph permissions with admin consent, so",
        "setup takes an existing app over only when an administrator has already approved it for",
        "all of them, or when nobody else can use it: you are its only owner (or it has none),",
        "every credential on it is this machine's, and it asks for no other permissions.",
    ):
        print(f"  {line}")
    for reason in reasons:
        print(f"  {reason}")
    for candidate in candidates:
        print(f"    - '{name}' (application ID {candidate.app_id})")
        print(f"      created {candidate.created}, owners: {candidate.owners}")
        notes = [candidate.approval] if candidate.approval else []
        for problem in [*notes, *candidate.unsafe, *candidate.unknown]:
            print(f"      {problem}")
    print(
        "  Nothing was changed in Microsoft Entra. To go on, do one of these and run setup again:"
    )
    if reasons or any(candidate.unknown for candidate in candidates):
        print("    - fix what could not be read above (for example, run 'az login', or activate")
        print("      your administrator role);")
    which = "it, if you don't recognise it" if one else "the ones you don't recognise"
    print(f"    - delete or rename {which} (Entra admin center >")
    print("      App registrations > All applications);")
    which = "it is yours" if one else "one of them is yours"
    print(f"    - or, once you have checked that {which}, set")
    print(f"      {VERIFIED_APP_ID_ENV} to its application ID.")


def _provisioner_app_for_this_machine(
    named: str | None, tenant_id: str, stored_pem: str | None, required_values: list[str]
) -> tuple[str, bool]:
    """The Provisioner app for a machine that has none recorded: the app
    ENTRABOT_PROVISIONER_APP_ID_VERIFIED names; else the one app with the Provisioner's
    name, when taking it over grants nobody anything; else, when no app has that name, a
    new one. Returns (application ID, whether this run may give it admin consent).

    It is recorded here, except an app taken over because an administrator already approved
    it: the caller records that one once the approval has been read again (see
    _confirm_already_approved), and this run consents nothing for it.

    Raises, with nothing in Entra changed, when an app has the name but can't be shown to
    be safe to take over, or when looking could not be finished: "could not check" is
    never "safe".
    """
    if named:
        return _use_named_app(named), True

    name = PROVISIONER_APP_DISPLAY_NAME
    try:
        listed = _objects(_az_json(["ad", "app", "list", "--display-name", name]))
    except ProvisionerBootstrapError as exc:
        print(COULD_NOT_CHECK_MARKER)
        raise ProvisionerBootstrapError(
            f"Could not look for an existing '{name}' app registration ({exc}). Nothing was "
            "changed in Microsoft Entra. Fix that (for example, run 'az login') and run "
            "setup again."
        ) from exc
    # az matches the start of the name, in any case. Only the exact name is one this
    # script could have created; an app with another name is never taken over.
    apps = [app for app in listed if app.get("displayName") == name]
    if not apps:
        return _create_provisioner_app(), True

    reasons: list[str] = []
    me, me_error = _signed_in_user_object_id()
    if me is None:
        whose = "it" if len(apps) == 1 else "them"
        reasons.append(
            f"Who is signed in could not be read ({me_error}), so who else owns {whose} is unknown."
        )
    local: set[str] | None
    try:
        local, local_error = _local_provisioner_thumbprints(tenant_id, stored_pem), ""
    except ProvisionerBootstrapError as exc:
        local, local_error = None, str(exc)
    role_ids: set[str] | None = None
    try:
        graph_roles = resolve_graph_permissions()
    except ProvisionerBootstrapError as exc:
        reasons.append(
            f"Microsoft Graph's permissions could not be read ({exc}), so what admin consent "
            "would grant is unknown."
        )
    else:
        unknown_values = [value for value in required_values if value not in graph_roles]
        if unknown_values:
            reasons.append(
                f"Microsoft Graph has no permission named {', '.join(unknown_values)} in this "
                "tenant, so what setup needs can't be checked."
            )
        else:
            role_ids = {str(graph_roles[value]).lower() for value in required_values}
    graph_id: str | None
    try:
        graph_id, graph_error = _graph_service_principal_id(), ""
    except ProvisionerBootstrapError as exc:
        graph_id, graph_error = None, str(exc)

    facts = _Facts(me, local, local_error, role_ids, graph_id, graph_error)
    candidates = [_assess_candidate(app, facts) for app in apps]
    (first, *rest) = candidates
    if not rest and not reasons and not first.unsafe and not first.unknown:
        print(f"  Found existing provisioner app: {first.app_id}")
        who = "it has no owners" if first.owners == "none" else f"its only owner is {first.owners}"
        print(f"  Taking it over: {who},")
        print("  and nothing on it lets anyone else sign in as it.")
        _record_provisioner_app(first.app_id)
        return first.app_id, True
    if not rest and first.approved:
        # Whoever can use it already holds everything setup needs, and this run consents
        # nothing; its owners and certificates (other computers', usually) don't change that.
        print(f"  Found existing provisioner app: {first.app_id}")
        print(f"  Taking it over: {first.approval}.")
        return first.app_id, False

    _print_takeover_refused(candidates, reasons)
    raise ProvisionerBootstrapError(
        f"Setup did not take over an existing '{name}' app registration (see above), and "
        "nothing was changed in Microsoft Entra. Delete or rename the one you don't "
        f"recognise, or set {VERIFIED_APP_ID_ENV} to the application ID of the one you "
        "have checked is yours, then run setup again."
    )


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

    The app is the one recorded in the state file by an earlier run on this machine. With
    none recorded it is the app ``ENTRABOT_PROVISIONER_APP_ID_VERIFIED`` names, else an
    existing app with the Provisioner's name that this run would grant nobody anything by
    taking over, else a new one; an app with the name that can't be shown to be safe stops
    setup before anything in Entra changes.
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
    named = _provisioner_app_override()
    # Windows keeps the key in CNG; its certificate is looked up further down.
    stored_pem = None if _uses_cng() else _keychain_get_cert(tenant_id)
    pem_bundle = stored_pem

    # Stale-app detection. Only Entra saying "no such app" makes the cached app stale;
    # a lookup that failed changes nothing, because acting on it would throw away a
    # working credential.
    if client_id:
        status = _application_status(client_id)
        if status == APP_UNKNOWN:
            print(COULD_NOT_CHECK_MARKER)
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

    # The recorded app is trusted: an earlier run on this machine created or took it over.
    # Naming a different one isn't a way to move off it, and is refused before any change.
    if client_id and named and named != client_id.lower():
        print(APP_CHOICE_REJECTED_MARKER)
        raise ProvisionerBootstrapError(
            f"{VERIFIED_APP_ID_ENV} is set to {named}, but this machine already uses the "
            f"Provisioner app {client_id} (recorded in {_STATE_FILE.name} by an earlier setup "
            "run). Nothing was changed in Microsoft Entra. To keep using it, unset "
            f"{VERIFIED_APP_ID_ENV} and run setup again."
        )

    # Find, take over, or create the Provisioner app registration: before any certificate
    # upload, permission change or consent.
    may_consent = True
    if not client_id:
        client_id, may_consent = _provisioner_app_for_this_machine(
            named, tenant_id, stored_pem, required_values
        )

    if may_consent:
        permissions_changed = _ensure_permissions_and_consent(client_id, required_values)
    else:
        # Taken over because an administrator already approved it: this run consents
        # nothing, and the app is recorded only once that approval has been read again, so
        # a run that stops here leaves nothing behind that a later run would consent for.
        permissions_changed = _confirm_already_approved(client_id, required_values)
        _record_provisioner_app(client_id)

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
