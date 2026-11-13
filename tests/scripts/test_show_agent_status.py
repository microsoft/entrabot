"""Tests for scripts/show_agent_status.py."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "show_agent_status.py"
spec = importlib.util.spec_from_file_location("show_agent_status", _SCRIPT)
status_mod = importlib.util.module_from_spec(spec)
sys.modules["show_agent_status"] = status_mod

# Stub entra_provisioning before the module is exec'd
_prov_stub = MagicMock()
_prov_stub.get_existing_graph_token.return_value = "tok-test"
_prov_stub.get_state.return_value = None
_prov_stub.ProvisionerBootstrapError = type("ProvisionerBootstrapError", (Exception,), {})
sys.modules.setdefault("entra_provisioning", _prov_stub)


def _load_module():
    spec.loader.exec_module(status_mod)


@pytest.fixture(autouse=True)
def _local_certificate_is_healthy(monkeypatch):
    """These tests mock a fully healthy agent: state, token and Graph. On Windows the
    status also looks in the real certificate store, so mock that too, or the result
    depends on what the test machine happens to hold. Patched on ``wincert`` itself
    because each test re-executes the status module, which would undo a patch there.
    ``TestWindowsCertificateSources`` covers the check's own outcomes on every platform.
    """
    from entrabot.platform import wincert

    monkeypatch.setattr(
        wincert,
        "cert_health",
        lambda sha1: wincert.CertHealth(
            status="ok", days_left=300, not_after=None, message="certificate is ok"
        ),
    )


def _json_resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "" if body is None else json.dumps(body)
    r.json.return_value = body or {}
    return r


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
STATE = {
    "TENANT_ID": "tenant-111",
    "BLUEPRINT_APP_ID": "bp-app-222",
    "BLUEPRINT_OBJECT_ID": "bp-obj-333",
    "AGENT_ID": "agent-app-444",
    "AGENT_OBJECT_ID": "agent-app-444",
    "AGENT_USER_ID": "user-666",
    "AGENT_USER_UPN": "agent@example.com",
    "PROVISIONER_CLIENT_ID": "prov-777",
    "PROVISIONER_CERT_THUMBPRINT": "AABB",
    "AGENT_USER_WORK_IQ_LICENSE_SKU": "Microsoft_365_Copilot",
}

_FAKE_CERT_DER = b"fake-cert-der-bytes-for-status-test"
_FAKE_CERT_B64 = base64.b64encode(_FAKE_CERT_DER).decode()
_FAKE_CERT_THUMB = (
    base64.urlsafe_b64encode(hashlib.sha256(_FAKE_CERT_DER).digest()).rstrip(b"=").decode()
)
STATE["BLUEPRINT_CERT_THUMBPRINT"] = _FAKE_CERT_THUMB

from entrabot.platform import wincert as _wincert  # noqa: E402

_REAL_CERT_HEALTH = _wincert.cert_health

SPONSORS = [
    {
        "id": "s1",
        "displayName": "Alice",
        "userPrincipalName": "alice@example.com",
        "mail": "alice@example.com",
    },
]

LICENSES = {
    "value": [{"skuId": "sku-1", "skuPartNumber": "ENTERPRISEPACK"}],
}

GRANTS = {
    "value": [
        {
            "id": "g1",
            "resourceId": "res-1",
            "consentType": "Principal",
            "scope": "Chat.ReadWrite Mail.Send",
        },
    ],
}


def _fake_graph(method, path, token, **kw):
    if "/sponsors" in path:
        return _json_resp(200, {"value": SPONSORS})
    if "assignedLicenses" in path:
        return _json_resp(200, {"assignedLicenses": [{"skuId": "sku-1"}]})
    if "subscribedSkus" in path:
        return _json_resp(200, LICENSES)
    if "oauth2PermissionGrants" in path:
        return _json_resp(200, GRANTS)
    if "/applications/" in path and "keyCredentials" in path:
        return _json_resp(
            200,
            {
                "keyCredentials": [
                    {
                        "keyId": "key-1",
                        "displayName": "EntraBot Device Certificate - TestHost",
                        "type": "AsymmetricX509Cert",
                        "usage": "Verify",
                        "key": _FAKE_CERT_B64,
                        "customKeyIdentifier": "sha1-ish",
                        "endDateTime": "2027-01-01T00:00:00Z",
                    }
                ]
            },
        )
    if "/servicePrincipals/res-1" in path:
        return _json_resp(200, {"displayName": "Microsoft Graph"})
    return _json_resp(200, {})


class TestShowStatus:
    """Happy path: show full status."""

    @patch.dict(
        "os.environ",
        {
            "ENTRABOT_BLOB_ENDPOINT": "https://entclawtest.blob.core.windows.net",
            "ENTRABOT_BLOB_CONTAINER": "agent-user-666",
            "ENTRABOT_RESOURCE_GROUP": "rg-test",
        },
        clear=False,
    )
    def test_full_status(self, capsys):
        _load_module()

        def fake_state(key):
            return STATE.get(key)

        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=fake_state),
            patch.object(status_mod, "graph_request", side_effect=_fake_graph),
        ):
            rc = status_mod.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "bp-app-222" in out
        assert "agent-app-444" in out
        assert "agent@example.com" in out
        assert "Alice" in out
        assert "ENTERPRISEPACK" in out
        assert "Health" in out or "pass" in out.lower()
        assert "Local Platform" in out
        assert "Storage" in out
        assert "rg-test" in out
        assert "entclawtest" in out
        assert "Microsoft Graph" in out
        assert "Blueprint Key Credentials" in out
        assert "key-1" in out
        assert "EntraBot Device Certificate - TestHost" in out
        assert "Matches Local Cert:   yes" in out


class TestShowStatusJson:
    """--json produces valid JSON output."""

    @patch.dict("os.environ", {}, clear=False)
    def test_json_output(self, capsys):
        _load_module()

        def fake_state(key):
            return STATE.get(key)

        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=fake_state),
            patch.object(status_mod, "graph_request", side_effect=_fake_graph),
        ):
            rc = status_mod.main(["--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["blueprint"]["app_object_id"] == "bp-app-222"
        assert data["agent_identity"]["app_object_id"] == "agent-app-444"
        assert data["health"]["failed"] == 0
        assert data["licenses"][0]["skuPartNumber"] == "ENTERPRISEPACK"
        assert data["permissions"][0]["resourceName"] == "Microsoft Graph"
        assert data["key_credentials"][0]["displayName"] == "EntraBot Device Certificate - TestHost"
        assert data["key_credentials"][0]["matchesLocalBlueprintCert"] is True


class TestMissingState:
    """Graceful handling when state is incomplete."""

    @patch.dict("os.environ", {}, clear=False)
    def test_partial_state(self, capsys):
        _load_module()

        def fake_state(key):
            # Only tenant is set
            if key == "TENANT_ID":
                return "tenant-111"
            return None

        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=fake_state),
        ):
            rc = status_mod.main([])
        # Should still succeed — shows what's available
        assert rc == 0
        out = capsys.readouterr().out
        assert "tenant-111" in out
        assert "not set" in out.lower() or "N/A" in out


class TestTokenFailure:
    """Error when provisioner token cannot be acquired."""

    @patch.dict("os.environ", {}, clear=False)
    def test_token_fail(self):
        _load_module()
        exc_type = status_mod.ProvisionerBootstrapError
        with patch.object(status_mod, "get_existing_graph_token", side_effect=exc_type("no cert")):
            rc = status_mod.main([])
        assert rc == 1


class TestCLIHelp:
    """CLI --help exits 0."""

    @patch.dict("os.environ", {}, clear=False)
    def test_help(self):
        _load_module()
        with pytest.raises(SystemExit) as exc:
            status_mod.main(["--help"])
        assert exc.value.code == 0


class TestWindowsCertificateSources:
    """Windows setup records the Blueprint certificate in .env, not in the state file,
    and the key lives in the certificate store."""

    def _snapshot(self, monkeypatch, *, state=None, windows=False, health=None):
        _load_module()
        values = dict(STATE if state is None else state)
        monkeypatch.setattr(status_mod, "get_state", lambda key: values.get(key))
        monkeypatch.setattr(status_mod, "graph_request", _fake_graph)
        monkeypatch.setattr(status_mod, "_is_windows", lambda: windows)
        if health is not None:
            from entrabot.platform import wincert

            monkeypatch.setattr(wincert, "cert_health", lambda sha1: health)
        return status_mod._snapshot("tok")

    def _check(self, snapshot, name):
        return next(c for c in snapshot["health"]["checks"] if c["name"] == name)

    def test_thumbprint_from_env_counts_as_the_local_certificate(self, monkeypatch):
        state = {k: v for k, v in STATE.items() if k != "BLUEPRINT_CERT_THUMBPRINT"}
        monkeypatch.setenv("ENTRABOT_BLUEPRINT_CERT_THUMBPRINT", _FAKE_CERT_THUMB)

        snapshot = self._snapshot(monkeypatch, state=state)

        certificate = self._check(snapshot, "Certificate")
        assert certificate["status"] == "pass", certificate
        assert snapshot["key_credentials"][0]["matchesLocalBlueprintCert"] is True

    def test_no_local_key_check_off_windows(self, monkeypatch):
        snapshot = self._snapshot(monkeypatch, windows=False)
        assert "Local certificate" not in {c["name"] for c in snapshot["health"]["checks"]}

    @pytest.mark.parametrize(
        ("status", "expected"),
        [("ok", "pass"), ("expiring", "warn"), ("expired", "fail"), ("missing", "fail")],
    )
    def test_windows_reports_the_local_certificate_and_its_expiry(
        self, monkeypatch, status, expected
    ):
        from entrabot.platform import wincert

        health = wincert.CertHealth(
            status=status, days_left=12, not_after=None, message=f"certificate is {status}"
        )
        snapshot = self._snapshot(monkeypatch, windows=True, health=health)

        local = self._check(snapshot, "Local certificate")
        assert local["status"] == expected
        assert local["detail"] == f"certificate is {status}"

    def test_windows_with_nothing_configured_skips_like_the_other_checks(self, monkeypatch):
        monkeypatch.delenv("ENTRABOT_BLUEPRINT_CERT_SHA1", raising=False)
        snapshot = self._snapshot(monkeypatch, state={"TENANT_ID": "tenant-111"}, windows=True)

        local = self._check(snapshot, "Local certificate")
        assert local["status"] == "skip"
        assert self._check(snapshot, "Certificate")["status"] == "skip"

    def test_windows_with_a_blueprint_but_no_certificate_configured_fails(self, monkeypatch):
        # The agent is provisioned but this machine can't sign for it: a real failure.
        from entrabot.platform import wincert

        monkeypatch.delenv("ENTRABOT_BLUEPRINT_CERT_SHA1", raising=False)
        monkeypatch.setattr(wincert, "cert_health", _REAL_CERT_HEALTH)
        snapshot = self._snapshot(monkeypatch, windows=True)

        local = self._check(snapshot, "Local certificate")
        assert local["status"] == "fail"
        assert "ENTRABOT_BLUEPRINT_CERT_SHA1" in local["detail"]
        assert snapshot["health"]["failed"] >= 1

    def test_mocked_healthy_agent_has_no_failures_on_windows(self, monkeypatch):
        # What test_json_output asserts, made explicit for Windows on every platform.
        snapshot = self._snapshot(monkeypatch, windows=True)
        assert self._check(snapshot, "Local certificate")["status"] == "pass"
        assert snapshot["health"]["failed"] == 0

    def test_windows_store_failure_is_a_warning_not_a_crash(self, monkeypatch):
        from entrabot.platform import wincert

        _load_module()
        monkeypatch.setattr(status_mod, "get_state", lambda key: STATE.get(key))
        monkeypatch.setattr(status_mod, "graph_request", _fake_graph)
        monkeypatch.setattr(status_mod, "_is_windows", lambda: True)

        def broken(sha1):
            raise wincert.WinCertError("could not open the certificate store")

        monkeypatch.setattr(wincert, "cert_health", broken)
        snapshot = status_mod._snapshot("tok")
        local = self._check(snapshot, "Local certificate")
        assert local["status"] == "warn" and "certificate store" in local["detail"]


class TestOlderSetupKeys:
    """An older install kept the Provisioner's private key in a plain file. When setup could
    not take that key's certificate off the Provisioner app, it recorded it in the state file
    (PROVISIONER_CERT_REVOKE_PENDING, with its expiry in PROVISIONER_CERT_REVOKE_PENDING_EXPIRES)
    and retries on every run. Until then the status says so: a warning, not a failed check."""

    OLD_KEY_SHA1 = "c3f1a2b4d5e6f708192a3b4c5d6e7f8091a2b3c4"
    PENDING = {
        "PROVISIONER_CERT_REVOKE_PENDING": OLD_KEY_SHA1,
        "PROVISIONER_CERT_REVOKE_PENDING_EXPIRES": "2027-02-01",
    }
    NAME = "Older setup keys"

    def _snapshot(self, monkeypatch, state, *, windows=False):
        _load_module()
        monkeypatch.setattr(status_mod, "get_state", lambda key: state.get(key))
        monkeypatch.setattr(status_mod, "graph_request", _fake_graph)
        monkeypatch.setattr(status_mod, "_is_windows", lambda: windows)
        return status_mod._snapshot("tok")

    def _check(self, snapshot):
        return next(c for c in snapshot["health"]["checks"] if c["name"] == self.NAME)

    def _main(self, monkeypatch, state, argv):
        _load_module()
        monkeypatch.setattr(status_mod, "get_existing_graph_token", lambda: "tok")
        monkeypatch.setattr(status_mod, "get_state", lambda key: state.get(key))
        monkeypatch.setattr(status_mod, "graph_request", _fake_graph)
        return status_mod.main(argv)

    @pytest.mark.parametrize("windows", [False, True])
    def test_a_pending_key_is_a_warning_that_names_it_and_where_to_remove_it(
        self, monkeypatch, windows
    ):
        # Not tied to the platform: the state file says what it says wherever status runs.
        check = self._check(self._snapshot(monkeypatch, {**STATE, **self.PENDING}, windows=windows))

        assert check["status"] == "warn"
        assert set(check) == {"name", "status", "detail"}  # the shape of every other check
        detail = check["detail"]
        assert "could not be switched off in Microsoft Entra" in detail
        assert "the certificate whose thumbprint ends in B3C4 (expires 2027-02-01)" in detail
        assert (
            "Microsoft Entra admin center (https://entra.microsoft.com): App registrations > "
            "'EntraBot Agent ID Provisioner' (application ID prov-777) > Certificates & secrets "
            "> Certificates." in detail
        )
        assert detail.endswith("Setup will try again the next time it runs.")

    def test_it_is_a_warning_even_for_strict(self, monkeypatch, capsys):
        rc = self._main(monkeypatch, {**STATE, **self.PENDING}, ["--json", "--strict"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["health"]["failed"] == 0 and data["health"]["warnings"] >= 1
        assert self.NAME in [check["name"] for check in data["health"]["checks"]]

    @pytest.mark.parametrize("argv", [["--json"], ["--health-only"], []])
    def test_no_full_thumbprint_of_the_older_key_is_printed(self, monkeypatch, capsys, argv):
        self._main(monkeypatch, {**STATE, **self.PENDING}, argv)

        out = capsys.readouterr().out
        assert "B3C4" in out
        assert self.OLD_KEY_SHA1 not in out.lower() and self.OLD_KEY_SHA1[:36] not in out.lower()

    def test_a_record_that_cannot_be_read_is_a_warning_that_says_so(self, monkeypatch):
        state = {**STATE, "PROVISIONER_CERT_REVOKE_PENDING": "not-a-thumbprint"}

        check = self._check(self._snapshot(monkeypatch, state))

        assert check["status"] == "warn"
        assert check["detail"].startswith(
            "Setup could not read its record of older setup keys for this computer."
        )

    def test_nothing_pending_passes(self, monkeypatch):
        check = self._check(self._snapshot(monkeypatch, STATE))
        assert check == {
            "name": self.NAME,
            "status": "pass",
            "detail": "No older setup key is waiting to be switched off",
        }

    def test_with_no_provisioner_app_recorded_it_is_skipped_like_the_other_checks(
        self, monkeypatch
    ):
        check = self._check(self._snapshot(monkeypatch, {"TENANT_ID": "tenant-111"}))
        assert check["status"] == "skip"
        assert check["detail"] == "No setup (Provisioner) app is recorded on this machine"


class _Stream:
    """A stand-in for stdout that says whether it is a terminal."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestColourOnlyWhereItCanBeDrawn:
    """The PASS/FAIL labels are coloured with ANSI escapes. Written to a pipe, a file, an SSH
    session's captured output or a classic Windows console without VT processing, those
    escapes arrive as literal ``[32mPASS[0m``. Colour is for a terminal that can draw it."""

    def _healthy_run(self, argv: list[str]) -> int:
        _load_module()
        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=lambda key: STATE.get(key)),
            patch.object(status_mod, "graph_request", side_effect=_fake_graph),
        ):
            return status_mod.main(argv)

    def test_output_that_is_not_a_terminal_gets_plain_labels(self, capsys):
        self._healthy_run(["--health-only"])

        out = capsys.readouterr().out
        assert "\033" not in out
        assert "PASS" in out

    def test_the_full_report_is_plain_too(self, capsys):
        self._healthy_run([])

        assert "\033" not in capsys.readouterr().out

    def test_a_terminal_gets_colour(self, capsys, monkeypatch):
        _load_module()
        monkeypatch.setattr(status_mod, "_use_color", lambda stream=None: True)
        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=lambda key: STATE.get(key)),
            patch.object(status_mod, "graph_request", side_effect=_fake_graph),
        ):
            status_mod.main(["--health-only"])

        assert "\033[32mPASS\033[0m" in capsys.readouterr().out

    def test_json_is_never_coloured(self, capsys, monkeypatch):
        _load_module()
        monkeypatch.setattr(status_mod, "_use_color", lambda stream=None: True)
        with (
            patch.object(status_mod, "get_existing_graph_token", return_value="tok"),
            patch.object(status_mod, "get_state", side_effect=lambda key: STATE.get(key)),
            patch.object(status_mod, "graph_request", side_effect=_fake_graph),
        ):
            status_mod.main(["--json"])

        out = capsys.readouterr().out
        assert "\033" not in out
        json.loads(out)

    def test_not_a_terminal_means_no_colour(self, monkeypatch):
        _load_module()
        monkeypatch.delenv("NO_COLOR", raising=False)

        assert status_mod._use_color(_Stream(tty=False)) is False

    def test_no_color_is_honoured_even_on_a_terminal(self, monkeypatch):
        _load_module()
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setattr(status_mod, "_is_windows", lambda: False)

        assert status_mod._use_color(_Stream(tty=True)) is False

    def test_a_posix_terminal_gets_colour(self, monkeypatch):
        _load_module()
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(status_mod, "_is_windows", lambda: False)

        assert status_mod._use_color(_Stream(tty=True)) is True

    @pytest.mark.parametrize(("vt_enabled", "expected"), [(True, True), (False, False)])
    def test_a_classic_windows_console_gets_colour_only_if_vt_can_be_enabled(
        self, monkeypatch, vt_enabled, expected
    ):
        _load_module()
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("WT_SESSION", raising=False)
        monkeypatch.setattr(status_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(status_mod, "_enable_windows_vt", lambda: vt_enabled)

        assert status_mod._use_color(_Stream(tty=True)) is expected

    def test_windows_terminal_gets_colour_without_asking(self, monkeypatch):
        _load_module()
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("WT_SESSION", "abc")
        monkeypatch.setattr(status_mod, "_is_windows", lambda: True)

        def _boom() -> bool:
            raise AssertionError("Windows Terminal always draws colour; no need to ask")

        monkeypatch.setattr(status_mod, "_enable_windows_vt", _boom)

        assert status_mod._use_color(_Stream(tty=True)) is True

    def test_a_stream_that_cannot_say_is_treated_as_not_a_terminal(self, monkeypatch):
        _load_module()
        monkeypatch.delenv("NO_COLOR", raising=False)

        assert status_mod._use_color(object()) is False
