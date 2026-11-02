"""Read and update the certificates registered on a Blueprint application.

Microsoft Graph treats ``PATCH /applications/{id}`` with ``keyCredentials`` as a
replacement of the whole collection, so a one-element list removes every other
machine's certificate (#124). Everything that changes the list goes through this
module, which holds to three rules:

* **A failed read is never an empty list.** Any non-200 response or malformed body
  raises :class:`KeyCredentialsReadError`. No PATCH can happen unless the GET that
  precedes it returned 200, because a PATCH built from a wrong list would wipe it.
* **Existing entries pass through exactly as Graph returned them.** Nothing is
  de-duplicated by display name: short hostnames collide (many Macs are literally
  named ``MacBook-Pro``), and matching on them would silently bring #124 back.
* **A write is not done until it has been read back.** Graph is eventually
  consistent, so verification retries for a bounded time. A write whose outcome
  can't be confirmed raises :class:`RegistrationUnconfirmedError`, which is distinct
  from :class:`RegistrationRejectedError` because the private key for a certificate
  that may have landed must not be thrown away.

The read and the write aren't atomic, so two machines writing in the same moment can
still lose one of the two new certificates. The loser finds out when its own
verification doesn't see its certificate.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import socket
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization

KEYRING_SERVICE = "entrabot"
PRIVATE_KEY_ACCOUNT = "blueprint-private-key"
# Holds a new private key until its certificate is confirmed on the Blueprint.
PENDING_PRIVATE_KEY_ACCOUNT = "blueprint-private-key.pending"

# v1.0, not beta, for keyCredentials (Learning #15).
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_TIMEOUT_SECONDS = 15.0

CERTIFICATE_TYPE = "AsymmetricX509Cert"
CERTIFICATE_USAGE = "Verify"
# Dates come from the certificate itself, in Graph's 7-decimal-place format.
GRAPH_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.0000000Z"

# A new key has been seen to take 30-60 s to appear on read-back.
VERIFY_ATTEMPTS = 18
VERIFY_DELAY_SECONDS = 10.0

# Reported by swap_key_credential when a failure has no HTTP status of its own.
SWAP_FAILED_STATUS = 599

_ERROR_DETAIL_LIMIT = 300


class BlueprintCertError(RuntimeError):
    """Base class for failures reading or updating ``keyCredentials``."""


class KeyCredentialsReadError(BlueprintCertError):
    """``keyCredentials`` could not be read, so nothing is known about the list."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RegistrationRejectedError(BlueprintCertError):
    """Graph refused the write with a 4xx, so the Blueprint was not changed."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class RegistrationUnconfirmedError(BlueprintCertError):
    """The write may have landed, but it could not be confirmed by reading back.

    Attributes:
        missing: display names of the certificates that weren't seen on the last
            successful read (or all of them, if no read succeeded).
    """

    def __init__(self, message: str, *, missing: tuple[str, ...]) -> None:
        super().__init__(message)
        self.missing = missing


@dataclass(frozen=True)
class KeyCredential:
    """One ``keyCredentials`` entry.

    ``raw`` is the dictionary Graph returned and is what gets sent back on a merge,
    so fields this code doesn't know about survive the round trip.
    """

    raw: dict[str, Any]

    @property
    def key_id(self) -> str | None:
        return self.raw.get("keyId")

    @property
    def display_name(self) -> str:
        return self.raw.get("displayName") or "?"

    @property
    def expires(self) -> str:
        return (self.raw.get("endDateTime") or "?")[:10]

    @property
    def is_certificate(self) -> bool:
        return self.raw.get("type", CERTIFICATE_TYPE) == CERTIFICATE_TYPE

    @property
    def der(self) -> bytes | None:
        """The certificate bytes, or None if Graph didn't return readable key material."""
        value = self.raw.get("key")
        if not value or not isinstance(value, str):
            return None
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None

    @property
    def thumbprint(self) -> str | None:
        der = self.der
        return None if der is None else thumbprint_of_der(der)

    @property
    def public_key_der(self) -> bytes | None:
        """The certificate's SubjectPublicKeyInfo, or None if it can't be parsed."""
        der = self.der
        if der is None:
            return None
        try:
            certificate = x509.load_der_x509_certificate(der)
            return certificate.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        except (ValueError, TypeError):
            return None

    def summary(self) -> str:
        """One line for an operator: name and expiry only."""
        return f"    - {self.display_name:50s}  expires {self.expires}"


@dataclass(frozen=True)
class RegistrationResult:
    """A write that was confirmed by reading it back."""

    thumbprint: str
    prior: tuple[KeyCredential, ...]
    current: tuple[KeyCredential, ...]


def thumbprint_of_der(der: bytes) -> str:
    """SHA-256 of the DER certificate, base64url without padding (``x5t#S256``)."""
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()


