"""Certificate-based client assertion for Entra ID OAuth2.

Builds a JWT assertion signed by a private key, used in place of
client_secret for the Blueprint's client_credentials grant (Hop 1).
The private key lives in the OS credential store (Keychain/TPM/Keyring).
The public certificate is registered on the Blueprint app in Entra.

See ADR-003 for rationale.
"""

from __future__ import annotations

import base64
import hashlib
import time
from uuid import uuid4

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ASSERTION_LIFETIME_SECONDS = 600  # 10 minutes


def build_client_assertion(
    *,
    private_key_pem: str,
    cert_thumbprint: str,
    client_id: str,
    token_endpoint: str,
) -> str:
    """Build a signed JWT assertion for certificate-based client_credentials.

    The assertion replaces ``client_secret`` in the OAuth2 token request.
    Entra validates the signature using the public certificate registered
    on the Blueprint app registration.

    Args:
        private_key_pem: RSA private key in PEM format.
        cert_thumbprint: Base64url-encoded SHA-256 of the DER certificate.
        client_id: The Blueprint app's client ID.
        token_endpoint: The Entra token endpoint URL (used as JWT audience).

    Returns:
        Signed JWT string ready for the ``client_assertion`` parameter.
    """
    private_key = load_pem_private_key(private_key_pem.encode(), password=None)

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

    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers=headers,
    )


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
