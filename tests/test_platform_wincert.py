"""Tests for ``entrabot.platform.wincert``: the Windows certificate and key lifecycle.

The store (crypt32) and Windows PowerShell are replaced with fakes, so these run on
every host. The real thing is exercised by the win32-gated tests at the bottom and
in ``tests/test_certificate_windows.py``.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from cryptography.x509.oid import NameOID

from entrabot.auth import cncrypt_signer
from entrabot.platform import wincert

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _make_cert(
    *,
    not_before: datetime = NOW - timedelta(minutes=10),
    not_after: datetime = NOW + timedelta(days=365),
    common_name: str = "entrabot-blueprint",
) -> tuple[rsa.RSAPrivateKey, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.DER)


def _sha1(der: bytes) -> str:
    return hashlib.sha1(der).hexdigest().upper()  # noqa: S324


class FakeWindows:
    """A certificate store, a key store, and a PowerShell that acts on them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.certs: dict[str, wincert._RawCert] = {}
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail_create_on: dict[str, str] = {}  # provider name -> stderr
        self.unsignable_providers: set[str] = set()
        self.corrupt_stdout: str | None = None
        self.next_cert: tuple[rsa.RSAPrivateKey, bytes] | None = None

        monkeypatch.setattr(wincert, "_run_powershell", self.run_powershell)
        monkeypatch.setattr(wincert, "_read_store_cert", lambda sha1: self.certs.get(sha1))
        monkeypatch.setattr(wincert, "_enum_store_certs", lambda: list(self.certs.values()))
        monkeypatch.setattr(cncrypt_signer, "sign_pkcs1_sha256", self.sign)

    def add(
        self,
        *,
        provider: str | None = wincert.PROVIDER_SOFTWARE,
        friendly_name: str = "Entrabot Blueprint",
        key_container: str | None = None,
        **cert_kwargs: object,
    ) -> str:
        key, der = _make_cert(**cert_kwargs)  # type: ignore[arg-type]
        sha1 = _sha1(der)
        self.certs[sha1] = wincert._RawCert(
            der=der,
            friendly_name=friendly_name,
            provider_name=provider,
            key_container=key_container if provider is not None else None,
        )
        if provider is not None:
            self.keys[sha1] = key
        return sha1

    def run_powershell(self, script: str, variables: dict[str, str]) -> wincert._PowerShellResult:
        self.calls.append((script, dict(variables)))
        if "New-SelfSignedCertificate" in script:
            provider = variables[wincert._ENV_PROVIDER]
            if provider in self.fail_create_on:
                return wincert._PowerShellResult(1, "", self.fail_create_on[provider])
            if self.corrupt_stdout is not None:
                return wincert._PowerShellResult(0, self.corrupt_stdout, "")
            key, der = self.next_cert or _make_cert()
            self.next_cert = None
            sha1 = _sha1(der)
            self.certs[sha1] = wincert._RawCert(
                der=der,
                friendly_name=variables[wincert._ENV_FRIENDLY_NAME],
                provider_name=provider,
            )
            self.keys[sha1] = key
            return wincert._PowerShellResult(0, sha1 + "\r\n", "")
        if "Remove-Item" in script:
            sha1 = variables[wincert._ENV_SHA1]
            self.certs.pop(sha1, None)
            if "-DeleteKey" in script:
                self.keys.pop(sha1, None)
            return wincert._PowerShellResult(0, "", "")
        raise AssertionError(f"unexpected script: {script}")

    def sign(self, *, thumbprint: str, hash_bytes: bytes) -> bytes:
        raw = self.certs.get(thumbprint)
        if raw is None:
            raise cncrypt_signer.CertNotFoundError("certificate not found")
        if thumbprint not in self.keys:
            # NTE_BAD_KEYSET: the key container is gone.
            raise cncrypt_signer.SigningError("the key container no longer exists", code=0x80090016)
        if raw.provider_name in self.unsignable_providers:
            raise cncrypt_signer.SigningError("the key is not usable on this machine")
        return self.keys[thumbprint].sign(
            hash_bytes, padding.PKCS1v15(), utils.Prehashed(hashes.SHA256())
        )

    def creates(self) -> list[dict[str, str]]:
        return [v for s, v in self.calls if "New-SelfSignedCertificate" in s]

    def deletes(self) -> list[tuple[str, dict[str, str]]]:
        return [(s, v) for s, v in self.calls if "Remove-Item" in s]


