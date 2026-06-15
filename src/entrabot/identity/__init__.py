"""Identity state machine for progressive identity."""

from entrabot.identity.state_machine import IdentityStateMachine

_active_identity_state: IdentityStateMachine | None = None


def set_active_identity_state(identity: IdentityStateMachine | None) -> None:
    """Publish the process-wide identity state for non-MCP helper modules."""
    global _active_identity_state
    _active_identity_state = identity


def get_active_identity_state() -> IdentityStateMachine | None:
    """Return the process-wide identity state if the MCP server has initialized it."""
    return _active_identity_state


__all__ = [
    "IdentityStateMachine",
    "get_active_identity_state",
    "set_active_identity_state",
]