def public_key_of_private_pem(private_key_pem: str) -> bytes:
    """The SubjectPublicKeyInfo of a PEM private key.

    Raises:
        ValueError, TypeError: the PEM is not a usable unencrypted private key. The
            message never contains key material.
    """
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    return private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def find_credential_for_public_key(
    credentials: Sequence[KeyCredential], public_key_der: bytes
) -> KeyCredential | None:
    for credential in credentials:
        if credential.public_key_der == public_key_der:
            return credential
    return None


def unreadable_certificates(credentials: Sequence[KeyCredential]) -> list[KeyCredential]:
    """Certificates whose key material can't be read, so can't be ruled out as a match.

    A check that finds no match while any of these exist has not actually shown
    that a certificate is absent.
    """
    return [c for c in credentials if c.is_certificate and c.public_key_der is None]


def device_certificate_display_name(hostname: str | None = None) -> str:
    """The name that shows which machine owns a certificate in Entra.

    For people reading a list only. Short hostnames collide, so this must never be
    used to decide which entry is which.
    """
    host = socket.gethostname() if hostname is None else hostname
    return f"EntraBot Device Certificate — {host.split('.')[0] or 'unknown'}"


def build_key_credential(der_bytes: bytes, display_name: str) -> KeyCredential:
    """Build the entry for a new certificate, with dates taken from the certificate.

    Raises:
        ValueError: ``der_bytes`` is not a DER-encoded certificate.
    """
    certificate = x509.load_der_x509_certificate(der_bytes)
    return KeyCredential(
        raw={
            "type": CERTIFICATE_TYPE,
            "usage": CERTIFICATE_USAGE,
            "key": base64.b64encode(der_bytes).decode(),
            "displayName": display_name,
            "startDateTime": certificate.not_valid_before_utc.strftime(GRAPH_DATE_FORMAT),
            "endDateTime": certificate.not_valid_after_utc.strftime(GRAPH_DATE_FORMAT),
        }
    )


def merge_key_credentials(
    existing: Sequence[KeyCredential],
    new_entry: KeyCredential,
    *,
    remove_all_existing: bool = False,
) -> list[KeyCredential]:
    """The list to PATCH: existing entries untouched plus the new one.

    ``remove_all_existing=True`` returns only the new entry. It's the single path
    that drops an entry from the list without looking at its key material.
    """
    if remove_all_existing:
        return [new_entry]
    return [*existing, new_entry]


def _new_graph_client() -> httpx.Client:
    return httpx.Client(timeout=GRAPH_TIMEOUT_SECONDS)


def _graph_request(method: str, url: str, token: str, **kwargs: Any) -> httpx.Response:
    with _new_graph_client() as client:
        return client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=GRAPH_TIMEOUT_SECONDS,
            **kwargs,
        )


def _application_url(object_id: str) -> str:
    return f"{GRAPH_BASE_URL}/applications/{quote(object_id, safe='')}"


def _error_detail(response: httpx.Response) -> str:
    """Graph's error code and message, without the rest of the response."""
    try:
        error = response.json()["error"]
        detail = f"{error.get('code', '')}: {error.get('message', '')}"
    except (ValueError, KeyError, TypeError, AttributeError):
        detail = "no error detail"
    return detail[:_ERROR_DETAIL_LIMIT]


