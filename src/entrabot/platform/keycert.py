"""macOS and Linux certificate and private-key lifecycle: create, find, test-sign, delete.

The same job ``entrabot.platform.wincert`` does on Windows, with the same functions, so
setup, certificate rotation, teardown, preflight and the runtime treat a signing certificate
the same way on every system. Where Windows has a certificate store and non-exportable CNG
keys, macOS and Linux have the OS keystore (the Keychain; Secret Service, KWallet or
libsecret) through ``entrabot.platform.get_credential_store``, which refuses any backend
that is not the operating system's own. The rules:

* **One keystore item per certificate**, named by the certificate's SHA-1 thumbprint and
  holding the private key and the certificate together. Two agents on one account are two
  items, so neither overwrites the other's key. (Older setups kept one key for the whole
  account under ``entrabot`` / ``blueprint-private-key``; see :func:`legacy_private_key_pem`.)
* **A key is only "usable" once it has signed.** :func:`test_sign` signs with the stored key
  and verifies the result against the stored certificate, so a key that is not the
  certificate's is found here and not at sign-in.
* **Deleting a certificate deletes its key**: they are one item.
* **Fail closed.** A keystore that can't be reached, or is not an approved one, is an error
  with what to do about it. Nothing is ever written to a file instead.
* RSA 2048, SHA-256, signature-only, no extended key usages: the same shape as on Windows.

Full certificate thumbprints and key material never appear in messages or ``repr`` output.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from entrabot.platform import get_credential_store
from entrabot.platform.wincert import (
    EXPIRY_WARNING_DAYS,
    CertHealth,
    StoreCert,
    WinCertError,
    mask_thumbprint,
)

logger = logging.getLogger("entrabot.platform.keycert")

# The one error type every certificate store raises, under the name the callers that are
# written against ``wincert`` look for and the name that reads right here.
CertStoreError = WinCertError

SERVICE = "entrabot-blueprint-cert"
INDEX_ACCOUNT = "index"  # the SHA-1 thumbprints of the items above, as a JSON list
# Where scripts/entra_provisioning.py keeps the setup (Provisioner) certificate: one PEM
# bundle per tenant.
SETUP_SERVICE = "entrabot-provisioner-cert"
# Where setups before this module kept the Blueprint's private key: one for the account.
LEGACY_SERVICE = "entrabot"
LEGACY_ACCOUNT = "blueprint-private-key"

KSP_KEYCHAIN = "keychain"
KSP_KEYRING = "keyring"
STORE_NAMES = {KSP_KEYCHAIN: "macOS Keychain", KSP_KEYRING: "Linux keyring"}

SETUP_COMMAND = "./scripts/setup.sh"

_SHA1_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
_NAME_PREFIX = "friendly-name: "
_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z ]+)-----.*?-----END \1-----", re.DOTALL)

# Setup certificates shown by setup_certs(): thumbprint -> (service, account). They are
# entra_provisioning's items, so they are not in the index; this is how find, test-sign and
# delete reach one for the rest of the process.
_setup_items: dict[str, tuple[str, str]] = {}


# ─── the keystore ────────────────────────────────────────────────────────────


def _platform() -> str:
    return sys.platform


def _ksp() -> str:
    return KSP_KEYCHAIN if _platform() == "darwin" else KSP_KEYRING


def store_name() -> str:
    """What the person calls the place the keys are in."""
    return STORE_NAMES[_ksp()]


def _no_store_advice() -> str:
    if _platform() == "darwin":
        return "Unlock the login keychain (Keychain Access) and try again."
    return (
        "Entrabot needs a Secret Service keyring that is running and unlocked (for example "
        "gnome-keyring, or KWallet). On a server with no desktop, install gnome-keyring and "
        "run inside a D-Bus session with the keyring unlocked."
    )


def _store() -> Any:
    try:
        return get_credential_store()
    except Exception as exc:  # noqa: BLE001 — any reason there is no store is the same stop
        raise CertStoreError(
            f"This account has no secure key store Entrabot can use ({exc}). {_no_store_advice()}"
        ) from exc


def _retrieve(service: str, account: str) -> str | None:
    store = _store()
    try:
        return store.retrieve(service, account)
    except Exception as exc:  # noqa: BLE001 — keyring backends raise their own types
        raise CertStoreError(
            f"Could not read this account's key store, the {store_name()} "
            f"({type(exc).__name__}: {exc}). {_no_store_advice()}"
        ) from exc


def _save(service: str, account: str, value: str) -> None:
    store = _store()
    try:
        store.store(service, account, value)
    except Exception as exc:  # noqa: BLE001
        raise CertStoreError(
            f"Could not write to this account's key store, the {store_name()} "
            f"({type(exc).__name__}: {exc}). {_no_store_advice()}"
        ) from exc


def _remove(service: str, account: str) -> None:
    store = _store()
    try:
        store.delete(service, account)
    except Exception as exc:  # noqa: BLE001
        raise CertStoreError(
            f"Could not delete from this account's key store, the {store_name()} "
            f"({type(exc).__name__}: {exc})."
        ) from exc


# ─── bundles ─────────────────────────────────────────────────────────────────


def _normalize_sha1(sha1_hex: str) -> str:
    if not isinstance(sha1_hex, str) or not _SHA1_RE.match(sha1_hex):
        raise CertStoreError("The certificate thumbprint must be 40 hexadecimal characters.")
    return sha1_hex.upper()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sha1_of(der: bytes) -> str:
    return hashlib.sha1(der).hexdigest().upper()  # noqa: S324 — an identifier, not a defence


def _blocks(bundle: str) -> tuple[str | None, str | None]:
    """The private-key PEM and the certificate PEM in *bundle*, either of them None."""
    key_pem = cert_pem = None
    for match in _PEM_BLOCK.finditer(bundle):
        kind = match.group(1)
        if kind == "CERTIFICATE" and cert_pem is None:
            cert_pem = match.group(0) + "\n"
        elif kind.endswith("PRIVATE KEY") and key_pem is None:
            key_pem = match.group(0) + "\n"
    return key_pem, cert_pem


def _friendly_name(bundle: str) -> str:
    for line in bundle.splitlines():
        if line.startswith("-----BEGIN"):
            break
        if line.startswith(_NAME_PREFIX):
            return line[len(_NAME_PREFIX) :].strip()
    return ""


def _load_key(key_pem: str) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise CertStoreError("The stored private key could not be read.") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CertStoreError("The stored private key is not an RSA key.")
    return key


def _to_store_cert(bundle: str, *, friendly_name: str | None = None) -> StoreCert:
    key_pem, cert_pem = _blocks(bundle)
    if cert_pem is None:
        raise CertStoreError("A stored certificate could not be parsed.")
    try:
        certificate = x509.load_pem_x509_certificate(cert_pem.encode())
    except ValueError as exc:
        raise CertStoreError("A stored certificate could not be parsed.") from exc
    der = certificate.public_bytes(serialization.Encoding.DER)
    return StoreCert(
        sha1_hex=_sha1_of(der),
        x5t_s256=_b64url(hashlib.sha256(der).digest()),
        der=der,
        subject=certificate.subject.rfc4514_string(),
        friendly_name=friendly_name if friendly_name is not None else _friendly_name(bundle),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        has_private_key=key_pem is not None,
        ksp=_ksp(),
    )


def _bundle(friendly_name: str, key_pem: str, cert_pem: str) -> str:
    name = " ".join(friendly_name.split())  # one line, whatever was passed
    return f"{_NAME_PREFIX}{name}\n{key_pem.strip()}\n{cert_pem.strip()}\n"


def _locate(sha1: str) -> tuple[str, str]:
    return _setup_items.get(sha1, (SERVICE, sha1))


def _read_bundle(sha1: str) -> str | None:
    service, account = _locate(sha1)
    return _retrieve(service, account)


# ─── the index ───────────────────────────────────────────────────────────────


def _read_index() -> list[str]:
    raw = _retrieve(SERVICE, INDEX_ACCOUNT)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item.upper() for item in data if isinstance(item, str) and _SHA1_RE.match(item)]


def _write_index(sha1s: list[str]) -> None:
    if not sha1s:
        # The last certificate is gone: leave no empty item behind in the key store.
        _remove(SERVICE, INDEX_ACCOUNT)
        return
    _save(SERVICE, INDEX_ACCOUNT, json.dumps(sorted(set(sha1s))))


# ─── public API (the same functions as wincert) ──────────────────────────────


def find_cert(sha1_hex: str) -> StoreCert | None:
    """The certificate with this SHA-1 thumbprint in this account's keystore, or None."""
    sha1 = _normalize_sha1(sha1_hex)
    bundle = _read_bundle(sha1)
    if bundle is None:
        return None
    service, account = _locate(sha1)
    name = f"Entrabot Provisioner {account}" if service == SETUP_SERVICE else None
    return _to_store_cert(bundle, friendly_name=name)


