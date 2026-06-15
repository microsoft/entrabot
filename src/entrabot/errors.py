"""EntraBot error hierarchy.

All EntraBot errors inherit from EntraBotError so callers can catch broadly
or narrow down to specific failure modes.
"""

from __future__ import annotations


class EntraBotError(Exception):
    """Base class for all EntraBot errors."""


class AuditAttributionError(EntraBotError):
    """Audit event could not be attributed to a concrete identity."""

    def __init__(self, action: str, resource: str) -> None:
        self.action = action
        self.resource = resource
        super().__init__(
            f"Audit attribution failed for agent action {action!r} on {resource!r}: "
            "active agent identity is unavailable"
        )


class ConfigError(EntraBotError):
    """Configuration errors (invalid or removed settings)."""


class InsecureKeyringBackendError(EntraBotError):
    """The selected keyring backend is not an approved OS keystore."""

    def __init__(
        self,
        backend: str,
        expected: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.backend = backend
        self.expected = tuple(expected or ())
        message = f"insecure backend selected: {backend}"
        if self.expected:
            message += f" (expected one of: {', '.join(self.expected)})"
        super().__init__(message)


class RemovedModeError(ConfigError):
    """Raised when a removed ``ENTRABOT_MODE`` value is still configured.

    Bot mode was removed (it bypassed Agent Identity, which is the point of
    the project). Fail loud with a migration message rather than silently
    falling back to another mode — honors the zero-silent-failures rule.
    """


class AuthError(EntraBotError):
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


class AuthRefreshRequiredError(AuthError):
    """Silent refresh failed and interactive auth is required from a user path."""

    def __init__(self, error: str, error_description: str) -> None:
        self.error = error
        self.error_description = error_description
        super().__init__(f"Auth refresh required: {error} — {error_description}")


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


class TeamsError(EntraBotError):
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


class RateLimitError(EntraBotError):
    """Graph API returned 429 \u2014 too many requests."""

    def __init__(self, retry_after: int = 60) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s")


class InvalidTransitionError(EntraBotError):
    """Attempted an invalid state machine transition."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"Invalid transition: {from_state} \u2192 {to_state}")


class TransitionTimeoutError(EntraBotError):
    """State machine lock acquisition timed out (30s deadlock safety)."""


class TransitionError(EntraBotError):
    """Exception during state transition (rollback)."""

    def __init__(self, from_state: str, to_state: str, cause: Exception) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.cause = cause
        super().__init__(f"Transition {from_state} \u2192 {to_state} failed: {cause}")


class ProvisioningError(EntraBotError):
    """Background provisioner failed."""

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        msg = f"Provisioning failed: {detail}" if detail else "Provisioning failed"
        super().__init__(msg)


class FilesError(EntraBotError):
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
    """The resolved/target SharePoint site is in ``ENTRABOT_FILES_DENIED_SITES``."""

    def __init__(self, site_id: str) -> None:
        self.site_id = site_id
        super().__init__(
            f"SharePoint site {site_id!r} is in the operator denylist "
            f"(ENTRABOT_FILES_DENIED_SITES). The agent cannot read, write, "
            f"or comment on files from this site."
        )


class MissingPermissionError(FilesError):
    """Graph returned 403 — the Agent User doesn't have the right scope."""

    def __init__(self, scope_hint: str) -> None:
        self.scope_hint = scope_hint
        super().__init__(
            f"Graph rejected the call (403) — Agent User likely missing "
            f"{scope_hint}. Run `python scripts/grant_files_consent.py` to "
            f"PATCH the missing scopes into the existing oauth2PermissionGrant, "
            f"or re-run `./scripts/setup.sh` for a full provisioning sweep."
        )


class MissingPlaceholderError(EntraBotError):
    """A substantive Teams message was about to send without a recent placeholder.

    Channel-discipline rule (prompts/anatomy/channel-discipline.md): on
    substantive Teams DMs, post_thinking_placeholder must be the FIRST
    action — humans should see "thinking…" within seconds of pinging
    the agent, not silence followed by a wall of text.
    """

    def __init__(
        self,
        chat_id: str,
        placeholder_age_seconds: float,
        grace_seconds: int,
    ) -> None:
        self.chat_id = chat_id
        self.placeholder_age_seconds = placeholder_age_seconds
        self.grace_seconds = grace_seconds
        if placeholder_age_seconds == float("inf"):
            age_str = "never (no placeholder for this chat in this session)"
        else:
            age_str = f"{placeholder_age_seconds:.0f}s ago"
        super().__init__(
            f"Refusing to send substantive message to {chat_id!r}: "
            f"last placeholder was {age_str}, exceeds grace window "
            f"({grace_seconds}s). Call post_thinking_placeholder first, "
            f"then send. Bypass for non-substantive use cases: set "
            f"ENTRABOT_SKIP_PLACEHOLDER_CHECK=true. Disable per-call: "
            f"keep messages ≤200 chars and ≤2 terminal punctuation marks."
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
    """File exceeds ``ENTRABOT_FILES_MAX_PDF_BYTES`` — refuse to download."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"File is {size_bytes:,} bytes; max is {max_bytes:,} bytes "
            f"(ENTRABOT_FILES_MAX_PDF_BYTES). Refusing to download."
        )


class NotASponsorError(FilesError):
    """``share_file`` recipient is not in the Agent Identity sponsor list.

    DEPRECATED in favor of ``RequesterNotSponsorError``. The 2026-04-30
    refactor inverted the gate: the *requester* is now validated against
    sponsors, not the recipient. This class is retained for backward
    compatibility but is no longer raised.
    """

    def __init__(self, recipient: str, sponsors: list[str]) -> None:
        self.recipient = recipient
        self.sponsors = sponsors
        super().__init__(
            f"Cannot share with {recipient!r}: not an Agent Identity sponsor. "
            "Stop and ask the user — do not retry with a different address."
        )


class RequesterNotSponsorError(FilesError):
    """The human who asked the agent to share is not an Agent Identity sponsor.

    Only Agent Identity sponsors are authorized to direct the agent to
    share files. Sponsors may share with anyone they choose; non-sponsors
    cannot share at all.

    Note: this error message intentionally does NOT enumerate the
    allowed sponsor addresses. Listing alternatives gives the calling
    LLM a menu to retry against (Learning #59).
    """

    def __init__(self, requester: str) -> None:
        self.requester = requester
        super().__init__(
            f"{requester!r} is not authorized to ask the agent to share files. "
            "Only Agent Identity sponsors may initiate sharing. "
            "Stop and ask the user to confirm who is requesting this."
        )


class RequesterNotInChatError(FilesError):
    """The requester claimed to be a sponsor but is not a member of the cited chat.

    Defends against an LLM fabricating ``requester_email`` to match a
    real sponsor address while the actual conversation is happening with
    someone else. The requester's user_id MUST appear in the chat's
    member list, not merely in the sponsor allowlist.
    """

    def __init__(self, requester: str, chat_id: str) -> None:
        self.requester = requester
        self.chat_id = chat_id
        super().__init__(
            f"{requester!r} is a sponsor but is not a member of chat "
            f"{chat_id!r}. Refusing to share — verify which chat actually "
            "initiated this request, then re-call with the correct chat_id."
        )


class NoActiveSponsorChannelError(FilesError):
    """No live sponsor binding exists for this sponsor.

    Raised when ``add_teams_member`` / ``share_file`` is called but the
    server has not observed a recent inbound Teams message from the
    matched sponsor that was successfully pushed to the LLM. Defends
    against the LLM acting on a chat that the agent has been admitted
    to but where the sponsor is not actively present (authorization fix).
    """

    def __init__(self, *, sponsor_user_id: str, chat_id: str) -> None:
        self.sponsor_user_id = sponsor_user_id
        self.chat_id = chat_id
        super().__init__(
            f"No active sponsor channel binding for sponsor user_id={sponsor_user_id!r}; "
            f"refusing mutation on chat_id={chat_id!r}. The sponsor must post in the "
            f"target chat before authorizing this action."
        )


class ExpiredSponsorChannelError(FilesError):
    """Sponsor binding exists but is older than the authorization TTL.

    authorization fix. Distinct from ``NoActiveSponsorChannelError`` so audit
    logs can distinguish "sponsor never posted" from "sponsor stopped
    being active in time" — operationally different signals.
    """

    def __init__(
        self,
        *,
        sponsor_user_id: str,
        chat_id: str,
        age_seconds: int,
        ttl_seconds: int,
    ) -> None:
        self.sponsor_user_id = sponsor_user_id
        self.chat_id = chat_id
        self.age_seconds = age_seconds
        self.ttl_seconds = ttl_seconds
        super().__init__(
            f"Sponsor channel binding for user_id={sponsor_user_id!r} on "
            f"chat_id={chat_id!r} is {age_seconds}s old (TTL={ttl_seconds}s). "
            f"Sponsor must post again to re-authorize."
        )


class SponsorChannelMismatchError(FilesError):
    """Supplied chat_id does not match the sponsor's bound active channel.

    The LLM may have been induced (Chain A confused-deputy from the security report
    report) to act on a chat the sponsor is a passive member of but is
    not actively engaged in. Only the chat from which the sponsor's
    most-recent successfully-pushed message originated authorizes
    mutations targeting that chat.
    """

    def __init__(
        self,
        *,
        sponsor_user_id: str,
        supplied_chat_id: str,
        bound_chat_id: str,
    ) -> None:
        self.sponsor_user_id = sponsor_user_id
        self.supplied_chat_id = supplied_chat_id
        self.bound_chat_id = bound_chat_id
        super().__init__(
            f"Sponsor user_id={sponsor_user_id!r} most recently posted in "
            f"chat_id={bound_chat_id!r}, but the call targets "
            f"chat_id={supplied_chat_id!r}. Refusing — sponsor must post in "
            f"the target chat to authorize a mutation there."
        )


class GraphFilesError(FilesError):
    """Graph returned a non-2xx for a Files API call (not covered above)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Graph Files API error {status_code}: {message}")
