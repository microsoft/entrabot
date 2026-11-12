"""Tests for ``scripts/generate_windows_cert.py``.

The script is a thin CLI over ``entrabot.platform.wincert``: it owns the flags and
the machine-readable stdout contract, and nothing else. The crypto parameters, the
TPM-by-attempt fallback and the PowerShell call are tested in
``tests/test_platform_wincert.py``.
"""

from __future__ import annotations

import importlib.util
import sys as _sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from entrabot.platform import wincert

_THIS = Path(__file__).resolve()
_SCRIPT = _THIS.parents[1] / "scripts" / "generate_windows_cert.py"
_SPEC = importlib.util.spec_from_file_location("generate_windows_cert", _SCRIPT)
generate_windows_cert = importlib.util.module_from_spec(_SPEC)
_sys.modules["generate_windows_cert"] = generate_windows_cert
_SPEC.loader.exec_module(generate_windows_cert)

SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"
X5T = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
DER = b"\x30\x82-not-a-real-certificate"


def _cert(ksp: str) -> wincert.StoreCert:
    now = datetime.now(UTC)
    return wincert.StoreCert(
        sha1_hex=SHA1,
        x5t_s256=X5T,
        der=DER,
        subject="CN=entrabot-blueprint",
        friendly_name="Entrabot Blueprint",
        not_before=now - timedelta(minutes=10),
        not_after=now + timedelta(days=365),
        has_private_key=True,
        ksp=ksp,
    )


class _Wincert:
    """Records how the script called wincert."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.auto: list[dict[str, object]] = []
        self.explicit: list[dict[str, object]] = []
        self.auto_result: tuple[wincert.StoreCert, str | None] = (_cert("tpm"), None)
        self.error: wincert.WinCertError | None = None
        monkeypatch.setattr(wincert, "create_signing_cert", self._auto)
        monkeypatch.setattr(wincert, "create_signing_cert_with_ksp", self._explicit)
        monkeypatch.setattr(wincert, "find_cert", lambda sha1: _cert("software"))

    def _auto(self, **kwargs: object) -> tuple[wincert.StoreCert, str | None]:
        self.auto.append(kwargs)
        if self.error:
            raise self.error
        return self.auto_result

    def _explicit(self, **kwargs: object) -> wincert.StoreCert:
        self.explicit.append(kwargs)
        if self.error:
            raise self.error
        return _cert(str(kwargs["ksp"]))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Wincert:
    return _Wincert(monkeypatch)


class TestCli:
    def test_default_is_tpm_by_attempt_and_prints_the_contract(
        self, fake: _Wincert, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = generate_windows_cert.main(["--subject", "CN=entrabot-blueprint", "--days", "365"])

        assert rc == 0
        assert fake.explicit == []
        assert fake.auto == [
            {
                "subject": "CN=entrabot-blueprint",
                "friendly_name": generate_windows_cert.DEFAULT_FRIENDLY_NAME,
                "days": 365,
                "prefer_tpm": True,
            }
        ]
        out = capsys.readouterr()
        assert out.out.splitlines() == [f"thumbprint={SHA1}", "ksp=tpm", f"x5t_s256={X5T}"]

    def test_fallback_reason_goes_to_stderr_not_stdout(
        self, fake: _Wincert, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake.auto_result = (_cert("software"), "this machine has no TPM that is ready to use")

        assert generate_windows_cert.main([]) == 0

        out = capsys.readouterr()
        assert out.out.splitlines() == [f"thumbprint={SHA1}", "ksp=software", f"x5t_s256={X5T}"]
        assert "no TPM that is ready" in out.err
        assert SHA1 not in out.err

    @pytest.mark.parametrize("ksp", ["tpm", "software"])
    def test_explicit_ksp_is_honoured_without_fallback(
        self, fake: _Wincert, capsys: pytest.CaptureFixture[str], ksp: str
    ) -> None:
        assert generate_windows_cert.main(["--ksp", ksp, "--days", "30"]) == 0

        assert fake.auto == []
        assert fake.explicit == [
            {
                "subject": "CN=entrabot-blueprint",
                "friendly_name": generate_windows_cert.DEFAULT_FRIENDLY_NAME,
                "days": 30,
                "ksp": ksp,
            }
        ]
        assert f"ksp={ksp}" in capsys.readouterr().out.splitlines()

    def test_friendly_name_flag(self, fake: _Wincert) -> None:
        generate_windows_cert.main(["--friendly-name", "Entrabot Blueprint 1234"])
        assert fake.auto[0]["friendly_name"] == "Entrabot Blueprint 1234"

    def test_export_der_writes_the_public_certificate(self, fake: _Wincert, tmp_path: Path) -> None:
        dest = tmp_path / "O'Brien's folder" / "cert.cer"
        dest.parent.mkdir()

        assert generate_windows_cert.main(["--export-der", str(dest)]) == 0

        assert dest.read_bytes() == DER

    def test_failure_is_exit_1_with_a_message_and_no_traceback(
        self, fake: _Wincert, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake.error = wincert.WinCertError("New-SelfSignedCertificate failed: Access is denied.")

        assert generate_windows_cert.main([]) == 1

        out = capsys.readouterr()
        assert out.out == ""
        assert "Access is denied" in out.err and "Traceback" not in out.err


class TestCompatibilityHelpers:
    """Kept for callers that import the module (rotation, the Windows-gated tests)."""

    def test_generate_uses_the_named_ksp(self, fake: _Wincert) -> None:
        result = generate_windows_cert.generate(subject="CN=x", days_valid=7, ksp="software")
        assert result.thumbprint == SHA1 and result.ksp == "software"
        assert fake.explicit[0]["ksp"] == "software" and fake.explicit[0]["days"] == 7

    def test_generate_rejects_an_unknown_ksp(self, fake: _Wincert) -> None:
        with pytest.raises(ValueError, match="ksp"):
            generate_windows_cert.generate(subject="CN=x", days_valid=7, ksp="bogus")
        assert fake.explicit == []

    def test_export_der_reads_from_the_store(self, fake: _Wincert, tmp_path: Path) -> None:
        dest = tmp_path / "out.cer"
        assert generate_windows_cert.export_der(SHA1, dest) == dest
        assert dest.read_bytes() == DER

    def test_export_der_for_a_missing_certificate(
        self, fake: _Wincert, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wincert, "find_cert", lambda sha1: None)
        with pytest.raises(generate_windows_cert.GenerateCertError):
            generate_windows_cert.export_der(SHA1, tmp_path / "out.cer")

    def test_compute_x5t_s256(self) -> None:
        value = generate_windows_cert.compute_x5t_s256(b"abc")
        assert value == "ungWv48Bz-pBQUDeXa4iI7ADYaOWF3qctBD_YfIAFa0"


def test_no_powershell_7_and_no_script_text_in_this_file() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "pwsh" not in source
    assert "Get-Tpm" not in source
    assert "subprocess" not in source
