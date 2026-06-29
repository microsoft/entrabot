"""EntraBot MCP server — progressive identity with Agent User tools.

Authentication is automatic via progressive identity:
1. Try three-hop Agent User flow with existing creds (fast path)
2. If that fails, fall back to MSAL delegated auth (human's token)
3. Optionally background-provision an Agent User identity

The calling LLM does NOT need to provide any credentials, tokens, or
configuration — just call the tools directly.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from entrabot import efferent_copy
from entrabot.config import get_config
from entrabot.errors import (
    AuthRefreshRequiredError,
    EntraBotError,
    MissingPlaceholderError,
    TokenExchangeError,
)
from entrabot.identity import IdentityStateMachine, set_active_identity_state
from entrabot.logging_config import setup_logging
from entrabot.models import IdentityState
from entrabot.tools.interaction_log import detect_channel, log_interaction
from entrabot.tools.teams import acquire_agent_user_token
from entrabot.url_safety import _is_graph_url

logger: logging.Logger | None = None

# Local system-prompt file used when persona-sati isn't reachable. Kept as a
# module attribute so tests can monkey-patch it at an isolated path.
LOCAL_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "agent_system.md"


_HARDCODED_FALLBACK = (
    "EntraBot Teams Interface: provides tools for sending and "
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
            target_name = stripped[len("@include") :].strip()
            if target_name:
                target_path = base_dir / target_name
                try:
                    if target_path.is_file():
                        lines.append(target_path.read_text(encoding="utf-8").rstrip())
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
            "persona-sati env unset; serving body-only (body_loaded=%s, body_chars=%d)",
            bool(body),
            len(body) if body else 0,
        )
        return body or _HARDCODED_FALLBACK

    body_or_fallback = body or _HARDCODED_FALLBACK

    try:
        token = subprocess.check_output([token_cmd], text=True, timeout=30).strip()
    except (subprocess.SubprocessError, OSError) as exc:
        print(
            f"[entrabot] could not mint persona-sati token "
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
            f"[entrabot] token command {token_cmd} returned empty; using local fallback prompt",
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
            f"[entrabot] persona-sati fetch failed: {exc}; using local fallback prompt",
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
            "[entrabot] persona-sati returned empty prompt; using local fallback",
            file=sys.stderr,
        )
        log.warning(
            "persona-sati returned empty prompt (%s); using local fallback",
            remote_url,
        )
        return body_or_fallback

    print(
        f"[entrabot] loaded system prompt from persona-sati ({remote_url})",
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
    "EntraBot Agent Identity",
    instructions=_load_agent_instructions(),
)

# ---------------------------------------------------------------------------
# Host detection — informational only.
#
# Every MCP client that spawns entrabot (stdio) gets its own process and
# its own poll loops. There is no multi-client sharing at runtime, so
# there is no leader/slave gating. `_current_host` / `_capture_host_from_context`
# exist only to annotate logs with the connected client's name. Channel
# pushes fire unconditionally; clients that don't handle
# `notifications/claude/channel` ignore them, which is the MCP-spec behavior.
# ---------------------------------------------------------------------------


def _current_host() -> str:
    """Return the active MCP client's ``clientInfo.name`` lowercased.

    Logging only. Returns ``"unknown"`` when no active request context
    is available (module-load time, background asyncio tasks) or when
    ``clientInfo`` is not yet populated.
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

    Logging only. Called at tool entry so log lines from background
    tasks can annotate with the most recently seen client.
    """
    host = _current_host()
    if host and host != "unknown":
        _state["cached_host"] = host
    return host


# Module-level state populated by _initialize().
_state: dict[str, object] = {"cached_host": "", "recent_placeholders": {}}
_identity: IdentityStateMachine | None = None

# Commitment-language patterns for the outbound-discipline hook in
# ``send_teams_message``. When the agent's outbound text matches one
# of these and no recent ``add_promise`` is found, the wrapper appends
# a ``_discipline_warning`` to the result JSON (warn-not-block; false
# positives on language detection are real). See
# ``scripts/hooks/README.md`` for the full registry.
_COMMITMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bI(?:'ll| will| am going to| am gonna)\b", re.IGNORECASE),
    re.compile(r"\bat the next (?:pause|natural pause|break)\b", re.IGNORECASE),
    re.compile(r"\bI(?:'ll| will) (?:report back|follow up|circle back)\b", re.IGNORECASE),
    re.compile(r"\bdrafting (?:and|\+) writing\b", re.IGNORECASE),
    re.compile(r"\bbatch (?:a |an |the )?\w+ (?:update|write|memory)\b", re.IGNORECASE),
    re.compile(r"\bcoming (?:back|to this) later\b", re.IGNORECASE),
    re.compile(r"\bin a follow[- ]up\b", re.IGNORECASE),
]

# Window (seconds) we look back for an `add_promise` matching the chat_id
# (or the "terminal" channel for CLI-side commitments). Short — the
# heuristic is: a commitment phrase the model just emitted should be
# paired with an add_promise call in the same turn or the immediately
# preceding one. 120s covers normal tool-call latency.
_COMMITMENT_PROMISE_LOOKBACK_SECONDS = 120

TOKEN_REFRESH_THRESHOLD = 3300  # 55 min (5-min buffer on 60-min expiry)

# Sent-message tracking for delegated-mode echo prevention
SENT_MESSAGE_MAX = 1000
_sent_message_ids: set[str] = set()


# ---------------------------------------------------------------------------
# Outbound-discipline hooks (placeholder + commitment-language)
#
# Both hooks fire inside ``send_teams_message`` and are server-side by
# design — they live in the MCP tool wrapper, not in
# ``.claude/settings.json``, so they apply on every host that speaks MCP
# (Claude Code, Copilot CLI, Codex, Cursor) and every storage backend
# (Local/Blob). See ``scripts/hooks/README.md`` for the registry.
# ---------------------------------------------------------------------------


def _is_substantive_message(message: str) -> bool:
    """Heuristic for "substantive" Teams text.

    Mirrors the channel-discipline rule (>2 sentences) with two cheap
    proxies: length > 200 chars, OR more than 2 terminal punctuation
    marks. Either trips the placeholder-discipline gate.
    """
    if not message:
        return False
    if len(message) > 200:
        return True
    punctuation_count = message.count(".") + message.count("?") + message.count("!")
    return punctuation_count > 2


def _matches_commitment_language(message: str) -> bool:
    """Return True when the outbound text contains a commitment phrase."""
    if not message:
        return False
    return any(p.search(message) for p in _COMMITMENT_PATTERNS)


def _check_placeholder_discipline(message: str, chat_id: str) -> None:
    """Raise MissingPlaceholderError when sending a substantive message
    without a recent placeholder for ``chat_id``.

    No-op when:
    - message is short (≤ 200 chars and ≤ 2 terminal punctuation marks),
    - ``ENTRABOT_SKIP_PLACEHOLDER_CHECK=true``,
    - a placeholder for this chat was posted within the grace window
      (``ENTRABOT_PLACEHOLDER_GRACE_SECONDS``, default 300).
    """
    if os.environ.get("ENTRABOT_SKIP_PLACEHOLDER_CHECK", "").lower() in (
        "true",
        "1",
        "yes",
    ):
        return
    if not _is_substantive_message(message):
        return

    try:
        grace = int(os.environ.get("ENTRABOT_PLACEHOLDER_GRACE_SECONDS", "300"))
    except ValueError:
        grace = 300

    recent: dict[str, float] = _state.get("recent_placeholders") or {}  # type: ignore[assignment]
    last_at = recent.get(chat_id)
    age = float("inf") if last_at is None else time.monotonic() - last_at

    if age > grace:
        raise MissingPlaceholderError(
            chat_id=chat_id,
            placeholder_age_seconds=age,
            grace_seconds=grace,
        )


async def _has_recent_promise(chat_id: str) -> bool:
    """Return True if an open promise was created in the last ~120s for
    this chat_id (or for chat_id="terminal").

    Uses the existing ``list_promises`` implementation in
    ``entrabot.tools.promises`` so the lookup goes through whichever
    storage backend the operator configured (Local/Blob/etc.). Failures
    are swallowed — the commitment hook is advisory and must not break
    Teams sends.
    """
    try:
        from entrabot.tools.promises import list_promises as _list_promises

        promises = await _list_promises(open_only=True)
    except Exception as exc:  # pragma: no cover - defensive
        if logger:
            logger.warning("commitment hook: list_promises failed: %s", exc)
        return False

    cutoff = datetime.now(UTC) - timedelta(seconds=_COMMITMENT_PROMISE_LOOKBACK_SECONDS)
    for promise in promises:
        if promise.chat_id not in (chat_id, "terminal"):
            continue
        try:
            created = datetime.fromisoformat(promise.created_at)
        except (TypeError, ValueError):
            continue
        if created >= cutoff:
            return True
    return False


async def _check_commitment_language(message: str, chat_id: str) -> str | None:
    """Return a discipline-warning string when commitment language is
    detected and no recent promise covers it; return ``None`` otherwise.

    Bypass via ``ENTRABOT_SKIP_COMMITMENT_CHECK=true``.
    """
    if os.environ.get("ENTRABOT_SKIP_COMMITMENT_CHECK", "").lower() in (
        "true",
        "1",
        "yes",
    ):
        return None
    if not _matches_commitment_language(message):
        return None
    if await _has_recent_promise(chat_id):
        return None
    return (
        "Commitment language detected ('I'll', 'at the next pause', etc.) "
        "but no recent add_promise call. If this commits to future work, "
        "create a promise via add_promise so it survives session end."
    )


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
        oidc_url = f"https://login.microsoftonline.com/{domain}/.well-known/openid-configuration"
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
            await _identity.update_session(token=token, token_acquired_at=time.monotonic())
            _state["token"] = token

        elif current_state == IdentityState.DELEGATED:
            if logger:
                logger.info("Token near expiry — refreshing via MSAL silent")
            try:
                from entrabot.auth.delegated import MsalDelegatedAuth

                config = _state.get("config")
                auth = MsalDelegatedAuth(
                    client_id=config.client_id,
                    tenant_id=config.tenant_id or "common",
                )
                result = auth.try_silent()
                if result and "error" in result:
                    error = str(result.get("error") or "msal_error")
                    description = str(
                        result.get("error_description")
                        or "MSAL silent refresh returned an error response"
                    )
                    if logger:
                        logger.warning(
                            "MSAL silent refresh failed: %s: %s",
                            error,
                            description,
                        )
                    import contextlib

                    with contextlib.suppress(Exception):
                        await _identity.transition(IdentityState.UNAUTHENTICATED)
                    await _identity.update_session(
                        token=None,
                        token_acquired_at=None,
                        user_id=None,
                        auth_mode=None,
                        account_id=None,
                        tenant_id=None,
                    )
                    _state.pop("token", None)
                    raise TokenExchangeError(
                        "msal_silent_refresh",
                        error,
                        description,
                    )
                if result and "access_token" in result:
                    token = result["access_token"]
                    await _identity.update_session(
                        token=token,
                        token_acquired_at=time.monotonic(),
                    )
                    _state["token"] = token
                else:
                    error = "interaction_required"
                    description = (
                        "MSAL silent refresh returned no access token; "
                        "interactive auth must be initiated from the startup path"
                    )
                    if logger:
                        logger.warning(
                            "MSAL silent refresh failed without interactive fallback: %s",
                            error,
                        )
                    import contextlib

                    with contextlib.suppress(Exception):
                        await _identity.transition(IdentityState.UNAUTHENTICATED)
                    await _identity.update_session(
                        token=None,
                        token_acquired_at=None,
                        user_id=None,
                        auth_mode=None,
                        account_id=None,
                        tenant_id=None,
                    )
                    _state.pop("token", None)
                    raise AuthRefreshRequiredError(
                        error=error,
                        error_description=description,
                    )
            except (AuthRefreshRequiredError, TokenExchangeError):
                raise
            except Exception as exc:
                if logger:
                    logger.warning("MSAL refresh failed: %s", exc)
                # Transition to UNAUTHENTICATED on total failure
                import contextlib

                with contextlib.suppress(Exception):
                    await _identity.transition(IdentityState.UNAUTHENTICATED)
                await _identity.update_session(
                    token=None,
                    token_acquired_at=None,
                    user_id=None,
                    auth_mode=None,
                    account_id=None,
                    tenant_id=None,
                )
                _state.pop("token", None)
                raise AuthRefreshRequiredError(
                    error="silent_refresh_failed",
                    error_description=(
                        f"MSAL silent refresh failed in background context: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                ) from exc

        elif current_state == IdentityState.UNAUTHENTICATED:
            pass  # No token to refresh — auth needed first


async def _with_token_retry(fn, **kwargs):
    """Call *fn* with the current token; on TokenExpiredError, refresh and retry once.

    The function *fn* must accept a ``token`` keyword argument.
    Any additional kwargs are passed through to *fn*.
    """
    from entrabot.errors import TokenExpiredError

    token = _state.get("token") or (_identity.session.token if _identity else None)
    try:
        return await fn(token=str(token), **kwargs)
    except TokenExpiredError:
        if logger:
            logger.warning("Token expired mid-call — refreshing and retrying")
        # Force refresh by clearing token_acquired_at (token may be fresh but revoked)
        if _identity is not None:
            await _identity.update_session(token_acquired_at=None)
        await _ensure_valid_token()
        token = _state.get("token") or (_identity.session.token if _identity else None)
        return await fn(token=str(token), **kwargs)


OVERLAP_SECONDS = 2
SEEN_SET_MAX = 500
SEEN_SET_PRUNE_MINUTES = 10

# Debounce window for per-chat cursor saves (issue #17). The poll loop calls
# ``_schedule_cursor_save`` after every cycle that advances ``last_ts`` or
# ``seen_ids``; multiple advances inside this window coalesce to one backend
# write so a chatty group conversation doesn't trigger a write per message.
CURSOR_SAVE_DEBOUNCE_SECONDS = 1.0


# Indirection so tests can patch the actual backend-write site without having
# to monkeypatch through the chat_cursors module import.
def _chat_cursor_save(chat_id: str, state: dict) -> None:
    """Persist *state* as *chat_id*'s cursor through the configured backend.

    Thin wrapper around :func:`entrabot.tools.chat_cursors.save_cursor` so
    tests can swap it out at one site.
    """
    from entrabot.tools.chat_cursors import save_cursor as _save

    _save(chat_id, state)


def _mark_cursor_dirty(chat_id: str) -> None:
    """Record that *chat_id*'s in-memory cursor has advanced past last save.

    The flush path reads this set, persists each chat, and clears it. Kept
    separate from ``_schedule_cursor_save`` so graceful shutdown can flush
    even if the asyncio loop is already torn down.
    """
    dirty: set[str] = _state.setdefault("_dirty_cursor_chats", set())
    dirty.add(chat_id)


def _serialize_chat_state(chat_state: dict) -> dict:
    """Convert an in-memory ``chat_state`` dict into the on-disk cursor shape.

    ``seen_ids`` is a set in memory; ``seen_ids_tail`` is a list on disk so it
    serializes deterministically across MemoryBackend implementations.

    Set iteration order is not stable across Python runs (depends on insertion
    order, hash collisions, and ``PYTHONHASHSEED``). Sort lexicographically
    and keep the highest-valued tail so the persisted tail covers the IDs
    most likely to land inside the poll's overlap window after restart. Teams
    message IDs are numeric-as-string, so lexicographic order is monotonic
    with sent-at — same approach the poll loop uses at ``mcp_server.py:1301``
    when bounding the in-memory ``seen_ids`` set.
    """
    seen = chat_state.get("seen_ids") or set()
    bounded = sorted(seen)[-50:]
    return {
        "last_ts": chat_state.get("last_ts"),
        "seen_ids_tail": bounded,
        "bootstrapped": bool(chat_state.get("bootstrapped", False)),
    }


def _flush_chat_cursors() -> None:
    """Synchronously write every dirty chat's cursor through the backend.

    Called on graceful shutdown so the next process inherits the latest
    cursors. Failures are logged per-chat and never propagate — one bad write
    must not block the rest.
    """
    dirty: set[str] = _state.get("_dirty_cursor_chats") or set()
    if not dirty:
        return
    watched = _state.get("watched_chats") or {}
    failures: list[str] = []
    for chat_id in list(dirty):
        chat_state = watched.get(chat_id)
        if not chat_state:
            continue
        try:
            _chat_cursor_save(chat_id, _serialize_chat_state(chat_state))
        except Exception as exc:  # noqa: BLE001
            failures.append(chat_id)
            if logger:
                logger.warning(
                    "Cursor flush failed for chat %s: %s: %s",
                    chat_id,
                    type(exc).__name__,
                    exc,
                )
    # Clear only the chats that successfully flushed (or had no state to write).
    _state["_dirty_cursor_chats"] = set(failures)


def _schedule_cursor_save(chat_id: str) -> None:
    """Debounced async save: mark dirty + (re)arm the per-chat save timer.

    A burst of advances inside ``CURSOR_SAVE_DEBOUNCE_SECONDS`` coalesces to
    one backend write. The pending task is tracked in ``_cursor_save_tasks``
    so a follow-up schedule cancels the in-flight timer and starts a fresh
    one (last-write-wins debounce).
    """
    import asyncio

    _mark_cursor_dirty(chat_id)

    tasks: dict = _state.setdefault("_cursor_save_tasks", {})
    pending = tasks.get(chat_id)
    if pending is not None and not pending.done():
        pending.cancel()

    async def _delayed_save() -> None:
        try:
            await asyncio.sleep(CURSOR_SAVE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            chat_state = (_state.get("watched_chats") or {}).get(chat_id)
            if chat_state is None:
                return
            _chat_cursor_save(chat_id, _serialize_chat_state(chat_state))
            # Successful write → drop from dirty set.
            dirty = _state.get("_dirty_cursor_chats") or set()
            dirty.discard(chat_id)
            _state["_dirty_cursor_chats"] = dirty
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning(
                    "Debounced cursor save failed for chat %s: %s: %s",
                    chat_id,
                    type(exc).__name__,
                    exc,
                )

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # No running loop (rare — happens in some unit-test contexts). Best-effort
        # synchronous flush.
        try:
            chat_state = (_state.get("watched_chats") or {}).get(chat_id)
            if chat_state is not None:
                _chat_cursor_save(chat_id, _serialize_chat_state(chat_state))
                # Mirror the async branch: drop from dirty set on success so the
                # chat doesn't stay "dirty" forever and trigger redundant flush
                # attempts on shutdown.
                dirty = _state.get("_dirty_cursor_chats") or set()
                dirty.discard(chat_id)
                _state["_dirty_cursor_chats"] = dirty
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("Sync cursor save failed for %s: %s", chat_id, exc)
        return

    tasks[chat_id] = loop.create_task(_delayed_save())


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
    import asyncio

    global _identity
    _identity = IdentityStateMachine()
    set_active_identity_state(_identity)

    config = get_config()
    _state["config"] = config

    # Fast path: try three-hop with existing creds (unless SKIP_PROVISIONING)
    if not config.skip_provisioning and config.blueprint_app_id and config.tenant_id:
        try:
            # acquire_agent_user_token is synchronous and makes several
            # blocking HTTPS token calls (~seconds). Run it in a worker
            # thread so eager boot does not starve the asyncio loop and
            # stall the MCP `initialize` handshake — a stalled handshake
            # makes stdio/ACP engine hosts (e.g. copilot) time out the
            # server start and abort the launch. See test
            # TestInitAuthDoesNotBlockEventLoop.
            token = await asyncio.to_thread(acquire_agent_user_token, config)
            await _identity.update_session(
                token=token,
                token_acquired_at=time.monotonic(),
                auth_mode="agent_user",
                user_id=config.agent_user_id,
                display_name="EntraBot Agent",
            )
            await _identity.transition(IdentityState.AGENT_USER)
            _state["token"] = token
            if logger:
                logger.info("Fast path: Agent User token acquired via three-hop flow")
            return
        except (TokenExchangeError, EntraBotError) as exc:
            if logger:
                logger.warning(
                    "Three-hop flow failed, falling back to MSAL delegated: %s",
                    exc,
                )
        except Exception as exc:
            if logger:
                logger.warning("Unexpected auth error, falling back to MSAL: %s", exc)

    # Delegated path: MSAL interactive auth
    if config.client_id:
        try:
            from entrabot.auth.delegated import MsalDelegatedAuth

            auth = MsalDelegatedAuth(
                client_id=config.client_id,
                tenant_id=config.tenant_id or "common",
            )
            # Blocking (and potentially interactive) — keep it off the loop
            # so the MCP handshake stays responsive during boot.
            result = await asyncio.to_thread(auth.authenticate)
            if result and "error" in result:
                error = str(result.get("error") or "msal_error")
                description = str(
                    result.get("error_description")
                    or "MSAL delegated auth returned an error response"
                )
                if logger:
                    logger.warning("MSAL delegated auth failed: %s: %s", error, description)
                raise TokenExchangeError("msal_delegated_auth", error, description)
            token = result["access_token"]
            account = result.get("id_token_claims", {})

            await _identity.update_session(
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
        except TokenExchangeError as exc:
            if logger:
                logger.warning("MSAL delegated auth failed: %s", exc)
            raise
        except Exception as exc:
            if logger:
                logger.warning("MSAL delegated auth failed: %s", exc)

    # No auth method available — stay UNAUTHENTICATED
    # Per eng review decision 4A: no hard exits, state transitions instead
    if not config.blueprint_app_id and not config.client_id and logger:
        logger.warning(
            "No auth configured: set ENTRABOT_BLUEPRINT_APP_ID (three-hop) "
            "or ENTRABOT_CLIENT_ID (MSAL delegated) in .env"
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

    # Start background polling unconditionally. Every client that
    # spawns entrabot (stdio) gets its own process and its own poll
    # loops — no gating is needed.
    if _state.get("watched_chats"):
        _ensure_poll_task_running()

    # Start email poll + daily summary when authenticated as the Agent User
    # (its own mailbox and outbound mail rights). In delegated mode /me/*
    # would target the human's mailbox — not what we want.
    if _identity and _identity.session and _identity.session.auth_mode == "agent_user":
        import asyncio

        asyncio.get_event_loop().create_task(_background_poll_email())
        asyncio.get_event_loop().create_task(_background_daily_summary())
        asyncio.get_event_loop().create_task(_background_discover_chats())
        asyncio.get_event_loop().create_task(_background_persona_sati_heartbeat())


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

    # authorization fix: log the active-sponsor-channel binding TTL so operators
    # can verify the security configuration in production logs.
    if logger:
        from entrabot.identity.active_channel import get_bindings

        logger.info(
            "Active-sponsor-channel binding active (TTL=%ds, Gate 3)",
            get_bindings().ttl_seconds,
        )

    _state["initialized"] = True


BACKGROUND_POLL_INTERVAL = 5  # seconds between polls
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

    Resolves its own ``logging.getLogger("entrabot")`` so it emits
    records even when called before ``main()`` has configured the
    module-global ``logger`` (e.g. unit tests, module-import-time
    smoke checks).
    """
    import logging
    import os
    import subprocess
    import time as _time

    log = logging.getLogger("entrabot")

    remote_url = os.environ.get("PERSONA_SATI_MCP_URL", "").strip()
    token_cmd = os.environ.get("PERSONA_SATI_MCP_TOKEN_COMMAND", "").strip()
    if not remote_url or not token_cmd:
        return "skipped"

    try:
        token = subprocess.check_output([token_cmd], text=True, timeout=30).strip()
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning(
            "persona-sati heartbeat FAILED: token_mint_failed (%s): %s: %s",
            token_cmd,
            type(exc).__name__,
            exc,
        )
        return "token_mint_failed"
    if not token:
        log.warning(
            "persona-sati heartbeat FAILED: token_mint_failed (%s returned empty output)",
            token_cmd,
        )
        return "token_mint_failed"

    started = _time.monotonic()
    try:
        files = await _persona_sati_list_files(remote_url, token)
    except Exception as exc:  # noqa: BLE001 — classify but never raise
        log.warning(
            "persona-sati heartbeat FAILED: remote_failed (%s): %s: %s",
            remote_url,
            type(exc).__name__,
            exc,
        )
        return "remote_failed"

    elapsed_ms = int((_time.monotonic() - started) * 1000)
    log.info(
        "persona-sati heartbeat ok (url=%s, file_count=%d, elapsed_ms=%d)",
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
    what it exercises is "can entrabot reach persona-sati right
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


def _ensure_poll_task_running() -> None:
    """Start the Graph background poll task if one isn't already running.

    Idempotent.
    """
    existing = _state.get("poll_task")
    if existing is not None and not existing.done():
        return

    import asyncio

    _state["poll_task"] = asyncio.get_event_loop().create_task(_background_poll())
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
    via create_chat), lazily spins one up.
    """
    watched = _state.get("watched_chats", {})
    if chat_id not in watched:
        # Issue #17: try to rehydrate a persisted cursor first. If it exists
        # and its ``last_written_at`` is within the staleness cap (i.e. the
        # cursor was persisted recently, regardless of how old its newest
        # message is), we keep the prior process's seen-set + watermark and
        # skip ``_bootstrap_chat`` (which would otherwise re-fire the "newest
        # at boot" message even when that message is days old). Staleness is
        # judged by write time, NOT by ``last_ts`` — keying off ``last_ts``
        # re-bootstrapped every idle chat on each restart, re-firing its old
        # newest message as if it were live (the replay flood). If absent,
        # stale, or corrupt, fall through to the fresh-state path and let the
        # bootstrap path baseline.
        from entrabot.tools.chat_cursors import is_stale, load_cursor

        rehydrated: dict | None = None
        try:
            cursor = load_cursor(chat_id)
        except Exception as exc:  # noqa: BLE001
            # Backend read failure (transient disk/blob error) → bootstrap.
            cursor = None
            if logger:
                logger.warning(
                    "Cursor load failed for %s (treating as absent): %s: %s",
                    chat_id,
                    type(exc).__name__,
                    exc,
                )
        if cursor and not is_stale(cursor.get("last_written_at")):
            rehydrated = cursor

        if rehydrated is not None:
            watched[chat_id] = {
                "seen_ids": set(rehydrated.get("seen_ids_tail") or []),
                "last_ts": rehydrated.get("last_ts"),
                "bootstrapped": True,
            }
            if logger:
                logger.info(
                    "Rehydrated chat cursor (last_ts=%s, seen_ids=%d): %s",
                    rehydrated.get("last_ts"),
                    len(watched[chat_id]["seen_ids"]),
                    chat_id,
                )
        else:
            watched[chat_id] = {"seen_ids": set(), "last_ts": None, "bootstrapped": False}
            if logger:
                logger.info("Registered chat for background polling: %s", chat_id)
        _state["watched_chats"] = watched
        # Invalidate the cached sponsor gate so the next wait_for_sponsor_dm
        # call rebuilds it with the new chat's members enriched in. Without
        # this, a chat created mid-session (e.g. via create_chat in a fresh
        # Copilot CLI session) is invisible to the gate's user_ids set —
        # federated B2B sponsors never get matched and wait hangs forever.
        # See Learning #52 in docs/runbooks/hard-won-learnings.md.
        if _state.pop("sponsor_gate", None) is not None and logger:
            logger.info(
                "Invalidated cached sponsor gate after new watched chat: %s",
                chat_id,
            )

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
                    line.strip() for line in watched_file.read_text().splitlines() if line.strip()
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
    from entrabot.tools.teams import read

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
    # Issue #17: persist the bootstrap watermark so a restart inherits it.
    # Without this, every restart re-baselines from "the newest message at
    # boot" — including days-old messages.
    _schedule_cursor_save(chat_id)


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

    from entrabot.tools.teams import filter_human_messages, read

    if logger:
        logger.info("Starting background Teams poll (interval=%ds)", BACKGROUND_POLL_INTERVAL)

    # Must match the displayName that Graph API returns in message.from.user.displayName
    # Identity-aware: filter out messages from BOTH the agent and the human user
    # depending on current identity mode
    agent_display_name = "EntraBot Agent"

    while True:
        try:
            await asyncio.sleep(BACKGROUND_POLL_INTERVAL)

            # Skip polling if not authenticated
            if _identity and _identity.state in (
                IdentityState.UNAUTHENTICATED,
                IdentityState.ERROR,
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
                        read,
                        chat_id=chat_id,
                        count=10,
                    )
                    human_msgs = filter_human_messages(
                        raw_messages,
                        agent_display_name,
                    )
                    new_msgs = _filter_new_messages(
                        human_msgs,
                        chat_state["last_ts"],
                        chat_state["seen_ids"],
                    )

                    if new_msgs:
                        newest = max(new_msgs, key=lambda m: m.get("sent_at", ""))
                        chat_state["last_ts"] = newest["sent_at"]
                        for m in new_msgs:
                            chat_state["seen_ids"].add(m["message_id"])

                        # Bounded cleanup (keep last 500)
                        if len(chat_state["seen_ids"]) > SEEN_SET_MAX:
                            chat_state["seen_ids"] = set(sorted(chat_state["seen_ids"])[-100:])

                        for m in sorted(
                            new_msgs,
                            key=lambda m: m.get("sent_at", ""),
                        ):
                            await _push_channel_notification(m, chat_id=chat_id)

                        # Issue #17: persist the advanced cursor through the
                        # MemoryBackend so the next process picks up where we
                        # left off instead of replaying the newest-at-boot
                        # message as fresh. Debounced ~1s so a chatty group
                        # chat doesn't trigger a write per message.
                        _schedule_cursor_save(chat_id)
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

    from entrabot.tools.email_poll import load_cursor, poll_once, save_cursor

    if logger:
        logger.info("Starting background email poll (interval=%ds)", EMAIL_POLL_INTERVAL)

    cursor = load_cursor()
    if cursor is None:
        cursor = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_cursor(cursor)

    # Per-session dedup. Graph returns sub-second precision on
    # receivedDateTime but the cursor file we save may end up truncated
    # to second resolution, which causes the same email to be returned
    # every poll cycle (observed 2026-04-17 with a teammate's "Ball
    # game tonight" looping). Belt-and-suspenders: also track the
    # Graph-side message IDs we've already pushed this session so we
    # never double-push, regardless of cursor drift.
    pushed_email_ids: set[str] = set()
    _PUSHED_EMAIL_MAX = 500

    # Don't push our own outbound emails back as if they were inbound.
    # /me/messages returns the entire mailbox including the Sent Items
    # folder; emails the agent sends would otherwise loop into the
    # channel-notification stream. (Discovered 2026-04-17 when the
    # "EntraBot email pipeline test" email I sent to Brandon got
    # echoed back as an inbound notification ~10s later.)
    agent_self_upn = ((_state.get("config") or get_config()).agent_user_upn or "").lower()

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
                poll_once,
                cursor=cursor,
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
                    (msg.get("from") or {}).get("emailAddress", {}).get("address", "").lower()
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
    tool does; the raw ``entrabot.tools.teams.create_*`` functions do NOT,
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
        content = f"[email] {sender_name} ({sender_addr}) — {subject}\n{preview[:400]}"

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
        recipient="entrabot-agent",
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


async def _run_daily_summary_internal(*, day: str | None = None, send: bool = True) -> dict:
    """Read today's log → triage → render → archive → optionally send."""
    from entrabot.tools.daily_summary import (
        archive_summary,
        render_summary_html,
        send_summary_email,
        triage_interactions,
    )
    from entrabot.tools.interaction_log import read_day

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
            sender="entrabot-agent",
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

    from entrabot.tools.daily_summary import next_run_at

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
    """Strip HTML and truncate — used when the caller didn't supply a summary.

    ``<img src>`` and ``<a href>`` URLs are extracted into plain-text
    markers before tags are stripped so the channel push doesn't lose
    the only signal in giphy embeds and link cards (regression
    2026-04-27 — see ``docs/runbooks/mcp-disconnect-investigation.md``).

    HTML entities are unescaped *before* tag stripping so encoded
    angles (``&lt;p&gt;``) survive a round-trip without leaving
    decode-to-angle artifacts in the channel envelope. Claude Code's
    MCP client closes the stream on angle brackets after entity
    decoding (regression 2026-04-28 — same runbook).
    """
    import html
    import re

    text = content or ""
    text = html.unescape(text)
    text = re.sub(
        r"""<img\b[^>]*?\bsrc=["']([^"']+)["'][^>]*?/?>""",
        r" [image: \1] ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"""<a\b[^>]*?\bhref=["']([^"']+)["'][^>]*?>(.*?)</a>""",
        r" \2 (\1) ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


# ---------------------------------------------------------------------------
# Active-sponsor-channel binding (confused-deputy authorization fix)
# ---------------------------------------------------------------------------
# When an inbound Teams message from a configured Agent Identity sponsor is
# successfully pushed to the LLM, record (sponsor_user_id -> chat_id) so
# add_member / share_file can verify the sponsor is actively engaged in the
# chat the LLM is asking to mutate. See:
#   src/entrabot/identity/active_channel.py
#   docs/runbooks/hard-won-learnings.md (Learning #67)


async def _get_sponsor_records_for_binding() -> list:
    """Return cached sponsor records for the binding-update path.

    Wrapper so tests can patch a single symbol on ``mcp_server``. Reuses
    the existing sponsor cache used by add_member / share_file (Gate 1)
    via the shared ``entrabot.tools.files._get_sponsor_records`` helper.
    """
    from entrabot.tools.files import _get_sponsor_records

    return await _get_sponsor_records()


def _parse_iso8601_to_epoch(value: str) -> float | None:
    """Parse an ISO 8601 string (Graph ``sent_at``) to epoch seconds.

    Returns ``None`` on missing / non-string / unparseable input. Accepts
    both the ``Z`` UTC suffix and explicit ``+00:00`` offsets.
    """
    if not value or not isinstance(value, str):
        return None
    import datetime as _dt

    try:
        s = value.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


async def _maybe_record_sponsor_binding(
    *,
    message: dict,
    chat_id: str,
) -> None:
    """Update the active-channel binding if conditions are met.

    MUST be called only AFTER the channel-push to ``write_stream`` has
    succeeded. Skipped silently when:

    - ``chat_id`` is empty or the synthetic ``"email"`` channel (email
      arrival cannot authorize a Teams mutation),
    - the message has no Graph ``sender_id`` (cannot key the binding),
    - the message's ``sent_at`` is missing / unparseable / past TTL /
      in the future,
    - the sender is not a configured Agent Identity sponsor.

    Failures of the sponsor lookup are logged and swallowed — the
    binding is a defense layer, not a critical path.
    """
    if not chat_id or chat_id == "email":
        return

    sender_id = str(message.get("sender_id") or "").strip()
    if not sender_id:
        return

    sent_at_epoch = _parse_iso8601_to_epoch(message.get("sent_at", ""))
    if sent_at_epoch is None:
        return

    try:
        sponsors = await _get_sponsor_records_for_binding()
    except Exception as exc:
        if logger:
            logger.warning(
                "Sponsor-binding lookup failed for chat %s: %s", chat_id, exc
            )
        return

    sender_lower = sender_id.lower()
    matched = next(
        (
            s
            for s in sponsors
            if (getattr(s, "user_id", "") or "").lower() == sender_lower
        ),
        None,
    )
    if matched is None:
        return

    from entrabot.identity.active_channel import get_bindings

    recorded = get_bindings().record(
        sponsor_user_id=sender_id,
        chat_id=chat_id,
        graph_sent_at_epoch=sent_at_epoch,
        message_id=str(message.get("message_id") or ""),
    )
    if recorded and logger:
        logger.info(
            "Recorded active-sponsor-channel binding: user_id=%s chat_id=%s",
            sender_id,
            chat_id,
        )


async def _push_channel_notification(
    message: dict,
    *,
    chat_id: str | None = None,
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
        recipient="entrabot-agent",
        summary=_summarize_content(message.get("content", "")),
        action="push_channel_notification",
        content_ref=message.get("message_id"),
        metadata={
            "chat_id": resolved_chat_id,
            "ts": message.get("sent_at"),
        },
    )

    # Push unconditionally. Clients that don't handle
    # ``notifications/claude/channel`` drop the message silently per
    # the MCP spec — no harm done. Observation above is already
    # unconditional so daily summaries still see everything regardless.

    write_stream = _state.get("_write_stream")
    if not write_stream:
        if logger:
            logger.warning(
                "Channel push skipped — write stream not available (logged inbound %s from %s)",
                message.get("message_id", "?"),
                message.get("from", "?"),
            )
        return

    # The push envelope is restricted to {chat_id, message_id, user, ts}.
    # Forensic envelope dumps on 2026-04-28 confirmed Claude Code's MCP
    # client closes the stream whenever `params.meta` carries custom
    # fields like `reply_to_ids` or `quoted_messages`, regardless of
    # their content. The earlier 2026-04-24 sanitizer fix on
    # `params.content` is still required as defense-in-depth — see
    # docs/runbooks/mcp-disconnect-investigation.md.
    meta: dict = {
        "chat_id": resolved_chat_id,
        "message_id": message.get("message_id", ""),
        "user": _summarize_content(message.get("from", "unknown")),
        "ts": message.get("sent_at", ""),
    }

    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={
            "content": _summarize_content(message.get("content", "")),
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

    # authorization fix: only after a successful push do we consider the sponsor
    # "actively engaged" in this chat for authorization purposes. If the
    # LLM never received the message, the server must not mint authority
    # off of it.
    await _maybe_record_sponsor_binding(
        message=message, chat_id=resolved_chat_id
    )

    if logger:
        logger.info(
            "Pushed Teams message from %s: %s",
            message.get("from", "?"),
            message.get("content", "")[:50],
        )


@mcp.tool()
async def send_teams_message(
    message: str,
    content_type: str = "html",
    mentions: list[dict] | None = None,
    chat_id: str = "",
    ctx: Context | None = None,
) -> str:
    """Send a message via Microsoft Teams, then listen for the reply.

    You must pass ``chat_id`` — every Teams chat has its own ID. Get one
    from ``create_chat`` (for a new 1:1 DM) or from the ``meta.chat_id``
    of a channel notification that the background poll pushed to you.

    **Auto-wait behavior.** On hosts that lack a channel push (Copilot
    CLI, Codex, and other non-Claude-Code hosts), this tool
    automatically blocks after sending until the human sponsor DMs back
    in the same chat — the sponsor's reply is returned in the result.
    This is transparent on Claude Code (which has notifications/channel
    push) — the tool returns immediately there.

    The auto-wait uses the same mechanism as ``wait_for_sponsor_dm``
    (sponsor-gated polling with the dog animation) but is built into
    this tool so hosts that don't surface MCP instructions can't skip
    it by accident.

    REQUIRED FOLLOW-UP — when the returned JSON contains a non-empty
    ``sponsor_reply.message_id``, you MUST immediately reply by calling
    ``send_teams_message`` again with the same ``chat_id`` and a
    response that addresses ``sponsor_reply.content_text``. A 1:1 DM is
    a direct conversation: the sponsor is waiting in Teams, not
    watching your terminal. Treat ``sponsor_reply.content_text`` as the
    user's next turn — read it, answer it, send the answer back via
    this tool. Do NOT respond in CLI text — the sponsor will not see
    it. The next ``send_teams_message`` call will auto-wait again,
    forming a turn-by-turn loop. End the loop only when the sponsor
    explicitly says they're done, or the original task is fully
    complete. The ``_next_action`` field on the result restates this
    rule for hosts that don't surface tool docstrings into the LLM
    system prompt.

    ``content_type`` defaults to ``"html"`` per the channel-discipline
    rule in ``prompts/anatomy/channel-discipline.md`` ("Always HTML in
    Teams — no exceptions"). Wrap paragraphs in ``<p>…</p>``; escape
    literal ``<``, ``>``, ``&`` as ``&lt;``, ``&gt;``, ``&amp;``. Pass
    ``content_type="text"`` only when plain text is genuinely required.

    To @mention someone, put ``<at id="N">Display Name</at>`` tags in
    the HTML body and pass a mentions list. Each mention dict needs:
      - id: int matching the at-tag id
      - name: display name
      - user_id: their Entra user GUID (get from chat members via read_teams_messages)

    Example — DM someone:
      chat_id = await create_chat(target_email="alice@example.com")
      await send_teams_message("<p>Hey Alice</p>", chat_id=chat_id)

    Example — @mention in a chat:
      message: '<p><at id="0">Alice Example</at> check this out</p>'
      mentions: [{"id": 0, "name": "Alice Example", "user_id": "abc-123"}]
      chat_id: "19:...@thread.v2"

    Args:
        message: The text to send (HTML by default).
        content_type: "html" (default) or "text" — see note above.
        mentions: Optional list of mention dicts for @mentions.
        chat_id: The chat to send to. Required.

    Returns:
        JSON with message_id, sent_at, and (when waiting) the sponsor's
        reply in a ``sponsor_reply`` object. When ``sponsor_reply`` is
        present, the result also includes a ``_next_action`` string
        instructing you to call ``send_teams_message`` again with the
        reply — this is the turn-by-turn conversation loop. Honor it
        regardless of host.
    """
    await _initialize()

    from entrabot.tools.teams import send

    target_chat = chat_id
    if not target_chat:
        return json.dumps(
            {
                "error": (
                    "chat_id is required — pass the chat_id of the target Teams "
                    "chat (create one via create_chat if needed)."
                )
            }
        )

    # Outbound-discipline hook 1: placeholder check (HARD BLOCK).
    # Substantive Teams DMs require a recent post_thinking_placeholder
    # for this chat_id. See scripts/hooks/README.md.
    try:
        _check_placeholder_discipline(message, str(target_chat))
    except MissingPlaceholderError as exc:
        return json.dumps(
            {
                "error": str(exc),
                "error_type": "MissingPlaceholderError",
                "remediation": "post_thinking_placeholder + retry",
            }
        )

    await _ensure_valid_token()

    # In delegated mode the message comes from the human's identity,
    # so prefix it to distinguish agent-sent messages from the human.
    prefix = None
    if _identity and _identity.session.auth_mode == "delegated":
        prefix = "[EntraBot]"

    result = await _with_token_retry(
        send,
        chat_id=str(target_chat),
        message=message,
        content_type=content_type,
        mentions=mentions,
        prefix=prefix,
    )

    # Outbound-discipline hook 2: commitment-language → promise (WARN).
    # Scans the outbound text for "I'll do X" / "at the next pause"
    # phrases. If we find one and there's no recent add_promise covering
    # this chat_id (or chat_id="terminal"), tag the result with
    # _discipline_warning so the LLM sees it next turn. Does NOT block.
    if isinstance(result, dict):
        warning = await _check_commitment_language(message, str(target_chat))
        if warning is not None:
            result["_discipline_warning"] = warning

    # Log the outbound message for the daily summary.
    _log_interaction_safe(
        channel=detect_channel(str(target_chat)),
        direction="outbound",
        sender="entrabot-agent",
        recipient=str(target_chat),
        summary=_summarize_content(message),
        action="send_teams_message",
        content_ref=result.get("message_id") if isinstance(result, dict) else None,
        metadata={
            "content_type": content_type,
            "had_mentions": bool(mentions),
        },
    )

    # Determine whether to auto-wait for a sponsor reply.
    # Claude Code has the notifications/channel push so it doesn't need
    # to block here. All other hosts (Copilot CLI, Codex, etc.) do.
    _CHANNEL_PUSH_HOSTS = frozenset({"claude-code", "claude-ai", "claude code"})
    host = _current_host()
    cached = str(_state.get("cached_host") or "")
    has_channel_push = host in _CHANNEL_PUSH_HOSTS or cached in _CHANNEL_PUSH_HOSTS

    should_wait = not has_channel_push

    if not should_wait:
        return json.dumps(result, indent=2)

    # --- Auto-wait: block until sponsor replies ---
    import asyncio
    import contextlib
    from collections import deque

    from entrabot.identity.sponsors import (
        SponsorGate,
        load_agent_identity_sponsor_gate,
    )
    from entrabot.tools.teams import filter_human_messages, read
    from entrabot.tools.wait_tool import (
        wait_listener_banner,
        wait_loop,
    )

    wait_config = _state.get("config") or get_config()

    gate: SponsorGate | None = _state.get("sponsor_gate")  # type: ignore[assignment]
    if gate is None:
        try:
            gate = load_agent_identity_sponsor_gate(wait_config)
            _state["sponsor_gate"] = gate
            if logger:
                logger.info(
                    "send_teams_message auto-wait: sponsor gate loaded ids=%s upns=%s mails=%s",
                    sorted(gate.user_ids),
                    sorted(gate.upns),
                    sorted(gate.mails),
                )
        except Exception as exc:
            # If we can't load the gate, return the send result without
            # blocking — degrade gracefully rather than crash.
            if logger:
                logger.warning(
                    "send_teams_message: could not load sponsor gate for "
                    "auto-wait, returning without blocking: %s",
                    exc,
                )
            if isinstance(result, dict):
                result["wait_skipped"] = str(exc)
            return json.dumps(result, indent=2)

    dedup: deque[tuple[str, str]] = _state.setdefault(  # type: ignore[assignment]
        "wait_tool_dedup", deque()
    )

    agent_display_name = (wait_config.agent_user_upn or "").split("@", 1)[0] or "EntraBot Agent"

    def list_chat_ids() -> list[str]:
        watched = _state.get("watched_chats") or {}
        return list(watched.keys())

    async def read_chat_for_wait(cid: str) -> list[dict]:
        raw = await _with_token_retry(read, chat_id=cid, count=10)
        return filter_human_messages(raw, agent_display_name)

    async def heartbeat(elapsed_s: float = 0.0) -> None:
        if ctx is not None:
            import os as _os

            use_color = _os.environ.get("NO_COLOR", "") == ""
            frame = wait_listener_banner(color=use_color, elapsed_s=elapsed_s)
            with contextlib.suppress(Exception):
                await ctx.report_progress(progress=elapsed_s, total=None, message=frame)

    # Show the dog splash
    if ctx is not None:
        import os as _os

        use_color = _os.environ.get("NO_COLOR", "") == ""
        banner = wait_listener_banner(color=use_color)
        with contextlib.suppress(Exception):
            await ctx.report_progress(progress=0.0, total=None, message=banner)

    coro = wait_loop(
        list_chat_ids=list_chat_ids,
        read_chat=read_chat_for_wait,
        gate=gate,
        dedup=dedup,
        heartbeat=heartbeat,
    )
    try:
        picked = await coro
    except asyncio.CancelledError:
        # Ctrl+C — return the send result without the reply
        if isinstance(result, dict):
            result["wait_cancelled"] = True
        return json.dumps(result, indent=2)

    # Build the sponsor reply payload
    body_html = picked.get("content") or picked.get("content_html") or ""
    body_text = _summarize_content(body_html, limit=4000) if body_html else ""

    if isinstance(result, dict):
        reply_chat_id = str(picked.get("chat_id") or "")
        result["sponsor_reply"] = {
            "chat_id": reply_chat_id,
            "message_id": str(picked.get("message_id") or ""),
            "sender": str(picked.get("sender") or picked.get("from") or ""),
            "sender_id": str(picked.get("sender_id") or ""),
            "sent_at": str(picked.get("sent_at") or ""),
            "content_text": body_text,
            "content_html": body_html or None,
        }
        # Loop-continuation hint surfaced regardless of whether the
        # host CLI loaded the body prompt / AGENTS.md / docstring. This
        # is the conversation contract: a 1:1 DM stays in Teams. The
        # model must see this in the tool result, not just the
        # docstring, because some hosts (Copilot CLI when launched
        # outside the entrabot repo) never surface MCP instructions.
        result["_next_action"] = (
            "REQUIRED: call send_teams_message again with "
            f"chat_id={reply_chat_id!r} and a reply that addresses "
            "sponsor_reply.content_text. Do NOT answer in CLI text — "
            "the sponsor is waiting in Teams. The next call auto-waits "
            "for their next reply, forming a turn-by-turn loop. End "
            "only when the sponsor says they're done or the task is "
            "complete."
        )

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
        return json.dumps(
            {"error": ("chat_id is required — pass the chat_id of the target Teams chat.")}
        )

    from entrabot.tools.teams import post_thinking_placeholder as _post

    await _ensure_valid_token()
    message_id = await _with_token_retry(
        _post,
        chat_id=chat_id,
        text=text,
    )

    # Record placeholder timestamp so the placeholder-discipline hook
    # in ``send_teams_message`` can verify a recent placeholder for
    # this chat. ``time.monotonic()`` matches ``_state["token_acquired_at"]``
    # for consistency.
    recent: dict[str, float] = _state.setdefault(  # type: ignore[assignment]
        "recent_placeholders", {}
    )
    recent[chat_id] = time.monotonic()

    _log_interaction_safe(
        channel=detect_channel(chat_id),
        direction="outbound",
        sender="entrabot-agent",
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
        return json.dumps({"error": "chat_id and placeholder_id are required."})

    from entrabot.tools.teams import update_placeholder as _update

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
        sender="entrabot-agent",
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
        return json.dumps({"error": "chat_id and placeholder_id are required."})
    if mode not in ("edit", "delete_repost"):
        return json.dumps({"error": f"invalid mode: {mode!r} (expected 'edit' or 'delete_repost')"})

    # Audit before mutating — per security.md "Audit before acting". Fail
    # closed: if the audit write raises, the Graph call does not proceed.
    from entrabot.tools.audit import log_event

    config = get_config()
    if _identity:
        agent_id = _identity.session.user_id or config.agent_id or config.blueprint_app_id
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id
        attribution = "agent"
    log_event(
        action="resolve_placeholder",
        resource=f"{chat_id}:{placeholder_id}",
        outcome="pending",
        agent_id=agent_id,
        metadata={"mode": mode, "content_type": content_type},
        attribution_type=attribution,
    )

    from entrabot.tools.teams import resolve_placeholder as _resolve

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
        sender="entrabot-agent",
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
        return json.dumps(
            {"error": ("chat_id is required — pass the chat_id of the target Teams chat.")}
        )
    if not message_id:
        return json.dumps(
            {
                "error": (
                    "message_id is required — pass the message_id of the "
                    "agent's own message to delete."
                )
            }
        )

    # Audit before mutating — per security.md "Audit before acting". Fail
    # closed: if the audit write raises, the Graph call does not proceed.
    from entrabot.tools.audit import log_event

    config = get_config()
    if _identity:
        agent_id = _identity.session.user_id or config.agent_id or config.blueprint_app_id
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id
        attribution = "agent"
    log_event(
        action="delete_teams_message",
        resource=f"{chat_id}:{message_id}",
        outcome="pending",
        agent_id=agent_id,
        metadata={},
        attribution_type=attribution,
    )

    from entrabot.tools.teams import delete_chat_message

    await _ensure_valid_token()
    deleted = await _with_token_retry(
        delete_chat_message,
        chat_id=chat_id,
        message_id=message_id,
    )

    _log_interaction_safe(
        channel=detect_channel(chat_id),
        direction="outbound",
        sender="entrabot-agent",
        recipient=chat_id,
        summary=f"deleted message {message_id}" if deleted else f"delete failed for {message_id}",
        action="delete_teams_message",
        content_ref=message_id,
        metadata={
            "chat_id": chat_id,
            "message_id": message_id,
            "deleted": bool(deleted),
        },
    )

    if deleted:
        return json.dumps({"deleted": True, "message_id": message_id}, indent=2)
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
        return json.dumps(
            {"error": ("to is required — pass a comma-separated list of email addresses.")}
        )
    if not subject or not subject.strip():
        # Graph accepts empty subjects on replies, but reject uniformly —
        # audit resource needs something to key on, and a blank subject
        # is almost always a programming error.
        return json.dumps({"error": "subject is required."})

    # Audit before mutating — per security.md "Audit before acting". Fail
    # closed: if the audit write raises, the Graph call does not proceed.
    from entrabot.tools.audit import log_event

    config = get_config()
    if _identity:
        agent_id = _identity.session.user_id or config.agent_id or config.blueprint_app_id
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id
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

    from entrabot.tools.email import EmailSendError
    from entrabot.tools.email import send_email as _send

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
            sender="entrabot-agent",
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
        sender="entrabot-agent",
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
async def read_email(
    message_id: str,
    mailbox: str = "",
) -> str:
    """Fetch the full body + recipients + headers of an inbound email.

    Use when the 60-second email-poll channel push has truncated the
    body (forwarded recipient lists, threaded replies, attached
    metadata) and the agent needs to read past where the preview cut
    off. Calls Graph ``GET /me/messages/{message_id}`` with a
    ``$select`` covering body (text + HTML), all recipient lists,
    sender, subject, message headers, and ``hasAttachments``.

    Args:
        message_id: The Graph ``id`` of the message (from the channel
            push or a prior ``read_email`` / poll result).
        mailbox: Optional shared mailbox UPN/address. Empty (default)
            reads from the Agent User's own mailbox.

    Returns:
        JSON with the full Graph message object on success, or
        ``{"error": "...", "status": <code>, "message_id": "..."}`` on
        404 / 403 / 5xx. 401 triggers an automatic token refresh and
        retry; only an exhausted retry surfaces.
    """
    await _initialize()

    if not message_id or not message_id.strip():
        return json.dumps({"error": "message_id is required."})

    from entrabot.tools.email import read_email as _read

    await _ensure_valid_token()

    result = await _with_token_retry(
        _read,
        message_id=message_id,
        mailbox=mailbox,
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
    from entrabot.tools.cards import (
        build_result_card,
        card_attachment,
        task_status_card,
        tool_activity_card,
    )
    from entrabot.tools.teams import send

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
        return json.dumps(
            {
                "error": (
                    "chat_id is required — pass the chat_id of the target Teams "
                    "chat (create one via create_chat if needed)."
                )
            }
        )

    await _ensure_valid_token()

    # Cards deliberately don't carry the [EntraBot] prefix — the card
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
        sender="entrabot-agent",
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
    from entrabot.tools.teams import list_members

    target_chat = chat_id
    if not target_chat:
        return json.dumps(
            {
                "error": (
                    "chat_id is required — pass the chat_id of the chat whose "
                    "members you want to list."
                )
            }
        )

    await _ensure_valid_token()
    result = await _with_token_retry(
        list_members,
        chat_id=str(target_chat),
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def add_teams_member(
    email: str,
    chat_id: str,
    requester_email: str,
    tenant_id: str = "",
) -> str:
    """Add a new member to a Teams chat. The REQUESTER must be an Agent Identity sponsor.

    Authorization model (mirrors share_file): only sponsors can ask the
    agent to invite anyone. A sponsor may invite anyone they choose;
    the invitee (``email``) is unrestricted.

    Args:
        email: The invitee's email address (e.g., 'user@example.com').
        chat_id: The chat to add the member to. Required.
        requester_email: REQUIRED. Email of the human (sponsor) who asked
            the agent to invite. Derive this from the active
            conversation context — the sender of the Teams message that
            triggered this turn. NEVER use the agent's own address.
            NEVER fabricate.
        tenant_id: Optional override. Auto-resolved from email domain if empty.

    Returns:
        JSON with member_id, display_name, and roles, or ``{"error": "..."}``.

    Errors:
        RequesterNotSponsorError: requester_email is not in the sponsor allowlist.
        RequesterNotInChatError: requester is a sponsor but not a member of chat_id.
    """
    await _initialize()
    from entrabot.errors import (
        FilesError,
        RequesterNotInChatError,
        RequesterNotSponsorError,
    )
    from entrabot.tools.teams import add_member

    if not chat_id:
        return json.dumps(
            {
                "error": (
                    "chat_id is required — pass the chat_id of the target Teams "
                    "chat (create one via create_chat if needed)."
                )
            }
        )
    if not requester_email:
        return json.dumps(
            {
                "error": (
                    "requester_email is required — pass the email of the human "
                    "who asked you to add this member. Never use the agent's own "
                    "address."
                ),
                "error_type": "ValueError",
            }
        )

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
    try:
        result = await _with_token_retry(
            add_member,
            chat_id=chat_id,
            email=email,
            requester_email=requester_email,
            tenant_id=tenant_id or None,
        )
    except (RequesterNotSponsorError, RequesterNotInChatError, FilesError) as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})
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
    from entrabot.tools.teams import create_one_on_one_chat

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
    from entrabot.tools.teams import read

    target_chat = chat_id
    if not target_chat:
        return json.dumps(
            {
                "error": (
                    "chat_id is required — pass the chat_id of the chat to read "
                    "from (create one via create_chat if needed)."
                )
            }
        )

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
    from entrabot.tools.teams import filter_human_messages, read

    if not chat_id:
        return json.dumps(
            {
                "error": (
                    "chat_id is required — pass the chat_id of the Teams chat "
                    "to watch (create one via create_chat if needed)."
                )
            }
        )

    # Must match the displayName that Graph API returns in message.from.user.displayName
    # NOT the UPN — Graph returns "EntraBot Agent", not the agent's UPN.
    agent_display_name = "EntraBot Agent"

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
async def wait_for_sponsor_dm(
    timeout_seconds: int = 0,
    ctx: Context | None = None,
) -> str:
    """Block until a Sponsor sends a Teams DM, then return their message.

    **When to call this tool.** Any time you proactively send a Teams
    DM to a 1:1 sponsor chat as part of completing the operator's
    request, the human's next-turn reaction lands in Teams, not in the
    host CLI's terminal. You must therefore stay in-session and listen
    for it. After every such proactive 1:1 DM, immediately call
    `wait_for_sponsor_dm` — even when the task itself was trivial.
    Sending a DM is what opens the conversation; you do not get to
    fire-and-forget. The one explicit exception is when the operator
    says "no need to wait" or "don't listen for a reply" in the same
    turn.

    The canonical worked example is long-running work with a "ping me
    when it's done" promise — e.g. "I'm going to lunch. Ping me when
    the build's green." Send the human a heads-up via
    ``send_teams_message`` first (so they know what to expect), do the
    work, then call this tool. The tool sleeps INSIDE this MCP session
    until the Sponsor (and only the Sponsor — the Agent Identity's
    configured human sponsors, looked up at runtime via Graph) DMs the
    agent in any watched chat. The Sponsor's message text becomes this
    tool's return value, which the model then sees as next-turn input.
    The same pattern applies to any other proactive 1:1 DM, including
    quick "done!" pings on two-second tasks.

    REQUIRED FOLLOW-UP — when this tool returns a payload with
    ``chat_type == "oneOnOne"`` and a non-empty ``message_id``, you
    MUST immediately reply to the Sponsor by calling
    ``send_teams_message`` with that ``chat_id`` and a response that
    addresses what they said. A 1:1 DM is a direct conversation: the
    Sponsor is waiting in Teams, not watching your terminal. Treat
    the returned ``content_text`` as the user's next turn — read it,
    answer it, send the answer to ``chat_id``. After sending the
    reply, call ``wait_for_sponsor_dm`` again to wait for their next
    message. End the loop only when the Sponsor explicitly says they
    are done, or the original task is fully complete.

    For group chats (``chat_type == "group"`` or ``"meeting"``), do
    NOT auto-reply. A group message is informational unless the
    Sponsor explicitly addressed the agent or asked a question. Use
    your judgment: reply only if the message is clearly directed at
    you or requires a response. Otherwise, treat it as context and
    return to the wait or to the operator's outstanding task.

    On a timeout return (``timed_out`` is true, ``message_id`` empty),
    do NOT send anything to Teams. Either call this tool again with a
    longer ``timeout_seconds``, or ask the operator in the host CLI
    what to do next.

    Why this exists: Copilot CLI (and Claude Code) do not auto-display
    asynchronous Teams notifications in the operator's terminal. This
    tool turns the wait into a tool call so the next sponsor DM is
    handled in-session — no spawned daemon, no PTY hijack, no screen
    blanking. Ctrl+C in the host CLI cancels cleanly.

    Sponsor gating is mechanical: non-Sponsor messages never reach the
    return value. The wait silently dedupes already-returned messages
    so reruns of the tool do not replay the same DM.

    Args:
        timeout_seconds: Optional cap. ``0`` (default) means block
            indefinitely until a Sponsor DM arrives or the operator
            cancels. Positive values cause the tool to return a
            structured timeout payload (``timed_out: true``) when the
            cap is hit, instead of raising.

    Returns:
        JSON string with the sponsor's message. Keys: ``chat_id``,
        ``message_id``, ``sender``, ``sender_id``, ``sent_at``,
        ``content_text``, ``content_html``, ``metadata``,
        ``timed_out``. When ``timed_out`` is true all other fields are
        empty strings.
    """
    import asyncio
    import contextlib
    from collections import deque

    from entrabot.identity.sponsors import (
        SponsorGate,
        load_agent_identity_sponsor_gate,
    )
    from entrabot.tools.audit import log_event
    from entrabot.tools.teams import filter_human_messages, read
    from entrabot.tools.wait_tool import (
        WaitForSponsorDmResult,
        wait_loop,
    )

    await _initialize()

    config = _state.get("config") or get_config()
    agent_id = (
        (_identity.session.user_id if _identity else None)
        or config.agent_id
        or config.blueprint_app_id
    )
    attribution = _identity.session.attribution_type if _identity else "agent"

    log_event(
        action="wait_for_sponsor_dm.start",
        resource="teams:watched_chats",
        outcome="pending",
        agent_id=agent_id,
        metadata={"timeout_seconds": timeout_seconds},
        attribution_type=attribution,
    )

    # Lazy-load and cache the sponsor gate. Cheap to compute, but we
    # avoid hitting Graph on every wait_for_sponsor_dm invocation.
    gate: SponsorGate | None = _state.get("sponsor_gate")  # type: ignore[assignment]
    if gate is None:
        try:
            gate = load_agent_identity_sponsor_gate(config)
            _state["sponsor_gate"] = gate
            # Diagnostic: dump the loaded sponsor set once so operators
            # can correlate against rejection logs in wait_tool.
            logger.info(
                "wait_for_sponsor_dm: sponsor gate loaded ids=%s upns=%s mails=%s",
                sorted(gate.user_ids),
                sorted(gate.upns),
                sorted(gate.mails),
            )
        except Exception as exc:
            log_event(
                action="wait_for_sponsor_dm.start",
                resource="teams:watched_chats",
                outcome="error",
                agent_id=agent_id,
                metadata={"reason": "sponsor_gate_load_failed", "error": str(exc)},
                attribution_type=attribution,
            )
            raise

    dedup: deque[tuple[str, str]] = _state.setdefault(  # type: ignore[assignment]
        "wait_tool_dedup", deque()
    )

    agent_display_name = (config.agent_user_upn or "").split("@", 1)[0] or "EntraBot Agent"

    def list_chat_ids() -> list[str]:
        watched = _state.get("watched_chats") or {}
        return list(watched.keys())

    async def read_chat(chat_id: str) -> list[dict]:
        raw = await _with_token_retry(read, chat_id=chat_id, count=10)
        # Filter out the agent's own messages so the agent never gates
        # on its own echo. Sponsor gate runs on top of this.
        return filter_human_messages(raw, agent_display_name)

    async def heartbeat(elapsed_s: float = 0.0) -> None:
        if ctx is not None:
            import os as _os

            from entrabot.tools.wait_tool import wait_listener_banner

            use_color = _os.environ.get("NO_COLOR", "") == ""
            # Re-emit the banner with elapsed time so the dog stays
            # visible. Copilot CLI overwrites prior progress messages,
            # so a single-line cycling frame would erase the dog.
            frame = wait_listener_banner(color=use_color, elapsed_s=elapsed_s)
            with contextlib.suppress(Exception):
                await ctx.report_progress(progress=elapsed_s, total=None, message=frame)

    if ctx is not None:
        # One-shot startup splash. Honors NO_COLOR per the de-facto
        # convention (https://no-color.org). Emitted before the wait
        # loop so the operator sees "I'm listening" before any
        # heartbeat refreshes the banner with elapsed time.
        import os as _os

        from entrabot.tools.wait_tool import wait_listener_banner

        use_color = _os.environ.get("NO_COLOR", "") == ""
        banner = wait_listener_banner(color=use_color)
        with contextlib.suppress(Exception):
            await ctx.report_progress(progress=0.0, total=None, message=banner)

    coro = wait_loop(
        list_chat_ids=list_chat_ids,
        read_chat=read_chat,
        gate=gate,
        dedup=dedup,
        heartbeat=heartbeat,
    )
    try:
        if timeout_seconds and timeout_seconds > 0:
            picked = await asyncio.wait_for(coro, timeout=float(timeout_seconds))
        else:
            picked = await coro
    except asyncio.CancelledError:
        log_event(
            action="wait_for_sponsor_dm.cancelled",
            resource="teams:watched_chats",
            outcome="cancelled",
            agent_id=agent_id,
            metadata={},
            attribution_type=attribution,
        )
        raise
    except TimeoutError:
        log_event(
            action="wait_for_sponsor_dm.timeout",
            resource="teams:watched_chats",
            outcome="timeout",
            agent_id=agent_id,
            metadata={"timeout_seconds": timeout_seconds},
            attribution_type=attribution,
        )
        return WaitForSponsorDmResult.timeout(timeout_seconds=int(timeout_seconds)).to_json()

    body_html = picked.get("content") or picked.get("content_html") or ""
    body_text = _summarize_content(body_html, limit=4000) if body_html else ""

    chat_id_str = str(picked.get("chat_id") or "")
    chat_type_cache = _state.setdefault("wait_tool_chat_type_cache", {})
    chat_type = chat_type_cache.get(chat_id_str, "")
    if not chat_type and chat_id_str:
        try:
            from entrabot.tools.teams import (
                acquire_agent_user_token,
                fetch_chat_type,
            )

            tok = acquire_agent_user_token(config)
            chat_type = await fetch_chat_type(chat_id=chat_id_str, token=tok)
            if chat_type:
                chat_type_cache[chat_id_str] = chat_type
        except Exception as exc:  # noqa: BLE001 - chat_type is best-effort
            if logger:
                logger.warning(
                    "Failed to detect chat_type for chat %s: %r",
                    chat_id_str,
                    exc,
                )
            chat_type = ""

    result = WaitForSponsorDmResult(
        chat_id=chat_id_str,
        message_id=str(picked.get("message_id") or ""),
        sender=str(picked.get("sender") or picked.get("from") or ""),
        sender_id=str(picked.get("sender_id") or ""),
        sent_at=str(picked.get("sent_at") or ""),
        content_text=body_text,
        content_html=body_html or None,
        metadata={
            "reply_to_ids": picked.get("reply_to_ids") or [],
        },
        chat_type=chat_type,
    )

    log_event(
        action="wait_for_sponsor_dm.return",
        resource=f"teams:{result.chat_id}",
        outcome="success",
        agent_id=agent_id,
        metadata={
            "message_id": result.message_id,
            "sender_id": result.sender_id,
        },
        attribution_type=attribution,
    )

    return result.to_json()


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
    Events are written to ~/.entrabot/audit/ as daily JSONL files.

    Args:
        action: What the agent is doing (e.g., "file_read", "code_execute").
        resource: What is being acted on (e.g., file path, URL, repo name).
        outcome: "success", "failure", or "pending" (default "success").
        metadata: Optional JSON string of key-value pairs with extra context.

    Returns:
        JSON with event_id, timestamp, and the recorded event.
    """
    from entrabot.tools.audit import log_event

    config = get_config()
    meta = json.loads(metadata) if metadata else {}

    # Identity-aware attribution (eng review Tension 1)
    if _identity:
        agent_id = _identity.session.user_id or config.agent_id or config.blueprint_app_id
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id
        attribution = "agent" if agent_id else "none"

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
    from entrabot.tools.teams import fetch_hosted_image

    if not _is_graph_url(url):
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

    with tempfile.NamedTemporaryFile(suffix=ext, prefix="entrabot_img_", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    return json.dumps(
        {
            "file_path": tmp_path,
            "size_bytes": len(image_bytes),
        }
    )


@mcp.tool()
async def whoami() -> str:
    """Show the current agent identity, Teams connection status, and permissions.
    Call this to verify the agent is authenticated and connected to Teams.

    Authentication is automatic — no credentials needed.

    Returns:
        JSON with agent identity details and connection status.
    """
    await _initialize()
    from entrabot.tools.identity import whoami as _whoami

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

        # Install opt-in efferent-copy middleware before any tool dispatch runs.
        # With EFFERENT_COPY_ENABLE=1, schema-compatible peers advertising
        # observe(tool_name, args[, result]) become sinks. Zero sinks means
        # no wrapping and body behavior unchanged. Any discovery failure is
        # swallowed: the body MUST keep working without sinks.
        try:
            main_loop = asyncio.get_running_loop()
            sinks = await efferent_copy.discover_sinks()
            efferent_copy.install_into_fastmcp(mcp, sinks, main_loop=main_loop)
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
            # Issue #17: flush any pending per-chat cursor writes before exit
            # so the next process inherits the latest watermarks instead of
            # re-bootstrapping from "newest at boot."
            try:
                _flush_chat_cursors()
            except Exception as exc:  # noqa: BLE001
                if logger:
                    logger.warning(
                        "Cursor flush on shutdown failed: %s: %s",
                        type(exc).__name__,
                        exc,
                    )


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


# ---------------------------------------------------------------------------
# Outstanding-promise store (durable, identity-scoped; see tools/promises.py)
# ---------------------------------------------------------------------------
def _promise_audit_ids() -> tuple[str, str]:
    """Resolve (agent_id, attribution_type) for audit rows."""
    config = get_config()
    if _identity:
        agent_id = _identity.session.user_id or config.agent_id or config.blueprint_app_id
        attribution = _identity.session.attribution_type
    else:
        agent_id = config.agent_id or config.blueprint_app_id
        attribution = "agent"
    return agent_id, attribution


@mcp.tool()
async def add_promise(
    chat_id: str,
    description: str,
    due_by: str = "",
) -> str:
    """Record an outstanding human-facing commitment — survives restart.

    Use instead of ``TaskCreate`` for "I'll report back when X lands"
    shaped commitments. Persisted to the entrabot blob under the Agent
    Identity (``promises.jsonl``). Resolve via ``resolve_promise`` once
    both the underlying work completes AND the human-facing follow-up
    has been posted in the correct chat.

    Args:
        chat_id: The chat the promise is owed to. Use "terminal" for
            terminal-driven work and "email" for email threads when
            there's no Teams chat to tie it to.
        description: Enough detail to execute the follow-up without
            re-reading the conversation.
        due_by: Optional ISO-8601 deadline. Empty string means no due date.

    Returns:
        JSON of the persisted Promise, or ``{"error": "..."}`` on
        validation failure.
    """
    await _initialize()

    if not chat_id:
        return json.dumps(
            {
                "error": (
                    "chat_id is required — pass the target chat_id, or "
                    "'terminal' / 'email' for non-Teams promises."
                )
            }
        )
    if not description or not description.strip():
        return json.dumps({"error": "description is required — describe the follow-up."})

    from entrabot.tools.audit import log_event
    from entrabot.tools.promises import add_promise as _add

    agent_id, attribution = _promise_audit_ids()
    log_event(
        action="promise.add",
        resource="new",
        outcome="pending",
        agent_id=agent_id,
        metadata={"chat_id": chat_id, "has_due_by": bool(due_by)},
        attribution_type=attribution,
    )

    await _ensure_valid_token()

    async def _call(token: str) -> object:  # noqa: ARG001 — token unused
        return await _add(
            chat_id=chat_id,
            description=description,
            due_by=due_by or None,
        )

    promise = await _with_token_retry(_call)

    _log_interaction_safe(
        channel=detect_channel(
            chat_id
            if chat_id not in {"terminal", "email"}
            else None
            if chat_id == "terminal"
            else "email"
        ),
        direction="outbound",
        sender="entrabot-agent",
        recipient=chat_id,
        summary=f"promise: {description[:120]}",
        action="promise.add",
        content_ref=promise.id,
        metadata={
            "promise_id": promise.id,
            "chat_id": chat_id,
            "due_by": due_by or None,
        },
    )

    return json.dumps(promise.to_entry(), indent=2)


@mcp.tool()
async def list_promises(open_only: bool = True) -> str:
    """List outstanding promises for this Agent Identity.

    Returns JSON array of ``{id, chat_id, description, created_at,
    due_by, status, resolved_at, resolution}``. Default shows only open
    promises. Call at session start to see what you owe whom. Reads do
    not write an interaction-log entry — only mutations do.
    """
    await _initialize()

    from entrabot.tools.audit import log_event
    from entrabot.tools.promises import list_promises as _list

    agent_id, attribution = _promise_audit_ids()
    log_event(
        action="promise.list",
        resource="all",
        outcome="pending",
        agent_id=agent_id,
        metadata={"open_only": open_only},
        attribution_type=attribution,
    )

    await _ensure_valid_token()

    async def _call(token: str) -> list:  # noqa: ARG001 — token unused
        return await _list(open_only=open_only)

    promises = await _with_token_retry(_call)
    return json.dumps([p.to_entry() for p in promises], indent=2)


@mcp.tool()
async def read_interactions(
    chat_id: str = "",
    sender: str = "",
    action: str = "",
    direction: str = "",
    since: str = "",
    limit: int = 10,
) -> str:
    """Query the agent's own interaction log — body-side analogue of recall.

    Every inbound + outbound message the agent handles is appended to
    ``interactions/<day>.jsonl`` by the MCP server. This tool reads
    that log with structured filters so the model can answer "did I
    already say this?" / "what did the sponsor ping me about earlier?"
    without re-hitting Graph.

    Default window is the last 24 h; ``since`` may reach back up to 7
    days. Use BEFORE every outbound send (``send_teams_message``,
    ``send_email``, ``send_card``, ``share_file``) with
    ``chat_id=<target>`` to avoid repeating yourself.

    Args:
        chat_id: Teams chat ID. For outbound entries this matches
            ``recipient``; for inbound, ``metadata.chat_id``. Empty
            string = no filter.
        sender: Exact sender (case-insensitive). Empty = no filter.
        action: Exact action name, e.g. ``"send_teams_message"``.
            Empty = no filter.
        direction: ``"inbound"`` or ``"outbound"``. Empty = no filter.
        since: ISO 8601 timestamp. Default = now − 24 h. Entries at or
            before this cutoff are excluded.
        limit: Max entries to return (default 10).

    Returns:
        JSON array of raw interaction-log entries, most-recent first.
        On validation failure: ``{"error": "..."}``.
    """
    from entrabot.tools.read_interactions import read_interactions as _read

    try:
        entries = _read(
            chat_id=chat_id or None,
            sender=sender or None,
            action=action or None,
            direction=direction or None,
            since=since or None,
            limit=limit,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(entries, indent=2)


@mcp.tool()
async def bootstrap_body_state() -> str:
    """Single-packet snapshot of body-side operational state.

    Counterpart to persona-sati's ``bootstrap_session``. Returns an
    INDEX (not content) of today's activity, the most-active chats,
    every open promise, and watched-chat cursor freshness. Call at
    session start to land operational continuity in the first turn
    without chaining multiple read tools.

    Full message content stays in ``read_interactions`` — bootstrap
    is the catalog, ``read_interactions`` is the read.

    Returns:
        JSON object: ``today_counts``, ``top_chats_today``,
        ``open_promises``, ``cursor_freshness``, ``watched_chat_count``,
        ``generated_at``.
    """
    from entrabot.tools.body_bootstrap import bootstrap_body_state as _bootstrap

    return json.dumps(_bootstrap(), indent=2)


@mcp.tool()
async def resolve_promise(promise_id: str, resolution: str) -> str:
    """Mark a promise resolved.

    Only call AFTER the human-facing update has been posted in the
    correct chat — not when the internal signal (sub-agent completion,
    build finish) arrives.

    Args:
        promise_id: The id returned by ``add_promise`` (also visible in
            ``list_promises``).
        resolution: One-line closure reason, e.g. "PR #42 merged, reply
            posted to c1" or "agent-stalled, respawning".

    Returns:
        JSON of the resolved Promise, or ``{"error": "..."}`` on
        validation / not-found failure.
    """
    await _initialize()

    if not promise_id:
        return json.dumps({"error": "promise_id is required — get it from list_promises."})
    if not resolution or not resolution.strip():
        return json.dumps({"error": "resolution is required — one-line closure reason."})

    from entrabot.tools.audit import log_event
    from entrabot.tools.promises import (
        PromiseNotFound,
    )
    from entrabot.tools.promises import (
        resolve_promise as _resolve,
    )

    agent_id, attribution = _promise_audit_ids()
    log_event(
        action="promise.resolve",
        resource=promise_id,
        outcome="pending",
        agent_id=agent_id,
        metadata={},
        attribution_type=attribution,
    )

    await _ensure_valid_token()

    async def _call(token: str) -> object:  # noqa: ARG001 — token unused
        return await _resolve(
            promise_id=promise_id,
            resolution=resolution,
        )

    try:
        promise = await _with_token_retry(_call)
    except PromiseNotFound:
        _log_interaction_safe(
            channel="terminal",
            direction="outbound",
            sender="entrabot-agent",
            recipient="self",
            summary=f"resolve_promise: {promise_id} not found",
            action="promise.resolve",
            content_ref=promise_id,
            metadata={"promise_id": promise_id, "outcome": "not_found"},
        )
        return json.dumps({"error": f"promise not found: {promise_id}"})

    _log_interaction_safe(
        channel=detect_channel(
            promise.chat_id
            if promise.chat_id not in {"terminal", "email"}
            else None
            if promise.chat_id == "terminal"
            else "email"
        ),
        direction="outbound",
        sender="entrabot-agent",
        recipient=promise.chat_id,
        summary=f"resolved: {resolution[:120]}",
        action="promise.resolve",
        content_ref=promise_id,
        metadata={
            "promise_id": promise_id,
            "chat_id": promise.chat_id,
            "resolution": resolution,
        },
    )

    return json.dumps(promise.to_entry(), indent=2)


# ───────────────────────────────────────────────────────────────────────
# Files (PR1) — read + comment on SharePoint / OneDrive driveItems
#
# These tools wrap ``entrabot.tools.files`` with the standard MCP
# discipline: ``_initialize`` for auth state, ``_ensure_valid_token``
# for refresh, ``_with_token_retry`` for transparent re-auth on 401,
# JSON-only return values. Audit logging is handled inside
# ``tools.files`` via the ``_audit_graph_call`` helper, so these
# wrappers stay thin.
# ───────────────────────────────────────────────────────────────────────


def _serialize_file_ref(ref: object) -> dict:
    from entrabot.tools.files import FileRef

    assert isinstance(ref, FileRef)
    return {
        "drive_id": ref.drive_id,
        "item_id": ref.item_id,
        "name": ref.name,
        "mime_type": ref.mime_type,
        "kind": ref.kind,
        "site_id": ref.site_id,
        "web_url": ref.web_url,
        "size_bytes": ref.size_bytes,
    }


@mcp.tool()
async def resolve_file_url(url: str) -> str:
    """Resolve a SharePoint / OneDrive / shared-link URL to a stable file handle.

    Use this BEFORE ``read_file`` or ``add_file_comment``. The returned
    handle (a ``FileRef``) carries ``drive_id``, ``item_id``, ``site_id``
    (for SharePoint) and the file's ``kind`` (sharepoint /
    onedrive_business / onedrive_personal). Pass that handle to
    downstream Files tools — they do NOT re-resolve.

    Args:
        url: A full SharePoint / OneDrive / shared-link URL.

    Returns:
        JSON with the resolved file handle, or ``{"error": "..."}`` on
        failure (URL malformed, file not found, permission denied,
        site denied by operator).
    """
    await _initialize()
    from entrabot.errors import (
        FilesError,
    )
    from entrabot.tools.files import resolve_file_url as _resolve

    if not url or not url.strip():
        return json.dumps({"error": "url is required"})

    await _ensure_valid_token()
    try:
        ref = await _with_token_retry(_resolve, url=url.strip())
    except FilesError as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})
    return json.dumps(_serialize_file_ref(ref), indent=2)


@mcp.tool()
async def list_recent_files(limit: int = 25) -> str:
    """List files recently shared with the agent (Graph ``/me/drive/sharedWithMe``).

    Returns up to ``limit`` files, post-filtered by the operator
    site denylist (``ENTRABOT_FILES_DENIED_SITES``). The ``denied_count``
    field is the number of files that WERE filtered out — surface
    that to the user so they know N files exist but are operator-denied.

    Args:
        limit: Max number of files to return (default 25, must be >= 1).

    Returns:
        JSON ``{"files": [...], "denied_count": N}`` or ``{"error": "..."}``.
    """
    await _initialize()
    from entrabot.errors import FilesError
    from entrabot.tools.files import list_recent_files as _list

    await _ensure_valid_token()
    try:
        page = await _with_token_retry(_list, limit=limit)
    except FilesError as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps(
        {
            "files": [
                {
                    "drive_id": f.drive_id,
                    "item_id": f.item_id,
                    "name": f.name,
                    "web_url": f.web_url,
                    "mime_type": f.mime_type,
                    "size_bytes": f.size_bytes,
                    "modified_at": f.modified_at,
                    "shared_by": f.shared_by,
                    "site_id": f.site_id,
                }
                for f in page.files
            ],
            "denied_count": page.denied_count,
        },
        indent=2,
    )


@mcp.tool()
async def read_file(
    drive_id: str,
    item_id: str,
    name: str,
    mime_type: str = "application/octet-stream",
    kind: str = "sharepoint",
    site_id: str = "",
    web_url: str = "",
    size_bytes: int = 0,
    as_format: str = "auto",
) -> str:
    """Read a SharePoint / OneDrive file as text.

    Pass the ``FileRef`` fields returned from ``resolve_file_url`` or
    ``list_recent_files`` directly. Supported formats:

    - ``.md`` / ``.txt`` / ``.html`` / ``.htm`` — fetched raw, decoded as UTF-8.
    - ``.docx`` — converted to PDF via Graph (``?format=pdf``), text
      extracted via ``pypdf``.
    - ``.pdf`` — fetched raw, text extracted via ``pypdf``. Files larger
      than ``ENTRABOT_FILES_MAX_PDF_BYTES`` (default 50 MiB) are
      refused BEFORE download.

    Excel and PowerPoint are intentionally rejected — use
    ``read_workbook_range`` (PR3) for Excel, or paste slide content
    into chat for PowerPoint.

    Args:
        drive_id, item_id, name, mime_type, kind, site_id, web_url,
        size_bytes: ``FileRef`` fields. Pass ``site_id=""`` for OneDrive
        and ``size_bytes=0`` if unknown.
        as_format: ``"auto"`` (default) — choose by extension. ``"raw"``
            forces UTF-8 text decode; only valid for raw-text formats.

    Returns:
        JSON with extracted text + page_count + truncation flag, or
        ``{"error": "..."}`` on failure.
    """
    await _initialize()
    from entrabot.errors import FilesError
    from entrabot.tools.files import FileRef
    from entrabot.tools.files import read_file as _read

    if not drive_id or not item_id or not name:
        return json.dumps({"error": "drive_id, item_id, and name are required"})

    ref = FileRef(
        drive_id=drive_id,
        item_id=item_id,
        name=name,
        mime_type=mime_type,
        kind=kind,  # type: ignore[arg-type]
        site_id=site_id or None,
        web_url=web_url or None,
        size_bytes=size_bytes if size_bytes > 0 else None,
    )

    await _ensure_valid_token()
    try:
        content = await _with_token_retry(
            _read,
            file_ref=ref,
            as_format=as_format,  # type: ignore[arg-type]
        )
    except FilesError as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "drive_id": content.drive_id,
            "item_id": content.item_id,
            "name": content.name,
            "mime_type": content.mime_type,
            "text": content.text,
            "page_count": content.page_count,
            "truncated": content.truncated,
        },
        indent=2,
    )