@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch) -> FakeWindows:
    return FakeWindows(monkeypatch)


# ─── the scripts themselves ──────────────────────────────────────────────────


class TestScriptText:
    def test_hard_locked_crypto_parameters(self) -> None:
        script = wincert._CREATE_SCRIPT
        for line in (
            "Type = 'Custom'",
            "KeyExportPolicy = 'NonExportable'",
            "KeyAlgorithm = 'RSA'",
            "KeyLength = 2048",
            "HashAlgorithm = 'SHA256'",
            "KeyUsage = 'DigitalSignature'",
            "KeyUsageProperty = 'Sign'",
            "CertStoreLocation = 'Cert:\\CurrentUser\\My'",
        ):
            assert line in script, line

    def test_no_tls_usages_and_no_explicit_not_before(self) -> None:
        # No EKU extension: the cert must not be offered as a TLS client certificate.
        # NotBefore is left to the cmdlet, which backdates it for clock skew.
        script = wincert._CREATE_SCRIPT
        assert "TextExtension" not in script
        assert "NotBefore" not in script

    @pytest.mark.parametrize(
        "script",
        [wincert._CREATE_SCRIPT, wincert._DELETE_SCRIPT, wincert._DELETE_WITH_KEY_SCRIPT],
    )
    def test_scripts_are_constrained_language_safe_and_argv_safe(self, script: str) -> None:
        assert "]::" not in script, "no static .NET calls"
        assert not re.search(r"\.\w+\(", script), "no .NET method calls"
        assert '"' not in script, "double quotes don't survive argv quoting reliably"
        assert "{0}" not in script and "%s" not in script

    @pytest.mark.parametrize(
        "script",
        [wincert._CREATE_SCRIPT, wincert._DELETE_SCRIPT, wincert._DELETE_WITH_KEY_SCRIPT],
    )
    def test_progress_is_silenced_before_anything_else_runs(self, script: str) -> None:
        # Windows PowerShell 5.1 writes "Preparing modules for first use" progress
        # records to stderr as CLIXML when stderr isn't a console. Silence them
        # before the first cmdlet triggers a module load.
        assert script.splitlines()[0] == "$ProgressPreference = 'SilentlyContinue'"

    def test_delete_with_key_uses_deletekey(self) -> None:
        assert "-DeleteKey" in wincert._DELETE_WITH_KEY_SCRIPT
        assert "-DeleteKey" not in wincert._DELETE_SCRIPT