def list_certs(*, friendly_name: str | None = None) -> list[StoreCert]:
    """The Blueprint certificates in this account's keystore, optionally only those with this
    friendly name. An index entry whose item is gone or unreadable is skipped."""
    certs: list[StoreCert] = []
    for sha1 in _read_index():
        bundle = _retrieve(SERVICE, sha1)
        if bundle is None:
            continue
        try:
            cert = _to_store_cert(bundle)
        except CertStoreError:
            continue
        if friendly_name is None or cert.friendly_name == friendly_name:
            certs.append(cert)
    return certs


def setup_certs(tenant_id: str) -> list[StoreCert]:
    """This account's setup (Provisioner) certificate for *tenant_id*, as a list of none or one.

    ``scripts/entra_provisioning.py`` keeps it, as its own keystore item per tenant. It is
    named ``Entrabot Provisioner <tenant>`` here, as on Windows, and after this call
    :func:`find_cert`, :func:`test_sign` and :func:`delete_cert` reach it by its thumbprint.
    """
    if not tenant_id:
        return []
    bundle = _retrieve(SETUP_SERVICE, tenant_id)
    if bundle is None:
        return []
    try:
        cert = _to_store_cert(bundle, friendly_name=f"Entrabot Provisioner {tenant_id}")
    except CertStoreError:
        return []
    _setup_items[cert.sha1_hex] = (SETUP_SERVICE, tenant_id)
    return [cert]


