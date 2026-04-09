"""EntraClaw error hierarchy.

All EntraClaw errors inherit from EntraClawError so callers can catch broadly
or narrow down to specific failure modes.
"""

from __future__ import annotations


class EntraClawError(Exception):
    """Base class for all EntraClaw errors."""


class AuthError(EntraClawError):
    """Authentication/identity errors."""


class TokenExchangeError(AuthError):
    """Three-hop token exchange failed (Blueprint -> Agent Identity -> Agent User)."""

    def __init__(self, hop: str, error: str, description: str) -> None:
        self.hop = hop
        self.error = error
        self.description = description
        super().__init__(f"Token exchange failed at {hop} \u2014 {error}: {description}")


class AgentIDNotAvailable(AuthError):
    """Agent identity has not been bootstrapped yet."""


class TokenExpiredError(AuthError):
    """Cached token has expired and needs refresh."""


class AuthTimeoutError(AuthError):
    """Auth flow exceeded timeout (e.g. no browser opened in 10s)."""


class AuthCancelledError(AuthError):
    """User cancelled or denied consent."""


class MsalAuthError(AuthError):
    """MSAL returned an error response."""

    def __init__(self, error: str, error_description: str) -> None:
        self.error = error
        self.error_description = error_description
        super().__init__(f"MSAL auth error: {error} \u2014 {error_description}")


class TeamsError(EntraClawError):
    """Teams Graph API errors."""


class TeamsNotLicensed(TeamsError):
    """Agent User does not have a Teams license."""


class ChatNotFound(TeamsError):
    """Referenced chat does not exist or is inaccessible."""


class MessageTooLong(TeamsError):
    """Message exceeds the Teams character limit."""


class GraphApiError(TeamsError):
    """Graph API returned an error."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Graph API error {status_code}: {message}")


class RateLimitError(EntraClawError):
    """Graph API returned 429 \u2014 too many requests."""

    def __init__(self, retry_after: int = 60) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s")


class InvalidTransitionError(EntraClawError):
    """Attempted an invalid state machine transition."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid transition: {from_state} \u2192 {to_state}"
        )


class TransitionTimeoutError(EntraClawError):
    """State machine lock acquisition timed out (30s deadlock safety)."""


class TransitionError(EntraClawError):
    """Exception during state transition (rollback)."""

    def __init__(
        self, from_state: str, to_state: str, cause: Exception
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.cause = cause
        super().__init__(
            f"Transition {from_state} \u2192 {to_state} failed: {cause}"
        )


class ProvisioningError(EntraClawError):
    """Background provisioner failed."""

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        msg = f"Provisioning failed: {detail}" if detail else "Provisioning failed"
        super().__init__(msg)