@mcp.tool()
async def add_file_comment(
    drive_id: str,
    item_id: str,
    name: str,
    content: str,
    mime_type: str = "application/octet-stream",
    kind: str = "sharepoint",
    site_id: str = "",
) -> str:
    """Post a document comment to a Word or Excel file (Files-only).

    Files-only by design — does NOT cross-post to chat. If you want
    to also reply in Teams chat, call ``send_teams_message`` separately
    with the comment URL or summary.

    Restrictions:
    - File must be ``.docx`` or ``.xlsx`` — ``.pptx`` and other formats
      rejected with ``UnsupportedCommentFormatError``.
    - Personal OneDrive files rejected (Microsoft beta endpoint does
      not support them).
    - Folder driveItems rejected.
    - Site must not be in ``ENTRABOT_FILES_DENIED_SITES``.

    Endpoint: ``POST /beta/drives/{drive-id}/items/{item-id}/comments``
    (Microsoft's beta surface — V1 plan accepts this risk; see
    ``docs/architecture/PLAN-files-mcp-tools.md`` §"Failure-mode registry").

    Args:
        drive_id, item_id, name, mime_type, kind, site_id: ``FileRef``
            fields (from ``resolve_file_url`` or ``list_recent_files``).
        content: Text of the comment.

    Returns:
        JSON with ``comment_id``, ``content``, optional ``web_url``;
        or ``{"error": "..."}``.
    """
    await _initialize()
    from entrabot.errors import FilesError
    from entrabot.tools.files import FileRef
    from entrabot.tools.files import add_file_comment as _comment

    if not drive_id or not item_id or not name:
        return json.dumps({"error": "drive_id, item_id, and name are required"})
    if not content or not content.strip():
        return json.dumps({"error": "content is required (non-empty)"})

    ref = FileRef(
        drive_id=drive_id,
        item_id=item_id,
        name=name,
        mime_type=mime_type,
        kind=kind,  # type: ignore[arg-type]
        site_id=site_id or None,
    )

    await _ensure_valid_token()
    try:
        result = await _with_token_retry(_comment, file_ref=ref, content=content)
    except FilesError as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "comment_id": result.comment_id,
            "content": result.content,
            "web_url": result.web_url,
        },
        indent=2,
    )