def forget_setup_certs() -> None:
    """Drop what :func:`setup_certs` remembered (for tests)."""
    _setup_items.clear()


def load_private_key_pem(sha1_hex: str) -> str | None:
    """The private key that belongs to this certificate, as PEM, or None when the certificate
    is not in this account's keystore. For the code that signs; never log the result."""
    bundle = _read_bundle(_normalize_sha1(sha1_hex))
    if bundle is None:
        return None
    key_pem, _ = _blocks(bundle)
    return key_pem


def test_sign(sha1_hex: str) -> None:
    """Prove the certificate's private key signs on this machine, as this user.

    Signs random data the way Hop 1 does (RSA, PKCS#1 v1.5, SHA-256) and verifies the
    signature against the certificate's public key.

    Raises:
        CertStoreError: the certificate is missing, or its key can't sign for it.
    """
    sha1 = _normalize_sha1(sha1_hex)
    bundle = _read_bundle(sha1)
    if bundle is None:
        raise CertStoreError(
            f"The certificate ending {mask_thumbprint(sha1)} is not in this account's key "
            f"store, the {store_name()}."
        )
    key_pem, cert_pem = _blocks(bundle)
    if key_pem is None or cert_pem is None:
        raise CertStoreError(
            f"The key store item for the certificate ending {mask_thumbprint(sha1)} does not "
            "hold both a certificate and its private key."
        )
    key = _load_key(key_pem)
    try:
        certificate = x509.load_pem_x509_certificate(cert_pem.encode())
    except ValueError as exc:
        raise CertStoreError("The stored certificate could not be parsed.") from exc
    message = os.urandom(32)
    signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    public_key = certificate.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise CertStoreError("The stored certificate does not hold an RSA key.")
    try:
        public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise CertStoreError(
            "The stored private key does not match the certificate stored with it."
        ) from exc


test_sign.__test__ = False  # type: ignore[attr-defined]  # not a pytest test


def _store_pair(friendly_name: str, key_pem: str, cert_pem: str) -> StoreCert:
    """Write one certificate with its key, read it back, and prove it signs."""
    cert = _to_store_cert(_bundle(friendly_name, key_pem, cert_pem))
    sha1 = cert.sha1_hex
    _save(SERVICE, sha1, _bundle(friendly_name, key_pem, cert_pem))
    try:
        _write_index([*_read_index(), sha1])
        test_sign(sha1)
        stored = find_cert(sha1)
        if stored is None or stored.der != cert.der:
            raise CertStoreError(
                f"The {store_name()} did not hand back the certificate that was just stored."
            )
    except CertStoreError:
        _discard(sha1)
        raise
    return stored


