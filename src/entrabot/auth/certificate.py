"""Certificate-based client assertion for Entra ID OAuth2.

Builds a JWT assertion signed by a private key, used in place of
client_secret for the Blueprint's client_credentials grant (Hop 1).

Per-platform key storage:

- Mac/Linux: PEM private key in OS keystore (Keychain / Secret Service);
  signed with ``cryptography`` via ``private_key_pem``.
- Windows: non-exportable CNG key in ``Cert:\\CurrentUser\\My`` (TPM-
  or software-backed); signed via ``cncrypt_signer.sign_pkcs1_sha256``
  using the cert's SHA-1 thumbprint to locate the key.

The JWT header always carries ``x5t#S256`` (SHA-256 b64url of the DER
certificate, per RFC 7515 §4.1.8) — same shape on every platform.

See ADR-003 for rationale.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from uuid import uuid4

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ASSERTION_LIFETIME_SECONDS = 600  # 10 minutes


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def build_client_assertion(
    *,
    private_key_pem: str | None = None,
    cert_thumbprint: str,
    client_id: str,
    token_endpoint: str,
    cert_sha1: str | None = None,
) -> str:
    """Build a signed JWT assertion for certificate-based client_credentials.

    The assertion replaces ``client_secret`` in the OAuth2 token request.
    Entra validates the signature using the public certificate registered
    on the Blueprint app registration.

    Mac/Linux callers pass ``private_key_pem``. Windows callers omit it
    and pass ``cert_sha1`` (the 40-char hex SHA-1 thumbprint of the cert
    in ``Cert:\\CurrentUser\\My``); signing happens via CNG against the
    non-exportable key.

    Args:
        private_key_pem: RSA private key in PEM format (Mac/Linux).
        cert_thumbprint: Base64url-encoded SHA-256 of the DER certificate
            (becomes the ``x5t#S256`` header value).
        client_id: The Blueprint app's client ID.
        token_endpoint: The Entra token endpoint URL (used as JWT audience).
        cert_sha1: 40-char hex SHA-1 thumbprint identifying the cert in
            the Windows cert store. Required on Windows when
            ``private_key_pem`` is None.

    Returns:
        Signed JWT string ready for the ``client_assertion`` parameter.
    """
    now = int(time.time())
    payload = {
        "aud": token_endpoint,
        "iss": client_id,
        "sub": client_id,
        "jti": str(uuid4()),
        "exp": now + ASSERTION_LIFETIME_SECONDS,
        "nbf": now,
        "iat": now,
    }
    headers = {
        "x5t#S256": cert_thumbprint,
    }

    if private_key_pem is not None:
        private_key = load_pem_private_key(private_key_pem.encode(), password=None)
        return jwt.encode(payload, private_key, algorithm="RS256", headers=headers)

    if cert_sha1 is None:
        raise ValueError(
            "build_client_assertion requires either private_key_pem (Mac/Linux) "
            "or cert_sha1 (Windows)"
        )

    # Windows CNG path: build header + payload manually, sign the SHA-256
    # of the signing input via ncrypt.dll PKCS1+SHA256.
    from entrabot.auth.cncrypt_signer import sign_pkcs1_sha256

    header = {"alg": "RS256", "typ": "JWT", **headers}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    digest = hashlib.sha256(signing_input.encode()).digest()
    signature = sign_pkcs1_sha256(thumbprint=cert_sha1, hash_bytes=digest)
    return signing_input + "." + _b64url(signature)


def compute_cert_thumbprint(cert_pem: str) -> str:
    """Compute the base64url-encoded SHA-256 thumbprint of a certificate.

    This is the ``x5t#S256`` value used in JWT assertion headers,
    per RFC 7515 Section 4.1.8.

    Args:
        cert_pem: X.509 certificate in PEM format.

    Returns:
        Base64url-encoded (no padding) SHA-256 digest of the DER certificate.
    """
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    der_bytes = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der_bytes).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
