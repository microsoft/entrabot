"""MSAL-based delegated authentication for multi-tenant access.

Provides interactive auth (localhost redirect + device code fallback)
for users from any Entra ID tenant. Token cache backed by OS keystore
via msal-extensions.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import msal
import platformdirs
from msal_extensions import PersistedTokenCache, build_encrypted_persistence

from entrabot.errors import AuthCancelledError, AuthTimeoutError, MsalAuthError

logger = logging.getLogger("entrabot.auth.delegated")

DEFAULT_SCOPES = ["Chat.ReadWrite", "User.Read"]
LOCALHOST_PORT = 8400
LOCALHOST_TIMEOUT = 120  # seconds — browser sign-in needs time
CACHE_FILENAME = "entrabot_msal_cache"
# Path only — use _resolve_cache_location() for production access so the
# parent directory is created and permission-hardened before use.
CACHE_LOCATION = Path(platformdirs.user_cache_dir("entrabot")) / CACHE_FILENAME


def _resolve_cache_location() -> Path:
    """Resolve the persistent MSAL token cache path under a stable per-user directory.

    Creates the parent directory with 0o700 permissions on Unix.
    """
    cache_location = CACHE_LOCATION
    cache_location.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(cache_location.parent, 0o700)
    return cache_location


def _build_token_cache() -> msal.SerializableTokenCache:
    """Build an MSAL token cache backed by OS-encrypted persistence.

    Uses msal-extensions PersistedTokenCache with build_encrypted_persistence()
    for cross-platform OS keystore encryption (Keychain/DPAPI/Secret Service).

    If persistent cache setup fails, logs the failure and returns an in-memory cache.
    """
    try:
        persistence = build_encrypted_persistence(str(_resolve_cache_location()))
        cache = PersistedTokenCache(persistence)
        return cache
    except Exception:
        logger.warning(
            "Failed to build persistent MSAL token cache; falling back to in-memory cache",
            exc_info=True,
        )
        return msal.SerializableTokenCache()


def _build_app(
    client_id: str,
    authority: str,
    cache: msal.SerializableTokenCache | None = None,
) -> msal.PublicClientApplication:
    """Build an MSAL PublicClientApplication."""
    return msal.PublicClientApplication(
        client_id,
        authority=authority,
        token_cache=cache,
    )


class MsalDelegatedAuth:
    """MSAL interactive authentication with localhost redirect + device code fallback.

    Usage::

        auth = MsalDelegatedAuth(client_id="...", tenant_id="common")
        result = auth.authenticate()
        token = result["access_token"]
    """

    def __init__(
        self,
        client_id: str,
        tenant_id: str = "common",
        scopes: list[str] | None = None,
        port: int = LOCALHOST_PORT,
    ) -> None:
        self.client_id = client_id
        self.authority = f"https://login.microsoftonline.com/{tenant_id}"
        self.scopes = scopes or DEFAULT_SCOPES
        self.port = port
        self._cache = _build_token_cache()
        self._app = _build_app(client_id, self.authority, self._cache)

    def try_silent(self) -> dict[str, Any] | None:
        """Attempt silent token acquisition from cache.

        Returns the token result dict if successful, None if no cached token
        or silent acquisition fails.
        """
        accounts = self._app.get_accounts()
        if not accounts:
            return None

        result = self._app.acquire_token_silent(
            self.scopes,
            account=accounts[0],
        )

        if result and "access_token" in result:
            logger.info("Silent token acquisition succeeded")
            return result

        return None

    def authenticate(self) -> dict[str, Any]:
        """Acquire a token interactively.

        Tries localhost redirect first, falls back to device code if:
        - Port is in use
        - No browser can be opened
        - User doesn't complete within LOCALHOST_TIMEOUT

        Returns:
            Token result dict with access_token, account, etc.

        Raises:
            AuthTimeoutError: If both flows timeout.
            AuthCancelledError: If user cancels.
            MsalAuthError: If MSAL returns an error.
        """
        silent = self.try_silent()
        if silent:
            return silent

        try:
            result = self._try_localhost()
            if result and "access_token" in result:
                return result
        except (AuthTimeoutError, OSError) as exc:
            logger.warning("Localhost redirect failed: %s, falling back to device code", exc)

        return self._try_device_code()

    def _try_localhost(self) -> dict[str, Any]:
        """Attempt interactive auth via localhost redirect.

        Raises:
            AuthTimeoutError: If browser isn't opened in time.
            AuthCancelledError: If user cancels consent.
            MsalAuthError: If MSAL returns an error.
        """
        logger.info("Starting localhost redirect auth on port %d", self.port)

        result = self._app.acquire_token_interactive(
            self.scopes,
            port=self.port,
            timeout=LOCALHOST_TIMEOUT,
            prompt="select_account",
        )

        return self._check_result(result, method="localhost")

    def _try_device_code(self) -> dict[str, Any]:
        """Attempt auth via device code flow.

        Raises:
            AuthTimeoutError: If user doesn't complete in time.
            AuthCancelledError: If user cancels.
            MsalAuthError: If MSAL returns an error.
        """
        logger.info("Starting device code flow")

        flow = self._app.initiate_device_flow(self.scopes)
        if "error" in flow:
            raise MsalAuthError(
                error=flow.get("error", "unknown"),
                error_description=flow.get(
                    "error_description", "Failed to initiate device code flow"
                ),
            )

        print(flow.get("message", ""))  # noqa: T201

        result = self._app.acquire_token_by_device_flow(flow)
        return self._check_result(result, method="device_code")

    def _check_result(self, result: dict[str, Any], *, method: str) -> dict[str, Any]:
        """Validate an MSAL token result.

        Raises appropriate errors for failures.
        """
        if not result:
            raise MsalAuthError(
                error="no_result",
                error_description=f"MSAL {method} returned None",
            )

        if "error" in result:
            error = result.get("error", "unknown")
            description = result.get("error_description", "")

            if error == "authentication_cancelled" or "cancel" in description.lower():
                raise AuthCancelledError(f"User cancelled {method} auth: {description}")

            if "timeout" in error.lower() or "timeout" in description.lower():
                raise AuthTimeoutError(f"{method} auth timed out: {description}")

            raise MsalAuthError(error=error, error_description=description)

        if "access_token" not in result:
            raise MsalAuthError(
                error="no_access_token",
                error_description=f"MSAL {method} result missing access_token",
            )

        logger.info(
            "Auth complete via %s, scopes=%s",
            method,
            result.get("scope", self.scopes),
        )
        return result

    @property
    def accounts(self) -> list[dict[str, Any]]:
        """Return cached MSAL accounts."""
        return self._app.get_accounts()