@mcp.tool()
async def read_word_document(url: str) -> str:
    """Read a Word document and its comments through Agent 365 Work IQ Word.

    This is the production path for Word document comments. Use this instead
    of Graph beta file-comment endpoints when the goal is to inspect Word UI
    comments or prepare a comment-thread reply.
    """
    await _initialize()
    from entrabot.a365.errors import A365Error
    from entrabot.a365.word import get_document_content

    if not url or not url.strip():
        return json.dumps({"error": "url is required"})

    try:
        content = await get_document_content(url.strip())
    except (A365Error, ValueError) as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "content_html": content.content_html,
            "comments": content.comments,
            "raw": content.raw,
        },
        indent=2,
    )


@mcp.tool()
async def create_word_document(file_name: str, content_html: str) -> str:
    """Create a Word document through Agent 365 Work IQ Word."""
    await _initialize()
    from entrabot.a365.errors import A365Error
    from entrabot.a365.word import create_document

    if not file_name or not file_name.strip():
        return json.dumps({"error": "file_name is required"})
    if not content_html or not content_html.strip():
        return json.dumps({"error": "content_html is required"})

    try:
        document = await create_document(file_name.strip(), content_html)
    except (A365Error, ValueError) as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "file_name": document.file_name,
            "url": document.url,
            "raw": document.raw,
        },
        indent=2,
    )


