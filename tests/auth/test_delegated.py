"""Tests for MSAL delegated authentication.

All MSAL interactions are mocked — no real auth endpoints are called.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import msal
import pytest

from entrabot.errors import AuthCancelledError, MsalAuthError


def _token_result(
    access_token: str = "test-access-token",
    **overrides: object,
) -> dict:
    """Build a fake MSAL token result dict."""
    base = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "Chat.ReadWrite User.Read",
        "id_token_claims": {"preferred_username": "user@contoso.com"},
    }
    base.update(overrides)
    return base


def _make_account(username: str = "user@contoso.com") -> dict:
    return {"username": username, "home_account_id": "uid.utid"}


# ---------------------------------------------------------------------------
# TestMsalDelegatedAuth
# ---------------------------------------------------------------------------
class TestMsalDelegatedAuth:
    """Tests for the MsalDelegatedAuth class."""

    @patch("entrabot.auth.delegated._build_token_cache")
    @patch("entrabot.auth.delegated._build_app")
    def _make_auth(
        self,
        mock_build_app: MagicMock,
        mock_build_cache: MagicMock,
        *,
        app: MagicMock | None = None,
    ):
        """Helper: create an MsalDelegatedAuth with mocked app."""
        from entrabot.auth.delegated import MsalDelegatedAuth

        mock_build_cache.return_value = msal.SerializableTokenCache()
        mock_app = app or MagicMock(spec=msal.PublicClientApplication)
        mock_build_app.return_value = mock_app
        auth = MsalDelegatedAuth(client_id="test-client-id", tenant_id="test-tenant")
        return auth, mock_app

    def test_silent_success(self) -> None:
        """Silent acquisition succeeds when cache has a valid token."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = [_make_account()]
        mock_app.acquire_token_silent.return_value = _token_result()

        result = auth.try_silent()

        assert result is not None
        assert result["access_token"] == "test-access-token"
        mock_app.acquire_token_silent.assert_called_once()

    def test_silent_no_accounts(self) -> None:
        """Silent returns None when no cached accounts exist."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = []

        result = auth.try_silent()

        assert result is None
        mock_app.acquire_token_silent.assert_not_called()

    def test_silent_expired(self) -> None:
        """Silent returns None when cached token is expired/unusable."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = [_make_account()]
        mock_app.acquire_token_silent.return_value = None

        result = auth.try_silent()

        assert result is None

    def test_localhost_success(self) -> None:
        """Localhost interactive auth succeeds."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = []
        mock_app.acquire_token_interactive.return_value = _token_result()

        result = auth.authenticate()

        assert result["access_token"] == "test-access-token"
        mock_app.acquire_token_interactive.assert_called_once()

    def test_localhost_falls_back_to_device_code(self) -> None:
        """When localhost raises OSError, falls back to device code."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = []
        mock_app.acquire_token_interactive.side_effect = OSError("Port in use")
        mock_app.initiate_device_flow.return_value = {
            "message": "Go to https://...",
            "user_code": "ABCD",
        }
        mock_app.acquire_token_by_device_flow.return_value = _token_result()

        result = auth.authenticate()

        assert result["access_token"] == "test-access-token"
        mock_app.initiate_device_flow.assert_called_once()
        mock_app.acquire_token_by_device_flow.assert_called_once()

    def test_localhost_timeout_falls_back(self) -> None:
        """When localhost returns a timeout error, falls back to device code."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = []
        mock_app.acquire_token_interactive.return_value = {
            "error": "timeout",
            "error_description": "User did not complete auth in time",
        }
        mock_app.initiate_device_flow.return_value = {
            "message": "Go to https://...",
            "user_code": "ABCD",
        }
        mock_app.acquire_token_by_device_flow.return_value = _token_result()

        result = auth.authenticate()

        assert result["access_token"] == "test-access-token"
        mock_app.acquire_token_by_device_flow.assert_called_once()

    def test_device_code_success(self) -> None:
        """Device code flow succeeds directly."""
        auth, mock_app = self._make_auth()
        flow = {"message": "Go to https://...", "user_code": "ABCD"}
        mock_app.initiate_device_flow.return_value = flow
        mock_app.acquire_token_by_device_flow.return_value = _token_result()

        result = auth._try_device_code()

        assert result["access_token"] == "test-access-token"
        mock_app.initiate_device_flow.assert_called_once_with(auth.scopes)
        mock_app.acquire_token_by_device_flow.assert_called_once_with(flow)

    def test_device_code_initiate_error(self) -> None:
        """initiate_device_flow returns error dict → MsalAuthError."""
        auth, mock_app = self._make_auth()
        mock_app.initiate_device_flow.return_value = {
            "error": "invalid_scope",
            "error_description": "Scopes not supported",
        }

        with pytest.raises(MsalAuthError) as exc_info:
            auth._try_device_code()

        assert exc_info.value.error == "invalid_scope"
        assert "Scopes not supported" in exc_info.value.error_description

    def test_user_cancels_raises_cancelled(self) -> None:
        """Result with error=authentication_cancelled raises AuthCancelledError."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = []
        mock_app.acquire_token_interactive.return_value = {
            "error": "authentication_cancelled",
            "error_description": "User pressed cancel",
        }

        with pytest.raises(AuthCancelledError):
            auth.authenticate()

    def test_msal_error_raises_msal_auth_error(self) -> None:
        """Result with an error key raises MsalAuthError."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = []
        mock_app.acquire_token_interactive.return_value = {
            "error": "interaction_required",
            "error_description": "Consent required",
        }

        with pytest.raises(MsalAuthError) as exc_info:
            auth._try_localhost()

        assert exc_info.value.error == "interaction_required"

    def test_no_access_token_raises(self) -> None:
        """Result without access_token raises MsalAuthError."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = []
        mock_app.acquire_token_interactive.return_value = {"id_token": "xyz"}

        with pytest.raises(MsalAuthError) as exc_info:
            auth._try_localhost()

        assert exc_info.value.error == "no_access_token"

    def test_authenticate_tries_silent_first(self) -> None:
        """authenticate() returns cached token without interactive flow."""
        auth, mock_app = self._make_auth()
        mock_app.get_accounts.return_value = [_make_account()]
        mock_app.acquire_token_silent.return_value = _token_result()

        result = auth.authenticate()

        assert result["access_token"] == "test-access-token"
        mock_app.acquire_token_interactive.assert_not_called()

    def test_accounts_property(self) -> None:
        """accounts property delegates to get_accounts()."""
        auth, mock_app = self._make_auth()
        accts = [_make_account(), _make_account("admin@contoso.com")]
        mock_app.get_accounts.return_value = accts

        assert auth.accounts == accts


