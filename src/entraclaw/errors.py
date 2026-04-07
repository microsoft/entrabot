"""EntraClaw error hierarchy.

All EntraClaw errors inherit from EntraClawError so callers can catch broadly
or narrow down to specific failure modes.
"""


class EntraClawError(Exception):
    """Base class for all EntraClaw errors."""


class AuthError(EntraClawError):
    """Authentication/identity errors."""


class TokenExchangeError(AuthError):
    """Three-hop token exchange failed (Blueprint → Agent Identity → Agent User)."""

    def __init__(self, hop: str, error: str, description: str) -> None:
        self.hop = hop
        self.error = error
        self.description = description
        super().__init__(f"Token exchange failed at {hop} — {error}: {description}")


class AgentIDNotAvailable(AuthError):
    """Agent identity has not been bootstrapped yet."""


class TokenExpiredError(AuthError):
    """Cached token has expired and needs refresh."""


class TeamsError(EntraClawError):
    """Teams Graph API errors."""


class TeamsNotLicensed(TeamsError):
    """Agent User does not have a Teams license."""


class ChatNotFound(TeamsError):
    """Referenced chat does not exist or is inaccessible."""


class MessageTooLong(TeamsError):
    """Message exceeds the Teams character limit."""


class RateLimitError(EntraClawError):
    """Graph API returned 429 — too many requests."""

    def __init__(self, retry_after: int = 60) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s")