class TestRunPowershell:
    def test_uses_inbox_windows_powershell_and_passes_values_in_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, b"OK\r\n", b"")

        monkeypatch.setattr(wincert.sys, "platform", "win32")
        monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
        monkeypatch.setattr(wincert.subprocess, "run", fake_run)

        result = wincert._run_powershell(
            "Write-Output OK", {"ENTRABOT_WINCERT_SUBJECT": "CN=O'Brien"}
        )

        argv = captured["argv"]
        assert isinstance(argv, list)
        exe = argv[0].replace("/", "\\")
        assert exe.endswith("System32\\WindowsPowerShell\\v1.0\\powershell.exe")
        assert "pwsh" not in exe.lower()
        assert "-NoProfile" in argv and "-NonInteractive" in argv
        assert argv[-2:] == ["-Command", "Write-Output OK"]
        assert not any("O'Brien" in part for part in argv), "values must not be in script text"
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["env"]["ENTRABOT_WINCERT_SUBJECT"] == "CN=O'Brien"
        assert kwargs["timeout"]
        assert result.returncode == 0 and result.stdout.strip() == "OK"

    @pytest.mark.parametrize("spelling", ["PSModulePath", "PSMODULEPATH", "psmodulepath"])
    def test_child_does_not_inherit_psmodulepath(
        self, monkeypatch: pytest.MonkeyPatch, spelling: str
    ) -> None:
        # Started from a PowerShell 7 terminal, this process inherits PowerShell 7's
        # module path. Windows PowerShell 5.1 would then resolve in-box modules to
        # PowerShell 7 versions it can't load. Without the variable it builds its own.
        captured: dict[str, dict[str, str]] = {}

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            captured["env"] = kwargs["env"]  # type: ignore[assignment]
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(wincert.sys, "platform", "win32")
        monkeypatch.setattr(wincert.subprocess, "run", fake_run)
        monkeypatch.setattr(
            wincert.os,
            "environ",
            {
                spelling: r"C:\Program Files\PowerShell\7\Modules;C:\Windows\System32\x",
                "SYSTEMROOT": r"C:\Windows",
                "PATH": r"C:\Windows\System32",
            },
        )

        wincert._run_powershell("Write-Output OK", {"ENTRABOT_WINCERT_SHA1": "A" * 40})

        env = captured["env"]
        assert not [name for name in env if name.upper() == "PSMODULEPATH"]
        assert env["ENTRABOT_WINCERT_SHA1"] == "A" * 40
        assert env["PATH"] == r"C:\Windows\System32" and env["SYSTEMROOT"] == r"C:\Windows"

    def test_missing_powershell_is_a_wincert_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_a: object, **_k: object) -> None:
            raise FileNotFoundError("powershell.exe")

        monkeypatch.setattr(wincert.sys, "platform", "win32")
        monkeypatch.setattr(wincert.subprocess, "run", fake_run)
        with pytest.raises(wincert.WinCertError, match="PowerShell"):
            wincert._run_powershell("x", {})

    def test_error_text_unwraps_clixml(self) -> None:
        stderr = (
            '#< CLIXML\r\n<Objs><S S="Error">New-SelfSignedCertificate : CertEnroll failed'
            '_x000D__x000A_</S><S S="Error">0x80090030</S></Objs>'
        )
        text = wincert._error_text(stderr)
        assert "CertEnroll failed" in text and "0x80090030" in text
        assert "CLIXML" not in text and "<S" not in text


CLIXML_PROGRESS = (
    "#< CLIXML\r\n"
    '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
    '<Obj S="progress" RefId="0"><TN RefId="0"><T>System.Management.Automation.PSCustomObject'
    '</T><T>System.Object</T></TN><MS><I64 N="SourceId">1</I64><PR N="Record">'
    "<AV>Preparing modules for first use.</AV><AI>0</AI><Nil /><PI>-1</PI><PC>-1</PC>"
    "<T>Completed</T><SR>-1</SR><SD> </SD></PR></MS></Obj></Objs>"
)


class TestStderrIsNotAVerdict:
    """The exit code decides. PowerShell 5.1 puts progress on stderr even on success."""

    def test_create_succeeds_with_progress_noise_on_stderr(
        self, win: FakeWindows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = win.run_powershell

        def noisy(script: str, variables: dict[str, str]) -> wincert._PowerShellResult:
            result = real(script, variables)
            return wincert._PowerShellResult(result.returncode, result.stdout, CLIXML_PROGRESS)

        monkeypatch.setattr(wincert, "_run_powershell", noisy)

        cert, reason = wincert.create_signing_cert(subject="CN=x", friendly_name="f")
        assert cert.ksp == "tpm" and reason is None
        assert wincert.delete_cert(cert.sha1_hex) is True

    def test_failure_text_ignores_progress_records(self, win: FakeWindows) -> None:
        win.fail_create_on[wincert.PROVIDER_SOFTWARE] = CLIXML_PROGRESS
        with pytest.raises(wincert.WinCertError) as excinfo:
            wincert.create_signing_cert_with_ksp(subject="CN=x", friendly_name="f", ksp="software")
        message = str(excinfo.value)
        assert "CLIXML" not in message and "Preparing modules" not in message
        assert "no error text" in message

    def test_error_and_progress_together_keep_only_the_error(self) -> None:
        stderr = CLIXML_PROGRESS.replace(
            "</Objs>", '<S S="Error">Access is denied._x000D__x000A_</S></Objs>'
        )
        assert wincert._error_text(stderr) == "Access is denied."


# ─── non-Windows ─────────────────────────────────────────────────────────────


class TestWindowsOnly:
    @pytest.fixture(autouse=True)
    def _not_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wincert.sys, "platform", "linux")

    def test_every_public_function_raises(self) -> None:
        sha1 = "A" * 40
        calls = [
            lambda: wincert.find_cert(sha1),
            lambda: wincert.list_certs(),
            lambda: wincert.test_sign(sha1),
            lambda: wincert.delete_cert(sha1),
            lambda: wincert.cert_health(sha1),
            lambda: wincert.days_until_expiry(sha1),
            lambda: wincert.create_signing_cert(subject="CN=x", friendly_name="x"),
            lambda: wincert.create_signing_cert_with_ksp(
                subject="CN=x", friendly_name="x", ksp="software"
            ),
        ]
        for call in calls:
            with pytest.raises(wincert.WinCertError, match="Windows only"):
                call()