@mcp.tool()
async def add_word_comment(drive_id: str, document_id: str, content: str) -> str:
    """Create a top-level Word comment through Agent 365 Work IQ Word."""
    await _initialize()
    from entrabot.a365.errors import A365Error
    from entrabot.a365.word import create_comment

    if not drive_id or not document_id:
        return json.dumps({"error": "drive_id and document_id are required"})
    if not content or not content.strip():
        return json.dumps({"error": "content is required"})

    try:
        comment = await create_comment(drive_id, document_id, content)
    except (A365Error, ValueError) as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "comment_id": comment.comment_id,
            "content": comment.content,
            "raw": comment.raw,
        },
        indent=2,
    )


@mcp.tool()
async def reply_to_word_comment(
    drive_id: str,
    document_id: str,
    comment_id: str,
    content: str,
) -> str:
    """Reply inside an existing Word comment thread through Agent 365 Work IQ Word."""
    await _initialize()
    from entrabot.a365.errors import A365Error
    from entrabot.a365.word import reply_to_comment

    if not drive_id or not document_id or not comment_id:
        return json.dumps({"error": "drive_id, document_id, and comment_id are required"})
    if not content or not content.strip():
        return json.dumps({"error": "content is required"})

    try:
        reply = await reply_to_comment(drive_id, document_id, comment_id, content)
    except (A365Error, ValueError) as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "reply_id": reply.reply_id,
            "comment_id": reply.comment_id,
            "content": reply.content,
            "raw": reply.raw,
        },
        indent=2,
    )


