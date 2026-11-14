"""Hop 1 credential selection in ``tools.teams._build_blueprint_assertion``.

On Windows the Blueprint key lives in CNG and is found by the SHA-1 in ``.env``;
Credential Manager is not involved, and is not even reachable from an SSH or
scheduled-task logon session. On macOS and Linux the SHA-1 names the keystore item
that holds this agent's certificate and key (``entrabot.platform.keycert``); a setup from
before that, with no SHA-1 in ``.env``, still signs with the account's single key.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from entrabot.auth import cncrypt_signer
from entrabot.config import EntraBotConfig
from entrabot.errors import AgentIDNotAvailable
from entrabot.platform import wincert
from entrabot.tools import teams

SHA1 = "0123456789ABCDEF0123456789ABCDEF01234567"
ENDPOINT = "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"


def _config(**overrides: object) -> EntraBotConfig:
    values: dict[str, object] = {
        "tenant_id": "tenant",
        "blueprint_app_id": "blueprint-app",
        "blueprint_cert_thumbprint": "x5t-s256-value",
        "blueprint_cert_sha1": SHA1,
    }
    values.update(overrides)
    return EntraBotConfig(**values)  # type: ignore[arg-type]


def _health(status: str, *, days_left: int | None = 200, message: str = "") -> wincert.CertHealth:
    return wincert.CertHealth(
        status=status,
        days_left=days_left,
        not_after=datetime.now(UTC) + timedelta(days=days_left or 0),
        message=message or f"certificate is {status}",
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "signed.jwt.value"


@pytest.fixture
def assertion(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(teams, "build_client_assertion", recorder)
    return recorder


@pytest.fixture
def no_keyring(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail the test if the credential store is touched."""
    touched: list[str] = []

    def explode() -> object:
        touched.append("get_credential_store")
        raise AssertionError("the credential store must not be used on this path")

    monkeypatch.setattr(teams, "get_credential_store", explode)
    return touched


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teams.sys, "platform", "win32")
    monkeypatch.setattr(teams, "_expiry_warned", set(), raising=False)