def create_signing_cert(
    *, subject: str, friendly_name: str, days: int = 365, prefer_tpm: bool = True
) -> tuple[StoreCert, str | None]:
    """Create a signing certificate whose private key is kept in this account's keystore.

    The second value is always None: it is where ``wincert`` says why the TPM was not used,
    and there is no TPM choice here (*prefer_tpm* is accepted and has no effect).
    """
    del prefer_tpm
    try:
        name = x509.Name.from_rfc4514_string(subject)
    except ValueError as exc:
        raise CertStoreError(f"The certificate subject {subject!r} is not a valid name.") from exc
    _store()  # no store: stop before a key is generated
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    return _store_pair(friendly_name, key_pem, cert_pem), None


def import_cert(*, private_key_pem: str, der: bytes, friendly_name: str) -> StoreCert:
    """Store an existing private key with its certificate as one item, the way
    :func:`create_signing_cert` stores a new pair. For a key an older setup kept on its own.

    Raises:
        CertStoreError: the key is not the certificate's, or the store refused it. Nothing
            is left behind.
    """
    key = _load_key(private_key_pem)
    try:
        certificate = x509.load_der_x509_certificate(der)
    except ValueError as exc:
        raise CertStoreError("That certificate could not be parsed.") from exc
    if public_key_der(private_key_pem) != certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        raise CertStoreError("That private key does not match the certificate.")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    return _store_pair(friendly_name, key_pem, cert_pem)


def delete_cert(sha1_hex: str, *, delete_key: bool = True) -> bool:
    """Remove the certificate and its private key: they are one keystore item.

    When the account's legacy single slot holds this same private key (an older setup's copy
    of it), that goes too, so no copy of the key outlives its certificate. A legacy slot that
    holds any other key is another agent's and is left alone. *delete_key* is accepted for
    ``wincert``'s shape and has no effect.

    That makes this the wrong call for tidying up after a test of a key that was taken over
    from an older setup: code from before this module, in any folder, still signs with the
    legacy slot, and would lose its only key. To remove just this module's item, delete the
    keystore item itself (service :data:`SERVICE`, account = the thumbprint) and its entry in
    the index.

    Returns:
        True when something was deleted, False when the certificate was not there.
    Raises:
        CertStoreError: the item is still in the store.
    """
    del delete_key
    sha1 = _normalize_sha1(sha1_hex)
    service, account = _locate(sha1)
    bundle = _retrieve(service, account)
    if bundle is None:
        if service == SERVICE and sha1 in _read_index():
            _write_index([item for item in _read_index() if item != sha1])
        return False
    _remove(service, account)
    if _retrieve(service, account) is not None:
        raise CertStoreError(
            f"The certificate ending {mask_thumbprint(sha1)} and its private key are still in "
            f"the {store_name()}: the store did not delete them."
        )
    _setup_items.pop(sha1, None)
    if service == SERVICE:
        _write_index([item for item in _read_index() if item != sha1])
        _delete_legacy_copy(bundle)
    return True


def _delete_legacy_copy(bundle: str) -> None:
    """Delete the legacy slot's key when it is the key of the bundle just deleted."""
    key_pem, _ = _blocks(bundle)
    legacy = _retrieve(LEGACY_SERVICE, LEGACY_ACCOUNT)
    if not key_pem or not legacy:
        return
    try:
        same = public_key_der(legacy) == public_key_der(key_pem)
    except CertStoreError:
        return
    if not same:
        return
    _remove(LEGACY_SERVICE, LEGACY_ACCOUNT)
    if _retrieve(LEGACY_SERVICE, LEGACY_ACCOUNT) is not None:
        raise CertStoreError(
            "The certificate was deleted, but an older copy of its private key is still in the "
            f"{store_name()} (item '{LEGACY_SERVICE}', account '{LEGACY_ACCOUNT}')."
        )


def _discard(sha1: str) -> None:
    """Best-effort removal of a certificate that turned out to be unusable."""
    try:
        _remove(SERVICE, sha1)
        _write_index([item for item in _read_index() if item != sha1])
    except CertStoreError as exc:
        logger.warning("Could not clean up an unusable certificate: %s", exc)


# ─── keys from before this module ────────────────────────────────────────────


def legacy_private_key_pem() -> str | None:
    """The Blueprint private key an older setup kept in the account's single slot, or None."""
    return _retrieve(LEGACY_SERVICE, LEGACY_ACCOUNT) or None


