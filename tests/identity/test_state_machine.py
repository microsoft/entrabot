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
    """If state changes while waiting for lock, InvalidTransitionError is raised."""

    @pytest.mark.asyncio
    async def test_state_changed_while_waiting_for_lock(self) -> None:
        sm = IdentityStateMachine()

        # Simulate: while waiting, another task transitions state
        original_acquire = sm._lock.acquire

        async def mutating_acquire() -> bool:
            result = await original_acquire()
            # Directly mutate state to simulate race
            sm._session.state = IdentityState.ERROR
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