@mcp.tool()
async def get_a365_file_metadata_by_url(url: str) -> str:
    """Read OneDrive/SharePoint file metadata by URL through Agent 365 Work IQ."""
    await _initialize()
    from entrabot.a365.errors import A365Error
    from entrabot.a365.odsp import get_file_or_folder_metadata_by_url

    if not url or not url.strip():
        return json.dumps({"error": "url is required"})

    try:
        metadata = await get_file_or_folder_metadata_by_url(url.strip())
    except (A365Error, ValueError) as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "item_id": metadata.item_id,
            "name": metadata.name,
            "web_url": metadata.web_url,
            "document_library_id": metadata.document_library_id,
            "raw": metadata.raw,
        },
        indent=2,
    )


@mcp.tool()
async def read_a365_text_file(document_library_id: str, file_id: str) -> str:
    """Read a small text file from OneDrive/SharePoint through Agent 365 Work IQ."""
    await _initialize()
    from entrabot.a365.errors import A365Error
    from entrabot.a365.odsp import read_small_text_file

    if not document_library_id or not file_id:
        return json.dumps({"error": "document_library_id and file_id are required"})

    try:
        content = await read_small_text_file(document_library_id, file_id)
    except (A365Error, ValueError) as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "content": content.content,
            "encoding": content.encoding,
            "raw": content.raw,
        },
        indent=2,
    )


