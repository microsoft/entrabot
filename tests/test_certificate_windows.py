"""End-to-end Windows certificate tests — gated on ``sys.platform == 'win32'``.

These run only on the Windows CI runner (and any Windows dev box). They
exercise the full path:

  generate_windows_cert.generate (real ``New-SelfSignedCertificate``)
    → platform.windows.find_cert_by_thumbprint (real Cert: store)
    → cncrypt_signer.sign_pkcs1_sha256 (real CNG ncrypt.dll)
    → certificate.build_client_assertion (real JWT, manual b64url path)

Skipped on Mac/Linux.
"""

from __future__ import annotations

import sys
import time

import pytest

if sys.platform != "win32":
    pytest.skip("Windows-only suite", allow_module_level=True)

# Imports below this line are evaluated only on Windows so module-level
# import of platform.windows / cncrypt_signer doesn't crash on Mac.
import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

from entrabot.auth import certificate, cncrypt_signer  # noqa: E402
from entrabot.platform import windows  # noqa: E402

_HERE = Path(__file__).resolve()
_GEN_SPEC = importlib.util.spec_from_file_location(
    "generate_windows_cert",
    _HERE.parents[1] / "scripts" / "generate_windows_cert.py",
)
_GEN = importlib.util.module_from_spec(_GEN_SPEC)
sys.modules["generate_windows_cert"] = _GEN
_GEN_SPEC.loader.exec_module(_GEN)


@pytest.fixture
def fresh_software_cert(tmp_path: Path):
    """Generate a software-KSP cert; remove it after the test."""
    subject = f"CN=entrabot-test-{int(time.time())}"
    result = _GEN.generate(subject=subject, days_valid=1, ksp="software")
    yield result
    # Cleanup — don't fail the test if removal fails.
    import subprocess

    cleanup_cmd = (
        f"Remove-Item Cert:\\CurrentUser\\My\\{result.thumbprint} "
        f"-ErrorAction SilentlyContinue"
    )
    subprocess.run(  # noqa: S603,S607
        ["pwsh", "-NoProfile", "-Command", cleanup_cmd],
        check=False,
    )


def test_full_signer_chain_software_ksp(fresh_software_cert) -> None:
    """End-to-end: generate cert, look it up, sign a JWT, validate it loads."""
    sha1 = fresh_software_cert.thumbprint

    # platform/windows finds the cert.
    assert windows.find_cert_by_thumbprint(sha1) is True

    # cncrypt_signer signs an arbitrary 32-byte hash (the SHA-256 of "hello").
    import hashlib

    digest = hashlib.sha256(b"hello").digest()
    sig = cncrypt_signer.sign_pkcs1_sha256(thumbprint=sha1, hash_bytes=digest)
    assert isinstance(sig, bytes) and len(sig) == 256  # RSA-2048 = 256 bytes

    # certificate.build_client_assertion produces a signed JWT.
    tenant = "00000000-0000-0000-0000-000000000000"
    assertion = certificate.build_client_assertion(
        cert_thumbprint="dummy-thumbprint",
        client_id="11111111-1111-1111-1111-111111111111",
        token_endpoint=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        cert_sha1=sha1,
    )
    parts = assertion.split(".")
    assert len(parts) == 3, "JWT must be header.payload.signature"


def test_find_cert_returns_false_for_missing_thumbprint() -> None:
    bogus = "F" * 40
    assert windows.find_cert_by_thumbprint(bogus) is False
