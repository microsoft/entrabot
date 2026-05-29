"""Identity state machine with asyncio.Lock-protected transitions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from entrabot.errors import (
    InvalidTransitionError,
    TransitionError,
    TransitionTimeoutError,
)
from entrabot.models import IdentitySession, IdentityState

logger = logging.getLogger("entrabot.identity.state_machine")

LOCK_TIMEOUT = 30.0  # seconds — deadlock safety net, not operation timeout

# Valid state transitions
VALID_TRANSITIONS: dict[IdentityState, set[IdentityState]] = {
    IdentityState.UNAUTHENTICATED: {
        IdentityState.DELEGATED,
        IdentityState.AGENT_USER,
    },
    IdentityState.DELEGATED: {
        IdentityState.PROVISIONING,
        IdentityState.UNAUTHENTICATED,
    },
    IdentityState.PROVISIONING: {
        IdentityState.AGENT_USER,
        IdentityState.ERROR,
        IdentityState.DELEGATED,
    },
    IdentityState.ERROR: {
        IdentityState.DELEGATED,
        IdentityState.UNAUTHENTICATED,
    },
    IdentityState.AGENT_USER: {
        IdentityState.ERROR,
        IdentityState.UNAUTHENTICATED,
    },
}


class IdentityStateMachine:
    """Manages identity state transitions with asyncio.Lock protection.

    The lock covers only the state mutation (microsecond hold time).
    Auth and provisioning operations run OUTSIDE the lock.
    """

    def __init__(self) -> None:
        self._session = IdentitySession()
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[IdentityState, IdentityState], Any]] = []

    @property
    def state(self) -> IdentityState:
        return self._session.state

    @property
    def session(self) -> IdentitySession:
        return self._session

    def add_listener(self, callback: Callable[[IdentityState, IdentityState], Any]) -> None:
        """Register a callback for state transitions. Called with (from_state, to_state)."""
        self._listeners.append(callback)

    async def transition(
        self,
        to_state: IdentityState,
        *,
        callback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Transition to a new state with optional callback.

        The callback runs INSIDE the lock — keep it fast (no I/O).
        For I/O operations, do them before calling transition and pass
        results via the callback closure.

        Raises:
            InvalidTransitionError: If the transition is not valid
            TransitionTimeoutError: If lock acquisition times out (30s)
            TransitionError: If the callback raises (auto-rollback)
        """
        from_state = self._session.state

        # Validate transition
        valid = VALID_TRANSITIONS.get(from_state, set())
        if to_state not in valid:
            raise InvalidTransitionError(
                from_state=from_state.value,
                to_state=to_state.value,
            )

        # Acquire lock with timeout
        try:
            await asyncio.wait_for(
                self._lock.acquire(),
                timeout=LOCK_TIMEOUT,
            )
        except TimeoutError:
            raise TransitionTimeoutError(
                f"Lock acquisition timed out after {LOCK_TIMEOUT}s "
                f"(from={from_state.value}, to={to_state.value})"
            ) from None

        try:
            # Re-check state hasn't changed while waiting for lock
            if self._session.state != from_state:
                raise InvalidTransitionError(
                    from_state=self._session.state.value,
                    to_state=to_state.value,
                )

            # Execute callback if provided
            if callback:
                try:
                    await callback()
                except Exception as exc:
                    # Rollback — state stays at from_state
                    logger.error(
                        "Transition callback failed, rolling back: %s → %s, error: %s",
                        from_state.value,
                        to_state.value,
                        exc,
                    )
                    raise TransitionError(
                        from_state=from_state.value,
                        to_state=to_state.value,
                        cause=exc,
                    ) from exc

            # Commit transition
            self._session.state = to_state

            # Update attribution
            if to_state == IdentityState.DELEGATED:
                self._session.attribution_type = "delegated-human"
            elif to_state == IdentityState.AGENT_USER:
                self._session.attribution_type = "agent"
            elif to_state == IdentityState.UNAUTHENTICATED:
                self._session.attribution_type = "none"

            logger.info(
                "State transition: %s → %s",
                from_state.value,
                to_state.value,
            )
        finally:
            self._lock.release()

        # Notify listeners (outside lock)
        for listener in self._listeners:
            try:
                result = listener(from_state, to_state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.warning(
                    "Listener error during %s → %s",
                    from_state.value,
                    to_state.value,
                )

    def update_session(self, **kwargs: Any) -> None:
        """Update session fields without a state transition.

        Use this for token updates, user_id changes, etc. that don't
        change the identity state.
        """
        for key, value in kwargs.items():
            if hasattr(self._session, key) and key != "state":
                setattr(self._session, key, value)
            else:
                raise AttributeError(f"IdentitySession has no field '{key}'")
