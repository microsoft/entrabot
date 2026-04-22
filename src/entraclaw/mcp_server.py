"""EntraClaw MCP server — progressive identity with Agent User tools.

Authentication is automatic via progressive identity:
1. Try three-hop Agent User flow with existing creds (fast path)
2. If that fails, fall back to MSAL delegated auth (human's token)
3. Optionally background-provision an Agent User identity

The calling LLM does NOT need to provide any credentials, tokens, or
configuration — just call the tools directly.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from entraclaw import efferent_copy
from entraclaw.config import get_config
from entraclaw.errors import EntraClawError, TokenExchangeError
from entraclaw.identity.state_machine import IdentityStateMachine
from entraclaw.logging_config import setup_logging
from entraclaw.models import IdentityState
from entraclaw.tools.interaction_log import detect_channel, log_interaction
from entraclaw.tools.teams import acquire_agent_user_token, fetch_message

logger: logging.Logger | None = None

# Local system-prompt file used when persona-sati isn't reachable. Kept as a
# module attribute so tests can monkey-patch it at an isolated path.
LOCAL_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "agent_system.md"
)


_HARDCODED_FALLBACK = (
    "EntraClaw Teams Interface: provides tools for sending and "
    "receiving Microsoft Teams messages, managing group chats, "
    "email polling, and daily summary generation. This server "
    "handles communication channels only. For personality, memory, "
    "and behavioral rules, connect to the persona-sati MCP server."
)


def _expand_includes(text: str, base_dir: Path) -> str:
    """Replace ``@include <path>`` lines with the target file's contents.

    ``@include`` is a deliberately simple directive: it matches a line
    whose first non-whitespace token is ``@include``, followed by a
    relative path resolved against *base_dir*. Missing files are
    replaced with a visible comment so boot never crashes on a typo.
    Included files are NOT recursively expanded — one level only.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("@include"):
            target_name = stripped[len("@include"):].strip()
            if target_name:
                target_path = base_dir / target_name
                try:
                    if target_path.is_file():
                        lines.append(
                            target_path.read_text(encoding="utf-8").rstrip()
                        )
                        continue
                except OSError:
                    pass
                lines.append(f"<!-- missing @include {target_name} -->")
                continue
        lines.append(line)
    return "\n".join(lines)


def _load_body_prompt() -> str:
    """Return the expanded body prompt, or an empty string if no file.

    Reads ``LOCAL_PROMPT_PATH`` and expands any ``@include`` directives
    relative to its parent directory (so files under ``prompts/anatomy/``
    can be composed into one body prompt).
    """
    try:
        if not LOCAL_PROMPT_PATH.is_file():
            return ""
        raw = LOCAL_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    expanded = _expand_includes(raw, LOCAL_PROMPT_PATH.parent).strip()
    return expanded