@mcp.tool()
async def read_a365_binary_file(document_library_id: str, file_id: str) -> str:
    """Read a small binary file from OneDrive/SharePoint through Agent 365 Work IQ."""
    await _initialize()
    from entrabot.a365.errors import A365Error
    from entrabot.a365.odsp import read_small_binary_file

    if not document_library_id or not file_id:
        return json.dumps({"error": "document_library_id and file_id are required"})

    try:
        content = await read_small_binary_file(document_library_id, file_id)
    except (A365Error, ValueError) as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "content": content.content,
            "encoding": content.encoding,
            "raw": content.raw,
        },
        indent=2,
    )


# ───────────────────────────────────────────────────────────────────────
# PR2: Author / Upload / Share Tools
# ───────────────────────────────────────────────────────────────────────


@mcp.tool()
async def write_text_file(
    target_type: str,  # "onedrive" or "sharepoint"
    file_name: str,
    content: str,
    folder_path: str = "/",
    drive_id: str = "",
    site_id: str = "",
    conflict_behavior: str = "fail",
) -> str:
    """Write text to a file (create or update per conflict_behavior).

    Args:
        target_type: "onedrive" or "sharepoint"
        file_name: Name of file to create/overwrite
        content: Text content to write
        folder_path: Target folder path (default "/")
        drive_id: (SharePoint only) Drive ID
        site_id: (SharePoint only) Site ID
        conflict_behavior: rename / replace / fail

    Returns:
        JSON with file metadata (drive_id, item_id, name, web_url, size_bytes)
        or ``{"error": "..."}``.
    """
    await _initialize()
    from entrabot.errors import FilesError
    from entrabot.tools.files import (
        OneDriveTarget,
        SharePointTarget,
    )
    from entrabot.tools.files import (
        write_text_file as _write,
    )

    if not file_name or not content:
        return json.dumps({"error": "file_name and content are required"})
    if target_type not in ("onedrive", "sharepoint"):
        return json.dumps({"error": "target_type must be 'onedrive' or 'sharepoint'"})
    if target_type == "sharepoint" and not (drive_id and site_id):
        return json.dumps({"error": "SharePoint requires drive_id and site_id"})

    target = (
        SharePointTarget(site_id=site_id, drive_id=drive_id, folder_path=folder_path)
        if target_type == "sharepoint"
        else OneDriveTarget(folder_path=folder_path)
    )

    await _ensure_valid_token()
    try:
        result = await _with_token_retry(
            _write,
            target=target,
            file_name=file_name,
            content=content,
            conflict_behavior=conflict_behavior,  # type: ignore[arg-type]
        )
    except FilesError as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "drive_id": result.drive_id,
            "item_id": result.item_id,
            "name": result.name,
            "mime_type": result.mime_type,
            "kind": result.kind,
            "site_id": result.site_id,
            "web_url": result.web_url,
            "size_bytes": result.size_bytes,
        },
        indent=2,
    )