# ---------------------------------------------------------------------------
# TestTokenCache
# ---------------------------------------------------------------------------
class TestTokenCache:
    """Tests for the _build_token_cache helper."""

    def test_cache_location_uses_stable_user_cache_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache path comes from platformdirs and does not depend on cwd."""
        from entrabot.auth import delegated

        scratch = Path.cwd() / ".pytest-scratch" / "delegated-cache-stable"
        cwd_one = scratch / "cwd-one"
        cwd_two = scratch / "cwd-two"
        cwd_one.mkdir(parents=True)
        cwd_two.mkdir(parents=True)

        try:
            monkeypatch.chdir(cwd_one)
            first = delegated.CACHE_LOCATION
            monkeypatch.chdir(cwd_two)
            second = delegated.CACHE_LOCATION

            assert first == second
            assert first.parent == Path(delegated.platformdirs.user_cache_dir("entrabot"))
            assert first.name == "entrabot_msal_cache"
            assert not os.fspath(first).startswith("entrabot_msal_cache")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_cache_location_parent_created_when_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Resolving the cache path creates the per-user cache parent."""
        from entrabot.auth import delegated

        scratch = Path.cwd() / ".pytest-scratch" / "delegated-cache-parent"
        cache_location = scratch / "missing-cache-root" / "entrabot" / "entrabot_msal_cache"
        monkeypatch.setattr(delegated, "CACHE_LOCATION", cache_location)

        try:
            resolved = delegated._resolve_cache_location()

            assert resolved == cache_location
            assert resolved.parent.is_dir()
            if os.name != "nt":
                assert stat.S_IMODE(resolved.parent.stat().st_mode) == 0o700
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @patch("entrabot.auth.delegated.build_encrypted_persistence")
    @patch("entrabot.auth.delegated.PersistedTokenCache")
    def test_build_token_cache_success(
        self,
        mock_persisted: MagicMock,
        mock_build_persistence: MagicMock,
    ) -> None:
        """Encrypted persistence + PersistedTokenCache are wired correctly."""
        from entrabot.auth.delegated import _build_token_cache

        mock_persistence = MagicMock()
        mock_build_persistence.return_value = mock_persistence
        mock_cache = MagicMock(spec=msal.SerializableTokenCache)
        mock_persisted.return_value = mock_cache
        resolved_path = Path("/stable/user/cache/entrabot/entrabot_msal_cache")

        with patch("entrabot.auth.delegated._resolve_cache_location", return_value=resolved_path):
            result = _build_token_cache()

        mock_build_persistence.assert_called_once_with(str(resolved_path))
        mock_persisted.assert_called_once_with(mock_persistence)
        assert result is mock_cache

    @patch("entrabot.auth.delegated.build_encrypted_persistence")
    def test_build_token_cache_corruption_fallback(
        self,
        mock_build_persistence: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Corrupted cache falls back to an in-memory SerializableTokenCache."""
        from entrabot.auth.delegated import _build_token_cache

        mock_build_persistence.side_effect = Exception("Corrupt cache file")
        resolved_path = Path("/stable/user/cache/entrabot/entrabot_msal_cache")

        with (
            patch("entrabot.auth.delegated._resolve_cache_location", return_value=resolved_path),
            caplog.at_level(logging.WARNING, logger="entrabot.auth.delegated"),
        ):
            result = _build_token_cache()

        assert isinstance(result, msal.SerializableTokenCache)
        record = next(
            record
            for record in caplog.records
            if "Failed to build persistent MSAL token cache" in record.message
        )
        assert record.levelno == logging.WARNING
        assert record.exc_info is not None

    def test_cache_location_constant_uses_resolved_path(self) -> None:
        """CACHE_LOCATION is an absolute, per-user cache path."""
        from entrabot.auth.delegated import CACHE_LOCATION

        assert CACHE_LOCATION.is_absolute()
        assert CACHE_LOCATION.name == "entrabot_msal_cache"