# ─── find / parse ────────────────────────────────────────────────────────────


class TestFindCert:
    def test_returns_none_when_absent(self, win: FakeWindows) -> None:
        assert wincert.find_cert("A" * 40) is None

    def test_parses_the_certificate(self, win: FakeWindows) -> None:
        sha1 = win.add(provider=wincert.PROVIDER_TPM, friendly_name="Entrabot Blueprint abc")
        cert = wincert.find_cert(sha1.lower())
        assert cert is not None
        assert cert.sha1_hex == sha1
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", cert.x5t_s256)
        assert cert.subject == "CN=entrabot-blueprint"
        assert cert.friendly_name == "Entrabot Blueprint abc"
        assert cert.not_after.tzinfo is not None and cert.not_before < cert.not_after
        assert cert.has_private_key is True
        assert cert.ksp == "tpm"

    def test_software_and_unknown_and_keyless(self, win: FakeWindows) -> None:
        soft = win.add(provider=wincert.PROVIDER_SOFTWARE)
        other = win.add(provider="Some Smart Card Provider")
        keyless = win.add(provider=None)
        assert wincert.find_cert(soft).ksp == "software"  # type: ignore[union-attr]
        assert wincert.find_cert(other).ksp == "unknown"  # type: ignore[union-attr]
        found = wincert.find_cert(keyless)
        assert found is not None and found.has_private_key is False

    @pytest.mark.parametrize("bad", ["", "zz", "A" * 39, "A" * 41, "G" * 40])
    def test_rejects_malformed_thumbprint_without_echoing_it(
        self, win: FakeWindows, bad: str
    ) -> None:
        with pytest.raises(wincert.WinCertError) as excinfo:
            wincert.find_cert(bad)
        if bad:
            assert bad not in str(excinfo.value)

    def test_repr_never_shows_the_full_thumbprint_or_der(self, win: FakeWindows) -> None:
        sha1 = win.add()
        text = repr(wincert.find_cert(sha1))
        assert sha1 not in text and "der=" not in text
        assert sha1[-4:] in text

    def test_list_by_friendly_name(self, win: FakeWindows) -> None:
        mine = win.add(friendly_name="Entrabot Provisioner t1")
        win.add(friendly_name="Something else")
        assert [
            c.sha1_hex for c in wincert.list_certs(friendly_name="Entrabot Provisioner t1")
        ] == [mine]
        assert len(wincert.list_certs()) == 2


# ─── test_sign ───────────────────────────────────────────────────────────────


class TestTestSign:
    def test_passes_when_the_key_signs_and_matches(self, win: FakeWindows) -> None:
        wincert.test_sign(win.add())

    def test_missing_cert(self, win: FakeWindows) -> None:
        with pytest.raises(wincert.WinCertError, match="not in this user's certificate store"):
            wincert.test_sign("B" * 40)

    def test_key_that_cannot_sign(self, win: FakeWindows) -> None:
        sha1 = win.add()
        del win.keys[sha1]
        with pytest.raises(wincert.WinCertError, match="could not sign"):
            wincert.test_sign(sha1)

    def test_key_that_does_not_match_the_certificate(self, win: FakeWindows) -> None:
        sha1 = win.add()
        win.keys[sha1] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(wincert.WinCertError, match="does not match"):
            wincert.test_sign(sha1)

    def test_is_not_collected_by_pytest(self) -> None:
        assert wincert.test_sign.__test__ is False  # type: ignore[attr-defined]


