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
        super().__init__(f"Invalid transition: {from_state} \u2192 {to_state}")


class TransitionTimeoutError(EntraClawError):
    """State machine lock acquisition timed out (30s deadlock safety)."""


class TransitionError(EntraClawError):
    """Exception during state transition (rollback)."""

    def __init__(self, from_state: str, to_state: str, cause: Exception) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.cause = cause
        super().__init__(f"Transition {from_state} \u2192 {to_state} failed: {cause}")


class ProvisioningError(EntraClawError):
    """Background provisioner failed."""

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        msg = f"Provisioning failed: {detail}" if detail else "Provisioning failed"
        super().__init__(msg)


class FilesError(EntraClawError):
    """Files Graph API errors (resolve, read, comment, upload, share)."""


class UrlNotResolvableError(FilesError):
    """A URL passed to ``resolve_file_url`` is malformed or unrecognized."""

    def __init__(self, url: str, reason: str = "unrecognized URL form") -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"Cannot resolve {url!r}: {reason}")


class FileNotFoundError(FilesError):
    """Graph returned 404 when locating a driveItem (or shared link)."""

    def __init__(self, target: str) -> None:
        self.target = target
        super().__init__(f"File not found: {target}")


class SiteNotAllowedError(FilesError):
    """The resolved/target SharePoint site is in ``ENTRACLAW_FILES_DENIED_SITES``."""

    def __init__(self, site_id: str) -> None:
        self.site_id = site_id
        super().__init__(
            f"SharePoint site {site_id!r} is in the operator denylist "
            f"(ENTRACLAW_FILES_DENIED_SITES). The agent cannot read, write, "
            f"or comment on files from this site."
        )


class MissingPermissionError(FilesError):
    """Graph returned 403 — the Agent User doesn't have the right scope."""

    def __init__(self, scope_hint: str) -> None:
        self.scope_hint = scope_hint
        super().__init__(
            f"Graph rejected the call (403) — Agent User likely missing "
            f"{scope_hint}. Re-run setup with --with-files."
        )


class UnsupportedReadFormatError(FilesError):
    """``read_file`` was called on a file extension it does not support."""

    def __init__(self, extension: str, hint: str) -> None:
        self.extension = extension
        self.hint = hint
        super().__init__(f"Cannot read {extension} files: {hint}")


class UnsupportedCommentFormatError(FilesError):
    """``add_file_comment`` was called on a file/target it cannot comment on."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Cannot add file comment: {reason}")


class FileTooLargeError(FilesError):
    """File exceeds ``ENTRACLAW_FILES_MAX_PDF_BYTES`` — refuse to download."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"File is {size_bytes:,} bytes; max is {max_bytes:,} bytes "
            f"(ENTRACLAW_FILES_MAX_PDF_BYTES). Refusing to download."
        )


class NotASponsorError(FilesError):
    """``share_file`` recipient is not in the Agent Identity sponsor list.

    Note: the error message intentionally does NOT enumerate the allowed
    sponsor addresses. Listing alternatives gave the calling LLM a menu
    to try — when one address failed, the agent would retry with a
    different one without user consent (Learning #59, 2026-04-30). The
    sponsor allowlist is a fixed identity-binding, not a routing hint.
    The user supplied a recipient; if it's not allowed, fail loudly and
    stop. The user (the human) can re-issue the request with a different
    address themselves.
    """

    def __init__(self, recipient: str, sponsors: list[str]) -> None:
        self.recipient = recipient
        self.sponsors = sponsors
        super().__init__(
            f"Cannot share with {recipient!r}: not an Agent Identity sponsor. "
            "Stop and ask the user — do not retry with a different address."
        )


class GraphFilesError(FilesError):
    """Graph returned a non-2xx for a Files API call (not covered above)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Graph Files API error {status_code}: {message}")