@mcp.tool()
async def upload_file(
    target_type: str,
    file_name: str,
    content_base64: str,
    folder_path: str = "/",
    drive_id: str = "",
    site_id: str = "",
    conflict_behavior: str = "fail",
) -> str:
    """Upload binary file with automatic chunking for large files.

    Args:
        target_type: "onedrive" or "sharepoint"
        file_name: Name of file to create/upload
        content_base64: Base64-encoded file content
        folder_path: Target folder path (default "/")
        drive_id: (SharePoint only) Drive ID
        site_id: (SharePoint only) Site ID
        conflict_behavior: rename / replace / fail

    Returns:
        JSON with file metadata (drive_id, item_id, name, web_url, size_bytes)
        or ``{"error": "..."}``.
    """
    await _initialize()
    from entrabot.errors import FilesError
    from entrabot.tools.files import (
        OneDriveTarget,
        SharePointTarget,
    )
    from entrabot.tools.files import (
        upload_file as _upload,
    )

    if not file_name or not content_base64:
        return json.dumps({"error": "file_name and content_base64 are required"})
    if target_type not in ("onedrive", "sharepoint"):
        return json.dumps({"error": "target_type must be 'onedrive' or 'sharepoint'"})
    if target_type == "sharepoint" and not (drive_id and site_id):
        return json.dumps({"error": "SharePoint requires drive_id and site_id"})

    try:
        content_bytes = base64.b64decode(content_base64)
    except Exception:
        return json.dumps({"error": "content_base64 is not valid base64"})

    target = (
        SharePointTarget(site_id=site_id, drive_id=drive_id, folder_path=folder_path)
        if target_type == "sharepoint"
        else OneDriveTarget(folder_path=folder_path)
    )

    await _ensure_valid_token()
    try:
        result = await _with_token_retry(
            _upload,
            target=target,
            file_name=file_name,
            content_bytes=content_bytes,
            conflict_behavior=conflict_behavior,  # type: ignore[arg-type]
        )
    except FilesError as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "drive_id": result.drive_id,
            "item_id": result.item_id,
            "name": result.name,
            "mime_type": result.mime_type,
            "kind": result.kind,
            "site_id": result.site_id,
            "web_url": result.web_url,
            "size_bytes": result.size_bytes,
        },
        indent=2,
    )


@mcp.tool()
async def share_file(
    drive_id: str,
    item_id: str,
    name: str,
    recipient_email: str,
    requester_email: str,
    chat_id: str,
    role: str = "read",
    mime_type: str = "application/octet-stream",
    kind: str = "sharepoint",
    site_id: str = "",
) -> str:
    """Share a file. The REQUESTER must be an Agent Identity sponsor; the recipient is unrestricted.

    Authorization model: only sponsors can ask the agent to share. A
    sponsor may share with anyone they choose. The recipient is passed
    straight through to Microsoft Graph.

    Args:
        drive_id, item_id, name, mime_type, kind, site_id: ``FileRef`` fields.
        recipient_email: Address to share with (any address — sponsors may share with non-sponsors).
        requester_email: REQUIRED. Email of the human (sponsor) who asked
            the agent to share. Derive this from the active conversation
            context — the sender of the Teams message that triggered
            this turn. NEVER use the agent's own address. NEVER fabricate.
        chat_id: REQUIRED. The Teams chat ID that initiated this share
            request. Used to verify the requester is genuinely a member
            of that conversation. There is no "no-chat" bypass.
        role: read / write

    Returns:
        JSON with permission metadata (permission_id, role, recipient_email,
        web_url, expiration_at) or ``{"error": "..."}``.

    Errors:
        RequesterNotSponsorError: requester_email is not in the sponsor allowlist.
        RequesterNotInChatError: requester is a sponsor but not a member of chat_id.
    """
    await _initialize()
    from entrabot.errors import FilesError
    from entrabot.tools.files import FileRef
    from entrabot.tools.files import share_file as _share

    if not drive_id or not item_id or not name or not recipient_email:
        return json.dumps({"error": "drive_id, item_id, name, and recipient_email are required"})
    if not requester_email:
        return json.dumps(
            {
                "error": (
                    "requester_email is required — pass the email of the human "
                    "who asked you to share. Never use the agent's own address."
                ),
                "error_type": "ValueError",
            }
        )
    if not chat_id:
        return json.dumps(
            {
                "error": (
                    "chat_id is required — pass the Teams chat ID that initiated "
                    "this request. There is no no-chat bypass."
                ),
                "error_type": "ValueError",
            }
        )
    if role not in ("read", "write"):
        return json.dumps({"error": "role must be 'read' or 'write'"})

    ref = FileRef(
        drive_id=drive_id,
        item_id=item_id,
        name=name,
        mime_type=mime_type,
        kind=kind,  # type: ignore[arg-type]
        site_id=site_id or None,
    )

    await _ensure_valid_token()
    try:
        result = await _with_token_retry(
            _share,
            file_ref=ref,
            recipient_email=recipient_email,
            requester_email=requester_email,
            chat_id=chat_id,
            role=role,  # type: ignore[arg-type]
        )
    except FilesError as exc:
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__})

    return json.dumps(
        {
            "permission_id": result.permission_id,
            "role": result.role,
            "recipient_email": result.recipient_email,
            "web_url": result.web_url,
            "expiration_at": result.expiration_at,
        },
        indent=2,
    )


# ============================================================================
# run_code — Sandboxed Local Code Execution (MXC)
# ============================================================================
# Conditionally registered based on ENTRABOT_ENABLE_RUN_CODE env var.
# Design: docs/architecture/DESIGN-mxc-sandbox.md
# Security model: disabled by default, positive-allowlist-only, backend-aware
# fail-closed, audit-first, operator ceiling enforcement (Learning #54).


# Check env flag to decide whether to register run_code tool
_ENABLE_RUN_CODE = os.environ.get("ENTRABOT_ENABLE_RUN_CODE") == "1"


# Substrings the sandbox helper (e.g. wxc-exec.exe) emits on stderr when it
# launched successfully but could NOT spawn the inner command via CreateProcessW.
# This is an internal sandbox-configuration problem (the helper ran but the
# command it was told to run is not a launchable executable) — it is NOT a
# blocked path or a missing target file, and must be reported distinctly so the
# agent does not tell the user "the file does not exist / is outside the ceiling"
# when the real cause is the sandbox command construction or MXC binary.
_SANDBOX_SPAWN_FAILURE_SIGNATURES = (
    "createprocessw failed",
    "0x80070002",
    "error_file_not_found",
    "backend_error",
)


def _is_sandbox_spawn_failure(stderr: str | None) -> bool:
    """True if ``stderr`` carries the sandbox-helper spawn-failure signature."""
    s = (stderr or "").lower()
    return any(sig in s for sig in _SANDBOX_SPAWN_FAILURE_SIGNATURES)


def _local_file_failure_response(result, *, operation: str, path: str) -> dict:
    """Build the failure dict for read_local_file / write_local_file.

    Discriminates two genuinely different failures that both surface as a nonzero
    runner exit:

    * **Sandbox-helper spawn failure** (``CreateProcessW failed`` / ``0x80070002``
      / ``backend_error``): the helper ran but could not spawn the inner command.
      This is an internal sandbox configuration problem, not a policy denial.
    * **Policy denial / nonzero inner exit**: the command ran inside the sandbox
      and was blocked by the operator ceiling, or the target file is missing.
    """
    verb = "Read" if operation == "read" else "Write"
    response = {
        "success": False,
        "path": path,
        "stderr": (result.stderr or "").strip()[:1024],
        "exit_code": result.exit_code,
    }
    if _is_sandbox_spawn_failure(result.stderr):
        response["error"] = "Sandbox helper could not run the command"
        response["help"] = (
            "The sandbox helper launched but could not spawn the inner command "
            "(CreateProcessW failed / file not found at the OS-spawn level). This "
            "is an internal sandbox configuration problem — the helper ran but the "
            "command it was given is not a launchable executable. It is NOT a "
            "blocked path or a missing target file: check the sandbox command "
            "construction / MXC binary, not the operator's allow-list."
        )
        return response
    response["error"] = f"{verb} blocked or failed"
    if operation == "read":
        response["help"] = (
            "The path is likely outside the sandbox's allowed read paths "
            "(the operator's ceiling), or the file does not exist."
        )
    else:
        response["help"] = (
            "The target directory is likely outside the sandbox's allowed "
            "write paths (the operator's ceiling)."
        )
    return response