# ─── delete ──────────────────────────────────────────────────────────────────


class TestDeleteCert:
    def test_false_when_absent_and_nothing_runs(self, win: FakeWindows) -> None:
        assert wincert.delete_cert("C" * 40) is False
        assert win.calls == []

    def test_deletes_certificate_and_key(self, win: FakeWindows) -> None:
        sha1 = win.add()
        assert wincert.delete_cert(sha1) is True
        assert sha1 not in win.certs and sha1 not in win.keys
        script, variables = win.deletes()[0]
        assert "-DeleteKey" in script
        assert variables == {wincert._ENV_SHA1: sha1}

    def test_can_keep_the_key(self, win: FakeWindows) -> None:
        sha1 = win.add()
        assert wincert.delete_cert(sha1, delete_key=False) is True
        assert sha1 in win.keys
        assert "-DeleteKey" not in win.deletes()[0][0]

    def _deletekey_fails(
        self, win: FakeWindows, monkeypatch: pytest.MonkeyPatch, stderr: str
    ) -> None:
        real = win.run_powershell

        def flaky(script: str, variables: dict[str, str]) -> wincert._PowerShellResult:
            if "-DeleteKey" in script:
                win.calls.append((script, dict(variables)))
                return wincert._PowerShellResult(1, "", stderr)
            return real(script, variables)

        monkeypatch.setattr(wincert, "_run_powershell", flaky)

    # The contract: the key is deleted with the certificate (-DeleteKey). When the key
    # can't be deleted, the certificate stays in the store, so a retry can still delete
    # both and the key stays identifiable. Nothing ever removes the certificate alone
    # after a failed key deletion. True only when both are gone.

    def test_key_that_cannot_be_deleted_leaves_both_and_says_so(
        self, win: FakeWindows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha1 = win.add(key_container="te-entrabot-0b9c2f7e")
        self._deletekey_fails(win, monkeypatch, "The TPM is defending against dictionary attacks")

        with pytest.raises(wincert.WinCertError) as excinfo:
            wincert.delete_cert(sha1)

        message = str(excinfo.value)
        assert "and its private key are still on this computer" in message
        assert "could not be deleted" in message
        assert "dictionary attacks" in message
        assert "te-entrabot-0b9c2f7e" in message, "the key stays identifiable"
        assert wincert.PROVIDER_SOFTWARE in message
        assert sha1 not in message and sha1[-4:] in message
        assert sha1 in win.certs and sha1 in win.keys
        assert len(win.deletes()) == 1, "no certificate-only removal"

    def test_a_container_name_that_would_reveal_the_thumbprint_is_left_out(
        self, win: FakeWindows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha1 = win.add(key_container="placeholder")
        win.certs[sha1] = wincert._RawCert(
            der=win.certs[sha1].der,
            friendly_name=win.certs[sha1].friendly_name,
            provider_name=wincert.PROVIDER_SOFTWARE,
            key_container=f"le-{sha1.lower()}",
        )
        self._deletekey_fails(win, monkeypatch, "Access is denied")

        with pytest.raises(wincert.WinCertError) as excinfo:
            wincert.delete_cert(sha1)

        message = str(excinfo.value)
        assert sha1 not in message.upper()
        assert "are still on this computer" in message

    def test_key_already_gone_is_not_papered_over_by_a_certificate_only_removal(
        self, win: FakeWindows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # -DeleteKey fails because there is no key left. The certificate is not removed
        # behind the caller's back; the message says the key is already gone and how
        # to remove the certificate on its own, which the caller may then ask for.
        sha1 = win.add()
        del win.keys[sha1]
        self._deletekey_fails(win, monkeypatch, "Keyset does not exist")

        with pytest.raises(wincert.WinCertError) as excinfo:
            wincert.delete_cert(sha1)

        message = str(excinfo.value)
        assert "still in the certificate store" in message
        assert "no longer exists" in message
        assert "delete_key=False" in message
        assert "Keyset does not exist" in message
        assert sha1 not in message and sha1[-4:] in message
        assert sha1 in win.certs
        assert len(win.deletes()) == 1, "no certificate-only removal"

        assert wincert.delete_cert(sha1, delete_key=False) is True
        assert sha1 not in win.certs

    def test_an_orphaned_key_is_reported_when_the_certificate_went_without_it(
        self, win: FakeWindows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Can't normally happen (-DeleteKey deletes the key first), but if the
        # certificate is gone and the key deletion failed, the key remains: say so.
        sha1 = win.add(key_container="te-entrabot-77aa")
        real = win.run_powershell

        def cert_only(script: str, variables: dict[str, str]) -> wincert._PowerShellResult:
            if "-DeleteKey" in script:
                win.calls.append((script, dict(variables)))
                win.certs.pop(variables[wincert._ENV_SHA1], None)
                return wincert._PowerShellResult(1, "", "Access is denied")
            return real(script, variables)

        monkeypatch.setattr(wincert, "_run_powershell", cert_only)

        with pytest.raises(wincert.WinCertError) as excinfo:
            wincert.delete_cert(sha1)

        message = str(excinfo.value)
        assert "no longer in the certificate store" in message
        assert "private key remains" in message
        assert "te-entrabot-77aa" in message
        assert "Access is denied" in message
        assert sha1 not in message and sha1[-4:] in message
        assert len(win.deletes()) == 1

    def test_success_is_only_reported_once_the_certificate_is_gone(
        self, win: FakeWindows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha1 = win.add()
        monkeypatch.setattr(
            wincert, "_run_powershell", lambda *_a: wincert._PowerShellResult(0, "", "")
        )
        with pytest.raises(wincert.WinCertError, match="Could not remove"):
            wincert.delete_cert(sha1)

    def test_certificate_without_a_key_is_removed_without_deletekey(self, win: FakeWindows) -> None:
        sha1 = win.add(provider=None)
        assert wincert.delete_cert(sha1) is True
        assert sha1 not in win.certs
        assert ["-DeleteKey" in script for script, _ in win.deletes()] == [False]

    def test_raises_when_the_certificate_is_still_there(
        self, win: FakeWindows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha1 = win.add()
        monkeypatch.setattr(
            wincert,
            "_run_powershell",
            lambda *_a: wincert._PowerShellResult(1, "", "Access is denied"),
        )
        with pytest.raises(wincert.WinCertError) as excinfo:
            wincert.delete_cert(sha1)
        assert "Access is denied" in str(excinfo.value)
        assert sha1 not in str(excinfo.value)


# ─── create: TPM by attempt ──────────────────────────────────────────────────


class TestCreateSigningCert:
    def test_tpm_first_and_values_travel_in_the_environment(self, win: FakeWindows) -> None:
        cert, reason = wincert.create_signing_cert(
            subject="CN=O'Brien's agent", friendly_name="Entrabot Blueprint 1234", days=30
        )
        assert reason is None
        assert cert.ksp == "tpm" and cert.has_private_key
        (create,) = win.creates()
        assert create[wincert._ENV_PROVIDER] == wincert.PROVIDER_TPM
        assert create[wincert._ENV_SUBJECT] == "CN=O'Brien's agent"
        assert create[wincert._ENV_FRIENDLY_NAME] == "Entrabot Blueprint 1234"
        not_after = datetime.strptime(create[wincert._ENV_NOT_AFTER], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        assert timedelta(days=29, hours=23) < not_after - datetime.now(UTC) <= timedelta(days=30)
        script = win.calls[0][0]
        assert script == wincert._CREATE_SCRIPT and "O'Brien" not in script

    def test_falls_back_to_software_when_the_tpm_cannot_create_the_key(
        self, win: FakeWindows
    ) -> None:
        win.fail_create_on[wincert.PROVIDER_TPM] = (
            "New-SelfSignedCertificate : CertEnroll::CX509Enrollment::_CreateRequest: The device "
            "that is required by this cryptographic provider is not ready for use. 0x80090030"
        )
        cert, reason = wincert.create_signing_cert(subject="CN=x", friendly_name="f")
        assert cert.ksp == "software"
        assert reason == "this machine has no TPM that is ready to use"
        assert [c[wincert._ENV_PROVIDER] for c in win.creates()] == [
            wincert.PROVIDER_TPM,
            wincert.PROVIDER_SOFTWARE,
        ]
        assert len(win.certs) == 1, "nothing left over from the failed attempt"

    def test_tpm_key_that_cannot_sign_is_deleted_with_its_key_then_software(
        self, win: FakeWindows
    ) -> None:
        win.unsignable_providers.add(wincert.PROVIDER_TPM)
        cert, reason = wincert.create_signing_cert(subject="CN=x", friendly_name="f")
        assert cert.ksp == "software"
        assert reason and "TPM" in reason
        assert len(win.certs) == 1 and len(win.keys) == 1
        assert list(win.certs) == [cert.sha1_hex]
        (delete_script, _vars) = win.deletes()[0]
        assert "-DeleteKey" in delete_script

    def test_software_failure_after_tpm_failure_raises_and_leaves_nothing(
        self, win: FakeWindows
    ) -> None:
        win.fail_create_on[wincert.PROVIDER_TPM] = "0x80090030"
        win.fail_create_on[wincert.PROVIDER_SOFTWARE] = "Access is denied."
        with pytest.raises(wincert.WinCertError, match="Access is denied"):
            wincert.create_signing_cert(subject="CN=x", friendly_name="f")
        assert win.certs == {} and win.keys == {}

    def test_prefer_tpm_false_never_touches_the_tpm(self, win: FakeWindows) -> None:
        cert, reason = wincert.create_signing_cert(
            subject="CN=x", friendly_name="f", prefer_tpm=False
        )
        assert cert.ksp == "software" and reason
        assert [c[wincert._ENV_PROVIDER] for c in win.creates()] == [wincert.PROVIDER_SOFTWARE]

    def test_explicit_ksp_does_not_fall_back(self, win: FakeWindows) -> None:
        win.fail_create_on[wincert.PROVIDER_TPM] = "0x80090030"
        with pytest.raises(wincert.WinCertError, match="tpm"):
            wincert.create_signing_cert_with_ksp(subject="CN=x", friendly_name="f", ksp="tpm")
        assert len(win.creates()) == 1

    @pytest.mark.parametrize(
        "stdout", ["not-a-thumbprint\n", "ABCDEF1234567890ABCDEF1234567890ABCDEF12\njunk\n", ""]
    )
    def test_corrupt_thumbprint_output_is_rejected(self, win: FakeWindows, stdout: str) -> None:
        win.corrupt_stdout = stdout
        with pytest.raises(wincert.WinCertError, match="thumbprint"):
            wincert.create_signing_cert_with_ksp(subject="CN=x", friendly_name="f", ksp="software")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"subject": "CN=x", "friendly_name": "f", "ksp": "bogus"},
            {"subject": "CN=x", "friendly_name": "f", "ksp": "software", "days": 0},
            {"subject": " ", "friendly_name": "f", "ksp": "software"},
            {"subject": "CN=x", "friendly_name": "", "ksp": "software"},
        ],
    )
    def test_invalid_arguments_run_nothing(self, win: FakeWindows, kwargs: dict) -> None:
        with pytest.raises(wincert.WinCertError):
            wincert.create_signing_cert_with_ksp(**kwargs)
        assert win.calls == []


# ─── health / expiry ─────────────────────────────────────────────────────────


class TestCertHealth:
    def test_ok(self, win: FakeWindows) -> None:
        health = wincert.cert_health(win.add(), now=NOW)
        assert health.status == "ok" and health.usable
        assert health.days_left == 365

    def test_expiring_within_thirty_days(self, win: FakeWindows) -> None:
        sha1 = win.add(not_after=NOW + timedelta(days=12))
        health = wincert.cert_health(sha1, now=NOW)
        assert health.status == "expiring" and health.usable
        assert health.days_left == 12
        assert "setup-windows.cmd" in health.message

    def test_expired(self, win: FakeWindows) -> None:
        sha1 = win.add(not_before=NOW - timedelta(days=400), not_after=NOW - timedelta(days=2))
        health = wincert.cert_health(sha1, now=NOW)
        assert health.status == "expired" and not health.usable
        assert "expired on" in health.message and "setup-windows.cmd" in health.message
        assert sha1 not in health.message

    def test_not_yet_valid(self, win: FakeWindows) -> None:
        sha1 = win.add(not_before=NOW + timedelta(days=1), not_after=NOW + timedelta(days=9))
        assert wincert.cert_health(sha1, now=NOW).status == "not_yet_valid"

    def test_missing_and_unconfigured(self, win: FakeWindows) -> None:
        missing = wincert.cert_health("D" * 40, now=NOW)
        assert missing.status == "missing" and "certificate store" in missing.message
        assert "D" * 40 not in missing.message
        unset = wincert.cert_health(None, now=NOW)
        assert unset.status == "missing" and "ENTRABOT_BLUEPRINT_CERT_SHA1" in unset.message

    def test_no_private_key(self, win: FakeWindows) -> None:
        health = wincert.cert_health(win.add(provider=None), now=NOW)
        assert health.status == "no_private_key" and not health.usable

    def test_days_until_expiry(self, win: FakeWindows) -> None:
        sha1 = win.add(not_after=NOW + timedelta(days=45))
        assert wincert.days_until_expiry(sha1, now=NOW) == 45
        assert wincert.days_until_expiry("E" * 40, now=NOW) is None


def test_mask_thumbprint() -> None:
    assert wincert.mask_thumbprint("abcdef" + "0" * 30 + "12cd") == "…12CD"
    assert wincert.mask_thumbprint(None) == "(none)"


# ─── real Windows ────────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="needs the Windows certificate store")
class TestRealWindows:
    def test_software_lifecycle(self) -> None:
        cert, reason = wincert.create_signing_cert(
            subject="CN=entrabot-wincert-test",
            friendly_name="Entrabot wincert test",
            days=1,
            prefer_tpm=False,
        )
        try:
            assert reason
            assert cert.ksp == "software" and cert.has_private_key
            found = wincert.find_cert(cert.sha1_hex)
            assert found is not None and found.der == cert.der
            assert found.friendly_name == "Entrabot wincert test"
            assert found.subject == "CN=entrabot-wincert-test"
            # Backdated by the cmdlet for clock skew.
            assert found.not_before < datetime.now(UTC)
            wincert.test_sign(cert.sha1_hex)
            assert cert.sha1_hex in {
                c.sha1_hex for c in wincert.list_certs(friendly_name="Entrabot wincert test")
            }
            # No TLS usages: not offered as a browser client certificate.
            parsed = x509.load_der_x509_certificate(cert.der)
            with pytest.raises(x509.ExtensionNotFound):
                parsed.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        finally:
            assert wincert.delete_cert(cert.sha1_hex) is True
        assert wincert.find_cert(cert.sha1_hex) is None

    def test_tpm_by_attempt_always_yields_a_working_key(self) -> None:
        cert, reason = wincert.create_signing_cert(
            subject="CN=entrabot-wincert-tpm-test",
            friendly_name="Entrabot wincert tpm test",
            days=1,
        )
        try:
            assert cert.ksp in ("tpm", "software")
            assert (reason is None) == (cert.ksp == "tpm")
            wincert.test_sign(cert.sha1_hex)
        finally:
            wincert.delete_cert(cert.sha1_hex)

    def test_apostrophe_in_subject_and_friendly_name(self) -> None:
        cert, _ = wincert.create_signing_cert(
            subject="CN=O'Brien entrabot test",
            friendly_name="O'Brien's Entrabot test",
            days=1,
            prefer_tpm=False,
        )
        try:
            assert "O'Brien" in cert.subject
            assert cert.friendly_name == "O'Brien's Entrabot test"
        finally:
            wincert.delete_cert(cert.sha1_hex)