def fetch_key_credentials(object_id: str, token: str) -> list[KeyCredential]:
    """Read the Blueprint's ``keyCredentials``.

    Uses the single-object ``$select``, the only form of the request for which Graph
    returns the ``key`` property.

    Raises:
        KeyCredentialsReadError: anything other than a 200 with a well-formed list.
            In particular a missing or null ``keyCredentials`` is an error, not an
            empty list.
    """
    if not object_id:
        raise KeyCredentialsReadError("the Blueprint object ID is empty")

    url = f"{_application_url(object_id)}?$select=keyCredentials"
    try:
        response = _graph_request("GET", url, token)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise KeyCredentialsReadError(
            f"could not reach Microsoft Graph ({type(exc).__name__})"
        ) from exc

    if response.status_code != 200:
        raise KeyCredentialsReadError(
            f"Graph answered {response.status_code} ({_error_detail(response)})",
            status=response.status_code,
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise KeyCredentialsReadError(
            "Graph answered 200 with a body that isn't JSON", status=200
        ) from exc

    entries = body.get("keyCredentials") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise KeyCredentialsReadError("Graph answered 200 with no keyCredentials list", status=200)
    if not all(isinstance(entry, dict) and entry.get("keyId") for entry in entries):
        raise KeyCredentialsReadError("Graph answered 200 with a malformed entry", status=200)
    return [KeyCredential(raw=entry) for entry in entries]


def _patch_key_credentials(
    object_id: str, token: str, credentials: Sequence[KeyCredential]
) -> httpx.Response:
    return _graph_request(
        "PATCH",
        _application_url(object_id),
        token,
        json={"keyCredentials": [credential.raw for credential in credentials]},
    )


def _missing_after_write(
    current: Sequence[KeyCredential],
    new_entry: KeyCredential,
    required_prior: Sequence[KeyCredential],
) -> tuple[str, ...]:
    missing: list[str] = []
    current_thumbprints = {credential.thumbprint for credential in current}
    if new_entry.thumbprint not in current_thumbprints:
        missing.append(new_entry.display_name)
    current_ids = {credential.key_id for credential in current}
    # A prior certificate is kept if its keyId survived or, since Graph may reissue keyId
    # when key is resent, if the same certificate is still on the list.
    missing.extend(
        c.display_name
        for c in required_prior
        if c.key_id not in current_ids
        and (c.thumbprint is None or c.thumbprint not in current_thumbprints)
    )
    return tuple(missing)


def register_key_credential(
    *,
    object_id: str,
    token: str,
    new_entry: KeyCredential,
    remove_all_existing: bool = False,
    attempts: int = VERIFY_ATTEMPTS,
    delay_seconds: float = VERIFY_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> RegistrationResult:
    """Fetch, merge, PATCH, then confirm the write by reading it back.

    Confirmation means the new certificate's thumbprint is on the list and, unless
    ``remove_all_existing`` is set, so is every certificate that was there before,
    matched by ``keyId`` or by thumbprint.

    Raises:
        KeyCredentialsReadError: the read before the write failed. Nothing was sent.
        RegistrationRejectedError: Graph refused the write. Nothing changed.
        RegistrationUnconfirmedError: the write may have landed (a 2xx that didn't
            become visible, or a 5xx or transport failure, neither of which proves
            nothing was written).
    """
    new_thumbprint = new_entry.thumbprint
    if new_thumbprint is None:
        raise ValueError("the new certificate entry has no readable key material")

    prior = fetch_key_credentials(object_id, token)
    merged = merge_key_credentials(prior, new_entry, remove_all_existing=remove_all_existing)

    write_problem = ""
    try:
        response = _patch_key_credentials(object_id, token, merged)
    except httpx.HTTPError as exc:
        write_problem = f"The PATCH didn't complete ({type(exc).__name__}). "
    else:
        if 400 <= response.status_code < 500:
            raise RegistrationRejectedError(
                f"Graph refused the update with {response.status_code} ({_error_detail(response)})",
                status=response.status_code,
            )
        if not response.is_success:
            write_problem = f"Graph answered the PATCH with {response.status_code}. "

    required_prior = () if remove_all_existing else prior
    missing = _missing_after_write((), new_entry, required_prior)
    read_problem = ""
    for attempt in range(attempts):
        if attempt:
            sleep(delay_seconds)
        try:
            current = fetch_key_credentials(object_id, token)
        except KeyCredentialsReadError as exc:
            read_problem = f"The last read failed: {exc}. "
            continue
        read_problem = ""
        missing = _missing_after_write(current, new_entry, required_prior)
        if not missing:
            return RegistrationResult(
                thumbprint=new_thumbprint,
                prior=tuple(prior),
                current=tuple(current),
            )

    names = ", ".join(f"'{name}'" for name in missing)
    raise RegistrationUnconfirmedError(
        f"Could not confirm the certificate update after {attempts} read(s). "
        f"{write_problem}{read_problem}Not seen on the Blueprint: {names}.",
        missing=missing,
    )


def swap_key_credential(
    *,
    object_id: str,
    token: str,
    der_bytes: bytes,
    old_der: bytes,
    new_der: bytes,
    display_name: str,
) -> int:
    """One step of a Windows rotation, keeping every other machine's certificate.

    Drops only the entries whose key material is ``old_der`` or ``new_der``, then adds
    ``der_bytes``. Rotating forward passes ``der_bytes=new_der`` and rolling back
    passes ``der_bytes=old_der``, so both directions use the same call.

    This is the ``graph_patch`` callable ``rotate_cert_windows.rotate`` expects: it
    never raises, and it returns an HTTP-style status that is >= 400 on any failure,
    including a failed read, in which case nothing is sent. Details go to stderr
    because a status is all the caller receives.
    """
    try:
        current = fetch_key_credentials(object_id, token)
        kept = [credential for credential in current if credential.der not in (old_der, new_der)]
        merged = merge_key_credentials(kept, build_key_credential(der_bytes, display_name))
        response = _patch_key_credentials(object_id, token, merged)
    except KeyCredentialsReadError as exc:
        print(f"  Could not read the Blueprint's certificates: {exc}", file=sys.stderr)
        return exc.status if exc.status is not None and exc.status >= 400 else SWAP_FAILED_STATUS
    except Exception as exc:  # the rotate() contract: report by status, never raise
        print(f"  Could not update the Blueprint's certificates: {exc!r}", file=sys.stderr)
        return SWAP_FAILED_STATUS

    if response.is_success:
        return response.status_code
    print(
        f"  Graph refused the certificate update with {response.status_code} "
        f"({_error_detail(response)})",
        file=sys.stderr,
    )
    return response.status_code if response.status_code >= 400 else SWAP_FAILED_STATUS
