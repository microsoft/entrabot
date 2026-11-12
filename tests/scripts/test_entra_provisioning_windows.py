"""Windows behaviour of ``scripts/entra_provisioning.py``.

Three things that only matter on Windows, simulated here so they run everywhere:

* ``az`` is ``az.cmd``. Arguments that pass through ``cmd.exe`` are re-parsed, so a
  parenthesis in a URL ends the batch file's ``IF ( ... )`` block. ``run_az`` runs the
  CLI's own Python directly instead.
* The Provisioner's private key is a non-exportable CNG key in the certificate
  store. It is never written to disk, and a PEM left by an older setup is removed
  once the new credential is on the app.
* The token is acquired with a client assertion signed by that key.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from entrabot.platform import wincert
from tests.locale_codec import force_locale_codec

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "entra_provisioning.py"

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"
OTHER_SHA1 = "FEDCBA9876543210FEDCBA9876543210FEDCBA98"
X5T = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"


def _legacy_pem() -> tuple[str, str, str]:
    """A PEM bundle as an older setup left it: (bundle, SHA-1 hex upper, expiry date)."""
    import hashlib

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "entrabot-provisioner")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=30))
        .not_valid_after(now + timedelta(days=200))
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    bundle = cert.public_bytes(serialization.Encoding.PEM).decode() + (
        "-----BEGIN PRIVATE KEY-----\nold-key\n-----END PRIVATE KEY-----\n"
    )
    return (
        bundle,
        hashlib.sha1(der).hexdigest().upper(),  # noqa: S324
        cert.not_valid_after_utc.date().isoformat(),
    )


LEGACY_PEM, LEGACY_SHA1, LEGACY_EXPIRES = _legacy_pem()


@pytest.fixture
def ep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    spec = importlib.util.spec_from_file_location("entra_provisioning", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["entra_provisioning"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_STATE_FILE", tmp_path / ".entrabot-state.json")
    monkeypatch.delenv("ENTRABOT_TENANT_ID", raising=False)
    # The operator's "I checked this app", if their shell has one; and the app ID setup
    # writes to .env, which importing entrabot.config loads into the environment.
    monkeypatch.delenv("ENTRABOT_PROVISIONER_APP_ID_VERIFIED", raising=False)
    monkeypatch.delenv("ENTRABOT_PROVISIONER_APP_ID", raising=False)
    yield module
    sys.modules.pop("entra_provisioning", None)


def _store_cert(
    sha1: str = SHA1,
    *,
    friendly_name: str = f"Entrabot Provisioner {TENANT}",
    days: int = 300,
    has_private_key: bool = True,
    ksp: str = "tpm",
) -> wincert.StoreCert:
    now = datetime.now(UTC)
    return wincert.StoreCert(
        sha1_hex=sha1,
        x5t_s256=X5T,
        der=b"\x30\x03\x02\x01\x01",
        subject="CN=entrabot-provisioner",
        friendly_name=friendly_name,
        not_before=now - timedelta(days=365 - days) - timedelta(minutes=10),
        not_after=now + timedelta(days=days),
        has_private_key=has_private_key,
        ksp=ksp,
    )


# ─── run_az ──────────────────────────────────────────────────────────────────


class TestRunAzOnWindows:
    @pytest.fixture
    def captured(self, ep: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        class _Done:
            returncode = 0
            stdout = " ok \n"
            stderr = ""

        def fake_run(command: Any, **kwargs: Any) -> _Done:
            seen["command"] = command
            seen["kwargs"] = kwargs
            return _Done()

        monkeypatch.setattr(ep.subprocess, "run", fake_run)
        monkeypatch.setattr(ep.sys, "platform", "win32")
        return seen

    def test_msi_layout_runs_the_bundled_python_not_the_cmd_shim(
        self, ep: Any, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        wbin = tmp_path / "CLI2" / "wbin"
        wbin.mkdir(parents=True)
        (wbin / "az.cmd").write_text("@echo off\n", encoding="utf-8")
        python = tmp_path / "CLI2" / "python.exe"
        python.write_bytes(b"")
        monkeypatch.setattr(ep.shutil, "which", lambda name: str(wbin / "az.cmd"))
        url = "https://graph.microsoft.com/v1.0/servicePrincipals(appId='x')/appRoleAssignments"

        rc, out, err = ep.run_az(["rest", "--url", url])

        assert (rc, out, err) == (0, "ok", "")
        assert captured["command"] == [str(python), "-IBm", "azure.cli", "rest", "--url", url]

    def test_pip_layout_runs_the_venv_python(
        self, ep: Any, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        scripts = tmp_path / "azcli" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "az.bat").write_text("@echo off\n", encoding="utf-8")
        python = scripts / "python.exe"
        python.write_bytes(b"")
        monkeypatch.setattr(ep.shutil, "which", lambda name: str(scripts / "az.bat"))

        ep.run_az(["account", "show"])

        assert captured["command"] == [str(python), "-m", "azure.cli", "account", "show"]

    def test_unknown_layout_goes_through_the_shim_with_metacharacters_quoted(
        self, ep: Any, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shim = tmp_path / "odd" / "az.cmd"
        shim.parent.mkdir()
        shim.write_text("@echo off\n", encoding="utf-8")
        monkeypatch.setattr(ep.shutil, "which", lambda name: str(shim))

        ep.run_az(["rest", "--url", "https://x/sp(appId='y')/z", "--query", "a&b", "plain"])

        command = captured["command"]
        assert isinstance(command, str), "a string, so the quoting below is what cmd.exe sees"
        assert "\"https://x/sp(appId='y')/z\"" in command
        assert '"a&b"' in command
        assert command.endswith(" plain")

    def test_other_platforms_are_unchanged(self, ep: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        class _Done:
            returncode = 3
            stdout = ""
            stderr = "nope\n"

        def fake_run(command: Any, **kwargs: Any) -> _Done:
            seen["command"] = command
            return _Done()

        monkeypatch.setattr(ep.subprocess, "run", fake_run)
        monkeypatch.setattr(ep.sys, "platform", "darwin")
        monkeypatch.setattr(ep.shutil, "which", lambda name: "/opt/homebrew/bin/az")

        assert ep.run_az(["account", "show"]) == (3, "", "nope")
        assert seen["command"] == ["/opt/homebrew/bin/az", "account", "show"]


class TestAzArguments:
    def test_no_tsv_parsing_anywhere(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        assert '"tsv"' not in source

    def test_consented_roles_resolve_the_service_principal_then_read_by_object_id(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        graph_sp = "99999999-0000-0000-0000-000000000003"
        graph_app = "00000003-0000-0000-c000-000000000000"

        def granted(role: str, resource: str, name: str) -> dict[str, str]:
            return {
                "principalId": "sp-object-id",
                "resourceId": resource,
                "resourceDisplayName": name,
                "appRoleId": role,
            }

        def fake_run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
            calls.append(args)
            if args[:5] == ["ad", "sp", "show", "--id", graph_app]:
                return 0, json.dumps([graph_sp, graph_app]), ""
            if args[:3] == ["ad", "sp", "show"]:
                return 0, '"sp-object-id"', ""
            return (
                0,
                json.dumps(
                    {
                        "value": [
                            granted("role-rw", graph_sp, "Microsoft Graph"),
                            granted("role-x", "some-other-api", "Some Other API"),
                            granted("role-apra", graph_sp, "Microsoft Graph"),
                            # Calls itself Microsoft Graph, but is somebody's own app.
                            granted("role-fake", "look-alike", "Microsoft Graph"),
                        ]
                    }
                ),
                "",
            )

        monkeypatch.setattr(ep, "run_az", fake_run_az)

        assert ep._get_consented_graph_role_ids(CLIENT) == {"role-rw", "role-apra"}

        assert calls[0][:5] == ["ad", "sp", "show", "--id", CLIENT]
        assert calls[1][:5] == ["ad", "sp", "show", "--id", graph_app], "Graph, by its appId"
        url = calls[2][calls[2].index("--url") + 1]
        assert url == (
            "https://graph.microsoft.com/v1.0/servicePrincipals/sp-object-id/appRoleAssignments"
        )
        for args in calls:
            for arg in args:
                assert not re.search(r"[()&|^<>%]", arg), f"cmd.exe metacharacter in {arg!r}"

    def test_scalars_come_back_as_json(self, ep: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []

        def fake_run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
            seen.append(args)
            return 0, '"user-object-id"', ""

        monkeypatch.setattr(ep, "run_az", fake_run_az)

        assert ep.get_signed_in_user_id() == "user-object-id"
        assert seen[0][-2:] == ["-o", "json"]


# ─── admin consent ───────────────────────────────────────────────────────────


class TestAdminConsent:
    def _setup(self, ep: Any, monkeypatch: pytest.MonkeyPatch, consent_results: list) -> list:
        monkeypatch.setattr(ep, "_ensure_service_principal", lambda client_id: None)
        monkeypatch.setattr(
            ep,
            "_resolve_permission_specs",
            lambda values: [("Application.ReadWrite.All", "r=Role")],
        )
        monkeypatch.setattr(ep, "_get_existing_permission_role_ids", lambda client_id: {"r"})
        monkeypatch.setattr(ep, "_get_consented_graph_role_ids", lambda client_id: set())
        sleeps: list[float] = []
        monkeypatch.setattr(ep.time, "sleep", sleeps.append)
        results = list(consent_results)

        def fake_run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
            assert args[:4] == ["ad", "app", "permission", "admin-consent"]
            return results.pop(0)

        monkeypatch.setattr(ep, "run_az", fake_run_az)
        return sleeps

    @pytest.mark.parametrize(
        "stderr",
        [
            "ERROR: Insufficient privileges to complete the operation.",
            "Forbidden({'ClassName': 'Microsoft.Portal.Extensions.AadIam.ClientException'})",
            "Authorization_RequestDenied: Insufficient privileges",
        ],
    )
    def test_missing_privilege_fails_fast_and_names_the_role(
        self,
        ep: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        stderr: str,
    ) -> None:
        sleeps = self._setup(ep, monkeypatch, [(1, "", stderr)])

        with pytest.raises(ep.ProvisionerBootstrapError) as excinfo:
            ep._ensure_permissions_and_consent(CLIENT, ["Application.ReadWrite.All"])

        assert sleeps == [], "no retries when the account simply can't do this"
        text = capsys.readouterr().out + str(excinfo.value)
        assert "Privileged Role Administrator" in text and "Global Administrator" in text

    def test_transient_failure_is_still_retried(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps = self._setup(
            ep,
            monkeypatch,
            [(1, "", "Bad Request: service principal not found yet"), (0, "", "")],
        )
        assert ep._ensure_permissions_and_consent(CLIENT, ["Application.ReadWrite.All"]) is True
        assert len(sleeps) == 1


# ─── state file ──────────────────────────────────────────────────────────────


def test_state_file_is_utf8_whatever_the_locale(ep: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A state file holding raw UTF-8 (hand-edited, or written by another tool) must
    # read back intact on a machine whose ANSI code page is cp1252.
    upn = "agent-Zoë-Ł@contoso.onmicrosoft.com"
    ep._STATE_FILE.write_bytes(
        json.dumps({"AGENT_USER_UPN": upn}, ensure_ascii=False).encode("utf-8")
    )
    force_locale_codec(monkeypatch)

    assert ep.get_state("AGENT_USER_UPN") == upn
    ep.set_state("TENANT_ID", TENANT)
    assert ep.get_state("AGENT_USER_UPN") == upn


# ─── the Provisioner credential on Windows ───────────────────────────────────


class _Env:
    """A Windows machine as ``ensure_app_registration`` sees it."""

    def __init__(self, ep: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.ep = ep
        self.store: dict[str, wincert.StoreCert] = {}
        self.unsignable: set[str] = set()
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.upload_error: Exception | None = None
        self.events: list[str] = []
        self.token_error: Exception | None = None
        self.revokes: list[dict[str, Any]] = []
        self.revoke_outcome: str | Exception = "removed"
        self.local_appdata = tmp_path / "LocalAppData"
        self.local_appdata.mkdir()

        monkeypatch.setenv("LOCALAPPDATA", str(self.local_appdata))
        monkeypatch.setattr(ep, "_uses_cng", lambda: True)
        self.app_status = "exists"
        monkeypatch.setattr(ep, "_application_status", lambda client_id: self.app_status)
        monkeypatch.setattr(ep, "_ensure_permissions_and_consent", lambda *a: False)
        monkeypatch.setattr(ep, "_remove_legacy_password_credentials", lambda *a: 0)
        monkeypatch.setattr(ep, "_upload_cert_to_app", self._upload)
        monkeypatch.setattr(ep, "_acquire_graph_token_cng", self._token)
        monkeypatch.setattr(ep, "_revoke_app_certificate", self._revoke)
        monkeypatch.setattr(wincert, "list_certs", self._list)
        monkeypatch.setattr(wincert, "test_sign", self._test_sign)
        monkeypatch.setattr(wincert, "create_signing_cert", self._create)
        monkeypatch.setattr(wincert, "delete_cert", self._delete)
        ep.set_state("TENANT_ID", TENANT)
        ep.set_state("PROVISIONER_CLIENT_ID", CLIENT)

    @property
    def legacy_pem(self) -> Path:
        return self.local_appdata / "entrabot" / f"provisioner-cert-{TENANT}.pem"

    def write_legacy_pem(self) -> None:
        self.legacy_pem.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_pem.write_text(LEGACY_PEM, encoding="utf-8")

    def _upload(self, client_id: str, cert_pem: str) -> None:
        if self.upload_error:
            raise self.upload_error
        self.events.append("upload")
        self.uploads.append((client_id, cert_pem))

    def _token(self, tenant_id: str, client_id: str, credential: Any) -> str:
        self.events.append("token")
        if self.token_error:
            raise self.token_error
        return "graph-token"

    def _revoke(self, client_id: str, sha1_hex: str, **kwargs: Any) -> str:
        self.events.append("revoke")
        self.revokes.append({"client_id": client_id, "sha1_hex": sha1_hex, **kwargs})
        # The key file must still be there: revocation comes before deletion.
        self.events.append("pem-present" if self.legacy_pem.exists() else "pem-gone")
        if isinstance(self.revoke_outcome, Exception):
            raise self.revoke_outcome
        return self.revoke_outcome

    def _list(self, *, friendly_name: str | None = None) -> list[wincert.StoreCert]:
        return [c for c in self.store.values() if c.friendly_name == friendly_name]

    def _test_sign(self, sha1: str) -> None:
        if sha1 in self.unsignable:
            raise wincert.WinCertError("the key is not usable here")

    def _create(self, **kwargs: Any) -> tuple[wincert.StoreCert, str | None]:
        self.created.append(kwargs)
        cert = _store_cert(OTHER_SHA1, friendly_name=kwargs["friendly_name"], ksp="software")
        self.store[cert.sha1_hex] = cert
        return cert, "this machine has no TPM that is ready to use"

    def _delete(self, sha1: str, *, delete_key: bool = True) -> bool:
        self.deleted.append(sha1)
        return self.store.pop(sha1, None) is not None

    def ensure(self) -> tuple[str, Any, str]:
        return self.ep.ensure_app_registration(["Application.ReadWrite.All"])


@pytest.fixture
def env(ep: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Env:
    return _Env(ep, monkeypatch, tmp_path)


class TestWindowsProvisionerCredential:
    def test_first_run_creates_a_cng_key_uploads_the_public_cert_and_writes_no_file(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client_id, credential, tenant_id = env.ensure()

        assert (client_id, tenant_id) == (CLIENT, TENANT)
        assert isinstance(credential, env.ep.CngProvisionerCert)
        assert credential.sha1_hex == OTHER_SHA1 and credential.x5t_s256 == X5T
        assert env.created == [
            {
                "subject": "CN=entrabot-provisioner",
                "friendly_name": f"Entrabot Provisioner {TENANT}",
                "days": 365,
            }
        ]
        (upload,) = env.uploads
        assert upload[0] == CLIENT
        assert upload[1].startswith("-----BEGIN CERTIFICATE-----")
        assert "PRIVATE KEY" not in upload[1]
        assert env.ep.get_state("PROVISIONER_CERT_THUMBPRINT") == OTHER_SHA1.lower()
        assert list(env.local_appdata.rglob("*.pem")) == []

        out = capsys.readouterr().out
        assert "Keychain" not in out
        assert "certificate store" in out
        assert OTHER_SHA1 not in out and OTHER_SHA1.lower() not in out

    def test_recorded_certificate_is_reused_without_an_upload(self, env: _Env) -> None:
        env.store[SHA1] = _store_cert(SHA1)
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())

        _, credential, _ = env.ensure()

        assert credential.sha1_hex == SHA1
        assert env.created == [] and env.uploads == []

    def test_unrecorded_certificate_is_uploaded_and_recorded(self, env: _Env) -> None:
        # The same Windows user set up another checkout, or the app was recreated.
        env.store[SHA1] = _store_cert(SHA1)

        _, credential, _ = env.ensure()

        assert credential.sha1_hex == SHA1
        assert env.created == [] and len(env.uploads) == 1
        assert env.ep.get_state("PROVISIONER_CERT_THUMBPRINT") == SHA1.lower()

    @pytest.mark.parametrize("problem", ["expired", "no_key", "cannot_sign"])
    def test_a_certificate_that_cannot_authenticate_is_replaced(
        self, env: _Env, problem: str
    ) -> None:
        if problem == "expired":
            env.store[SHA1] = _store_cert(SHA1, days=-1)
        elif problem == "no_key":
            env.store[SHA1] = _store_cert(SHA1, has_private_key=False)
        else:
            env.store[SHA1] = _store_cert(SHA1)
            env.unsignable.add(SHA1)
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())

        _, credential, _ = env.ensure()

        assert credential.sha1_hex == OTHER_SHA1
        assert len(env.created) == 1 and len(env.uploads) == 1

    def test_failed_upload_removes_the_new_key_and_keeps_the_old_credential(
        self, env: _Env
    ) -> None:
        env.write_legacy_pem()
        env.upload_error = env.ep.ProvisionerBootstrapError("Insufficient privileges")

        with pytest.raises(env.ep.ProvisionerBootstrapError, match="Insufficient privileges"):
            env.ensure()

        assert env.deleted == [OTHER_SHA1], "no orphaned key from the failed attempt"
        assert env.legacy_pem.read_text(encoding="utf-8") == LEGACY_PEM
        assert env.ep.get_state("PROVISIONER_CERT_THUMBPRINT") is None

    def test_migration_proves_the_new_key_then_revokes_the_old_one_then_deletes_the_file(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.write_legacy_pem()

        _, credential, _ = env.ensure()

        assert isinstance(credential, env.ep.CngProvisionerCert)
        assert env.events == ["upload", "token", "revoke", "pem-present"]
        assert env.revokes == [
            {"client_id": CLIENT, "sha1_hex": LEGACY_SHA1, "keep_sha1": OTHER_SHA1}
        ]
        assert not env.legacy_pem.exists()
        assert env.ep.get_state("PROVISIONER_CERT_REVOKE_PENDING") is None
        out = capsys.readouterr().out
        assert "old-key" not in out and LEGACY_SHA1 not in out
        assert "WARNING" not in out

    def test_legacy_pem_is_revoked_and_removed_when_a_recorded_cng_certificate_exists(
        self, env: _Env
    ) -> None:
        env.write_legacy_pem()
        env.store[SHA1] = _store_cert(SHA1)
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())

        env.ensure()

        assert env.events == ["token", "revoke", "pem-present"]
        assert env.revokes[0]["keep_sha1"] == SHA1
        assert not env.legacy_pem.exists()

    def test_old_certificate_that_is_not_on_the_app_just_loses_its_file(self, env: _Env) -> None:
        env.write_legacy_pem()
        env.revoke_outcome = "not_registered"

        env.ensure()

        assert not env.legacy_pem.exists()
        assert env.ep.get_state("PROVISIONER_CERT_REVOKE_PENDING") is None

    def test_new_key_not_accepted_yet_keeps_the_old_credential_working(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Never leave the machine without a working credential: until Entra has
        # accepted the key-store certificate, the old one stays, on disk and on the app.
        env.write_legacy_pem()
        env.token_error = env.ep.ProvisionerBootstrapError("AADSTS700027: not registered yet")

        _, credential, _ = env.ensure()

        assert credential == LEGACY_PEM
        assert env.revokes == []
        assert env.legacy_pem.read_text(encoding="utf-8") == LEGACY_PEM
        out = capsys.readouterr().out
        assert "AADSTS700027" in out and "setup again" in out

    def test_failed_revocation_still_finishes_and_tells_the_operator_what_to_remove(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.write_legacy_pem()
        env.revoke_outcome = env.ep.ProvisionerBootstrapError("Insufficient privileges")

        _, credential, _ = env.ensure()

        assert isinstance(credential, env.ep.CngProvisionerCert)
        assert not env.legacy_pem.exists(), "the plaintext key is removed either way"
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert f"…{LEGACY_SHA1[-4:]}" in out and LEGACY_SHA1 not in out
        assert LEGACY_EXPIRES in out
        assert CLIENT in out and env.ep.PROVISIONER_APP_DISPLAY_NAME in out
        assert "Certificates & secrets" in out
        assert "Insufficient privileges" in out
        assert env.ep.get_state("PROVISIONER_CERT_REVOKE_PENDING") == LEGACY_SHA1.lower()

    def test_a_pending_revocation_is_retried_on_the_next_run(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.store[SHA1] = _store_cert(SHA1)
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())
        env.ep.set_state("PROVISIONER_CERT_REVOKE_PENDING", LEGACY_SHA1.lower())

        env.ensure()

        assert env.revokes == [{"client_id": CLIENT, "sha1_hex": LEGACY_SHA1, "keep_sha1": SHA1}]
        assert env.ep.get_state("PROVISIONER_CERT_REVOKE_PENDING") is None

        env.ep.set_state("PROVISIONER_CERT_REVOKE_PENDING", LEGACY_SHA1.lower())
        env.revoke_outcome = env.ep.ProvisionerBootstrapError("still forbidden")
        capsys.readouterr()
        env.ensure()
        assert env.ep.get_state("PROVISIONER_CERT_REVOKE_PENDING") == LEGACY_SHA1.lower()
        out = capsys.readouterr().out
        assert "WARNING" in out and f"…{LEGACY_SHA1[-4:]}" in out

    def test_a_new_legacy_file_and_an_older_pending_revocation_are_both_handled(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.write_legacy_pem()
        env.store[SHA1] = _store_cert(SHA1)
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())
        older = "C" * 36 + "BEEF"
        env.ep.set_state("PROVISIONER_CERT_REVOKE_PENDING", older.lower())
        env.revoke_outcome = env.ep.ProvisionerBootstrapError("Insufficient privileges")

        env.ensure()

        assert [r["sha1_hex"] for r in env.revokes] == [LEGACY_SHA1, older]
        assert not env.legacy_pem.exists()
        assert env.ep.get_state("PROVISIONER_CERT_REVOKE_PENDING") == (
            f"{LEGACY_SHA1.lower()},{older.lower()}"
        )
        out = capsys.readouterr().out
        assert f"…{LEGACY_SHA1[-4:]}" in out and "…BEEF" in out
        assert LEGACY_SHA1 not in out and older not in out

    def test_losing_this_machines_certificate_puts_it_back(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.write_legacy_pem()
        env.store[SHA1] = _store_cert(SHA1)
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())
        env.revoke_outcome = env.ep.ProvisionerCertificateLostError("no longer listed")

        _, credential, _ = env.ensure()

        assert credential.sha1_hex == SHA1
        assert env.events == ["token", "revoke", "pem-present", "upload"]
        assert not env.legacy_pem.exists()
        assert env.ep.get_state("PROVISIONER_CERT_REVOKE_PENDING") is None
        assert env.ep.get_state("PROVISIONER_CERT_THUMBPRINT") == SHA1.lower()
        assert SHA1 not in capsys.readouterr().out

    def test_a_lost_certificate_that_cannot_be_put_back_is_an_error_and_is_retried(
        self, env: _Env
    ) -> None:
        env.write_legacy_pem()
        env.store[SHA1] = _store_cert(SHA1)
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())
        env.revoke_outcome = env.ep.ProvisionerCertificateLostError("no longer listed")
        env.upload_error = env.ep.ProvisionerBootstrapError("upload refused")

        with pytest.raises(env.ep.ProvisionerBootstrapError, match="upload refused"):
            env.ensure()

        # Unrecorded, so the next run uploads it again.
        assert env.ep.get_state("PROVISIONER_CERT_THUMBPRINT") is None
        assert SHA1 in env.store and env.deleted == []

    def test_unreadable_old_key_file_is_deleted_with_a_warning(
        self, env: _Env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env.legacy_pem.parent.mkdir(parents=True, exist_ok=True)
        env.legacy_pem.write_text("not a certificate\n", encoding="utf-8")

        env.ensure()

        assert env.revokes == []
        assert not env.legacy_pem.exists()
        out = capsys.readouterr().out
        assert "WARNING" in out and "Certificates & secrets" in out

    def test_confirmed_stale_app_keeps_the_old_key_file_until_a_replacement_works(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env.write_legacy_pem()
        env.app_status = "not_found"

        def fake_run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
            if args[:3] == ["ad", "app", "list"]:
                return 0, "[]", ""  # no app with the name: the replacement is created
            return 0, '"' + CLIENT + '"', ""

        monkeypatch.setattr(env.ep, "run_az", fake_run_az)
        env.upload_error = env.ep.ProvisionerBootstrapError("upload refused")

        with pytest.raises(env.ep.ProvisionerBootstrapError, match="upload refused"):
            env.ensure()
        assert env.legacy_pem.exists()

        env.upload_error = None
        env.ensure()
        assert not env.legacy_pem.exists()

    def test_a_failed_check_discards_nothing(self, env: _Env) -> None:
        env.write_legacy_pem()
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())
        env.app_status = "unknown"

        with pytest.raises(env.ep.ProvisionerBootstrapError, match="[Cc]ould not check"):
            env.ensure()

        assert env.legacy_pem.exists()
        assert env.ep.get_state("PROVISIONER_CLIENT_ID") == CLIENT
        assert env.ep.get_state("PROVISIONER_CERT_THUMBPRINT") == SHA1.lower()
        assert env.created == [] and env.uploads == []

    def test_key_creation_failure_is_a_bootstrap_error(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(**_kwargs: Any) -> None:
            raise wincert.WinCertError("New-SelfSignedCertificate failed: Access is denied.")

        monkeypatch.setattr(wincert, "create_signing_cert", boom)
        with pytest.raises(env.ep.ProvisionerBootstrapError, match="Access is denied"):
            env.ensure()

    def test_the_plaintext_writer_is_gone(self, env: _Env) -> None:
        with pytest.raises(env.ep.ProvisionerBootstrapError):
            env.ep._keychain_store_cert(TENANT, LEGACY_PEM)
        assert list(env.local_appdata.rglob("*.pem")) == []

    def test_empty_localappdata_does_not_become_a_relative_path(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("LOCALAPPDATA", "")
        monkeypatch.setattr(env.ep.Path, "home", classmethod(lambda cls: tmp_path / "home"))
        path = env.ep._windows_legacy_pem_path(TENANT)
        assert path.is_absolute()
        assert path == tmp_path / "home" / "AppData" / "Local" / "entrabot" / (
            f"provisioner-cert-{TENANT}.pem"
        )


class TestTakeOverOnWindows:
    """Taking over an existing app with the Provisioner's name works as on other platforms
    (``test_entra_provisioning.py``); only how this machine's certificates are found
    differs: the key-store certificates for this tenant, and an older setup's key file."""

    ME = "aaaaaaaa-0000-0000-0000-000000000001"
    APP_OBJECT = "cccccccc-0000-0000-0000-000000000001"

    def _directory(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch, certificates: list[bytes]
    ) -> None:
        """One app with the Provisioner's name, owned by you, holding these certificates."""
        env.ep.clear_state("PROVISIONER_CLIENT_ID")
        monkeypatch.setattr(
            env.ep, "resolve_graph_permissions", lambda: {"Application.ReadWrite.All": "role"}
        )
        app = {
            "id": self.APP_OBJECT,
            "appId": CLIENT,
            "displayName": env.ep.PROVISIONER_APP_DISPLAY_NAME,
            "createdDateTime": "2026-03-04T05:06:07Z",
            "passwordCredentials": [],
            "requiredResourceAccess": [],
        }
        you = {"@odata.type": "#microsoft.graph.user", "id": self.ME, "userPrincipalName": "a@b.c"}
        keys = [
            {"keyId": f"k{n}", "key": base64.b64encode(der).decode()}
            for n, der in enumerate(certificates)
        ]
        replies = {
            ("ad", "app", "list"): [app],
            ("ad", "signed-in-user", "show"): self.ME,
            ("ad", "app", "owner"): [you],
            ("rest", "--method", "GET"): {"keyCredentials": keys},
            ("ad", "app", "federated-credential"): [],
            ("ad", "sp", "list"): [],  # no service principal yet, so nothing approved either
            # Microsoft Graph's service principal, looked up by its well-known appId.
            ("ad", "sp", "show"): [
                "99999999-0000-0000-0000-000000000003",
                "00000003-0000-0000-c000-000000000000",
            ],
        }

        def fake_run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
            assert args[-2:] == ["-o", "json"], args
            reply = replies.get(tuple(args[:3]), AssertionError(f"unexpected az call: {args}"))
            if isinstance(reply, AssertionError):
                raise reply
            return 0, json.dumps(reply), ""

        monkeypatch.setattr(env.ep, "run_az", fake_run_az)

    def test_an_app_holding_only_this_users_key_store_certificate_is_taken_over(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        # A real certificate: the script reads the thumbprint from the certificate itself,
        # so bytes that are not one cannot be matched to anything in the key store.
        own = x509.load_pem_x509_certificate(
            env.ep._cert_pem_from_bundle(_legacy_pem()[0]).encode()
        )
        der = own.public_bytes(serialization.Encoding.DER)
        sha1 = hashlib.sha1(der).hexdigest().upper()  # noqa: S324
        env.store[sha1] = _store_cert(sha1)
        self._directory(env, monkeypatch, [der])

        client_id, credential, _ = env.ensure()

        assert client_id == CLIENT and env.ep.get_state("PROVISIONER_CLIENT_ID") == CLIENT
        assert credential.sha1_hex == sha1
        assert [upload[0] for upload in env.uploads] == [CLIENT]

    def test_the_certificate_of_an_older_setups_key_file_counts_as_this_machines(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        env.write_legacy_pem()
        legacy = x509.load_pem_x509_certificate(env.ep._cert_pem_from_bundle(LEGACY_PEM).encode())
        self._directory(env, monkeypatch, [legacy.public_bytes(serialization.Encoding.DER)])

        client_id, _, _ = env.ensure()

        assert client_id == CLIENT
        assert env.revokes[0]["sha1_hex"] == LEGACY_SHA1, "then migrated as usual"

    @pytest.mark.parametrize("local", ["none", "other", "no_private_key"])
    def test_a_certificate_whose_key_is_not_in_this_users_store_blocks_the_takeover(
        self,
        env: _Env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        local: str,
    ) -> None:
        der = b"a certificate on the app"
        sha1 = hashlib.sha1(der).hexdigest().upper()  # noqa: S324
        if local == "other":
            env.store[SHA1] = _store_cert(SHA1)
        elif local == "no_private_key":
            # Anyone can put a public certificate in the store; it proves nothing.
            env.store[sha1] = _store_cert(sha1, has_private_key=False)
        self._directory(env, monkeypatch, [der])

        with pytest.raises(env.ep.ProvisionerBootstrapError):
            env.ensure()

        assert env.uploads == [] and env.created == []
        assert env.ep.get_state("PROVISIONER_CLIENT_ID") is None
        out = capsys.readouterr().out
        assert "private key is not on this machine" in out
        assert "ENTRABOT_PROVISIONER_APP_ID_VERIFIED" in out

    def test_a_key_store_that_cannot_be_read_blocks_the_takeover(
        self, env: _Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._directory(env, monkeypatch, [b"a certificate on the app"])

        def unreadable(*, friendly_name: str | None = None) -> list[wincert.StoreCert]:
            raise wincert.WinCertError("Cert:\\CurrentUser\\My could not be opened")

        monkeypatch.setattr(wincert, "list_certs", unreadable)

        with pytest.raises(env.ep.ProvisionerBootstrapError):
            env.ensure()

        assert env.uploads == [] and env.created == []
        assert "could not be opened" in capsys.readouterr().out


class TestRevokeAppCertificate:
    """Removing one certificate from the Provisioner app, through the az session that
    just uploaded the new one, confirmed by reading the app back."""

    OBJECT_ID = "app-object-id"
    KEEP_DER = b"the-certificate-this-machine-uses"

    @staticmethod
    def _sha1(der: bytes) -> str:
        import hashlib

        return hashlib.sha1(der).hexdigest().upper()  # noqa: S324

    def _entry(self, der: bytes, key_id: str, **overrides: Any) -> dict[str, Any]:
        import base64

        entry = {
            "keyId": key_id,
            "type": "AsymmetricX509Cert",
            "usage": "Verify",
            "key": base64.b64encode(der).decode(),
            "customKeyIdentifier": self._sha1(der),
            "endDateTime": "2027-01-01T00:00:00Z",
        }
        entry.update(overrides)
        return entry

    def _az(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]], **opts: Any
    ):
        calls: list[list[str]] = []
        state = {"entries": list(entries), "stale_reads": opts.get("stale_reads", 0)}
        sleeps: list[float] = []
        monkeypatch.setattr(ep.time, "sleep", sleeps.append)

        def fake_run_az(args: list[str], capture: bool = True) -> tuple[int, str, str]:
            calls.append(args)
            if args[:3] == ["ad", "app", "show"]:
                return 0, json.dumps(self.OBJECT_ID), ""
            if args[0] == "rest":
                if opts.get("read_fails"):
                    return 1, "", "ERROR: Forbidden"
                shown = state["entries"]
                if state.get("deleted") and state["stale_reads"] > 0:
                    state["stale_reads"] -= 1
                    shown = state["before_delete"]
                return 0, json.dumps({"keyCredentials": shown}), ""
            if args[:4] == ["ad", "app", "credential", "delete"]:
                if opts.get("delete_fails"):
                    return 1, "", "ERROR: Insufficient privileges to complete the operation."
                key_id = args[args.index("--key-id") + 1]
                state["before_delete"] = list(state["entries"])
                state["deleted"] = True
                if not opts.get("delete_is_ignored"):
                    doomed = {key_id} | set(opts.get("also_lost", ()))
                    state["entries"] = [e for e in state["entries"] if e["keyId"] not in doomed]
                return 0, "", ""
            raise AssertionError(f"unexpected az call: {args}")

        monkeypatch.setattr(ep, "run_az", fake_run_az)
        return calls, sleeps

    def _deletes(self, calls: list[list[str]]) -> list[list[str]]:
        return [c for c in calls if c[:4] == ["ad", "app", "credential", "delete"]]

    def test_removes_exactly_that_certificate_and_confirms_it(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = b"the-old-plaintext-key-certificate"
        calls, sleeps = self._az(
            ep, monkeypatch, [self._entry(self.KEEP_DER, "k-keep"), self._entry(old, "k-old")]
        )

        outcome = ep._revoke_app_certificate(
            CLIENT, self._sha1(old).lower(), keep_sha1=self._sha1(self.KEEP_DER)
        )

        assert outcome == "removed"
        assert self._deletes(calls) == [
            ["ad", "app", "credential", "delete", "--id", CLIENT, "--key-id", "k-old", "--cert"]
        ]
        reads = [c for c in calls if c[0] == "rest"]
        assert reads[0][reads[0].index("--url") + 1] == (
            f"https://graph.microsoft.com/v1.0/applications/{self.OBJECT_ID}?$select=keyCredentials"
        )
        assert len(reads) == 2 and sleeps == []

    def test_not_on_the_app_is_not_registered_and_nothing_is_deleted(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls, _ = self._az(ep, monkeypatch, [self._entry(self.KEEP_DER, "k-keep")])
        assert ep._revoke_app_certificate(CLIENT, "A" * 40) == "not_registered"
        assert self._deletes(calls) == []

    @pytest.mark.parametrize("form", ["hex-lower", "base64-raw", "base64-hex"])
    def test_identifies_a_certificate_whose_key_material_is_not_returned(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch, form: str
    ) -> None:
        import base64

        old = b"old-cert"
        sha1 = self._sha1(old)
        identifier = {
            "hex-lower": sha1.lower(),
            "base64-raw": base64.b64encode(bytes.fromhex(sha1)).decode(),
            "base64-hex": base64.b64encode(sha1.encode()).decode(),
        }[form]
        calls, _ = self._az(
            ep, monkeypatch, [self._entry(old, "k-old", key=None, customKeyIdentifier=identifier)]
        )
        assert ep._revoke_app_certificate(CLIENT, sha1) == "removed"
        assert len(self._deletes(calls)) == 1

    def test_an_unidentifiable_certificate_means_could_not_check(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls, _ = self._az(
            ep,
            monkeypatch,
            [self._entry(b"x", "k-x", key=None, customKeyIdentifier=None)],
        )
        with pytest.raises(ep.ProvisionerBootstrapError, match="could not be identified"):
            ep._revoke_app_certificate(CLIENT, "A" * 40)
        assert self._deletes(calls) == []

    def test_refuses_to_remove_the_certificate_this_machine_uses(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls, _ = self._az(ep, monkeypatch, [self._entry(self.KEEP_DER, "k-keep")])
        keep = self._sha1(self.KEEP_DER)
        with pytest.raises(ep.ProvisionerBootstrapError, match="in use"):
            ep._revoke_app_certificate(CLIENT, keep.lower(), keep_sha1=keep)
        assert calls == []

    def test_read_failure_changes_nothing(self, ep: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls, _ = self._az(ep, monkeypatch, [], read_fails=True)
        with pytest.raises(ep.ProvisionerBootstrapError, match="Forbidden"):
            ep._revoke_app_certificate(CLIENT, "A" * 40)
        assert self._deletes(calls) == []

    def test_delete_refused_is_an_error(self, ep: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        old = b"old-cert"
        self._az(ep, monkeypatch, [self._entry(old, "k-old")], delete_fails=True)
        with pytest.raises(ep.ProvisionerBootstrapError, match="Insufficient privileges"):
            ep._revoke_app_certificate(CLIENT, self._sha1(old))

    def test_waits_for_the_removal_to_show_up(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = b"old-cert"
        _, sleeps = self._az(ep, monkeypatch, [self._entry(old, "k-old")], stale_reads=2)
        assert ep._revoke_app_certificate(CLIENT, self._sha1(old)) == "removed"
        assert len(sleeps) == 2

    def test_still_listed_after_every_read_is_an_error(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = b"old-cert"
        self._az(ep, monkeypatch, [self._entry(old, "k-old")], delete_is_ignored=True)
        with pytest.raises(ep.ProvisionerBootstrapError, match="still listed"):
            ep._revoke_app_certificate(CLIENT, self._sha1(old))

    def test_nothing_is_removed_while_this_machines_certificate_is_not_listed(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = b"old-cert"
        calls, _ = self._az(ep, monkeypatch, [self._entry(old, "k-old")])
        with pytest.raises(ep.ProvisionerBootstrapError, match="not listed"):
            ep._revoke_app_certificate(CLIENT, self._sha1(old), keep_sha1=self._sha1(self.KEEP_DER))
        assert self._deletes(calls) == []

    def test_losing_this_machines_certificate_is_reported_loudly(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = b"old-cert"
        self._az(
            ep,
            monkeypatch,
            [self._entry(self.KEEP_DER, "k-keep"), self._entry(old, "k-old")],
            also_lost=["k-keep"],
        )
        with pytest.raises(ep.ProvisionerBootstrapError, match="no longer listed"):
            ep._revoke_app_certificate(CLIENT, self._sha1(old), keep_sha1=self._sha1(self.KEEP_DER))


class TestLoadExistingOnWindows:
    def test_prefers_the_recorded_cng_certificate(self, env: _Env) -> None:
        env.store[SHA1] = _store_cert(SHA1)
        env.store[OTHER_SHA1] = _store_cert(OTHER_SHA1, days=360)
        env.ep.set_state("PROVISIONER_CERT_THUMBPRINT", SHA1.lower())

        _, credential, _ = env.ep.load_existing_app_registration()

        assert credential.sha1_hex == SHA1
        assert env.uploads == [] and env.created == []

    def test_an_unmigrated_install_keeps_working_from_its_pem(self, env: _Env) -> None:
        env.write_legacy_pem()

        _, credential, _ = env.ep.load_existing_app_registration()

        assert credential == LEGACY_PEM
        assert env.legacy_pem.exists(), "read paths never migrate or delete"

    def test_nothing_local_is_an_actionable_error(self, env: _Env) -> None:
        with pytest.raises(env.ep.ProvisionerBootstrapError, match="setup-windows.cmd"):
            env.ep.load_existing_app_registration()


class TestTokenWithCngKey:
    @pytest.fixture
    def cred(self, ep: Any) -> Any:
        return ep.CngProvisionerCert(sha1_hex=SHA1, x5t_s256=X5T)

    def _wire(
        self, ep: Any, monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]
    ) -> tuple[list[httpx.Request], list[dict[str, Any]], list[float]]:
        requests: list[httpx.Request] = []
        assertions: list[dict[str, Any]] = []
        sleeps: list[float] = []
        queue = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return queue.pop(0)

        def fake_assertion(**kwargs: Any) -> str:
            assertions.append(kwargs)
            return "signed.assertion.jwt"

        monkeypatch.setattr(
            ep, "_new_token_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
        )
        monkeypatch.setattr(ep, "_build_cng_assertion", fake_assertion)
        monkeypatch.setattr(ep.time, "sleep", sleeps.append)
        return requests, assertions, sleeps

    def test_posts_a_client_assertion_and_returns_the_token(
        self, ep: Any, cred: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests, assertions, _ = self._wire(
            ep, monkeypatch, [httpx.Response(200, json={"access_token": "graph-token"})]
        )

        assert ep._acquire_graph_token_cng(TENANT, CLIENT, cred) == "graph-token"

        endpoint = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
        assert str(requests[0].url) == endpoint
        form = dict(httpx.QueryParams(requests[0].content.decode()))
        assert form == {
            "client_id": CLIENT,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": "signed.assertion.jwt",
        }
        assert assertions == [
            {
                "cert_thumbprint": X5T,
                "cert_sha1": SHA1,
                "client_id": CLIENT,
                "token_endpoint": endpoint,
            }
        ]

    def test_error_response_is_checked_before_the_token(
        self, ep: Any, cred: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._wire(
            ep,
            monkeypatch,
            [
                httpx.Response(
                    401,
                    json={
                        "error": "unauthorized_client",
                        "error_description": "AADSTS700016: application not found",
                        "access_token": "must-not-be-used",
                    },
                )
            ],
        )
        with pytest.raises(ep.ProvisionerBootstrapError, match="AADSTS700016"):
            ep._acquire_graph_token_cng(TENANT, CLIENT, cred)

    def test_waits_for_a_just_uploaded_certificate_to_propagate(
        self, ep: Any, cred: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        not_yet = httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "AADSTS700027: The certificate with identifier used to "
                "sign the client assertion is not registered on application.",
            },
        )
        requests, assertions, sleeps = self._wire(
            ep, monkeypatch, [not_yet, not_yet, httpx.Response(200, json={"access_token": "t"})]
        )

        assert ep._acquire_graph_token_cng(TENANT, CLIENT, cred) == "t"
        assert len(requests) == 3 and len(sleeps) == 2
        assert len(assertions) == 3, "a fresh assertion (new jti) for every attempt"

    def test_non_json_and_missing_token_are_errors(
        self, ep: Any, cred: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._wire(ep, monkeypatch, [httpx.Response(502, text="<html>bad gateway</html>")])
        with pytest.raises(ep.ProvisionerBootstrapError, match="502"):
            ep._acquire_graph_token_cng(TENANT, CLIENT, cred)

        self._wire(ep, monkeypatch, [httpx.Response(200, json={"token_type": "Bearer"})])
        with pytest.raises(ep.ProvisionerBootstrapError, match="access_token"):
            ep._acquire_graph_token_cng(TENANT, CLIENT, cred)

    def test_signing_failure_is_a_bootstrap_error(
        self, ep: Any, cred: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from entrabot.auth.cncrypt_signer import SigningError

        self._wire(ep, monkeypatch, [])

        def fail(**_kwargs: Any) -> str:
            raise SigningError("NCryptSignHash failed (0x80090016): key is gone")

        monkeypatch.setattr(ep, "_build_cng_assertion", fail)
        with pytest.raises(ep.ProvisionerBootstrapError, match="0x80090016"):
            ep._acquire_graph_token_cng(TENANT, CLIENT, cred)

    def test_get_graph_token_uses_the_cng_path_for_a_cng_credential(
        self, ep: Any, cred: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ep, "load_existing_app_registration", lambda: (CLIENT, cred, TENANT))
        seen: list[tuple[str, str, Any]] = []

        def fake_acquire(tenant_id: str, client_id: str, credential: Any) -> str:
            seen.append((tenant_id, client_id, credential))
            return "cng-token"

        monkeypatch.setattr(ep, "_acquire_graph_token_cng", fake_acquire)

        assert ep.get_existing_graph_token() == "cng-token"
        assert seen == [(TENANT, CLIENT, cred)]

    def test_credential_repr_masks_the_thumbprint(self, cred: Any) -> None:
        assert SHA1 not in repr(cred) and X5T not in repr(cred)


# ─── every platform ──────────────────────────────────────────────────────────


def test_provisioner_cert_not_before_is_backdated_for_clock_skew(ep: Any) -> None:
    from cryptography import x509

    cert_pem, _key_pem, _thumb = ep._generate_provisioner_cert()
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    assert cert.not_valid_before_utc <= datetime.now(UTC) - timedelta(minutes=5)


def test_keychain_flow_prints_no_thumbprint_and_names_the_right_store(
    ep: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stored: dict[str, str] = {}
    monkeypatch.setattr(ep, "_uses_cng", lambda: False)
    monkeypatch.setattr(ep.sys, "platform", "linux")
    monkeypatch.setattr(ep, "_application_status", lambda client_id: "exists")
    monkeypatch.setattr(ep, "_ensure_permissions_and_consent", lambda *a: False)
    monkeypatch.setattr(ep, "_remove_legacy_password_credentials", lambda *a: 0)
    monkeypatch.setattr(ep, "_keychain_get_cert", lambda tenant: None)
    monkeypatch.setattr(ep, "_keychain_store_cert", stored.__setitem__)
    monkeypatch.setattr(ep, "_upload_cert_to_app", lambda client_id, cert_pem: None)
    ep.set_state("TENANT_ID", TENANT)
    ep.set_state("PROVISIONER_CLIENT_ID", CLIENT)

    ep.ensure_app_registration(["Application.ReadWrite.All"])

    thumbprint = ep.get_state("PROVISIONER_CERT_THUMBPRINT")
    out = capsys.readouterr().out
    assert thumbprint and thumbprint not in out and thumbprint.upper() not in out
    assert "macOS Keychain" not in out, "this is Linux"
    assert TENANT in stored