class TestWindows:
    def test_goes_straight_to_cng_without_touching_credential_manager(
        self,
        windows: None,
        assertion: _Recorder,
        no_keyring: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(wincert, "cert_health", lambda sha1: _health("ok"))

        assert teams._build_blueprint_assertion(_config(), ENDPOINT) == "signed.jwt.value"

        assert no_keyring == []
        assert assertion.calls == [
            {
                "cert_thumbprint": "x5t-s256-value",
                "cert_sha1": SHA1,
                "client_id": "blueprint-app",
                "token_endpoint": ENDPOINT,
            }
        ]

    @pytest.mark.parametrize("status", ["missing", "expired", "no_private_key", "not_yet_valid"])
    def test_unusable_certificate_is_an_actionable_error_before_signing(
        self,
        windows: None,
        assertion: _Recorder,
        no_keyring: list[str],
        monkeypatch: pytest.MonkeyPatch,
        status: str,
    ) -> None:
        message = f"The Blueprint certificate is {status}. Run scripts\\setup-windows.cmd."
        monkeypatch.setattr(
            wincert, "cert_health", lambda sha1: _health(status, days_left=-3, message=message)
        )

        with pytest.raises(AgentIDNotAvailable) as excinfo:
            teams._build_blueprint_assertion(_config(), ENDPOINT)

        assert "setup-windows.cmd" in str(excinfo.value)
        assert SHA1 not in str(excinfo.value)
        assert assertion.calls == []

    def test_expiring_certificate_warns_once_and_still_signs(
        self,
        windows: None,
        assertion: _Recorder,
        no_keyring: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            wincert,
            "cert_health",
            lambda sha1: _health("expiring", days_left=9, message="expires in 9 day(s)"),
        )
        warned: list[str] = []
        monkeypatch.setattr(
            teams.logger, "warning", lambda fmt, *args: warned.append(fmt % args if args else fmt)
        )

        teams._build_blueprint_assertion(_config(), ENDPOINT)
        teams._build_blueprint_assertion(_config(), ENDPOINT)

        assert warned == ["expires in 9 day(s)"]
        assert len(assertion.calls) == 2

    def test_a_failed_health_read_does_not_block_signing(
        self,
        windows: None,
        assertion: _Recorder,
        no_keyring: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken(sha1: str) -> wincert.CertHealth:
            raise wincert.WinCertError("could not open the store")

        monkeypatch.setattr(wincert, "cert_health", broken)
        assert teams._build_blueprint_assertion(_config(), ENDPOINT) == "signed.jwt.value"

    @pytest.mark.parametrize(
        "error",
        [
            cncrypt_signer.SigningError("NCryptSignHash failed (0x80090016): key is gone"),
            cncrypt_signer.CertNotFoundError("certificate is not in the store"),
        ],
    )
    def test_signer_failure_becomes_agent_id_not_available(
        self,
        windows: None,
        no_keyring: list[str],
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        monkeypatch.setattr(wincert, "cert_health", lambda sha1: _health("ok"))

        def fail(**_kwargs: object) -> str:
            raise error

        monkeypatch.setattr(teams, "build_client_assertion", fail)

        with pytest.raises(AgentIDNotAvailable) as excinfo:
            teams._build_blueprint_assertion(_config(), ENDPOINT)
        assert str(error) in str(excinfo.value)
        assert excinfo.value.__cause__ is error

    def test_no_sha1_and_an_unreachable_credential_manager(
        self, windows: None, assertion: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What an SSH or scheduled-task logon session looks like.
        def unreachable() -> object:
            raise OSError(1312, "A specified logon session does not exist")

        monkeypatch.setattr(teams, "get_credential_store", unreachable)

        with pytest.raises(AgentIDNotAvailable, match="ENTRABOT_BLUEPRINT_CERT_SHA1"):
            teams._build_blueprint_assertion(_config(blueprint_cert_sha1=None), ENDPOINT)
        assert assertion.calls == []


class _Store:
    def __init__(self, pem: str | None) -> None:
        self.pem = pem
        self.asked: list[tuple[str, str]] = []

    def retrieve(self, service: str, key: str) -> str | None:
        self.asked.append((service, key))
        return self.pem


@pytest.mark.parametrize("platform", ["darwin", "linux"])
class TestMacAndLinuxOlderSetup:
    """No SHA-1 in ``.env``: the account's single key, as before."""

    def test_pem_from_the_keystore(
        self, platform: str, assertion: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _Store("PEM")
        monkeypatch.setattr(teams.sys, "platform", platform)
        monkeypatch.setattr(teams, "get_credential_store", lambda: store)

        teams._build_blueprint_assertion(_config(blueprint_cert_sha1=None), ENDPOINT)

        assert store.asked == [("entrabot", "blueprint-private-key")]
        assert assertion.calls == [
            {
                "private_key_pem": "PEM",
                "cert_thumbprint": "x5t-s256-value",
                "client_id": "blueprint-app",
                "token_endpoint": ENDPOINT,
            }
        ]

    def test_no_pem_is_the_setup_sh_error(
        self, platform: str, assertion: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(teams.sys, "platform", platform)
        monkeypatch.setattr(teams, "get_credential_store", lambda: _Store(None))
        with pytest.raises(AgentIDNotAvailable, match=r"\./scripts/setup\.sh"):
            teams._build_blueprint_assertion(_config(blueprint_cert_sha1=None), ENDPOINT)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
class TestMacAndLinuxCertificateStore:
    """A SHA-1 in ``.env``: this agent's own keystore item, never the account's single key."""

    @pytest.fixture(autouse=True)
    def _fresh(self, platform: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(teams.sys, "platform", platform)
        monkeypatch.setattr(teams, "_expiry_warned", set(), raising=False)

    def test_signs_with_the_key_stored_with_the_certificate(
        self,
        platform: str,
        assertion: _Recorder,
        no_keyring: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from entrabot.platform import keycert

        asked: list[str] = []
        monkeypatch.setattr(keycert, "cert_health", lambda sha1: _health("ok"))
        monkeypatch.setattr(
            keycert, "load_private_key_pem", lambda sha1: asked.append(sha1) or "AGENT PEM"
        )

        assert teams._build_blueprint_assertion(_config(), ENDPOINT) == "signed.jwt.value"

        assert asked == [SHA1]
        assert no_keyring == []  # the legacy single slot is not read
        assert assertion.calls == [
            {
                "private_key_pem": "AGENT PEM",
                "cert_thumbprint": "x5t-s256-value",
                "client_id": "blueprint-app",
                "token_endpoint": ENDPOINT,
            }
        ]

    @pytest.mark.parametrize("status", ["missing", "expired", "no_private_key", "not_yet_valid"])
    def test_unusable_certificate_is_an_actionable_error_before_signing(
        self,
        platform: str,
        assertion: _Recorder,
        no_keyring: list[str],
        monkeypatch: pytest.MonkeyPatch,
        status: str,
    ) -> None:
        from entrabot.platform import keycert

        message = f"The Blueprint certificate is {status}. Run ./scripts/setup.sh."
        monkeypatch.setattr(
            keycert, "cert_health", lambda sha1: _health(status, days_left=-3, message=message)
        )

        with pytest.raises(AgentIDNotAvailable) as excinfo:
            teams._build_blueprint_assertion(_config(), ENDPOINT)

        assert "./scripts/setup.sh" in str(excinfo.value)
        assert SHA1 not in str(excinfo.value)
        assert assertion.calls == [] and no_keyring == []

    def test_a_keystore_that_cannot_be_read_fails_closed(
        self,
        platform: str,
        assertion: _Recorder,
        no_keyring: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from entrabot.platform import keycert

        def broken(sha1: str) -> wincert.CertHealth:
            raise keycert.CertStoreError("Could not read this account's key store")

        monkeypatch.setattr(keycert, "cert_health", broken)

        with pytest.raises(AgentIDNotAvailable, match="key store"):
            teams._build_blueprint_assertion(_config(), ENDPOINT)
        assert assertion.calls == [] and no_keyring == []

    def test_expiring_certificate_warns_once_and_still_signs(
        self,
        platform: str,
        assertion: _Recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from entrabot.platform import keycert

        monkeypatch.setattr(
            keycert,
            "cert_health",
            lambda sha1: _health("expiring", days_left=9, message="expires in 9 day(s)"),
        )
        monkeypatch.setattr(keycert, "load_private_key_pem", lambda sha1: "AGENT PEM")
        warned: list[str] = []
        monkeypatch.setattr(
            teams.logger, "warning", lambda fmt, *args: warned.append(fmt % args if args else fmt)
        )

        teams._build_blueprint_assertion(_config(), ENDPOINT)
        teams._build_blueprint_assertion(_config(), ENDPOINT)

        assert warned == ["expires in 9 day(s)"]
        assert len(assertion.calls) == 2