if _ENABLE_RUN_CODE:
    @mcp.tool()
    def run_code(
        argv: list[str],
        readonly_paths: list[str] | None = None,
        readwrite_paths: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        """Run a command on the LOCAL machine inside the MXC security sandbox.

        **This is your only way to read OR write files on the user's LOCAL
        computer** (their actual disk — e.g. ``~/Documents``, ``~/Downloads``,
        ``/tmp``). It is separate from the Teams/Files/OneDrive tools (e.g.
        ``write_text_file``, ``upload_file``), which act on cloud/Graph resources.
        When the user refers to a file "on my machine", "in my Documents/Downloads
        folder", a local path, or anything on their disk — for BOTH reading and
        writing — use THIS tool. Do not route local file requests to the OneDrive
        tools, and do not conclude you "have no way to write locally": writing a
        local file IS done through this tool (e.g. ``bash -lc 'echo ... > path'``).

        The command runs in an OS-enforced sandbox (Apple Seatbelt on macOS): the
        operator has pre-authorized a set of readable and writable directories (the
        "ceiling"). Reads succeed from allowed read paths; writes succeed in allowed
        read-write paths; anything outside is blocked by the kernel and returns a
        nonzero exit with "Operation not permitted".

        **The sandbox is permission-based on the user's REAL filesystem — it is NOT
        a separate/virtual/throwaway container.** A file you read from an allowed
        path is the user's actual file; a file you write to an allowed path persists
        on the user's actual disk (you can read it back, the user sees it in Finder).
        The sandbox only restricts WHICH paths you may touch — it does not redirect
        them to some isolated location. So writing to an allowed path is a real,
        durable write you can honestly report as done.

        **Do not pre-judge whether a path is allowed — attempt the operation and
        report what actually happened.** If the kernel blocks it, tell the user the
        path is outside the sandbox's allowed write/read paths (the operator's
        ceiling) — NOT that the file is missing, that you have no local-file tool,
        or that the write went to an isolated container. Trying and being denied by
        the sandbox is the expected, correct behavior.

        **IMPORTANT: Disabled unless the operator set ``ENTRABOT_ENABLE_RUN_CODE=1``.**

        Security model:
        - Operator-set ceiling (env-configured); you can only NARROW it, never widen.
        - Positive-allowlist-only paths (no deniedPaths reliance).
        - Backend-aware fail-closed (refuses if policy unenforceable).
        - keychain_access=false (hardcoded) — never reads the user's Keychain.
        - Audit-first (every call is logged before it runs).

        Args:
            argv: Structured command as a list (e.g. ["cat", "/Users/me/Documents/notes.txt"]).
                  NO SHELL by default — passed directly as argv. For redirection or
                  pipes (e.g. writing a file), invoke a shell explicitly, e.g.
                  ["bash", "-lc", "echo hi > /tmp/out.txt"].
            readonly_paths: Optional paths to request read access to (narrows the
                  ceiling). Pass the directory or file you intend to read.
            readwrite_paths: Optional paths to request write access to (narrows the
                  ceiling). Pass the directory you intend to write into.
            timeout_ms: Optional timeout (narrows the ceiling), milliseconds.

        Returns:
            JSON string with: success (bool), stdout (str), stderr (str),
            exit_code (int), duration_ms (int), timed_out (bool). Or an error dict.

        Examples:
            # READ a local file in the user's Documents folder
            run_code(argv=["cat", "/Users/me/Documents/notes.txt"],
                     readonly_paths=["/Users/me/Documents"])

            # WRITE a local file into the user's Downloads folder (use a shell for >)
            run_code(argv=["bash", "-lc", "echo 'summary' > /Users/me/Downloads/report.txt"],
                     readwrite_paths=["/Users/me/Downloads"])

            # Run a quick computation
            run_code(argv=["python", "-c", "print(2 + 2)"])
        """
        from entrabot.sandbox import get_sandbox_runner
        from entrabot.sandbox.base import (
            SandboxBackendUnsupportedError,
            SandboxPolicy,
            SandboxPolicyError,
            SandboxTimeoutError,
            SandboxUnavailableError,
            SandboxUntrustedBinaryError,
        )
        from entrabot.sandbox.policy import canonicalize_paths, clamp_to_ceiling
        from entrabot.tools.audit import log_event as audit_event
        
        try:
            # Get operator ceiling from environment
            # In production, this would be configured via env vars
            # For now, use a restrictive default ceiling
            # Operator ceiling paths are an OS-path-separator-delimited list
            # (':' on POSIX, ';' on Windows). Using os.pathsep — not a hardcoded
            # ':' — is load-bearing on Windows: a colon split would shred drive
            # letters (e.g. 'C:\\Users\\me' -> ['C', '\\Users\\me']).
            ceiling_readonly = os.environ.get(
                "ENTRABOT_SANDBOX_READONLY_PATHS", ""
            ).split(os.pathsep)
            ceiling_readwrite = os.environ.get(
                "ENTRABOT_SANDBOX_READWRITE_PATHS", ""
            ).split(os.pathsep)
            ceiling_timeout = int(os.environ.get("ENTRABOT_SANDBOX_TIMEOUT_MS", "30000"))
            
            # Filter out empty strings from split
            ceiling_readonly = [p for p in ceiling_readonly if p]
            ceiling_readwrite = [p for p in ceiling_readwrite if p]
            
            # Build ceiling policy
            ceiling = SandboxPolicy(
                backend="process",  # Phase 1
                command_line="",  # Will be set from argv
                readonly_paths=ceiling_readonly,
                readwrite_paths=ceiling_readwrite,
                timeout_ms=ceiling_timeout,
                network_default_policy=os.environ.get("ENTRABOT_SANDBOX_NETWORK", "block"),
                keychain_access=False,  # Hardcoded
            )
            
            # Build LLM-requested policy
            llm_readonly = readonly_paths if readonly_paths is not None else ceiling_readonly
            llm_readwrite = readwrite_paths if readwrite_paths is not None else ceiling_readwrite
            llm_timeout = timeout_ms if timeout_ms is not None else ceiling_timeout
            
            # Convert argv to command_line
            command_line = " ".join(argv)  # Structured argv preserved
            
            llm_policy = SandboxPolicy(
                backend="process",
                command_line=command_line,
                readonly_paths=llm_readonly,
                readwrite_paths=llm_readwrite,
                timeout_ms=llm_timeout,
                network_default_policy="block",  # LLM can't widen
                keychain_access=False,
            )
            
            # Get sandbox runner (resolves + verifies binary)
            runner = get_sandbox_runner()
            backend_caps = runner.get_capabilities()
            
            # Clamp policy to ceiling (Learning #54)
            clamped_policy = clamp_to_ceiling(llm_policy, ceiling, backend_caps)
            
            # Canonicalize paths
            if clamped_policy.readonly_paths:
                clamped_policy.readonly_paths = canonicalize_paths(clamped_policy.readonly_paths)
            if clamped_policy.readwrite_paths:
                clamped_policy.readwrite_paths = canonicalize_paths(clamped_policy.readwrite_paths)
            
            # Audit: pending
            audit_event(
                action="run_code",
                resource="sandbox",
                outcome="pending",
                metadata={
                    "argv": argv,
                    "backend": backend_caps["backend"],
                    "timeout_ms": clamped_policy.timeout_ms,
                },
            )
            
            # Execute in sandbox
            result = runner.run(clamped_policy)
            
            # Truncate output (max 10KB each)
            MAX_OUTPUT = 10 * 1024
            stdout_truncated = result.stdout[:MAX_OUTPUT]
            stderr_truncated = result.stderr[:MAX_OUTPUT]
            
            # Audit: success/failure
            audit_event(
                action="run_code",
                resource="sandbox",
                outcome="success" if result.exit_code == 0 else "failure",
                metadata={
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                    "stdout_bytes": len(result.stdout),
                    "stderr_bytes": len(result.stderr),
                },
            )
            
            return json.dumps({
                "success": result.exit_code == 0,
                "stdout": stdout_truncated,
                "stderr": stderr_truncated,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
            }, indent=2)
            
        except SandboxUnavailableError as e:
            return json.dumps({
                "error": "Sandbox unavailable",
                "message": str(e),
                "help": "Install MXC binary or set MXC_BIN_DIR environment variable",
            }, indent=2)
        
        except SandboxUntrustedBinaryError as e:
            return json.dumps({
                "error": "Untrusted binary",
                "message": str(e),
                "help": "Binary SHA256 verification failed - binary may be tampered",
            }, indent=2)
        
        except SandboxBackendUnsupportedError as e:
            return json.dumps({
                "error": "Policy not enforceable",
                "message": str(e),
                "help": (
                    "Requested policy requires a primitive the backend cannot "
                    "enforce (fail-closed)"
                ),
            }, indent=2)
        
        except SandboxPolicyError as e:
            return json.dumps({
                "error": "Policy error",
                "message": str(e),
            }, indent=2)
        
        except SandboxTimeoutError as e:
            return json.dumps({
                "error": "Timeout",
                "message": str(e),
            }, indent=2)
        
        except Exception as e:
            # Catch-all for audit failures or unexpected errors
            if logger:
                logger.error(f"run_code failed: {e}", exc_info=True)
            return json.dumps({
                "error": "Execution failed",
                "message": str(e),
                "type": type(e).__name__,
            }, indent=2)

    @mcp.tool()
    def read_local_file(path: str) -> str:
        """Read a file on the user's LOCAL computer (their actual disk).

        Use this whenever the user asks you to read/open/show a file "on my
        machine", "in my Documents/Downloads folder", or at any local path like
        ``/Users/.../notes.txt`` or ``~/Documents/notes.txt``. This is for the
        user's REAL local disk — it is NOT the same as the OneDrive/SharePoint
        Files tools (``read_file`` et al.), which read Microsoft cloud storage.
        Default to THIS tool for local/on-disk file requests.

        The read happens inside an OS-enforced sandbox (Apple Seatbelt): the
        operator pre-authorized which directories may be read. If the path is
        inside an allowed directory you get the real file's contents; if it's
        outside, the kernel blocks it. **Just attempt the read and report what
        happened** — if it's blocked, tell the user the path is outside the
        sandbox's allowed read paths (the operator's ceiling), not that the file
        doesn't exist.

        Args:
            path: Absolute or ``~``-relative path to the local file to read.

        Returns:
            JSON with: success (bool), and on success ``content`` (str); on
            failure ``error`` plus ``stderr``/``exit_code`` describing why
            (e.g. blocked by the sandbox ceiling, or file not found).
        """
        from entrabot.sandbox import get_sandbox_runner
        from entrabot.sandbox.base import (
            SandboxPolicyError,
            SandboxUnavailableError,
            SandboxUntrustedBinaryError,
        )
        from entrabot.sandbox.local_files import ceiling_from_env, sandboxed_read
        from entrabot.tools.audit import log_event as audit_event

        try:
            runner = get_sandbox_runner()
            ceiling = ceiling_from_env()
            audit_event(
                action="read_local_file", resource=path, outcome="pending",
                metadata={"backend": runner.get_capabilities()["backend"]},
            )
            result = sandboxed_read(path, ceiling=ceiling, runner=runner)
            ok = result.exit_code == 0
            audit_event(
                action="read_local_file", resource=path,
                outcome="success" if ok else "failure",
                metadata={"exit_code": result.exit_code, "bytes": len(result.stdout)},
            )
            if ok:
                return json.dumps(
                    {"success": True, "path": path,
                     "content": result.stdout[:10 * 1024]}, indent=2
                )
            return json.dumps(
                _local_file_failure_response(result, operation="read", path=path),
                indent=2,
            )
        except SandboxPolicyError as e:
            return json.dumps({"success": False, "path": path,
                               "error": "Path not accessible", "message": str(e)}, indent=2)
        except SandboxUnavailableError as e:
            return json.dumps({"success": False, "error": "Sandbox unavailable",
                               "message": str(e)}, indent=2)
        except SandboxUntrustedBinaryError as e:
            return json.dumps({"success": False, "error": "Untrusted binary",
                               "message": str(e)}, indent=2)
        except Exception as e:
            if logger:
                logger.error(f"read_local_file failed: {e}", exc_info=True)
            return json.dumps({"success": False, "error": "Read failed",
                               "message": str(e), "type": type(e).__name__}, indent=2)

    @mcp.tool()
    def write_local_file(path: str, content: str) -> str:
        """Write/save a file on the user's LOCAL computer (their actual disk).

        Use this whenever the user asks you to write/save/create/append a file
        "on my machine", "in my Documents/Downloads folder", or at any local path
        like ``/Users/.../note.txt`` or ``~/Downloads/report.txt``. This writes to
        the user's REAL local disk — it is NOT the OneDrive/SharePoint Files tools
        (``write_text_file``, ``upload_file``), which write to Microsoft cloud
        storage. For local/on-disk save requests, default to THIS tool; do not
        substitute a OneDrive write and report it as if it were local.

        The write happens inside an OS-enforced sandbox (Apple Seatbelt): the
        operator pre-authorized which directories may be written. It is
        permission-based on the user's REAL filesystem — a successful write
        persists on their actual disk (they see it in Finder); it is NOT an
        isolated/throwaway container. If the target directory is outside the
        operator's allowed write paths, the kernel blocks it. **Just attempt the
        write and report what happened** — if it's blocked, tell the user the path
        is outside the sandbox's allowed write paths (the operator's ceiling),
        not that you have no way to write locally.

        Args:
            path: Absolute or ``~``-relative path of the local file to write.
            content: Text content to write (overwrites the file).

        Returns:
            JSON with: success (bool); on failure ``error`` plus
            ``stderr``/``exit_code`` describing why (e.g. blocked by the sandbox).
        """
        from entrabot.sandbox import get_sandbox_runner
        from entrabot.sandbox.base import (
            SandboxPolicyError,
            SandboxUnavailableError,
            SandboxUntrustedBinaryError,
        )
        from entrabot.sandbox.local_files import ceiling_from_env, sandboxed_write
        from entrabot.tools.audit import log_event as audit_event

        try:
            runner = get_sandbox_runner()
            ceiling = ceiling_from_env()
            audit_event(
                action="write_local_file", resource=path, outcome="pending",
                metadata={"backend": runner.get_capabilities()["backend"],
                          "content_length": len(content)},
            )
            result = sandboxed_write(path, content, ceiling=ceiling, runner=runner)
            ok = result.exit_code == 0
            audit_event(
                action="write_local_file", resource=path,
                outcome="success" if ok else "failure",
                metadata={"exit_code": result.exit_code},
            )
            if ok:
                return json.dumps(
                    {"success": True, "path": path, "bytes_written": len(content)},
                    indent=2,
                )
            return json.dumps(
                _local_file_failure_response(result, operation="write", path=path),
                indent=2,
            )
        except SandboxPolicyError as e:
            return json.dumps({"success": False, "path": path,
                               "error": "Path not accessible", "message": str(e)}, indent=2)
        except SandboxUnavailableError as e:
            return json.dumps({"success": False, "error": "Sandbox unavailable",
                               "message": str(e)}, indent=2)
        except SandboxUntrustedBinaryError as e:
            return json.dumps({"success": False, "error": "Untrusted binary",
                               "message": str(e)}, indent=2)
        except Exception as e:
            if logger:
                logger.error(f"write_local_file failed: {e}", exc_info=True)
            return json.dumps({"success": False, "error": "Write failed",
                               "message": str(e), "type": type(e).__name__}, indent=2)


# ============================================================================
# unsafe_write_local_file — Demonstration Tool (DELIBERATELY UNSAFE)
# ============================================================================
# This tool exists to demonstrate WHY sandboxing is necessary.
# It provides UNPROTECTED filesystem access that contrasts with the
# sandboxed read_local_file / write_local_file tools.
#
# Security model: NONE (intentionally dangerous for demonstration purposes)
#
# Use cases:
# - Show what happens without sandboxing (writes anywhere)
# - Contrast with the sandboxed local-file tools (clamped to operator ceiling)
# - Educational: demonstrate attack surface of unrestricted file access


# NOTE: unsafe_write_local_file is the DELIBERATELY-UNSAFE contrast tool. It
# bypasses the sandbox and writes anywhere, so it is registered as an MCP tool
# ONLY when the operator explicitly opts in via ENTRABOT_ENABLE_UNSAFE_WRITE=1.
# Registering it by default would hand the agent an unsandboxed write path that
# defeats the sandbox. The function stays defined (importable for tests) but is
# not exposed to the model unless enabled.
def unsafe_write_local_file(path: str, content: str) -> str:
    """Write content to local filesystem (UNPROTECTED - for demonstration only).
    
    ⚠️  **DANGER: This tool has NO security restrictions!**
    
    This tool exists to demonstrate WHY sandboxing is necessary. It can write
    to ANY path on the local filesystem without validation or containment.
    
    **DO NOT USE in production.** This is an educational tool to show:
    1. What unrestricted file access looks like (dangerous)
    2. How the sandboxed read_local_file / write_local_file tools protect (safe)

    For SAFE local file operations, use the sandboxed ``write_local_file`` /
    ``read_local_file`` tools, which clamp every access to the operator ceiling.

    Args:
        path: Absolute file path (NO VALIDATION - can be anywhere!)
        content: Content to write

    Returns:
        JSON with success status and path, or error dict

    Example (UNSAFE):
        unsafe_write_local_file(path="/Users/you/Desktop/hack.txt", content="pwned")
        → ✅ Succeeds (DANGEROUS!)

    Example (SAFE alternative):
        write_local_file(path="/tmp/safe.txt", content="data")
        → ✅ Succeeds only if /tmp is in the operator's read-write ceiling
    """
    from entrabot.tools.audit import log_event as audit_event
    
    try:
        # Audit: Log this dangerous operation
        audit_event(
            action="unsafe_write_local_file",
            resource=path,
            outcome="pending",
            metadata={
                "content_length": len(content),
                "warning": "UNPROTECTED file write - no sandboxing",
            },
        )
        
        # DANGEROUS: Write to any path without validation
        # Real production code would never do this!
        with open(path, 'w') as f:
            f.write(content)
        
        # Audit: Success
        audit_event(
            action="unsafe_write_local_file",
            resource=path,
            outcome="success",
            metadata={
                "bytes_written": len(content),
            },
        )
        
        return json.dumps({
            "success": True,
            "path": path,
            "bytes_written": len(content),
            "warning": "UNPROTECTED write succeeded - this is why sandboxing matters!",
        }, indent=2)
        
    except PermissionError as e:
        audit_event(
            action="unsafe_write_local_file",
            resource=path,
            outcome="failure",
            metadata={"error": "PermissionError", "message": str(e)},
        )
        return json.dumps({
            "success": False,
            "error": "Permission denied",
            "path": path,
            "message": str(e),
        }, indent=2)
    
    except Exception as e:
        audit_event(
            action="unsafe_write_local_file",
            resource=path,
            outcome="failure",
            metadata={"error": type(e).__name__, "message": str(e)},
        )
        if logger:
            logger.error(f"unsafe_write_local_file failed: {e}", exc_info=True)
        return json.dumps({
            "success": False,
            "error": "Write failed",
            "path": path,
            "message": str(e),
            "type": type(e).__name__,
        }, indent=2)


# Expose the unsafe demonstration tool ONLY when explicitly enabled.
_ENABLE_UNSAFE_WRITE = os.environ.get("ENTRABOT_ENABLE_UNSAFE_WRITE") == "1"
if _ENABLE_UNSAFE_WRITE:
    unsafe_write_local_file = mcp.tool()(unsafe_write_local_file)


def main() -> None:
    """Entry point for ``entrabot-mcp`` console script."""
    import anyio

    from entrabot.singleton import run_or_exit_if_held

    # Acquire the process-singleton flock BEFORE any side-effecting work
    # (logging, keychain reads, blob clients). When two MCP hosts attach to
    # the same workstation simultaneously, every spawn after the first will
    # exit(2) here with a clear stderr message naming the holder PID instead
    # of dying silently between module import and main() — see GitHub
    # issue #62.
    #
    # The handle is bound to ``_singleton_handle`` so the GC can't release
    # it; the lock lives for the process lifetime and the kernel cleans up
    # on exit.
    _singleton_handle = run_or_exit_if_held()  # noqa: F841

    global logger
    logger = setup_logging()
    logger.info("Starting EntraBot MCP server (progressive identity)")
    anyio.run(_run_stdio_with_write_stream)


if __name__ == "__main__":
    main()
