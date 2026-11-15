from __future__ import annotations

import importlib.util
import sys
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "upload_blueprint_cert.py"


@pytest.fixture
def upload_module():
    spec = importlib.util.spec_from_file_location("upload_blueprint_cert", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _certificate_der() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def test_upload_registers_public_certificate_on_selected_blueprint(upload_module):
    captured = {}

    class Response:
        status_code = 204
        text = ""

    def request(method, url, **kwargs):
        if method == "GET":
            return Mock(status_code=200, json=lambda: {"keyCredentials": []})
        captured.update(url=url, **kwargs)
        return Response()

    upload_module.upload_blueprint_certificate(
        token="test-token",
        blueprint_object_id="blueprint-object",
        der_bytes=_certificate_der(),
        display_name="fixture cert",
        request=request,
    )

    assert captured["url"].endswith("/applications/blueprint-object")
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    credential = captured["json"]["keyCredentials"][0]
    assert credential["displayName"] == "fixture cert"
    assert credential["type"] == "AsymmetricX509Cert"
    assert credential["usage"] == "Verify"
    assert credential["key"]
    assert credential["startDateTime"].endswith(".0000000Z")
    assert credential["endDateTime"].endswith(".0000000Z")


def test_upload_rejects_graph_failure(upload_module):
    class Response:
        status_code = 403
        text = "denied"

    with pytest.raises(upload_module.BlueprintCertUploadError, match="403"):
        upload_module.upload_blueprint_certificate(
            token="test-token",
            blueprint_object_id="blueprint-object",
            der_bytes=_certificate_der(),
            request=lambda method, *args, **kwargs: (
                Mock(status_code=200, json=lambda: {"keyCredentials": []})
                if method == "GET" else Response()
            ),
        )


@pytest.mark.parametrize(
    ("token", "object_id", "der_bytes"),
    [
        ("", "blueprint-object", b"cert"),
        ("token", "", b"cert"),
        ("token", "blueprint-object", b""),
    ],
)
def test_upload_rejects_missing_inputs(upload_module, token, object_id, der_bytes):
    with pytest.raises(upload_module.BlueprintCertUploadError):
        upload_module.upload_blueprint_certificate(
            token=token,
            blueprint_object_id=object_id,
            der_bytes=der_bytes,
        )


def test_upload_preserves_other_machines_registered_certificates(upload_module):
    other = {
        "keyId": "other-key", "type": "AsymmetricX509Cert", "usage": "Verify",
        "key": b64encode(_certificate_der()).decode(), "displayName": "Other machine",
    }
    request = Mock(side_effect=[
        Mock(status_code=200, json=lambda: {"keyCredentials": [other]}),
        Mock(status_code=204),
    ])

    upload_module.upload_blueprint_certificate(
        token="fixture-token", blueprint_object_id="bp-object",
        der_bytes=_certificate_der(), request=request,
    )

    uploaded = request.call_args.kwargs["json"]["keyCredentials"]
    assert len(uploaded) == 2
    assert uploaded[0] == other


def test_upload_does_not_write_when_certificate_already_registered(upload_module):
    der = _certificate_der()
    request = Mock(return_value=Mock(
        status_code=200,
        json=lambda: {"keyCredentials": [{"key": b64encode(der).decode()}]},
    ))

    upload_module.upload_blueprint_certificate(
        token="fixture-token", blueprint_object_id="bp-object", der_bytes=der, request=request,
    )

    request.assert_called_once()
    assert request.call_args.args[0] == "GET"


@pytest.mark.parametrize("response", [
    Mock(status_code=403),
    Mock(status_code=200, json=lambda: {}),
    Mock(status_code=200, json=lambda: {"keyCredentials": [{"key": None}]}),
    Mock(status_code=200, json=Mock(side_effect=ValueError("bad json"))),
])
def test_upload_never_overwrites_unreadable_existing_keys(upload_module, response):
    request = Mock(return_value=response)
    with pytest.raises(upload_module.BlueprintCertUploadError):
        upload_module.upload_blueprint_certificate(
            token="fixture-token", blueprint_object_id="bp-object",
            der_bytes=_certificate_der(), request=request,
        )
    request.assert_called_once()
