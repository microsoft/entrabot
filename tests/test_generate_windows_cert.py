"""Tests for ``scripts/generate_windows_cert.py``.

Mock-based — no real PowerShell or ``New-SelfSignedCertificate`` invocation.
The plan (D9) hard-locks the crypto params; these tests verify those exact
flags land in the subprocess call AND that thumbprint validation rejects
malformed PowerShell output.
"""

from __future__ import annotations

import importlib.util
import sys as _sys
from pathlib import Path
from unittest.mock import patch

import pytest

_THIS = Path(__file__).resolve()
_SPEC = importlib.util.spec_from_file_location(
    "generate_windows_cert",
    _THIS.parents[1] / "scripts" / "generate_windows_cert.py",
)
generate_windows_cert = importlib.util.module_from_spec(_SPEC)
_sys.modules["generate_windows_cert"] = generate_windows_cert
_SPEC.loader.exec_module(generate_windows_cert)


HARD_LOCKED_FLAGS = [
    "-KeyAlgorithm",
    "RSA",
    "-KeyLength",
    "2048",
    "-HashAlgorithm",
    "SHA256",
    "-KeyUsage",
    "DigitalSignature",
    "-KeyUsageProperty",
    "Sign",
]


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestGenerateCert:
    def test_emits_hard_locked_crypto_params(self, tmp_path: Path) -> None:
        valid_thumbprint = "A" * 40

        def fake_run(cmd, *args, **kwargs):
            joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            for flag in HARD_LOCKED_FLAGS:
                assert flag in joined, f"missing required flag: {flag} in {joined}"
            return _FakeCompleted(stdout=valid_thumbprint + "\n")

        with patch.object(generate_windows_cert.subprocess, "run", side_effect=fake_run):
            result = generate_windows_cert.generate(
                subject="CN=entraclaw-blueprint", days_valid=365, ksp="software"
            )
        assert result.thumbprint == valid_thumbprint
        assert result.ksp == "software"

    def test_tpm_provider_string_used_when_ksp_tpm(self) -> None:
        captured: list[str] = []

        def fake_run(cmd, *args, **kwargs):
            captured.extend(cmd if isinstance(cmd, list) else [str(cmd)])
            return _FakeCompleted(stdout="B" * 40 + "\n")

        with patch.object(generate_windows_cert.subprocess, "run", side_effect=fake_run):
            generate_windows_cert.generate(
                subject="CN=x", days_valid=365, ksp="tpm"
            )
        joined = " ".join(captured)
        assert "Microsoft Platform Crypto Provider" in joined

    def test_software_provider_string_used_when_ksp_software(self) -> None:
        captured: list[str] = []

        def fake_run(cmd, *args, **kwargs):
            captured.extend(cmd if isinstance(cmd, list) else [str(cmd)])
            return _FakeCompleted(stdout="C" * 40 + "\n")

        with patch.object(generate_windows_cert.subprocess, "run", side_effect=fake_run):
            generate_windows_cert.generate(
                subject="CN=x", days_valid=365, ksp="software"
            )
        joined = " ".join(captured)
        assert "Microsoft Software Key Storage Provider" in joined

    def test_non_exportable_flag_only_when_tpm(self) -> None:
        captured: list[str] = []

        def fake_run(cmd, *args, **kwargs):
            captured.extend(cmd if isinstance(cmd, list) else [str(cmd)])
            return _FakeCompleted(stdout="D" * 40 + "\n")

        with patch.object(generate_windows_cert.subprocess, "run", side_effect=fake_run):
            generate_windows_cert.generate(
                subject="CN=x", days_valid=365, ksp="tpm"
            )
        joined = " ".join(captured)
        assert "NonExportable" in joined

    def test_rejects_malformed_thumbprint(self) -> None:
        with patch.object(
            generate_windows_cert.subprocess,
            "run",
            return_value=_FakeCompleted(stdout="not-a-thumbprint\n"),
        ), pytest.raises(generate_windows_cert.ThumbprintValidationError):
            generate_windows_cert.generate(
                subject="CN=x", days_valid=365, ksp="software"
            )

    def test_rejects_multiline_stdout(self) -> None:
        bad = "ABCDEF1234567890ABCDEF1234567890ABCDEF12\nbonus garbage\n"
        with patch.object(
            generate_windows_cert.subprocess,
            "run",
            return_value=_FakeCompleted(stdout=bad),
        ), pytest.raises(generate_windows_cert.ThumbprintValidationError):
            generate_windows_cert.generate(
                subject="CN=x", days_valid=365, ksp="software"
            )

    def test_powershell_failure_raises(self) -> None:
        with patch.object(
            generate_windows_cert.subprocess,
            "run",
            return_value=_FakeCompleted(returncode=1, stderr="boom"),
        ), pytest.raises(generate_windows_cert.PowerShellError, match="boom"):
            generate_windows_cert.generate(
                subject="CN=x", days_valid=365, ksp="software"
            )

    def test_invalid_ksp_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="ksp"):
            generate_windows_cert.generate(
                subject="CN=x", days_valid=365, ksp="bogus"
            )


class TestProbeTpm:
    def test_probe_returns_true_when_get_tpm_reports_ready(self) -> None:
        with patch.object(
            generate_windows_cert.subprocess,
            "run",
            return_value=_FakeCompleted(stdout="True\n"),
        ):
            assert generate_windows_cert.probe_tpm_ready() is True

    def test_probe_returns_false_when_get_tpm_reports_not_ready(self) -> None:
        with patch.object(
            generate_windows_cert.subprocess,
            "run",
            return_value=_FakeCompleted(stdout="False\n"),
        ):
            assert generate_windows_cert.probe_tpm_ready() is False

    def test_probe_returns_false_when_get_tpm_errors(self) -> None:
        with patch.object(
            generate_windows_cert.subprocess,
            "run",
            return_value=_FakeCompleted(returncode=1, stderr="access denied"),
        ):
            assert generate_windows_cert.probe_tpm_ready() is False