def older_key_is_someone_elses(sha1_hexes: Sequence[str]) -> bool:
    """Does the account's older single slot hold a key that belongs to none of these
    certificates? Then another agent, set up before this module, lives on this account, and
    what the two share (the setup certificate) must not be taken away with this one.

    False when the slot is empty, holds something that is not a key, or holds the key of one
    of *sha1_hexes* (this agent's own older copy).
    """
    legacy = _retrieve(LEGACY_SERVICE, LEGACY_ACCOUNT)
    if not legacy:
        return False
    try:
        older = public_key_der(legacy)
    except CertStoreError:
        return False
    for sha1 in sha1_hexes:
        key_pem = load_private_key_pem(sha1)
        if key_pem and public_key_der(key_pem) == older:
            return False
    return True


def public_key_der(private_key_pem: str) -> bytes:
    """The public half of a private key (SubjectPublicKeyInfo, DER): safe to pass around."""
    return (
        _load_key(private_key_pem)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


# ─── health ──────────────────────────────────────────────────────────────────


def cert_health(sha1_hex: str | None, *, now: datetime | None = None) -> CertHealth:
    """Is the configured certificate present, in date, and stored with its key?

    Local only: one keystore read, no network, no signing.
    """
    if not sha1_hex:
        return CertHealth(
            status="missing",
            days_left=None,
            not_after=None,
            message=(
                "No Blueprint certificate is configured (ENTRABOT_BLUEPRINT_CERT_SHA1 is "
                f"not set). Run {SETUP_COMMAND}."
            ),
        )
    cert = find_cert(sha1_hex)
    if cert is None:
        return CertHealth(
            status="missing",
            days_left=None,
            not_after=None,
            message=(
                f"The Blueprint certificate named in .env is not in this account's "
                f"{store_name()}. It was created by a different account or has been removed. "
                f"Run {SETUP_COMMAND} as the user who runs Entrabot."
            ),
        )

    now = now or datetime.now(UTC)
    days_left = (cert.not_after - now).days
    expires = cert.not_after.date().isoformat()
    if not cert.has_private_key:
        status, message = (
            "no_private_key",
            f"The Blueprint certificate is in the {store_name()} but its private key is not "
            f"stored with it. Run {SETUP_COMMAND} to create a new one.",
        )
    elif now > cert.not_after:
        status, message = (
            "expired",
            f"The Blueprint certificate expired on {expires}. "
            f"Run {SETUP_COMMAND} to create and register a new one.",
        )
    elif now < cert.not_before:
        status, message = (
            "not_yet_valid",
            "The Blueprint certificate is not valid yet "
            f"(valid from {cert.not_before.date().isoformat()}). Check this machine's clock.",
        )
    elif days_left <= EXPIRY_WARNING_DAYS:
        status, message = (
            "expiring",
            f"The Blueprint certificate expires on {expires} ({days_left} day(s) left). "
            f"Run {SETUP_COMMAND} --rotate-cert to replace it.",
        )
    else:
        status, message = (
            "ok",
            f"The Blueprint certificate is valid until {expires} ({store_name()}).",
        )
    return CertHealth(status=status, days_left=days_left, not_after=cert.not_after, message=message)


def days_until_expiry(sha1_hex: str, *, now: datetime | None = None) -> int | None:
    """Whole days until the certificate expires (negative once it has), or None if missing."""
    cert = find_cert(sha1_hex)
    if cert is None:
        return None
    return (cert.not_after - (now or datetime.now(UTC))).days


__all__ = [
    "EXPIRY_WARNING_DAYS",
    "INDEX_ACCOUNT",
    "KSP_KEYCHAIN",
    "KSP_KEYRING",
    "LEGACY_ACCOUNT",
    "LEGACY_SERVICE",
    "SERVICE",
    "SETUP_COMMAND",
    "SETUP_SERVICE",
    "CertHealth",
    "CertStoreError",
    "StoreCert",
    "WinCertError",
    "cert_health",
    "create_signing_cert",
    "days_until_expiry",
    "delete_cert",
    "find_cert",
    "forget_setup_certs",
    "import_cert",
    "legacy_private_key_pem",
    "list_certs",
    "load_private_key_pem",
    "mask_thumbprint",
    "older_key_is_someone_elses",
    "public_key_der",
    "setup_certs",
    "store_name",
    "test_sign",
]