def _load_agent_instructions() -> str:
    """Return the agent's composed system prompt.

    Layering (body-first so body rules can't be overridden):
      * **Body** — ``prompts/agent_system.md`` with ``@include`` expansion
        of anatomy modules. Always loaded first when the file exists.
      * **Persona** — fetched from persona-sati when configured and
        reachable. Appended AFTER the body.
      * **Hardcoded fallback** — used only when neither body nor persona
        is available, so boot never crashes.
    """
    import os
    import subprocess
    import sys

    # Resolve the structured logger up front. setup_logging() is
    # idempotent, so calling it here is safe even though main() also
    # calls it — and it's necessary because _load_agent_instructions
    # runs at module import time (FastMCP(...) call), well before
    # main() configures the handlers. Without this, every
    # persona-load outcome only surfaces as a transient stderr print
    # that Claude Code discards, so post-hoc "did persona load at
    # boot?" debugging has no trail.
    log = setup_logging()

    body = _load_body_prompt()

    remote_url = os.environ.get("PERSONA_SATI_MCP_URL", "").strip()
    token_cmd = os.environ.get("PERSONA_SATI_MCP_TOKEN_COMMAND", "").strip()
    if not remote_url or not token_cmd:
        log.info(
            "persona-sati env unset; serving body-only "
            "(body_loaded=%s, body_chars=%d)",
            bool(body),
            len(body) if body else 0,
        )
        return body or _HARDCODED_FALLBACK

    body_or_fallback = body or _HARDCODED_FALLBACK

    try:
        token = subprocess.check_output(
            [token_cmd], text=True, timeout=30
        ).strip()
    except (subprocess.SubprocessError, OSError) as exc:
        print(
            f"[entraclaw] could not mint persona-sati token "
            f"({token_cmd}): {exc}; using local fallback prompt",
            file=sys.stderr,
        )
        log.warning(
            "persona-sati token mint failed (%s): %s: %s",
            token_cmd,
            type(exc).__name__,
            exc,
        )
        return body_or_fallback
    if not token:
        print(
            f"[entraclaw] token command {token_cmd} returned empty; "
            "using local fallback prompt",
            file=sys.stderr,
        )
        log.warning(
            "persona-sati token command %s returned empty output",
            token_cmd,
        )
        return body_or_fallback

    try:
        import asyncio

        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async def _fetch_remote_prompt() -> str | None:
            sse_url = f"{remote_url.rstrip('/')}/sse"
            headers = {"Authorization": f"Bearer {token}"}
            async with (
                sse_client(sse_url, headers=headers) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool("get_system_prompt", {})
                for item in result.content:
                    if hasattr(item, "text") and item.text:
                        return item.text
            return None

        remote = asyncio.run(_fetch_remote_prompt())
    except Exception as exc:  # noqa: BLE001 — never break boot
        print(
            f"[entraclaw] persona-sati fetch failed: {exc}; "
            "using local fallback prompt",
            file=sys.stderr,
        )
        log.warning(
            "persona-sati fetch failed (%s): %s: %s",
            remote_url,
            type(exc).__name__,
            exc,
        )
        return body_or_fallback

    if not remote:
        print(
            "[entraclaw] persona-sati returned empty prompt; "
            "using local fallback",
            file=sys.stderr,
        )
        log.warning(
            "persona-sati returned empty prompt (%s); using local fallback",
            remote_url,
        )
        return body_or_fallback

    print(
        f"[entraclaw] loaded system prompt from persona-sati ({remote_url})",
        file=sys.stderr,
    )
    log.info(
        "persona-sati prompt loaded (url=%s, body_chars=%d, persona_chars=%d)",
        remote_url,
        len(body) if body else 0,
        len(remote),
    )
    # Body rules are non-overridable — prepend them so the LLM reads
    # security/channel discipline before any persona content.
    if body:
        return body + "\n\n---\n\n" + remote
    return remote


mcp = FastMCP(
    "EntraClaw Agent Identity",
    instructions=_load_agent_instructions(),
)

# ---------------------------------------------------------------------------
# Host detection — leader/slave gating for multi-MCP-client support.
#
# Claude Code is the canonical "leader" because it exposes the custom
# ``notifications/claude/channel`` message mechanism. Every other MCP host
# (Copilot CLI, any future host lacking a channel) is a slave.
#
# Scope of slave-mode gating:
#
#   - Background reads (Teams poll, email poll, chat discovery, daily
#     summary) run ALWAYS. They're process-level observability, not
#     per-connection behavior. Gating them at boot was a lifecycle bug
#     (PR #27 fix): clientInfo isn't populated until after initialize, so
#     the gate always read "unknown" at boot and the server went deaf.
#   - Channel pushes (``notifications/claude/channel``) are skipped when
#     the connected host is a slave — the slave has no channel to receive
#     them. The interaction log write still happens.
#   - Outbound tool disclosure (``_slave_disclosure_suffix``) is appended
#     to reply-expecting tool responses so the model can tell the user
#     their reply won't surface in this host.
#
# Cached-host semantic (the key to this file):
#
#   ``_current_host()`` reads the live FastMCP request context — only
#   valid inside a tool invocation. Background tasks run in detached
#   asyncio contexts with no live request, so ``_current_host()`` returns
#   ``"unknown"`` from them.
#
#   To answer "does a leader client hold this session?" from a background
#   task, we cache the last-seen ``clientInfo.name`` in ``_state["cached_host"]``
#   at every tool entry (via ``_capture_host_from_context`` called from
#   ``_initialize``). ``_is_leader_host`` prefers the live context and
#   falls back to the cache.
#
#   Boot order: at first MCP boot the cache is empty ("") and background
#   tasks correctly treat the session as slave (fail closed). The cache
#   populates on the first tool call, after which background pushes flow.
#
# Static designation via MCP ``clientInfo.name`` (not dynamic election).
# Deny-by-default: unknown names are slaves, so a mislabeled host fails
# safe (push-less) rather than double-pushing.
# ---------------------------------------------------------------------------
LEADER_HOSTS: frozenset[str] = frozenset({"claude-code", "claude code"})

# Disclosure appended to reply-expecting tool responses when the current
# host is not a leader. The model reads this and can relay it to the user.
SLAVE_REPLY_DISCLOSURE = (
    "Reply channel unavailable in this host — Teams replies won't push here. "
    "Check Teams directly or switch to Claude Code for channel events."
)


def _current_host() -> str:
    """Return the active MCP client's ``clientInfo.name`` lowercased.

    Reads from the FastMCP request context. Returns ``"unknown"`` when no
    active request context is available (e.g. module-load time, background
    asyncio tasks detached from any request) or when ``clientInfo`` is not
    yet populated.
    """
    try:
        ctx = mcp.get_context()
    except Exception:  # noqa: BLE001 — no active request context
        return "unknown"

    try:
        client_params = ctx.session.client_params
    except Exception:  # noqa: BLE001 — session not yet initialized
        return "unknown"

    if client_params is None:
        return "unknown"

    try:
        name = client_params.clientInfo.name
    except AttributeError:
        return "unknown"

    if not name:
        return "unknown"
    return str(name).lower()


def _capture_host_from_context() -> str:
    """Cache the live request-context host into ``_state["cached_host"]``.

    Called at every tool entry (from ``_initialize``). When there is no
    live context (or ``clientInfo`` is not populated yet) this is a no-op
    on the cache — so a stale but correct value from an earlier request
    survives, which is exactly what lets background pushes work.
    """
    host = _current_host()
    if host and host != "unknown":
        _state["cached_host"] = host
    return host


def _capture_host_from_initialize(client_params: object) -> str:
    """Cache ``clientInfo.name`` from an MCP Initialize request.

    Pairs with the ``ServerSession._received_request`` wrapper installed
    at module load. Fires as soon as the MCP initialize handshake
    populates ``_client_params`` on the session — well before the first
    tool call. Without this, the cold-start window between "MCP server
    booted" and "Claude Code made its first tool call" leaves
    ``_state["cached_host"]`` empty, so background channel pushes
    (inbound DMs, emails) hit the fail-closed branch and get silently
    dropped.

    Returns the lowercased host name (empty string on failure).
    """
    if client_params is None:
        return ""
    try:
        name = (client_params.clientInfo.name or "").lower()
    except AttributeError:
        return ""
    if name:
        _state["cached_host"] = name
    return name


def _is_leader_host() -> bool:
    """True iff the connected MCP client is in the canonical leader set.

    Prefers the live request context. Falls back to the most recently
    captured host in ``_state["cached_host"]`` so background tasks
    (detached from any request context) still get the right answer as
    long as the connected host has made at least one prior tool call.

    Deny-by-default: an empty cache AND no live context is treated as
    slave. Only Claude Code is a leader.
    """
    live = _current_host()
    if live and live != "unknown":
        return live in LEADER_HOSTS
    cached = _state.get("cached_host", "")
    return isinstance(cached, str) and cached in LEADER_HOSTS


def _slave_disclosure_suffix() -> str:
    """Return the slave-mode disclosure string, or empty string for leaders."""
    if _is_leader_host():
        return ""
    return SLAVE_REPLY_DISCLOSURE


# Module-level state populated by _initialize().
# ``cached_host`` is pre-seeded at module load so background tasks that
# fire before the first tool call see an empty string (slave — fail closed).
_state: dict[str, object] = {"cached_host": ""}
_identity: IdentityStateMachine | None = None


def _make_received_request_with_capture(original):
    """Build a ``ServerSession._received_request`` replacement that
    cascades to *original* and then, if the dispatched request was an
    ``InitializeRequest``, caches ``clientInfo.name`` via
    :func:`_capture_host_from_initialize`.

    Factored out so unit tests can exercise the wrapper directly
    without going through a real ``ServerSession`` instance.
    """
    from mcp.types import InitializeRequest

    async def _received_request_with_capture(self, responder):  # type: ignore[no-untyped-def]
        await original(self, responder)
        if isinstance(responder.request.root, InitializeRequest):
            host = _capture_host_from_initialize(self._client_params)
            if host and logger is not None:
                logger.info(
                    "MCP initialize: cached_host=%s (leader=%s)",
                    host,
                    host in LEADER_HOSTS,
                )

    return _received_request_with_capture


def _install_initialize_host_capture() -> None:
    """Wrap ``ServerSession._received_request`` to cache host on Initialize.

    MCP's ``initialize`` handshake is the very first request on a new
    session — the client sends its ``clientInfo.name``, the server
    populates ``ServerSession._client_params``, and only THEN does the
    client start making tool calls. Without this wrapper,
    ``_state["cached_host"]`` would not populate until the first tool
    entry fires ``_capture_host_from_context``. Any background channel
    push in the meantime (inbound DM, inbound email) would see the
    empty cache, hit the slave-mode fail-closed branch, and get
    silently dropped.

    Monkey-patching a private MCP method is brittle to library
    upgrades — the upside is a ~10-line fix vs. subclassing
    ``ServerSession`` and re-wiring it through FastMCP's internals. If
    a future ``mcp`` release renames ``_received_request`` or
    ``_client_params``, the wiring tests in
    ``tests/test_mcp_server_integration.py::TestInitializeHookWiring``
    fail loudly and we'll know to adapt.
    """
    from mcp.server.session import ServerSession

    original = ServerSession._received_request
    ServerSession._received_request = (  # type: ignore[method-assign]
        _make_received_request_with_capture(original)
    )


_install_initialize_host_capture()

TOKEN_REFRESH_THRESHOLD = 3300  # 55 min (5-min buffer on 60-min expiry)

# Sent-message tracking for delegated-mode echo prevention
SENT_MESSAGE_MAX = 1000
_sent_message_ids: set[str] = set()


async def _resolve_tenant_id(email: str, our_domain: str) -> str | None:
    """Resolve a tenant ID from an email domain via OpenID discovery.

    Returns the tenant GUID if the email domain differs from our_domain
    and OpenID discovery succeeds, otherwise None.

    Uses async httpx to avoid blocking the event loop in the MCP server.
    """
    if "@" not in email:
        return None

    domain = email.split("@")[1]
    if our_domain and domain.lower() == our_domain.lower():
        return None

    try:
        oidc_url = (
            f"https://login.microsoftonline.com/{domain}/.well-known/openid-configuration"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.get(oidc_url, timeout=10)
        if resp.status_code == 200:
            issuer = resp.json().get("issuer", "")
            # Issuer format varies:
            #   https://login.microsoftonline.com/{tenant_id}/v2.0
            #   https://sts.windows.net/{tenant_id}/
            # Both parse correctly — split by / and take index 3.
            parts = issuer.rstrip("/").split("/")
            if len(parts) > 3:
                tenant_id = parts[3]
                if logger:
                    logger.info("Auto-resolved tenant for %s: %s", domain, tenant_id)
                return tenant_id
    except Exception as exc:
        if logger:
            logger.warning("Could not auto-resolve tenant for %s: %s", domain, exc)

    return None


async def _ensure_valid_token() -> None:
    """Eagerly refresh the token if it's near expiry.

    Identity-aware dispatch (eng review decision 6A):
    - DELEGATED → MSAL silent refresh
    - AGENT_USER → three-hop flow
    - UNAUTHENTICATED → no-op (auth needed first)
    """
    if _identity is None:
        return

    session = _identity.session
    acquired_at = session.token_acquired_at
    if acquired_at is None or (time.monotonic() - acquired_at) > TOKEN_REFRESH_THRESHOLD:
        current_state = _identity.state

        if current_state == IdentityState.AGENT_USER:
            if logger:
                logger.info("Token near expiry — refreshing via three-hop flow")
            config = _state.get("config")
            token = acquire_agent_user_token(config)
            _identity.update_session(token=token, token_acquired_at=time.monotonic())
            _state["token"] = token

        elif current_state == IdentityState.DELEGATED:
            if logger:
                logger.info("Token near expiry — refreshing via MSAL silent")
            try:
                from entraclaw.auth.delegated import MsalDelegatedAuth

                config = _state.get("config")
                auth = MsalDelegatedAuth(
                    client_id=config.client_id,
                    tenant_id=config.tenant_id or "common",
                )
                result = auth.try_silent()
                if result and "access_token" in result:
                    token = result["access_token"]
                    _identity.update_session(
                        token=token, token_acquired_at=time.monotonic(),
                    )
                    _state["token"] = token
                else:
                    # Silent failed — try interactive
                    result = auth.authenticate()
                    token = result["access_token"]
                    _identity.update_session(
                        token=token, token_acquired_at=time.monotonic(),
                    )
                    _state["token"] = token
            except Exception as exc:
                if logger:
                    logger.warning("MSAL refresh failed: %s", exc)
                # Transition to UNAUTHENTICATED on total failure
                import contextlib

                with contextlib.suppress(Exception):
                    await _identity.transition(IdentityState.UNAUTHENTICATED)

        elif current_state == IdentityState.UNAUTHENTICATED:
            pass  # No token to refresh — auth needed first


async def _with_token_retry(fn, **kwargs):
    """Call *fn* with the current token; on TokenExpiredError, refresh and retry once.

    The function *fn* must accept a ``token`` keyword argument.
    Any additional kwargs are passed through to *fn*.
    """
    from entraclaw.errors import TokenExpiredError

    token = _state.get("token") or (_identity.session.token if _identity else None)
    try:
        return await fn(token=str(token), **kwargs)
    except TokenExpiredError:
        if logger:
            logger.warning("Token expired mid-call — refreshing and retrying")
        # Force refresh by clearing token_acquired_at (token may be fresh but revoked)
        if _identity is not None:
            _identity.update_session(token_acquired_at=None)
        await _ensure_valid_token()
        token = _state.get("token") or (_identity.session.token if _identity else None)
        return await fn(token=str(token), **kwargs)


OVERLAP_SECONDS = 2
SEEN_SET_MAX = 500
SEEN_SET_PRUNE_MINUTES = 10


def _overlap_timestamp(iso_timestamp: str) -> str:
    """Subtract OVERLAP_SECONDS from an ISO 8601 timestamp.

    Used to create a query window that overlaps with the previous poll,
    preventing message loss at timestamp boundaries (Learning #17).
    """
    dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    overlap_dt = dt - timedelta(seconds=OVERLAP_SECONDS)
    return overlap_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter_new_messages(
    messages: list[dict],
    last_seen_timestamp: str | None,
    seen_ids: set[str],
) -> list[dict]:
    """Return messages that are newer than cursor AND not already seen.

    Applies the overlap-window pattern: messages with sent_at >= (cursor - 2s)
    are candidates, then the seen-set filters duplicates from the overlap.
    """
    if not last_seen_timestamp:
        return messages

    overlap_ts = _overlap_timestamp(last_seen_timestamp)
    return [
        m
        for m in messages
        if m.get("sent_at", "") >= overlap_ts and m["message_id"] not in seen_ids
    ]


def _prune_seen_set(
    seen_ids: set[str],
    id_timestamps: dict[str, str],
) -> set[str]:
    """Prune the seen-set to only IDs from the last SEEN_SET_PRUNE_MINUTES.

    Called when seen-set exceeds SEEN_SET_MAX entries to prevent memory leaks
    in long-running polling sessions (Learning #20).
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=SEEN_SET_PRUNE_MINUTES)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {msg_id for msg_id in seen_ids if id_timestamps.get(msg_id, "") >= cutoff_str}


async def _init_auth() -> None:
    """Phase 1: Authenticate — try three-hop fast path, fall back to MSAL delegated.

    Per eng review decision 1A:
    - If SKIP_PROVISIONING is true → MSAL delegated only
    - Otherwise → try three-hop with existing creds (fast path to AGENT_USER)
    - If three-hop fails → warn + MSAL delegated auth → DELEGATED
    - If MSAL also fails → UNAUTHENTICATED
    """
    global _identity
    _identity = IdentityStateMachine()

    config = get_config()
    _state["config"] = config

    # Bot mode: no Graph token needed — bot server handles Teams I/O
    if config.mode == "bot":
        if logger:
            logger.info("Bot mode: skipping Graph auth — bot server handles Teams I/O")
        return

    # Fast path: try three-hop with existing creds (unless SKIP_PROVISIONING)
    if not config.skip_provisioning and config.blueprint_app_id and config.tenant_id:
        try:
            token = acquire_agent_user_token(config)
            _identity.update_session(
                token=token,
                token_acquired_at=time.monotonic(),
                auth_mode="agent_user",
                user_id=config.agent_user_id,
                display_name="EntraClaw Agent",
            )
            await _identity.transition(IdentityState.AGENT_USER)
            _state["token"] = token
            if logger:
                logger.info("Fast path: Agent User token acquired via three-hop flow")
            return
        except (TokenExchangeError, EntraClawError) as exc:
            if logger:
                logger.warning(
                    "Three-hop flow failed, falling back to MSAL delegated: %s", exc,
                )
        except Exception as exc:
            if logger:
                logger.warning("Unexpected auth error, falling back to MSAL: %s", exc)

    # Delegated path: MSAL interactive auth
    if config.client_id:
        try:
            from entraclaw.auth.delegated import MsalDelegatedAuth

            auth = MsalDelegatedAuth(
                client_id=config.client_id,
                tenant_id=config.tenant_id or "common",
            )
            result = auth.authenticate()
            token = result["access_token"]
            account = result.get("id_token_claims", {})

            _identity.update_session(
                token=token,
                token_acquired_at=time.monotonic(),
                auth_mode="delegated",
                user_id=account.get("oid"),
                display_name=account.get("name"),
                account_id=account.get("sub"),
                tenant_id=account.get("tid"),
            )
            await _identity.transition(IdentityState.DELEGATED)
            _state["token"] = token
            if logger:
                logger.info(
                    "MSAL delegated auth succeeded for %s",
                    account.get("preferred_username", "unknown"),
                )
            return
        except Exception as exc:
            if logger:
                logger.warning("MSAL delegated auth failed: %s", exc)

    # No auth method available — stay UNAUTHENTICATED
    # Per eng review decision 4A: no hard exits, state transitions instead
    if not config.blueprint_app_id and not config.client_id and logger:
        logger.warning(
            "No auth configured: set ENTRACLAW_BLUEPRINT_APP_ID (three-hop) "
            "or ENTRACLAW_CLIENT_ID (MSAL delegated) in .env"
        )


def _effective_user_id() -> str | None:
    """Return the user ID appropriate for the current identity state.

    In AGENT_USER mode, returns config.agent_user_id (the provisioned agent).
    In DELEGATED mode, returns the signed-in human's OID from the MSAL token.
    Returns None in delegated mode if OID is unavailable — callers (e.g.
    create_one_on_one_chat) will resolve via /me. Never falls through to
    config.agent_user_id in delegated mode — that's a different identity
    than the token holder.
    """
    config = _state.get("config")
    if _identity and _identity.state == IdentityState.DELEGATED:
        session_uid = _identity.session.user_id
        # Return the session user_id or None — do NOT fall through to
        # agent_user_id, which is a different identity than the human token.
        return session_uid
    return config.agent_user_id if config else None


async def _init_poll() -> None:
    """Phase 3: Initialize watched chats and start background polling."""
    _state["last_seen_timestamp"] = None
    _state["seen_message_ids"] = set()
    _state["seen_id_timestamps"] = {}

    # Watched chats: dict of chat_id -> {seen_ids: set, last_ts: str|None}
    # Only chats the agent has explicitly registered (via create_chat or
    # auto-discovery) are watched. There is no default group chat.
    _state["watched_chats"] = {}

    # Load persisted watched chats (DMs created via create_chat tool)
    config = _state.get("config")
    if config:
        watched_file = config.data_dir / "watched_chats"
        if watched_file.is_file():
            for line in watched_file.read_text().splitlines():
                cid = line.strip()
                if cid:
                    _register_watched_chat(cid, persist=False)
                    if logger:
                        logger.info("Loaded persisted watched chat: %s", cid)

    # Start background polling unconditionally.
    #
    # Background reads (Teams chat poll, email poll, chat auto-discovery,
    # daily summary) are process-level observability, not per-connection
    # behavior. Gating them on _is_leader_host() at boot time was a bug:
    # clientInfo isn't populated until after the MCP initialize handshake,
    # so _current_host() returns "unknown" here and the gate locks polling
    # off for the life of the process. Slave-mode now only affects channel
    # pushes (see _push_channel_notification) and outbound tool disclosure
    # (see _slave_disclosure_suffix), both evaluated per call.
    if config and config.mode == "bot":
        import asyncio

        _state["poll_task"] = asyncio.get_event_loop().create_task(
            _background_poll_bot()
        )
    elif _state.get("watched_chats"):
        _ensure_poll_task_running()

    # Start email poll + daily summary when authenticated as the Agent User
    # (its own mailbox and outbound mail rights). In delegated mode /me/*
    # would target the human's mailbox — not what we want.
    if (
        _identity
        and _identity.session
        and _identity.session.auth_mode == "agent_user"
    ):
        import asyncio

        asyncio.get_event_loop().create_task(_background_poll_email())
        asyncio.get_event_loop().create_task(_background_daily_summary())
        asyncio.get_event_loop().create_task(_background_discover_chats())
        asyncio.get_event_loop().create_task(
            _background_persona_sati_heartbeat()
        )


async def _initialize() -> None:
    """Acquire a token and start background polling.

    Called at the top of every @mcp.tool() wrapper. Two phases:
    1. _init_auth() — authenticate (three-hop fast path or MSAL delegated)
    2. _init_poll() — load persisted watched chats and start background polls

    Also captures the connected client's ``clientInfo.name`` into
    ``_state["cached_host"]`` on every call (not just the first) — this
    keeps the cache warm across the server's lifetime so background tasks
    that run in detached asyncio contexts (no live request) can still
    answer the leader/slave question correctly when deciding whether to
    push on ``notifications/claude/channel``.

    There is no longer a default Teams chat. Callers must pass a chat_id to
    any Teams tool; chats to watch come from the watched_chats file or the
    create_chat tool at runtime.
    """
    # Capture on every tool entry so background pushes can see the host
    # even after the one-shot init block below short-circuits.
    _capture_host_from_context()

    if _state.get("initialized"):
        return

    await _init_auth()
    await _init_poll()

    _state["initialized"] = True


BACKGROUND_POLL_INTERVAL = 5  # seconds between polls
BOT_POLL_INTERVAL = 2  # seconds between bot inbound file checks
EMAIL_POLL_INTERVAL = 60  # seconds between /me/messages polls
CHAT_DISCOVER_INTERVAL = 120  # seconds between /me/chats auto-discovery sweeps
PERSONA_SATI_HEARTBEAT_INTERVAL = 300  # 5 min smoke test against persona-sati


async def _persona_sati_list_files(url: str, token: str) -> list[str]:
    """Call persona-sati's ``list_memory_files`` tool via SSE.

    Factored out so tests can monkey-patch a fake without driving the
    full MCP client stack. Returns the parsed JSON list on success;
    raises on any failure so callers can classify.
    """
    import json as _json

    from mcp import ClientSession
    from mcp.client.sse import sse_client

    sse_url = f"{url.rstrip('/')}/sse"
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        sse_client(sse_url, headers=headers) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("list_memory_files", {})
        for item in result.content:
            if hasattr(item, "text") and item.text:
                parsed = _json.loads(item.text)
                # persona-sati wraps the JSON array inside a dict with
                # a "result" key whose value is itself the JSON string.
                if isinstance(parsed, dict) and "result" in parsed:
                    parsed = _json.loads(parsed["result"])
                if isinstance(parsed, list):
                    return parsed
        return []


async def _persona_sati_heartbeat_once() -> str:
    """Single smoke test against persona-sati; returns an outcome string.

    Outcomes:
      * ``"skipped"`` — env not configured for persona-sati (no remote).
      * ``"ok"`` — remote reachable, returned a file list. INFO-logged.
      * ``"token_mint_failed"`` — the token command errored or returned
        empty. WARNING-logged.
      * ``"remote_failed"`` — SSE call raised. WARNING-logged.

    Never raises: a broken heartbeat must not take down the MCP boot.

    Resolves its own ``logging.getLogger("entraclaw")`` so it emits
    records even when called before ``main()`` has configured the
    module-global ``logger`` (e.g. unit tests, module-import-time
    smoke checks).
    """
    import logging
    import os
    import subprocess
    import time as _time

    log = logging.getLogger("entraclaw")

    remote_url = os.environ.get("PERSONA_SATI_MCP_URL", "").strip()
    token_cmd = os.environ.get("PERSONA_SATI_MCP_TOKEN_COMMAND", "").strip()
    if not remote_url or not token_cmd:
        return "skipped"

    try:
        token = subprocess.check_output(
            [token_cmd], text=True, timeout=30
        ).strip()
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning(
            "persona-sati heartbeat FAILED: token_mint_failed "
            "(%s): %s: %s",
            token_cmd,
            type(exc).__name__,
            exc,
        )
        return "token_mint_failed"
    if not token:
        log.warning(
            "persona-sati heartbeat FAILED: token_mint_failed "
            "(%s returned empty output)",
            token_cmd,
        )
        return "token_mint_failed"

    started = _time.monotonic()
    try:
        files = await _persona_sati_list_files(remote_url, token)
    except Exception as exc:  # noqa: BLE001 — classify but never raise
        log.warning(
            "persona-sati heartbeat FAILED: remote_failed (%s): "
            "%s: %s",
            remote_url,
            type(exc).__name__,
            exc,
        )
        return "remote_failed"

    elapsed_ms = int((_time.monotonic() - started) * 1000)
    log.info(
        "persona-sati heartbeat ok (url=%s, file_count=%d, "
        "elapsed_ms=%d)",
        remote_url,
        len(files),
        elapsed_ms,
    )
    return "ok"


async def _background_persona_sati_heartbeat() -> None:
    """Schedule ``_persona_sati_heartbeat_once`` on a loop.

    Fires immediately at boot (no initial wait) so the smoke test
    surfaces misconfigurations right away rather than N minutes later.
    Then sleeps PERSONA_SATI_HEARTBEAT_INTERVAL between ticks.

    Detects silent-failure modes the one-shot boot load can't cover:
    Claude Code's SSE session token aging out mid-session, cloud
    endpoint going down after boot, persona-sati pod rolling. The
    heartbeat uses its own freshly-minted token on every tick, so
    what it exercises is "can entraclaw reach persona-sati right
    now?" — a strict subset of "can Claude Code reach persona-sati?"
    but enough to catch DNS / endpoint / auth-scope failures in both.
    """
    import asyncio

    if logger:
        logger.info(
            "Starting persona-sati heartbeat (interval=%ds)",
            PERSONA_SATI_HEARTBEAT_INTERVAL,
        )

    while True:
        try:
            await _persona_sati_heartbeat_once()
        except Exception as exc:  # noqa: BLE001 — must never die
            if logger:
                logger.warning(
                    "persona-sati heartbeat loop error: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        await asyncio.sleep(PERSONA_SATI_HEARTBEAT_INTERVAL)


async def _background_poll_bot() -> None:
    """Background polling loop for bot mode — reads from inbound.jsonl.

    Instead of polling Graph API, reads the shared JSONL file that the
    bot server writes inbound Teams messages to.
    """
    import asyncio

    from entraclaw.bot.handler import read_inbound

    if logger:
        logger.info("Starting bot-mode inbound poll (interval=%ds)", BOT_POLL_INTERVAL)

    seen_ids: set[str] = set()

    while True:
        try:
            await asyncio.sleep(BOT_POLL_INTERVAL)

            messages = read_inbound()
            for msg in messages:
                msg_id = msg.get("message_id", "")
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)

                await _push_channel_notification(
                    msg, chat_id=msg.get("conversation_id"),
                )

            # Bounded cleanup
            if len(seen_ids) > SEEN_SET_MAX:
                seen_ids = set(sorted(seen_ids)[-100:])

        except Exception as exc:
            if logger:
                logger.warning("Bot inbound poll error: %s", exc)
            await asyncio.sleep(BOT_POLL_INTERVAL)


def _ensure_poll_task_running() -> None:
    """Start the Graph background poll task if one isn't already running.

    Idempotent. Bot mode is skipped — the bot gateway handles inbound via
    _background_poll_bot which is started explicitly in _init_poll.
    """
    config = _state.get("config")
    if config is not None and getattr(config, "mode", None) == "bot":
        return

    existing = _state.get("poll_task")
    if existing is not None and not existing.done():
        return

    import asyncio

    _state["poll_task"] = asyncio.get_event_loop().create_task(
        _background_poll()
    )
    if logger:
        logger.info("Started background Teams poll task")


def _register_watched_chat(chat_id: str, *, persist: bool = True) -> None:
    """Register a chat for background polling.

    Each chat gets its own cursor and seen-set so message tracking is
    independent. Safe to call multiple times — idempotent.

    When ``persist`` is True (default), the chat ID is also appended to
    ``data_dir/watched_chats`` so it survives MCP server restarts.

    If no background poll task is currently running (e.g. the MCP server
    booted with zero watched chats and this is the first chat being added
    via create_chat), lazily spins one up. Bot mode is excluded — the bot
    gateway handles inbound via _background_poll_bot.
    """
    watched = _state.get("watched_chats", {})
    if chat_id not in watched:
        watched[chat_id] = {"seen_ids": set(), "last_ts": None, "bootstrapped": False}
        _state["watched_chats"] = watched
        if logger:
            logger.info("Registered chat for background polling: %s", chat_id)

    _ensure_poll_task_running()

    if persist:
        config = _state.get("config")
        if config:
            watched_file = config.data_dir / "watched_chats"
            watched_file.parent.mkdir(parents=True, exist_ok=True)
            # Read existing, add if not present
            existing = set()
            if watched_file.is_file():
                existing = {
                    line.strip()
                    for line in watched_file.read_text().splitlines()
                    if line.strip()
                }
            if chat_id not in existing:
                existing.add(chat_id)
                watched_file.write_text("\n".join(sorted(existing)) + "\n")
                if logger:
                    logger.info("Persisted watched chat: %s", chat_id)


async def _bootstrap_chat(chat_id: str) -> None:
    """Bootstrap a watched chat's cursor so the newest message surfaces once.

    Called once per chat on first poll cycle. Intent: don't flood Claude
    Code with the full pre-existing history when a chat is added mid-
    session, but DO surface the message that is most likely the reason
    the chat was created (e.g. the human adds the agent and posts an
    intro in the same minute — that intro must not be swallowed).

    Implementation: mark every fetched message EXCEPT the newest as
    ``seen_ids`` and watermark ``last_ts`` to the newest's sent_at. On
    the first real poll cycle, ``_filter_new_messages`` sees the newest
    message inside the 2-second overlap window and not in seen_ids, so
    it gets pushed. All older messages are dropped as duplicates. A
    chat with exactly one pre-existing message behaves the same — that
    one message surfaces once.
    """
    from entraclaw.tools.teams import read

    chat_state = _state["watched_chats"][chat_id]
    try:
        await _ensure_valid_token()
        bootstrap_msgs = await _with_token_retry(read, chat_id=chat_id, count=10)
        if bootstrap_msgs:
            newest = max(bootstrap_msgs, key=lambda m: m.get("sent_at", ""))
            chat_state["last_ts"] = newest["sent_at"]
            # Mark every message EXCEPT the newest as seen. The newest
            # must remain "unseen" so the first real poll pushes it.
            newest_id = newest.get("message_id")
            for m in bootstrap_msgs:
                mid = m.get("message_id")
                if mid and mid != newest_id:
                    chat_state["seen_ids"].add(mid)
    except Exception as exc:
        if logger:
            logger.warning("Bootstrap failed for chat %s: %s", chat_id, exc)
    chat_state["bootstrapped"] = True


async def _background_poll() -> None:
    """Background polling loop — pushes inbound Teams messages to Claude Code.

    Mirrors the iMessage channel pattern: poll the data source in the
    background, push new messages via ``notifications/claude/channel``
    so Claude Code sees them without needing to call a tool.

    Iterates over ALL watched chats each cycle. Each chat has its own
    cursor and seen-set so tracking is independent.

    IMPORTANT: Uses its OWN separate tracking state so it does NOT
    interfere with watch_teams_replies. Both can detect the same message
    independently — the background poll pushes a notification, and
    watch_teams_replies returns it as a tool result. This is intentional:
    if the notification doesn't reach Claude Code, watch_teams_replies
    still works as a fallback.
    """
    import asyncio

    from entraclaw.tools.teams import filter_human_messages, read

    if logger:
        logger.info("Starting background Teams poll (interval=%ds)", BACKGROUND_POLL_INTERVAL)

    # Must match the displayName that Graph API returns in message.from.user.displayName
    # Identity-aware: filter out messages from BOTH the agent and the human user
    # depending on current identity mode
    agent_display_name = "EntraClaw Agent"

    while True:
        try:
            await asyncio.sleep(BACKGROUND_POLL_INTERVAL)

            # Skip polling if not authenticated
            if _identity and _identity.state in (
                IdentityState.UNAUTHENTICATED, IdentityState.ERROR,
            ):
                continue

            await _ensure_valid_token()

            # Snapshot chat IDs to avoid mutation during iteration
            watched = dict(_state.get("watched_chats", {}))

            for chat_id, chat_state in watched.items():
                try:
                    # Bootstrap on first encounter
                    if not chat_state.get("bootstrapped"):
                        await _bootstrap_chat(chat_id)
                        continue

                    raw_messages = await _with_token_retry(
                        read, chat_id=chat_id, count=10,
                    )
                    human_msgs = filter_human_messages(
                        raw_messages, agent_display_name,
                    )
                    new_msgs = _filter_new_messages(
                        human_msgs, chat_state["last_ts"], chat_state["seen_ids"],
                    )

                    if new_msgs:
                        newest = max(new_msgs, key=lambda m: m.get("sent_at", ""))
                        chat_state["last_ts"] = newest["sent_at"]
                        for m in new_msgs:
                            chat_state["seen_ids"].add(m["message_id"])

                        # Bounded cleanup (keep last 500)
                        if len(chat_state["seen_ids"]) > SEEN_SET_MAX:
                            chat_state["seen_ids"] = set(
                                sorted(chat_state["seen_ids"])[-100:]
                            )

                        for m in sorted(
                            new_msgs, key=lambda m: m.get("sent_at", ""),
                        ):
                            await _push_channel_notification(m, chat_id=chat_id)
                except Exception as chat_exc:
                    # One chat's failure must not starve the others in this
                    # cycle. Log and move on; the next cycle will retry.
                    if logger:
                        logger.warning(
                            "Per-chat poll error (chat_id=%s): %s: %s",
                            chat_id,
                            type(chat_exc).__name__,
                            chat_exc,
                        )

        except Exception as exc:
            if logger:
                logger.warning(
                    "Background poll error: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            await asyncio.sleep(BACKGROUND_POLL_INTERVAL)


async def _background_poll_email() -> None:
    """Background poll of /me/messages for substantive inbound email.

    Pushes each substantive message as a ``notifications/claude/channel``
    notification and appends an inbound entry to the interaction log.
    Cursor (last receivedDateTime seen) persists across restarts in
    ``<data_dir>/email_cursor.txt``; on first run we initialize it to
    "now" so the agent isn't flooded with historical mail.
    """
    import asyncio

    from entraclaw.tools.email_poll import load_cursor, poll_once, save_cursor

    if logger:
        logger.info(
            "Starting background email poll (interval=%ds)", EMAIL_POLL_INTERVAL
        )

    cursor = load_cursor()
    if cursor is None:
        cursor = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_cursor(cursor)

    # Per-session dedup. Graph returns sub-second precision on
    # receivedDateTime but the cursor file we save may end up truncated
    # to second resolution, which causes the same email to be returned
    # every poll cycle (observed 2026-04-17 with Jack Test's "Ball
    # game tonight" looping). Belt-and-suspenders: also track the
    # Graph-side message IDs we've already pushed this session so we
    # never double-push, regardless of cursor drift.
    pushed_email_ids: set[str] = set()
    _PUSHED_EMAIL_MAX = 500

    # Don't push our own outbound emails back as if they were inbound.
    # /me/messages returns the entire mailbox including the Sent Items
    # folder; emails the agent sends would otherwise loop into the
    # channel-notification stream. (Discovered 2026-04-17 when the
    # "EntraClaw email pipeline test" email I sent to Brandon got
    # echoed back as an inbound notification ~10s later.)
    agent_self_upn = (
        (_state.get("config") or get_config()).agent_user_upn or ""
    ).lower()

    while True:
        try:
            await asyncio.sleep(EMAIL_POLL_INTERVAL)

            if _identity and _identity.state in (
                IdentityState.UNAUTHENTICATED,
                IdentityState.ERROR,
            ):
                continue

            await _ensure_valid_token()

            messages, new_cursor = await _with_token_retry(
                poll_once, cursor=cursor,
            )

            if new_cursor and new_cursor != cursor:
                cursor = new_cursor
                save_cursor(cursor)

            for msg in messages:
                msg_id = msg.get("id", "")
                if msg_id and msg_id in pushed_email_ids:
                    continue
                # Skip emails the agent itself sent (Sent Items folder
                # echoes through /me/messages; would create a self-push loop).
                sender_addr = (
                    (msg.get("from") or {})
                    .get("emailAddress", {})
                    .get("address", "")
                    .lower()
                )
                if agent_self_upn and sender_addr == agent_self_upn:
                    if msg_id:
                        pushed_email_ids.add(msg_id)  # mark seen so dedup works
                    continue
                await _push_email_notification(msg)
                if msg_id:
                    pushed_email_ids.add(msg_id)

            # Bounded cleanup so the set doesn't grow unbounded over
            # long-lived sessions.
            if len(pushed_email_ids) > _PUSHED_EMAIL_MAX:
                pushed_email_ids = set(list(pushed_email_ids)[-100:])

        except Exception as exc:
            if logger:
                logger.warning("Email poll error: %s", exc)
            await asyncio.sleep(EMAIL_POLL_INTERVAL)


async def _background_discover_chats() -> None:
    """Auto-discover new chats via ``GET /me/chats`` and register them.

    Without this, chats only get added to ``watched_chats`` when something
    explicitly calls ``_register_watched_chat`` (the MCP ``create_chat``
    tool does; the raw ``entraclaw.tools.teams.create_*`` functions do NOT,
    and chats created by OTHER humans adding the Agent User to a new
    conversation never trigger the registration code at all).

    This task runs every ``CHAT_DISCOVER_INTERVAL`` seconds, enumerates
    the Agent User's chats, and registers any chat_id not already in
    ``_state["watched_chats"]``. Persists to file so restarts inherit.
    New chats pick up their cursor on the next ``_bootstrap_chat`` — no
    historical flood.
    """
    import asyncio

    import httpx

    if logger:
        logger.info(
            "Starting chat auto-discovery (interval=%ds)",
            CHAT_DISCOVER_INTERVAL,
        )

    while True:
        try:
            await asyncio.sleep(CHAT_DISCOVER_INTERVAL)

            if _identity and _identity.state in (
                IdentityState.UNAUTHENTICATED,
                IdentityState.ERROR,
            ):
                continue

            await _ensure_valid_token()
            token = _state.get("token")
            if not token:
                continue

            new_count = 0
            async with httpx.AsyncClient() as client:
                # NOTE: $orderby on /me/chats 400s — just fetch and sort client-side if needed
                resp = await client.get(
                    "https://graph.microsoft.com/v1.0/me/chats",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"$top": "50"},
                )
                if resp.status_code != 200:
                    if logger:
                        logger.warning(
                            "Chat auto-discovery: /me/chats returned %d",
                            resp.status_code,
                        )
                    continue

                watched = _state.get("watched_chats", {})
                for chat in resp.json().get("value", []):
                    cid = chat.get("id")
                    if not cid or cid in watched:
                        continue
                    _register_watched_chat(cid, persist=True)
                    new_count += 1

            if new_count and logger:
                logger.info(
                    "Chat auto-discovery: registered %d new chat(s)",
                    new_count,
                )

        except Exception as exc:
            if logger:
                logger.warning(
                    "Chat auto-discovery error: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            await asyncio.sleep(CHAT_DISCOVER_INTERVAL)


async def _push_email_notification(msg: dict) -> None:
    """Push an inbound email to Claude Code and record it in the log."""
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    sender_addr = sender.get("address", "unknown")
    sender_name = sender.get("name") or sender_addr
    subject = msg.get("subject") or "(no subject)"
    preview = msg.get("bodyPreview") or ""
    received = msg.get("receivedDateTime", "")
    message_id = msg.get("id", "")
    encrypted = msg.get("_encrypted") is True

    # Render sender as "name (addr)" rather than "name <addr>" — the
    # angle-bracket form reads as an unknown HTML tag to strict parsers on
    # the receiving side, which was silently closing the MCP stream when
    # we pushed the notification (observed 2026-04-17: server shut down
    # clean-EOF immediately after every email push).
    if encrypted:
        content = (
            f"[email · encrypted] {sender_name} ({sender_addr}) — {subject}\n"
            f"(Purview-encrypted; body inaccessible without IRM decryption)"
        )
    else:
        content = (
            f"[email] {sender_name} ({sender_addr}) — {subject}\n{preview[:400]}"
        )

    write_stream = _state.get("_write_stream")
    if write_stream:
        # Mirror the Teams-push schema exactly — same top-level keys in meta,
        # no email-specific extras. Client-side channel notification handlers
        # appear sensitive to unexpected fields; keep the shape identical
        # across sources and carry email-specific bits in content instead.
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={
                "content": content,
                "meta": {
                    "chat_id": "email",  # synthetic — marks this as email-channel
                    "message_id": message_id,
                    "user": sender_addr,
                    "ts": received,
                },
            },
        )
        session_message = SessionMessage(message=JSONRPCMessage(notification))
        try:
            await write_stream.send(session_message)
        except Exception as exc:
            # Don't let a transport failure take down the poll loop.
            # Matches the swallow-and-log pattern from _push_channel_notification.
            if logger:
                logger.warning(
                    "Email push failed for %s: %s: %s",
                    message_id or "?",
                    type(exc).__name__,
                    exc,
                )
    elif logger:
        logger.warning("Cannot push email notification — write stream not available")

    _log_interaction_safe(
        channel="email",
        direction="inbound",
        sender=sender_addr,
        recipient="entraclaw-agent",
        summary=f"{subject} — {preview[:120]}".strip(" \u2014"),
        action="email_received",
        content_ref=message_id,
        metadata={
            "subject": subject,
            "conversationId": msg.get("conversationId"),
            "encrypted": encrypted,
            "ts": received,
        },
    )

    if logger:
        logger.info("Pushed email from %s: %s", sender_addr, subject[:60])


def _log_interaction_safe(**kwargs) -> None:
    """Best-effort wrapper around log_interaction.

    Never raises — logging failures must not break the primary send/receive
    path. Logged at warning level if it does fail.
    """
    try:
        log_interaction(**kwargs)
    except Exception as exc:  # pragma: no cover — defensive
        if logger:
            logger.warning("interaction log failed: %s", exc)


async def _run_daily_summary_internal(
    *, day: str | None = None, send: bool = True
) -> dict:
    """Read today's log → triage → render → archive → optionally send."""
    from entraclaw.tools.daily_summary import (
        archive_summary,
        render_summary_html,
        send_summary_email,
        triage_interactions,
    )
    from entraclaw.tools.interaction_log import read_day

    config = _state.get("config") or get_config()
    target_day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    entries = read_day(target_day)
    buckets = triage_interactions(entries, agent_upn=config.agent_user_upn)
    html = render_summary_html(buckets, day=target_day)
    archive_path = archive_summary(day=target_day, html=html, buckets=buckets)

    sent_to: list[str] = []
    if send and config.human_user_mails:
        await _ensure_valid_token()
        recipients = [config.human_user_mails[0]]  # primary sponsor
        await _with_token_retry(
            send_summary_email,
            html=html,
            subject=f"Daily summary — {target_day}",
            to=recipients,
        )
        sent_to = recipients
        # Record the outbound summary in the log itself.
        _log_interaction_safe(
            channel="email",
            direction="outbound",
            sender="entraclaw-agent",
            recipient=recipients[0],
            summary=f"Daily summary — {target_day}",
            action="daily_summary_sent",
            content_ref=str(archive_path),
            metadata={
                "counts": {k: len(v) for k, v in buckets.items()},
                "day": target_day,
            },
        )

    return {
        "day": target_day,
        "counts": {k: len(v) for k, v in buckets.items()},
        "archive": str(archive_path),
        "sent_to": sent_to,
    }


async def _background_daily_summary() -> None:
    """Wake at 5pm PDT each day and send the daily summary."""
    import asyncio

    from entraclaw.tools.daily_summary import next_run_at

    if logger:
        logger.info("Starting daily summary scheduler")

    while True:
        try:
            nxt = next_run_at(now=datetime.now(UTC))
            delay = max((nxt - datetime.now(UTC)).total_seconds(), 60.0)
            if logger:
                logger.info(
                    "Next daily summary at %s UTC (%.0fs)",
                    nxt.isoformat(),
                    delay,
                )
            await asyncio.sleep(delay)

            if _identity and _identity.state in (
                IdentityState.UNAUTHENTICATED,
                IdentityState.ERROR,
            ):
                continue

            result = await _run_daily_summary_internal(send=True)
            if logger:
                logger.info("Daily summary sent: %s", result)

        except Exception as exc:
            if logger:
                logger.warning("Daily summary scheduler error: %s", exc)
            await asyncio.sleep(3600)  # back off for an hour on failure


def _summarize_content(content: str, limit: int = 200) -> str:
    """Strip HTML and truncate — used when the caller didn't supply a summary."""
    import re

    text = re.sub(r"<[^>]+>", " ", content or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


async def _push_channel_notification(
    message: dict, *, chat_id: str | None = None,
) -> None:
    """Observe + push an inbound Teams message.

    Two concerns here, in order:

    1. **Observe** — always write to the interaction log first. Daily
       summaries must see every inbound message even when push transport
       is broken or no MCP client is attached. (Historical bug:
       interaction logging was gated behind the write-stream check and
       we lost visibility into inbound DMs entirely when push failed.)

    2. **Push** — notify Claude Code via ``notifications/claude/channel``
       so the inbound message surfaces in the conversation. Transport
       errors are swallowed — this path is observability, not primary,
       and the interaction log already has the record.
    """
    resolved_chat_id = chat_id or str(_state.get("chat_id", ""))

    _log_interaction_safe(
        channel=detect_channel(resolved_chat_id),
        direction="inbound",
        sender=message.get("from", "unknown"),
        recipient="entraclaw-agent",
        summary=_summarize_content(message.get("content", "")),
        action="push_channel_notification",
        content_ref=message.get("message_id"),
        metadata={
            "chat_id": resolved_chat_id,
            "ts": message.get("sent_at"),
        },
    )

    # Push is Claude-Code-only. Slaves (Copilot CLI, etc.) have no
    # ``notifications/claude/channel`` to receive on. Observation above
    # is unconditional so daily summaries still see everything.
    #
    # This runs in a detached asyncio task (_background_poll), so
    # _current_host() returns "unknown"; _is_leader_host() falls back to
    # _state["cached_host"], populated by every prior tool entry.
    if not _is_leader_host():
        if logger:
            logger.debug(
                "Skipping channel push for %s — host is slave (live=%s cached=%s)",
                message.get("message_id", "?"),
                _current_host(),
                _state.get("cached_host", ""),
            )
        return

    write_stream = _state.get("_write_stream")
    if not write_stream:
        if logger:
            logger.warning(
                "Channel push skipped — write stream not available (logged inbound %s from %s)",
                message.get("message_id", "?"),
                message.get("from", "?"),
            )
        return

    meta: dict = {
        "chat_id": resolved_chat_id,
        "message_id": message.get("message_id", ""),
        "user": message.get("from", "unknown"),
        "ts": message.get("sent_at", ""),
    }

    # Quote-reply enrichment: when Teams' Reply UI quotes a prior message,
    # Graph encodes the source as <attachment id=...> inline in body HTML
    # (parsed into reply_to_ids by read()). Forward the IDs and fetch each
    # quoted body so the agent has context without a tool round-trip.
    # Fail-open: a failed fetch must never block the primary push.
    reply_to_ids = message.get("reply_to_ids") or []
    if reply_to_ids:
        import asyncio

        meta["reply_to_ids"] = list(reply_to_ids)
        token = _state.get("token") or (_identity.session.token if _identity else None)
        quoted: list[dict] = []
        if token:
            results = await asyncio.gather(
                *(
                    fetch_message(
                        chat_id=resolved_chat_id,
                        message_id=rid,
                        token=str(token),
                    )
                    for rid in reply_to_ids
                ),
                return_exceptions=True,
            )
            for rid, r in zip(reply_to_ids, results, strict=False):
                if isinstance(r, BaseException):
                    if logger:
                        logger.warning(
                            "fetch_message failed for quoted id %s: %s: %s",
                            rid,
                            type(r).__name__,
                            r,
                        )
                    continue
                if r is not None:
                    quoted.append(r)
        meta["quoted_messages"] = quoted

    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={
            "content": message.get("content", ""),
            "meta": meta,
        },
    )
    session_message = SessionMessage(message=JSONRPCMessage(notification))
    try:
        await write_stream.send(session_message)
    except Exception as exc:
        if logger:
            logger.warning(
                "Channel push failed for %s (%s): %s: %s",
                message.get("message_id", "?"),
                detect_channel(resolved_chat_id),
                type(exc).__name__,
                exc,
            )
        return

    if logger:
        logger.info(
            "Pushed Teams message from %s: %s",
            message.get("from", "?"),
            message.get("content", "")[:50],
        )


@mcp.tool()
async def send_teams_message(
    message: str,
    content_type: str = "text",
    mentions: list[dict] | None = None,
    chat_id: str = "",
) -> str:
    """Send a message via Microsoft Teams.

    You must pass ``chat_id`` — every Teams chat has its own ID. Get one
    from ``create_chat`` (for a new 1:1 DM) or from the ``meta.chat_id``
    of a channel notification that the background poll pushed to you.

    After calling this, you don't need to call watch_teams_replies —
    the background poll pushes replies automatically via the channel
    notification for every watched chat.

    To @mention someone in the message, use HTML content_type with
    ``<at id="N">Display Name</at>`` tags in the message body, and pass
    a mentions list. Each mention dict needs:
      - id: int matching the at-tag id
      - name: display name
      - user_id: their Entra user GUID (get from chat members via read_teams_messages)

    Example — DM someone:
      chat_id = await create_chat(target_email="alice@example.com")
      await send_teams_message("Hey Alice", chat_id=chat_id)

    Example — @mention in a chat:
      message: '<at id="0">Alice Example</at> check this out'
      content_type: "html"
      mentions: [{"id": 0, "name": "Alice Example", "user_id": "abc-123"}]
      chat_id: "19:...@thread.v2"

    Args:
        message: The text to send.
        content_type: "text" (default) or "html" for rich formatting.
        mentions: Optional list of mention dicts for @mentions.
        chat_id: The chat to send to. Required.

    Returns:
        JSON with message_id and sent_at timestamp.
    """
    await _initialize()

    config = _state.get("config")

    # Bot mode: write to outbound.jsonl for the bot server to pick up
    if config and config.mode == "bot":
        from entraclaw.bot.handler import write_outbound

        outbound_msg = {
            "content": message,
            "content_type": content_type,
            "chat_id": chat_id or "",
        }
        if mentions:
            outbound_msg["mentions"] = mentions
        write_outbound(outbound_msg)
        return json.dumps({
            "message_id": f"bot-outbound-{id(outbound_msg)}",
            "sent_at": datetime.now(UTC).isoformat(),
            "mode": "bot",
        }, indent=2)

    from entraclaw.tools.teams import send

    target_chat = chat_id
    if not target_chat:
        return json.dumps({
            "error": (
                "chat_id is required — pass the chat_id of the target Teams "
                "chat (create one via create_chat if needed)."
            )
        })

    await _ensure_valid_token()

    # In delegated mode the message comes from the human's identity,
    # so prefix it to distinguish agent-sent messages from the human.
    prefix = None
    if _identity and _identity.session.auth_mode == "delegated":
        prefix = "[EntraClaw]"

    result = await _with_token_retry(
        send,
        chat_id=str(target_chat),
        message=message,
        content_type=content_type,
        mentions=mentions,
        prefix=prefix,
    )

    # Log the outbound message for the daily summary.
    _log_interaction_safe(
        channel=detect_channel(str(target_chat)),
        direction="outbound",
        sender="entraclaw-agent",
        recipient=str(target_chat),
        summary=_summarize_content(message),
        action="send_teams_message",
        content_ref=result.get("message_id") if isinstance(result, dict) else None,
        metadata={
            "content_type": content_type,
            "had_mentions": bool(mentions),
        },
    )

    # Slave-mode disclosure: no channel push means the reply won't arrive
    # in this host's turn. The model reads this and tells the user.
    notice = _slave_disclosure_suffix()
    if notice and isinstance(result, dict):
        result = dict(result)
        result["notice"] = notice

    return json.dumps(result, indent=2)


@mcp.tool()
async def post_thinking_placeholder(
    chat_id: str,
    text: str = "thinking…",
) -> str:
    """Post a short placeholder so humans see the agent was triggered.

    Use this when you've decided to answer a Teams chat and the real
    reply will take real work (tool calls, investigation, a sub-agent
    run). Resolve with ``resolve_placeholder`` when the reply is ready.
    Skip for purely conversational turns — a one-liner doesn't need
    a placeholder.

    Args:
        chat_id: The chat to post into. Required.
        text: Placeholder text (default "thinking…"). Kept italic + low-key.

    Returns:
        JSON with ``message_id`` of the placeholder (pass this to
        ``resolve_placeholder``).
    """
    await _initialize()

    if not chat_id:
        return json.dumps({
            "error": (
                "chat_id is required — pass the chat_id of the target "
                "Teams chat."
            )
        })

    from entraclaw.tools.teams import post_thinking_placeholder as _post

    await _ensure_valid_token()
    message_id = await _with_token_retry(
        _post,
        chat_id=chat_id,
        text=text,
    )

    _log_interaction_safe(
        channel=detect_channel(chat_id),
        direction="outbound",
        sender="entraclaw-agent",
        recipient=chat_id,
        summary=f"placeholder: {text}",
        action="post_thinking_placeholder",
        content_ref=message_id,
        metadata={"placeholder": True},
    )

    return json.dumps({"message_id": message_id}, indent=2)


@mcp.tool()
async def update_placeholder(
    chat_id: str,
    placeholder_id: str,
    progress_text: str,
) -> str:
    """Patch a thinking placeholder with a short italic progress note.

    Middle stage of the three-part placeholder flow:
      1. ``post_thinking_placeholder`` — ack the human immediately.
      2. ``update_placeholder`` (zero or more) — surface what you're
         doing so the human sees work-in-progress, not a frozen
         placeholder.
      3. ``resolve_placeholder`` — commit the final answer (audit-logged).

    Unlike ``resolve_placeholder``, this is NOT a final commitment:
    no audit event, no fallback-to-new-message on Graph failure.
    Progress updates are best-effort; a failed PATCH here is logged
    and reported as ``mode="edit_failed"``, but no alternate message
    is posted to avoid a spurious ping. The eventual
    ``resolve_placeholder`` handles the real fallback.

    Use one short phrase per call — "reading the interaction log",
    "grepping docs", "drafting reply". One line; italic.

    Args:
        chat_id: The chat holding the placeholder. Required.
        placeholder_id: The message_id returned by
            ``post_thinking_placeholder``.
        progress_text: One short progress phrase. Kept italic.

    Returns:
        JSON with ``message_id`` and ``mode`` (``edit`` on success,
        ``edit_failed`` when the PATCH did not land).
    """
    await _initialize()

    if not chat_id or not placeholder_id:
        return json.dumps({
            "error": "chat_id and placeholder_id are required."
        })

    from entraclaw.tools.teams import update_placeholder as _update

    await _ensure_valid_token()
    result = await _with_token_retry(
        _update,
        chat_id=chat_id,
        placeholder_id=placeholder_id,
        progress_text=progress_text,
    )

    _log_interaction_safe(
        channel=detect_channel(chat_id),
        direction="outbound",
        sender="entraclaw-agent",
        recipient=chat_id,
        summary=f"progress: {progress_text}",
        action="update_placeholder",
        content_ref=result.get("message_id") if isinstance(result, dict) else None,
        metadata={
            "placeholder": True,
            "progress": True,
            "placeholder_id": placeholder_id,
            "mode": result.get("mode") if isinstance(result, dict) else "edit",
        },
    )

    return json.dumps(result, indent=2)


@mcp.tool()
async def resolve_placeholder(
    chat_id: str,
    placeholder_id: str,
    final_message: str,
    content_type: str = "html",
    mentions: list[dict] | None = None,
    mode: str = "edit",
) -> str:
    """Replace a thinking placeholder with the final message.

    Pair with ``post_thinking_placeholder``. Modes:
      - ``edit`` (default, quieter): PATCH the placeholder in place.
      - ``delete_repost``: soft-delete the placeholder and send a fresh
        message. Use when a fresh ping matters (long sub-agent runs,
        multi-minute investigations).

    On Graph failure, falls back to posting the final as a NEW message
    and reports ``mode="fallback_new"`` so you can see the degradation.

    Args:
        chat_id: The target chat. Required.
        placeholder_id: The message_id returned by post_thinking_placeholder.
        final_message: The final reply (HTML per channel-discipline).
        content_type: "html" (default) or "text".
        mentions: Graph-shape mention dicts if the final reply @-mentions.
        mode: "edit" or "delete_repost".

    Returns:
        JSON with ``message_id`` and ``mode`` (one of edit, delete_repost,
        fallback_new).
    """
    await _initialize()

    if not chat_id or not placeholder_id:
        return json.dumps({
            "error": "chat_id and placeholder_id are required."
        })
    if mode not in ("edit", "delete_repost"):
        return json.dumps({
            "error": f"invalid mode: {mode!r} (expected 'edit' or 'delete_repost')"
        })

    # Audit before mutating — per security.md "Audit before acting". Fail
    # closed: if the audit write raises, the Graph call does not proceed.
    from entraclaw.tools.audit import log_event
    config = get_config()
    if _identity:
        agent_id = (
            _identity.session.user_id or config.agent_id
            or config.blueprint_app_id or "unknown"
        )
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id or "unknown"
        attribution = "agent"
    log_event(
        action="resolve_placeholder",
        resource=f"{chat_id}:{placeholder_id}",
        outcome="pending",
        agent_id=agent_id,
        metadata={"mode": mode, "content_type": content_type},
        attribution_type=attribution,
    )

    from entraclaw.tools.teams import resolve_placeholder as _resolve

    await _ensure_valid_token()
    result = await _with_token_retry(
        _resolve,
        chat_id=chat_id,
        placeholder_id=placeholder_id,
        final_message=final_message,
        content_type=content_type,
        mentions=mentions,
        mode=mode,
    )

    _log_interaction_safe(
        channel=detect_channel(chat_id),
        direction="outbound",
        sender="entraclaw-agent",
        recipient=chat_id,
        summary=_summarize_content(final_message),
        action="resolve_placeholder",
        content_ref=result.get("message_id") if isinstance(result, dict) else None,
        metadata={
            "mode": result.get("mode") if isinstance(result, dict) else mode,
            "requested_mode": mode,
            "placeholder_id": placeholder_id,
            "content_type": content_type,
            "had_mentions": bool(mentions),
        },
    )

    return json.dumps(result, indent=2)


@mcp.tool()
async def delete_teams_message(
    message_id: str,
    chat_id: str = "",
) -> str:
    """Soft-delete one of the agent's own Teams messages.

    Use when a human asks you to delete a message you sent. Graph
    replaces the body with a "this message has been deleted" tombstone
    visible to chat participants — the message id stays, but the content
    is gone. You can only delete messages the Agent User itself sent;
    Graph returns 403 on anyone else's, and that's the right failure
    mode.

    Prefer this over the ``resolve_placeholder`` delete-repost path when
    the intent is "just remove the message," not "swap a placeholder for
    a final reply."

    Args:
        message_id: The Teams message_id to delete.
        chat_id: The chat the message lives in. Required.

    Returns:
        JSON with ``{"deleted": true, "message_id": ...}`` on success, or
        ``{"deleted": false, "reason": "..."}`` on Graph failure.
    """
    await _initialize()

    if not chat_id:
        return json.dumps({
            "error": (
                "chat_id is required — pass the chat_id of the target "
                "Teams chat."
            )
        })
    if not message_id:
        return json.dumps({
            "error": (
                "message_id is required — pass the message_id of the "
                "agent's own message to delete."
            )
        })

    # Audit before mutating — per security.md "Audit before acting". Fail
    # closed: if the audit write raises, the Graph call does not proceed.
    from entraclaw.tools.audit import log_event
    config = get_config()
    if _identity:
        agent_id = (
            _identity.session.user_id or config.agent_id
            or config.blueprint_app_id or "unknown"
        )
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id or "unknown"
        attribution = "agent"
    log_event(
        action="delete_teams_message",
        resource=f"{chat_id}:{message_id}",
        outcome="pending",
        agent_id=agent_id,
        metadata={},
        attribution_type=attribution,
    )

    from entraclaw.tools.teams import delete_chat_message

    await _ensure_valid_token()
    deleted = await _with_token_retry(
        delete_chat_message,
        chat_id=chat_id,
        message_id=message_id,
    )

    _log_interaction_safe(
        channel=detect_channel(chat_id),
        direction="outbound",
        sender="entraclaw-agent",
        recipient=chat_id,
        summary=f"deleted message {message_id}" if deleted
        else f"delete failed for {message_id}",
        action="delete_teams_message",
        content_ref=message_id,
        metadata={
            "chat_id": chat_id,
            "message_id": message_id,
            "deleted": bool(deleted),
        },
    )

    if deleted:
        return json.dumps(
            {"deleted": True, "message_id": message_id}, indent=2
        )
    return json.dumps(
        {
            "deleted": False,
            "message_id": message_id,
            "reason": (
                "Graph softDelete returned a non-2xx status "
                "(likely 403 not-owner or 404 not-found). "
                "See audit + interaction log."
            ),
        },
        indent=2,
    )


def _split_addrs(raw: str) -> list[str]:
    """Split a comma-separated address string, strip, drop empties."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@mcp.tool()
async def send_email(
    to: str,
    subject: str,
    body: str,
    content_type: str = "html",
    cc: str = "",
    bcc: str = "",
    reply_to_message_id: str = "",
) -> str:
    """Send an email from the Agent User's mailbox.

    Use when channel-discipline says email-in → email-out, or when
    initiating correspondence the Sponsor specifically routed to email.
    When replying to a known inbound, pass ``reply_to_message_id`` so
    Graph preserves the thread headers — it uses the original message's
    subject, so any subject you pass here is informational only.

    Args:
        to: Comma-separated email addresses (single is fine).
        subject: Subject line (ignored by Graph on replies).
        body: Message body (default HTML — match Teams convention).
        content_type: "html" or "text".
        cc: Optional comma-separated CC addresses.
        bcc: Optional comma-separated BCC addresses.
        reply_to_message_id: If set, reply to that message_id (preserves
            thread). Omit for a new thread.

    Returns:
        JSON with ``{"sent_at": "..."}`` on success, or ``{"error": "..."}``
        on a validation/Graph failure.
    """
    await _initialize()

    to_list = _split_addrs(to)
    cc_list = _split_addrs(cc)
    bcc_list = _split_addrs(bcc)

    if not to_list:
        return json.dumps({
            "error": (
                "to is required — pass a comma-separated list of email addresses."
            )
        })
    if not subject or not subject.strip():
        # Graph accepts empty subjects on replies, but reject uniformly —
        # audit resource needs something to key on, and a blank subject
        # is almost always a programming error.
        return json.dumps({"error": "subject is required."})

    # Audit before mutating — per security.md "Audit before acting". Fail
    # closed: if the audit write raises, the Graph call does not proceed.
    from entraclaw.tools.audit import log_event
    config = get_config()
    if _identity:
        agent_id = (
            _identity.session.user_id or config.agent_id
            or config.blueprint_app_id or "unknown"
        )
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id or "unknown"
        attribution = "agent"
    log_event(
        action="send_email",
        resource=f"to={','.join(to_list)} subject={subject}",
        outcome="pending",
        agent_id=agent_id,
        metadata={
            "content_type": content_type,
            "has_cc": bool(cc_list),
            "has_bcc": bool(bcc_list),
            "is_reply": bool(reply_to_message_id),
        },
        attribution_type=attribution,
    )

    from entraclaw.tools.email import EmailSendError
    from entraclaw.tools.email import send_email as _send

    await _ensure_valid_token()

    try:
        result = await _with_token_retry(
            _send,
            to=to_list,
            subject=subject,
            body=body,
            content_type=content_type,
            cc=cc_list or None,
            bcc=bcc_list or None,
            reply_to_message_id=reply_to_message_id or None,
        )
    except EmailSendError as exc:
        _log_interaction_safe(
            channel="email",
            direction="outbound",
            sender="entraclaw-agent",
            recipient=to_list[0],
            summary=_summarize_content(subject),
            action="send_email",
            content_ref=reply_to_message_id or None,
            metadata={
                "to": to_list,
                "cc": cc_list,
                "bcc": bcc_list,
                "subject": subject,
                "content_type": content_type,
                "reply_to_message_id": reply_to_message_id or None,
                "outcome": "failure",
                "error": str(exc),
            },
        )
        return json.dumps({"error": f"email send failed: {exc}"})

    _log_interaction_safe(
        channel="email",
        direction="outbound",
        sender="entraclaw-agent",
        recipient=to_list[0],
        summary=_summarize_content(subject),
        action="send_email",
        content_ref=reply_to_message_id or None,
        metadata={
            "to": to_list,
            "cc": cc_list,
            "bcc": bcc_list,
            "subject": subject,
            "content_type": content_type,
            "reply_to_message_id": reply_to_message_id or None,
            "outcome": "success",
        },
    )

    return json.dumps(result, indent=2)


@mcp.tool()
async def send_card(
    card_type: str,
    chat_id: str = "",
    title: str = "",
    status: str = "complete",
    detail: str = "",
    duration: str = "",
    passed: bool = True,
    summary: str = "",
    details_text: str = "",
    extra: str = "",
) -> str:
    """Send a rich Adaptive Card to a Teams chat.

    Use this to send visually structured status updates, tool activity,
    or build results. Cards render natively in Teams with proper layout.

    Card types:
    - **tool_activity**: Show a tool running/completing (e.g., reading files,
      running git). Pass tool_name via ``title``, ``status``, and ``detail``.
    - **task_status**: Show task progress. Pass task name via ``title``,
      ``status`` (in_progress/complete/error), ``duration``, and optional
      ``extra`` as JSON dict of key-value details.
    - **build_result**: Show pass/fail with details. Pass ``passed``,
      ``summary``, and optional ``details_text``.

    Args:
        card_type: One of "tool_activity", "task_status", "build_result".
        chat_id: Target chat. Required — pass the chat_id of the chat to send the card to.
        title: Tool name or task name (for tool_activity and task_status).
        status: "running", "complete", "error", or "in_progress".
        detail: Short description (for tool_activity).
        duration: Human-readable duration (for task_status).
        passed: True/False (for build_result).
        summary: One-line summary (for build_result).
        details_text: Multi-line details (for build_result).
        extra: JSON string of extra key-value pairs (for task_status details).

    Returns:
        JSON with message_id and sent_at.
    """
    await _initialize()
    from entraclaw.tools.cards import (
        build_result_card,
        card_attachment,
        task_status_card,
        tool_activity_card,
    )
    from entraclaw.tools.teams import send

    if card_type == "tool_activity":
        card = tool_activity_card(
            tool_name=title or "tool",
            status=status,
            detail=detail,
        )
    elif card_type == "task_status":
        extra_dict = None
        if extra:
            try:
                extra_dict = json.loads(extra)
            except json.JSONDecodeError:
                extra_dict = None
        card = task_status_card(
            task=title or "Task",
            status=status,
            duration=duration,
            details=extra_dict,
        )
    elif card_type == "build_result":
        card = build_result_card(
            passed=passed,
            summary=summary or "Build result",
            details=details_text or None,
        )
    else:
        return json.dumps({"error": f"Unknown card_type: {card_type}"})

    attachment = card_attachment(card)

    target_chat = chat_id
    if not target_chat:
        return json.dumps({
            "error": (
                "chat_id is required — pass the chat_id of the target Teams "
                "chat (create one via create_chat if needed)."
            )
        })

    await _ensure_valid_token()

    # Cards deliberately don't carry the [EntraClaw] prefix — the card
    # body itself already signals agent origin, and prefixing the
    # `<attachment id="card1"></attachment>` placeholder is wrong.

    result = await _with_token_retry(
        send,
        chat_id=str(target_chat),
        message='<attachment id="card1"></attachment>',
        content_type="html",
        prefix=None,
        attachments=[attachment],
    )

    # Log the outbound card for the daily summary.
    _log_interaction_safe(
        channel=detect_channel(str(target_chat)),
        direction="outbound",
        sender="entraclaw-agent",
        recipient=str(target_chat),
        summary=f"card:{card_type} — {(title or summary or detail)[:80]}",
        action="send_card",
        content_ref=result.get("message_id") if isinstance(result, dict) else None,
        metadata={"card_type": card_type, "status": status},
    )

    return json.dumps(result, indent=2)


@mcp.tool()
async def list_chat_members(chat_id: str) -> str:
    """List all members of a Teams chat with their user IDs.

    Pass the chat_id of the chat you want member info for (e.g., a DM you
    created with create_chat, or from a channel notification's meta.chat_id).

    Use this to resolve display names to user GUIDs for @mentions in
    send_teams_message. Returns user_id, name, email, and roles for
    each member.

    Args:
        chat_id: The chat to list members of. Required.

    Returns:
        JSON array of chat members.
    """
    await _initialize()
    from entraclaw.tools.teams import list_members

    target_chat = chat_id
    if not target_chat:
        return json.dumps({
            "error": (
                "chat_id is required — pass the chat_id of the chat whose "
                "members you want to list."
            )
        })

    await _ensure_valid_token()
    result = await _with_token_retry(
        list_members,
        chat_id=str(target_chat),
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def add_teams_member(
    email: str, chat_id: str, tenant_id: str = ""
) -> str:
    """Add a new member to a Teams chat.

    Just provide the email address and the chat_id. For external users
    (different org), the tenant is auto-resolved from the email domain.

    Args:
        email: The user's email address (e.g., 'user@example.com').
        chat_id: The chat to add the member to. Required.
        tenant_id: Optional override. Auto-resolved from email domain if empty.

    Returns:
        JSON with member_id, display_name, and roles.
    """
    await _initialize()
    from entraclaw.tools.teams import add_member

    if not chat_id:
        return json.dumps({
            "error": (
                "chat_id is required — pass the chat_id of the target Teams "
                "chat (create one via create_chat if needed)."
            )
        })

    # Auto-resolve tenant ID from email domain if not provided
    if not tenant_id and "@" in email:
        config = _state.get("config")
        our_domain = ""
        if config and config.agent_user_upn and "@" in config.agent_user_upn:
            our_domain = config.agent_user_upn.split("@")[1]
        resolved = await _resolve_tenant_id(email, our_domain)
        if resolved:
            tenant_id = resolved

    await _ensure_valid_token()
    result = await _with_token_retry(
        add_member,
        chat_id=chat_id,
        email=email,
        tenant_id=tenant_id or None,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def create_chat(target_email: str, target_tenant_id: str = "") -> str:
    """Create a 1:1 private DM with a user by email.

    **Use this when the human asks you to DM, message privately, or
    start a 1:1 conversation with someone.**

    Returns a chat_id you can pass to send_teams_message,
    read_teams_messages, and list_chat_members to operate on that chat.

    The new chat is automatically registered for background polling —
    replies will push to you via channel notifications. Registration
    persists across MCP server restarts.

    Graph's oneOnOne chat creation is idempotent — calling this twice
    with the same email returns the existing chat, not a duplicate.

    For cross-tenant users (different org), the target_tenant_id is
    auto-resolved from the email domain. Just pass the email.

    Example:
      result = await create_chat(target_email="alice@example.com")
      chat_id = json.loads(result)["chat_id"]
      await send_teams_message("Hey Alice, private note", chat_id=chat_id)

    Args:
        target_email: The user's email address (e.g., 'alice@example.com').
        target_tenant_id: Optional home tenant GUID override. Usually
            auto-resolved from the email domain — only pass this if
            auto-resolution fails.

    Returns:
        JSON with chat_id and created_at.
    """
    await _initialize()
    from entraclaw.tools.teams import create_one_on_one_chat

    # Auto-resolve tenant ID from email domain if not provided
    if not target_tenant_id and "@" in target_email:
        config = _state.get("config")
        our_domain = ""
        if config and config.agent_user_upn and "@" in config.agent_user_upn:
            our_domain = config.agent_user_upn.split("@")[1]
        resolved = await _resolve_tenant_id(target_email, our_domain)
        if resolved:
            target_tenant_id = resolved

    await _ensure_valid_token()
    result = await _with_token_retry(
        create_one_on_one_chat,
        target_email=target_email,
        target_tenant_id=target_tenant_id or None,
        agent_user_id=_effective_user_id(),
    )

    # Auto-register the new chat for background polling
    new_chat_id = result.get("chat_id")
    if new_chat_id:
        _register_watched_chat(new_chat_id)

    return json.dumps(result, indent=2)


@mcp.tool()
async def read_teams_messages(chat_id: str, count: int = 5) -> str:
    """Read recent messages from a Microsoft Teams chat.

    Pass the chat_id of the chat you want to read — e.g., a DM you
    created with create_chat, or the meta.chat_id from a channel
    notification.

    Authentication is automatic. No credentials needed.

    Args:
        chat_id: The chat to read from. Required.
        count: Number of messages to return (default 5, max ~50).

    Returns:
        JSON array of messages, each with message_id, from, content,
        sent_at, reply_to_ids, and attachments (list of {id, content_type,
        content_url, name, thumbnail_url}; empty when the message has
        none). Use attachments[].content_url with view_image to resolve
        inline images referenced by ``<attachment id="UUID">`` tags in
        content.
    """
    await _initialize()
    from entraclaw.tools.teams import read

    target_chat = chat_id
    if not target_chat:
        return json.dumps({
            "error": (
                "chat_id is required — pass the chat_id of the chat to read "
                "from (create one via create_chat if needed)."
            )
        })

    await _ensure_valid_token()
    result = await _with_token_retry(
        read,
        chat_id=str(target_chat),
        count=count,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def watch_teams_replies(
    chat_id: str,
    timeout: int = 30,
    interval: int = 5,
    ctx: Context | None = None,
) -> str:
    """Poll Teams for new replies from the human in a specific chat.

    Returns when new messages arrive or after timeout seconds. Uses a
    server-side cursor to track what's been seen — only returns genuinely
    new human messages.

    WHEN TO CALL: Always after send_teams_message. This completes the
    bidirectional loop — send a message, then watch for the reply.

    If timed_out is true, the human hasn't replied yet. You can call this
    again with a longer timeout, or move on and check back later.

    Args:
        chat_id: The chat to watch. Required — pass the chat_id of the
            Teams conversation you want to watch (e.g. from create_chat).
        timeout: Max seconds to poll before returning empty (default 30).
        interval: Seconds between poll iterations (default 5).

    Returns:
        JSON with messages (list), timed_out (bool), and poll_count (int).
    """
    import asyncio

    await _initialize()
    from entraclaw.tools.teams import filter_human_messages, read

    if not chat_id:
        return json.dumps({
            "error": (
                "chat_id is required — pass the chat_id of the Teams chat "
                "to watch (create one via create_chat if needed)."
            )
        })

    # Must match the displayName that Graph API returns in message.from.user.displayName
    # NOT the UPN — Graph returns "EntraClaw Agent", not "entraclaw-agent@werner.ac"
    agent_display_name = "EntraClaw Agent"

    # Bootstrap cursor on first call: fetch latest messages, set cursor to newest
    if _state.get("last_seen_timestamp") is None:
        await _ensure_valid_token()
        bootstrap_msgs = await _with_token_retry(
            read,
            chat_id=str(chat_id),
            count=10,
        )
        if bootstrap_msgs:
            newest = max(bootstrap_msgs, key=lambda m: m.get("sent_at", ""))
            _state["last_seen_timestamp"] = newest["sent_at"]
            for m in bootstrap_msgs:
                _state["seen_message_ids"].add(m["message_id"])
                _state["seen_id_timestamps"][m["message_id"]] = m.get("sent_at", "")

    start = time.monotonic()
    poll_count = 0

    while True:
        poll_count += 1
        await _ensure_valid_token()

        # Report progress so the LLM knows we're actively polling
        if ctx:
            try:
                elapsed = int(time.monotonic() - start)
                await ctx.report_progress(
                    progress=float(elapsed),
                    total=float(timeout),
                    message=f"Polling for Teams replies... ({elapsed}s / {timeout}s)",
                )
            except Exception:
                pass  # Progress reporting is best-effort

        raw_messages = await _with_token_retry(
            read,
            chat_id=str(chat_id),
            count=10,
        )

        # Client-side filtering: human only, then dedup
        human_msgs = filter_human_messages(raw_messages, agent_display_name)
        new_msgs = _filter_new_messages(
            human_msgs,
            _state.get("last_seen_timestamp"),
            _state["seen_message_ids"],
        )

        if new_msgs:
            # Advance cursor and update seen-set
            newest = max(new_msgs, key=lambda m: m.get("sent_at", ""))
            _state["last_seen_timestamp"] = newest["sent_at"]
            for m in new_msgs:
                _state["seen_message_ids"].add(m["message_id"])
                _state["seen_id_timestamps"][m["message_id"]] = m.get("sent_at", "")

            # Bounded cleanup
            if len(_state["seen_message_ids"]) > SEEN_SET_MAX:
                _state["seen_message_ids"] = _prune_seen_set(
                    _state["seen_message_ids"],
                    _state["seen_id_timestamps"],
                )
                _state["seen_id_timestamps"] = {
                    k: v
                    for k, v in _state["seen_id_timestamps"].items()
                    if k in _state["seen_message_ids"]
                }

            # Return newest-last (Graph returns newest-first)
            new_msgs.sort(key=lambda m: m.get("sent_at", ""))
            return json.dumps(
                {
                    "messages": new_msgs,
                    "timed_out": False,
                    "poll_count": poll_count,
                },
                indent=2,
            )

        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return json.dumps(
                {
                    "messages": [],
                    "timed_out": True,
                    "poll_count": poll_count,
                },
                indent=2,
            )

        if interval > 0:
            await asyncio.sleep(interval)


@mcp.tool()
def audit_log(
    action: str,
    resource: str,
    outcome: str = "success",
    metadata: str = "{}",
) -> str:
    """Record an audit event. Call this BEFORE performing any action on the
    user's behalf. No credentials needed — works immediately.

    The audit trail proves the agent (not the human) performed the action.
    Events are written to ~/.entraclaw/audit/ as daily JSONL files.

    Args:
        action: What the agent is doing (e.g., "file_read", "code_execute").
        resource: What is being acted on (e.g., file path, URL, repo name).
        outcome: "success", "failure", or "pending" (default "success").
        metadata: Optional JSON string of key-value pairs with extra context.

    Returns:
        JSON with event_id, timestamp, and the recorded event.
    """
    from entraclaw.tools.audit import log_event

    config = get_config()
    meta = json.loads(metadata) if metadata else {}

    # Identity-aware attribution (eng review Tension 1)
    if _identity:
        agent_id = (
            _identity.session.user_id or config.agent_id
            or config.blueprint_app_id or "unknown"
        )
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id or "unknown"
        attribution = "agent"

    result = log_event(
        action=action,
        resource=resource,
        outcome=outcome,
        agent_id=agent_id,
        metadata=meta,
        attribution_type=attribution,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def view_image(url: str) -> str:
    """Fetch and display an image from a Teams chat message.

    Pass the Graph API hosted content URL from a chat message's
    ``<img src="...">`` tag. The image is downloaded with the agent's
    token, saved to a temp file, and the path is returned so Claude
    Code can render it.

    Only accepts URLs under ``graph.microsoft.com`` — will not send
    the Bearer token to arbitrary hosts.

    Args:
        url: The full Graph API hosted content URL
            (e.g., ``https://graph.microsoft.com/v1.0/chats/.../hostedContents/.../$value``).

    Returns:
        JSON with the local file path to the downloaded image, or an error.
    """
    import tempfile

    await _initialize()
    from entraclaw.tools.teams import fetch_hosted_image

    if "graph.microsoft.com" not in url:
        return json.dumps({"error": "Not a Graph API URL — refusing to send token"})

    await _ensure_valid_token()
    try:
        image_bytes = await _with_token_retry(
            fetch_hosted_image,
            url=url,
        )
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if image_bytes is None:
        return json.dumps({"error": "Image not found (404)"})

    ext = ".png"
    if ".jpg" in url or ".jpeg" in url:
        ext = ".jpg"
    elif ".gif" in url:
        ext = ".gif"

    with tempfile.NamedTemporaryFile(
        suffix=ext, prefix="entraclaw_img_", delete=False
    ) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    return json.dumps({
        "file_path": tmp_path,
        "size_bytes": len(image_bytes),
    })


@mcp.tool()
async def whoami() -> str:
    """Show the current agent identity, Teams connection status, and permissions.
    Call this to verify the agent is authenticated and connected to Teams.

    Authentication is automatic — no credentials needed.

    Returns:
        JSON with agent identity details and connection status.
    """
    await _initialize()
    from entraclaw.tools.identity import whoami as _whoami

    token = _state.get("token") or (_identity.session.token if _identity else None)
    result = await _whoami(token=str(token) if token else None)
    result["teams_chat_id"] = _state.get("chat_id", "not_connected")
    # Add identity state info
    if _identity:
        result["identity_state"] = _identity.state.value
        result["attribution_type"] = _identity.session.attribution_type
        result["auth_mode"] = _identity.session.auth_mode
    return json.dumps(result, indent=2)


async def _run_stdio_with_write_stream() -> None:
    """Run the MCP server on stdio, capturing the write stream for notifications.

    The standard ``mcp.run(transport="stdio")`` doesn't expose the write stream.
    We override it to capture the stream, enabling background notification push
    (the same pattern the iMessage channel plugin uses).

    Declares ``claude/channel`` experimental capability so Claude Code registers
    a notification handler for ``notifications/claude/channel`` from this server.
    Without this capability, channel notifications are silently dropped.
    """
    import asyncio

    async with stdio_server() as (read_stream, write_stream):
        _state["_write_stream"] = write_stream

        # Install efferent-copy middleware before any tool dispatch runs.
        # Discovery is schema-only — any peer that advertises an ``observe``
        # tool with ``{tool_name: string, args: object}`` becomes a sink.
        # Zero sinks → no wrapping, body behavior unchanged. Any discovery
        # failure is swallowed: the body MUST keep working without sinks.
        try:
            sinks = await efferent_copy.discover_sinks()
            efferent_copy.install_into_fastmcp(mcp, sinks)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning(
                    "efferent-copy discovery failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        # Eagerly kick off auth + watched-chat load + background polls so the
        # agent starts observing DMs/email the moment the server boots —
        # without waiting for the first MCP tool call. Lazy init left every
        # fresh server process deaf to inbound Teams traffic until a user
        # happened to invoke a tool.
        async def _eager_init() -> None:
            try:
                await _initialize()
            except Exception as exc:
                if logger:
                    logger.warning(
                        "Eager init failed: %s: %s",
                        type(exc).__name__,
                        exc,
                    )

        init_task = asyncio.create_task(_eager_init())
        try:
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(
                    experimental_capabilities={"claude/channel": {}},
                ),
            )
        finally:
            init_task.cancel()


@mcp.tool()
async def run_daily_summary(
    day: str = "",
    send: bool = True,
) -> str:
    """Triage today's interactions and (optionally) email a summary.

    Reads the interaction log for *day* (UTC, ``YYYY-MM-DD``; defaults to
    today), sorts entries into three buckets — ``needs_you``, ``handled``,
    ``heads_up`` — renders an HTML summary, archives it to
    ``<data_dir>/summaries/<day>.html``, and emails it to the primary
    sponsor via Graph ``/me/sendMail`` (when *send* is True).

    The scheduler fires this automatically at 5pm PDT each day when
    running in ``agent_user`` mode. Use this tool to trigger an ad-hoc
    summary or to preview without sending.

    Args:
        day: UTC day in ``YYYY-MM-DD`` format. Defaults to today.
        send: If True, also email the summary. If False, render + archive only.

    Returns:
        JSON with counts per bucket, archive path, and recipients (if sent).
    """
    await _initialize()
    result = await _run_daily_summary_internal(
        day=day or None,
        send=send,
    )
    return json.dumps(result, indent=2)


def main() -> None:
    """Entry point for ``entraclaw-mcp`` console script."""
    import anyio

    global logger
    logger = setup_logging()
    logger.info("Starting EntraClaw MCP server (progressive identity)")
    anyio.run(_run_stdio_with_write_stream)


if __name__ == "__main__":
    main()
