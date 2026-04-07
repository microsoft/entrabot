"""Tests for certificate-based JWT assertion builder."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from entraclaw.auth.certificate import build_client_assertion, compute_cert_thumbprint


@pytest.fixture
def keypair():
    """Generate a fresh RSA keypair + self-signed cert for testing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "test-cert"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    private_key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    # Compute thumbprint
    der_bytes = cert.public_bytes(serialization.Encoding.DER)
    thumbprint = (
        base64.urlsafe_b64encode(hashlib.sha256(der_bytes).digest())
        .rstrip(b"=")
        .decode()
    )
    return private_key_pem, cert_pem, thumbprint


class TestBuildClientAssertion:
    def test_returns_valid_jwt(self, keypair) -> None:
        private_key_pem, cert_pem, thumbprint = keypair
        token = build_client_assertion(
            private_key_pem=private_key_pem,
            cert_thumbprint=thumbprint,
            client_id="test-client-id",
            token_endpoint="https://login.microsoftonline.com/tid/oauth2/v2.0/token",
        )
        # Should be a valid JWT with 3 dot-separated parts
        parts = token.split(".")
        assert len(parts) == 3

    def test_audience_is_token_endpoint(self, keypair) -> None:
        private_key_pem, cert_pem, thumbprint = keypair
        endpoint = "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
        token = build_client_assertion(
            private_key_pem=private_key_pem,
            cert_thumbprint=thumbprint,
            client_id="test-client-id",
            token_endpoint=endpoint,
        )
        decoded = jwt.decode(token, options={"verify_signature": False})
        assert decoded["aud"] == endpoint

    def test_iss_and_sub_match_client_id(self, keypair) -> None:
        private_key_pem, cert_pem, thumbprint = keypair
        token = build_client_assertion(
            private_key_pem=private_key_pem,
            cert_thumbprint=thumbprint,
            client_id="my-blueprint-id",
            token_endpoint="https://login.microsoftonline.com/tid/oauth2/v2.0/token",
        )
        decoded = jwt.decode(token, options={"verify_signature": False})
        assert decoded["iss"] == "my-blueprint-id"
        assert decoded["sub"] == "my-blueprint-id"

    def test_expiry_is_10_minutes(self, keypair) -> None:
        private_key_pem, cert_pem, thumbprint = keypair
        token = build_client_assertion(
            private_key_pem=private_key_pem,
            cert_thumbprint=thumbprint,
            client_id="test-client-id",
            token_endpoint="https://login.microsoftonline.com/tid/oauth2/v2.0/token",
        )
        decoded = jwt.decode(token, options={"verify_signature": False})
        assert decoded["exp"] - decoded["iat"] == 600

    def test_jti_is_unique(self, keypair) -> None:
        private_key_pem, cert_pem, thumbprint = keypair
        t1 = build_client_assertion(
            private_key_pem=private_key_pem,
            cert_thumbprint=thumbprint,
            client_id="test-client-id",
            token_endpoint="https://login.microsoftonline.com/tid/oauth2/v2.0/token",
        )
        t2 = build_client_assertion(
            private_key_pem=private_key_pem,
            cert_thumbprint=thumbprint,
            client_id="test-client-id",
            token_endpoint="https://login.microsoftonline.com/tid/oauth2/v2.0/token",
        )
        d1 = jwt.decode(t1, options={"verify_signature": False})
        d2 = jwt.decode(t2, options={"verify_signature": False})
        assert d1["jti"] != d2["jti"]

    def test_header_contains_thumbprint(self, keypair) -> None:
        private_key_pem, cert_pem, thumbprint = keypair
        token = build_client_assertion(
            private_key_pem=private_key_pem,
            cert_thumbprint=thumbprint,
            client_id="test-client-id",
            token_endpoint="https://login.microsoftonline.com/tid/oauth2/v2.0/token",
        )
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"
        assert header["typ"] == "JWT"
        assert header["x5t#S256"] == thumbprint


class TestComputeCertThumbprint:
    def test_matches_expected(self, keypair) -> None:
        _, cert_pem, expected_thumbprint = keypair
        result = compute_cert_thumbprint(cert_pem)
        assert result == expected_thumbprint
