"""Tests for the identity state machine.

Covers:
- All valid transitions
- Invalid transitions raise InvalidTransitionError
- Lock timeout raises TransitionTimeoutError
- Callback exceptions cause rollback (TransitionError)
- State unchanged on invalid transition
- Re-check after lock acquisition
- attribution_type updates on transition
- update_session
- Listeners called
- IdentitySession repr redacts token
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from entrabot.errors import (
    InvalidTransitionError,
    TransitionError,
    TransitionTimeoutError,
)
from entrabot.identity.state_machine import (
    LOCK_TIMEOUT,
    VALID_TRANSITIONS,
    IdentityStateMachine,
)
from entrabot.models import IdentitySession, IdentityState


class TestValidTransitions:
    """Every edge in the valid-transitions table must succeed."""

    @pytest.mark.asyncio
    async def test_unauthenticated_to_delegated(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        assert sm.state == IdentityState.DELEGATED

    @pytest.mark.asyncio
    async def test_unauthenticated_to_agent_user_fast_path(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.AGENT_USER)
        assert sm.state == IdentityState.AGENT_USER

    @pytest.mark.asyncio
    async def test_delegated_to_provisioning(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        await sm.transition(IdentityState.PROVISIONING)
        assert sm.state == IdentityState.PROVISIONING

    @pytest.mark.asyncio
    async def test_delegated_to_unauthenticated(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        await sm.transition(IdentityState.UNAUTHENTICATED)
        assert sm.state == IdentityState.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_provisioning_to_agent_user(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        await sm.transition(IdentityState.PROVISIONING)
        await sm.transition(IdentityState.AGENT_USER)
        assert sm.state == IdentityState.AGENT_USER

    @pytest.mark.asyncio
    async def test_provisioning_to_error(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        await sm.transition(IdentityState.PROVISIONING)
        await sm.transition(IdentityState.ERROR)
        assert sm.state == IdentityState.ERROR

    @pytest.mark.asyncio
    async def test_provisioning_to_delegated_fallback(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        await sm.transition(IdentityState.PROVISIONING)
        await sm.transition(IdentityState.DELEGATED)
        assert sm.state == IdentityState.DELEGATED

    @pytest.mark.asyncio
    async def test_error_to_delegated_recovery(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        await sm.transition(IdentityState.PROVISIONING)
        await sm.transition(IdentityState.ERROR)
        await sm.transition(IdentityState.DELEGATED)
        assert sm.state == IdentityState.DELEGATED

    @pytest.mark.asyncio
    async def test_error_to_unauthenticated_reset(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        await sm.transition(IdentityState.PROVISIONING)
        await sm.transition(IdentityState.ERROR)
        await sm.transition(IdentityState.UNAUTHENTICATED)
        assert sm.state == IdentityState.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_agent_user_to_error(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.AGENT_USER)
        await sm.transition(IdentityState.ERROR)
        assert sm.state == IdentityState.ERROR

    @pytest.mark.asyncio
    async def test_agent_user_to_unauthenticated_reset(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.AGENT_USER)
        await sm.transition(IdentityState.UNAUTHENTICATED)
        assert sm.state == IdentityState.UNAUTHENTICATED


class TestInvalidTransitions:
    """Transitions not in the table must raise InvalidTransitionError."""

    @pytest.mark.asyncio
    async def test_unauthenticated_to_provisioning_invalid(self) -> None:
        sm = IdentityStateMachine()
        with pytest.raises(InvalidTransitionError) as exc_info:
            await sm.transition(IdentityState.PROVISIONING)
        assert exc_info.value.from_state == "unauthenticated"
        assert exc_info.value.to_state == "provisioning"

    @pytest.mark.asyncio
    async def test_unauthenticated_to_error_invalid(self) -> None:
        sm = IdentityStateMachine()
        with pytest.raises(InvalidTransitionError):
            await sm.transition(IdentityState.ERROR)

    @pytest.mark.asyncio
    async def test_delegated_to_agent_user_invalid(self) -> None:
        """Must go through provisioning first."""
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        with pytest.raises(InvalidTransitionError):
            await sm.transition(IdentityState.AGENT_USER)

    @pytest.mark.asyncio
    async def test_state_unchanged_on_invalid_transition(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        with pytest.raises(InvalidTransitionError):
            await sm.transition(IdentityState.AGENT_USER)
        assert sm.state == IdentityState.DELEGATED


class TestCallbackAndRollback:
    """Callback exceptions cause rollback via TransitionError."""

    @pytest.mark.asyncio
    async def test_callback_executes(self) -> None:
        sm = IdentityStateMachine()
        called = False

        async def cb() -> None:
            nonlocal called
            called = True

        await sm.transition(IdentityState.DELEGATED, callback=cb)
        assert called
        assert sm.state == IdentityState.DELEGATED

    @pytest.mark.asyncio
    async def test_callback_exception_causes_rollback(self) -> None:
        sm = IdentityStateMachine()
        original_error = ValueError("boom")

        async def bad_cb() -> None:
            raise original_error

        with pytest.raises(TransitionError) as exc_info:
            await sm.transition(IdentityState.DELEGATED, callback=bad_cb)

        assert sm.state == IdentityState.UNAUTHENTICATED  # rolled back
        assert exc_info.value.from_state == "unauthenticated"
        assert exc_info.value.to_state == "delegated"
        assert exc_info.value.cause is original_error

    @pytest.mark.asyncio
    async def test_callback_exception_restores_entire_session(self) -> None:
        """Rollback restores all IdentitySession fields mutated by the callback."""
        sm = IdentityStateMachine()

        async def set_old_identity() -> None:
            sm.update_session(
                token="old-token",
                token_acquired_at=100.0,
                user_id="old-user",
                display_name="Old User",
                attribution_type="none",
                auth_mode="delegated",
                account_id="old-account",
                tenant_id="old-tenant",
                provisioning_state="old-provisioning",
            )

        await sm.transition(IdentityState.DELEGATED, callback=set_old_identity)
        before = replace(sm.session)
        original_error = RuntimeError("callback failed after mutating identity")

        async def bad_cb() -> None:
            sm.update_session(
                token="new-token",
                token_acquired_at=200.0,
                user_id="new-user",
                display_name="New User",
                attribution_type="agent",
                auth_mode="agent_user",
                account_id="new-account",
                tenant_id="new-tenant",
                provisioning_state="new-provisioning",
            )
            raise original_error

        with pytest.raises(TransitionError):
            await sm.transition(IdentityState.PROVISIONING, callback=bad_cb)

        assert sm.session == before

    @pytest.mark.asyncio
    async def test_callback_success_persists_session_mutations(self) -> None:
        """Successful callbacks commit session field mutations and state change."""
        sm = IdentityStateMachine()

        async def cb() -> None:
            sm.update_session(
                token="new-token",
                token_acquired_at=200.0,
                user_id="new-user",
                display_name="New User",
                auth_mode="delegated",
                account_id="new-account",
                tenant_id="new-tenant",
                provisioning_state="new-provisioning",
            )

        await sm.transition(IdentityState.DELEGATED, callback=cb)

        assert sm.session.state == IdentityState.DELEGATED
        assert sm.session.token == "new-token"
        assert sm.session.token_acquired_at == 200.0
        assert sm.session.user_id == "new-user"
        assert sm.session.display_name == "New User"
        assert sm.session.auth_mode == "delegated"
        assert sm.session.account_id == "new-account"
        assert sm.session.tenant_id == "new-tenant"
        assert sm.session.provisioning_state == "new-provisioning"

    @pytest.mark.asyncio
    async def test_refresh_pattern_survives_later_failed_transition(self) -> None:
        """Token refresh update_session calls are not rolled back by later failures."""
        sm = IdentityStateMachine()

        sm.update_session(token="initial", user_id="agent")
        await sm.transition(IdentityState.AGENT_USER)

        sm.update_session(token="refreshed", token_acquired_at=12345.0)

        with pytest.raises(InvalidTransitionError):
            await sm.transition(IdentityState.DELEGATED)

        assert sm.session.state == IdentityState.AGENT_USER
        assert sm.session.token == "refreshed"
        assert sm.session.token_acquired_at == 12345.0
        assert sm.session.user_id == "agent"

    @pytest.mark.asyncio
    async def test_sequential_callback_failure_rolls_back_to_lock_snapshot(self) -> None:
        """Callback failure restores the session snapshot captured by transition()."""
        sm = IdentityStateMachine()
        sm.update_session(token="prepared", user_id="prepared-user")
        before = replace(sm.session)
        original_error = RuntimeError("callback failed")

        async def bad_cb() -> None:
            sm.update_session(token="callback-token", user_id="callback-user")
            raise original_error

        with pytest.raises(TransitionError) as exc_info:
            await sm.transition(IdentityState.DELEGATED, callback=bad_cb)

        assert sm.session == before
        assert exc_info.value.cause is original_error

    @pytest.mark.asyncio
    async def test_callback_none_succeeds(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED, callback=None)
        assert sm.state == IdentityState.DELEGATED


class TestLockTimeout:
    """Lock acquisition timeout raises TransitionTimeoutError."""

    @pytest.mark.asyncio
    async def test_lock_timeout_raises(self) -> None:
        sm = IdentityStateMachine()
        # Manually acquire the lock so transition cannot acquire it
        await sm._lock.acquire()

        with pytest.raises(TransitionTimeoutError):
            # Use a very short timeout by monkey-patching the constant
            import entrabot.identity.state_machine as sm_mod

            original_timeout = sm_mod.LOCK_TIMEOUT
            sm_mod.LOCK_TIMEOUT = 0.05  # 50ms
            try:
                await sm.transition(IdentityState.DELEGATED)
            finally:
                sm_mod.LOCK_TIMEOUT = original_timeout
                sm._lock.release()


class TestRecheckAfterLock:
    """Transition validity is checked against the state after lock acquisition."""

    @pytest.mark.asyncio
    async def test_concurrent_transition_does_not_wipe_successful_commit(self) -> None:
        sm = IdentityStateMachine()
        cb_started = asyncio.Event()
        cb_release = asyncio.Event()

        async def slow_cb() -> None:
            cb_started.set()
            await cb_release.wait()

        sm.update_session(token="A", user_id="u1")
        t1 = asyncio.create_task(
            sm.transition(IdentityState.DELEGATED, callback=slow_cb)
        )
        await cb_started.wait()

        sm.update_session(token="B", user_id="u2")
        t2 = asyncio.create_task(sm.transition(IdentityState.AGENT_USER))
        await asyncio.sleep(0)

        cb_release.set()
        results = await asyncio.gather(t1, t2, return_exceptions=True)

        assert results[0] is None
        assert isinstance(results[1], InvalidTransitionError)
        assert sm.session.state == IdentityState.DELEGATED
        assert sm.session.token == "B"
        assert sm.session.user_id == "u2"

    @pytest.mark.asyncio
    async def test_target_invalid_after_waiting_for_lock(self) -> None:
        sm = IdentityStateMachine()

        # Simulate: while waiting, another task transitions state
        original_acquire = sm._lock.acquire

        async def mutating_acquire() -> bool:
            result = await original_acquire()
            # Directly mutate state to simulate race
            sm._session.state = IdentityState.AGENT_USER
            return result

        sm._lock.acquire = mutating_acquire  # type: ignore[assignment]

        with pytest.raises(InvalidTransitionError):
            await sm.transition(IdentityState.DELEGATED)


class TestAttributionType:
    """Attribution type updates on transitions."""

    @pytest.mark.asyncio
    async def test_delegated_sets_delegated_human(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        assert sm.session.attribution_type == "delegated-human"

    @pytest.mark.asyncio
    async def test_agent_user_sets_agent(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.AGENT_USER)
        assert sm.session.attribution_type == "agent"

    @pytest.mark.asyncio
    async def test_unauthenticated_sets_none(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        await sm.transition(IdentityState.UNAUTHENTICATED)
        assert sm.session.attribution_type == "none"

    @pytest.mark.asyncio
    async def test_provisioning_preserves_attribution(self) -> None:
        sm = IdentityStateMachine()
        await sm.transition(IdentityState.DELEGATED)
        assert sm.session.attribution_type == "delegated-human"
        await sm.transition(IdentityState.PROVISIONING)
        # Provisioning doesn't change attribution
        assert sm.session.attribution_type == "delegated-human"


class TestUpdateSession:
    """update_session modifies fields without state transition."""

    def test_update_token(self) -> None:
        sm = IdentityStateMachine()
        sm.update_session(token="new-token", user_id="u123")
        assert sm.session.token == "new-token"
        assert sm.session.user_id == "u123"

    def test_update_rejects_state(self) -> None:
        sm = IdentityStateMachine()
        with pytest.raises(AttributeError, match="state"):
            sm.update_session(state=IdentityState.DELEGATED)

    def test_update_rejects_unknown_field(self) -> None:
        sm = IdentityStateMachine()
        with pytest.raises(AttributeError):
            sm.update_session(nonexistent_field="value")


class TestListeners:
    """Listeners are notified on successful transitions."""

    @pytest.mark.asyncio
    async def test_listener_called(self) -> None:
        sm = IdentityStateMachine()
        transitions_seen: list[tuple[IdentityState, IdentityState]] = []

        def listener(from_s: IdentityState, to_s: IdentityState) -> None:
            transitions_seen.append((from_s, to_s))

        sm.add_listener(listener)
        await sm.transition(IdentityState.DELEGATED)
        assert len(transitions_seen) == 1
        assert transitions_seen[0] == (
            IdentityState.UNAUTHENTICATED,
            IdentityState.DELEGATED,
        )

    @pytest.mark.asyncio
    async def test_async_listener_called(self) -> None:
        sm = IdentityStateMachine()
        called = AsyncMock()
        sm.add_listener(called)
        await sm.transition(IdentityState.DELEGATED)
        called.assert_called_once_with(IdentityState.UNAUTHENTICATED, IdentityState.DELEGATED)

    @pytest.mark.asyncio
    async def test_listener_error_does_not_block(self) -> None:
        sm = IdentityStateMachine()

        def bad_listener(from_s: IdentityState, to_s: IdentityState) -> None:
            raise RuntimeError("listener broke")

        sm.add_listener(bad_listener)
        # Should not raise despite listener failure
        await sm.transition(IdentityState.DELEGATED)
        assert sm.state == IdentityState.DELEGATED

    @pytest.mark.asyncio
    async def test_listener_not_called_on_invalid_transition(self) -> None:
        sm = IdentityStateMachine()
        listener = MagicMock()
        sm.add_listener(listener)
        with pytest.raises(InvalidTransitionError):
            await sm.transition(IdentityState.PROVISIONING)
        listener.assert_not_called()


class TestIdentitySessionRepr:
    """IdentitySession repr/str redact the token."""

    def test_repr_redacts_token(self) -> None:
        session = IdentitySession(token="super-secret-token-abc")
        assert "super-secret-token-abc" not in repr(session)
        assert "***REDACTED***" in repr(session)

    def test_str_redacts_token(self) -> None:
        session = IdentitySession(token="super-secret-token-abc")
        assert "super-secret-token-abc" not in str(session)
        assert "***REDACTED***" in str(session)

    def test_token_still_accessible(self) -> None:
        session = IdentitySession(token="my-secret")
        assert session.token == "my-secret"

    def test_f_string_redacts(self) -> None:
        session = IdentitySession(token="my-secret")
        formatted = f"session = {session}"
        assert "my-secret" not in formatted


class TestValidTransitionsTable:
    """The VALID_TRANSITIONS table covers all expected edges."""

    def test_all_states_have_entry(self) -> None:
        for state in IdentityState:
            assert state in VALID_TRANSITIONS

    def test_lock_timeout_constant(self) -> None:
        assert LOCK_TIMEOUT == 30.0
