"""
Phase 2 session isolation stub for MXC sandbox.

**Current status:** PHASE 2 NOT IMPLEMENTED. This module is a seam for future work.

Phase 2 Requirements (when Entra/Intune APIs are GA):
-----------------------------------------------------

1. **Identity Binding:**
   - Bind MXC sandbox sessions to Entra Agent User identity
   - Every sandboxed execution attributed to the agent, not the human operator
   - M365 audit logs distinguish "agent did this" from "human did this"

2. **Session Isolation:**
   - Each agent conversation gets isolated MXC session (Backend.SESSION)
   - Cross-conversation isolation via MXC session boundaries
   - Prevent one conversation from leaking state into another

3. **Governance Integration:**
   - Intune policies control sandbox capabilities per Agent User
   - Conditional access rules apply to agent actions (device trust, compliance)
   - Admin can revoke/narrow agent capabilities centrally

4. **Platform Requirements:**
   - Windows: Session-bound AppContainer with Entra SID
   - macOS: Per-session Seatbelt profile + identity attribution
   - MXC API surface for identity binding (not yet GA)

Gating Questions:
-----------------

Q: Is entrabot's Entra Agent User the same identity that MXC attributes sessions to?
A: UNVERIFIED. Assumption in design, needs validation when MXC+Entra APIs ship.

Q: Can MXC sessions reference external identity providers (Entra)?
A: UNCLEAR. Windows Insider builds show session isolation, but Entra binding unclear.

Q: Does Intune expose agent governance APIs for non-human principals?
A: NO (as of 2026-06). Intune device/user policies exist, but agent-specific unclear.

Phase 1 (CURRENT):
------------------

Process-level containment without identity binding:
- Backend.PROCESS only (no session isolation)
- Attribution via audit logs (entrabot layer), not OS-level
- Sufficient for basic containment, insufficient for compliance/governance

Usage (Phase 2, future):
-------------------------

    from entrabot.sandbox.session import Backend, SessionConfig, identity_binding

    # Get agent identity from entrabot auth layer
    agent_user_id = get_agent_user_id()  # From three-hop flow
    tenant_id = get_tenant_id()

    # Build session config
    config = SessionConfig(
        agent_user_id=agent_user_id,
        tenant_id=tenant_id,
        intune_policy_id="optional-policy-id",
    )

    # Bind MXC session to Entra identity (Phase 2 API call)
    session_token = identity_binding(config)  # Raises NotImplementedError now

    # Pass session_token to MXC binary via --session flag
    # MXC attributes all actions in this session to agent_user_id
"""

from dataclasses import dataclass
from enum import Enum


class Backend(Enum):
    """
    Sandbox backend types.

    PROCESS: Process-level containment (Phase 1, current).
             No session isolation, no identity binding.
             Uses: macOS Seatbelt, Windows AppContainer, Linux seccomp-bpf.

    SESSION: Session-bound containment (Phase 2, future).
             Per-conversation isolation with Entra identity attribution.
             Requires: MXC session API + Entra binding (not yet GA).
    """

    PROCESS = "process"
    SESSION = "session"  # Phase 2 - not implemented


@dataclass
class SessionConfig:
    """
    Configuration for Phase 2 Entra-bound MXC sessions.

    Attributes:
        agent_user_id: Entra Agent User object ID (from three-hop flow)
        tenant_id: Entra tenant ID where agent is provisioned
        intune_policy_id: Optional Intune policy governing agent capabilities
    """

    agent_user_id: str  # UUID format
    tenant_id: str  # UUID format
    intune_policy_id: str | None = None  # Optional governance


def identity_binding(config: SessionConfig) -> str:
    """
    Bind MXC session to Entra Agent User identity (PHASE 2 NOT IMPLEMENTED).

    When implemented, this function will:
    1. Authenticate to Entra as the Agent User (three-hop flow)
    2. Request MXC session token bound to agent identity
    3. Return session token for passing to MXC binary via --session flag
    4. All subsequent sandbox operations attributed to agent_user_id

    Current behavior:
        Raises NotImplementedError (Phase 2 APIs not GA yet)

    Args:
        config: SessionConfig with agent identity and optional governance

    Returns:
        Session token string (when implemented)

    Raises:
        NotImplementedError: Phase 2 not implemented (Entra/MXC APIs not GA)

    Phase 2 Requirements:
        - MXC session API (not in 0.6.0-alpha schema)
        - Entra Agent User provisioning (GA as of 2026-05-01)
        - MXC identity binding surface (unclear if GA)
        - Intune agent governance (APIs unclear)

    Example (future):
        >>> config = SessionConfig(
        ...     agent_user_id="00000000-0000-0000-0000-000000000000",
        ...     tenant_id="00000000-0000-0000-0000-000000000000",
        ... )
        >>> session_token = identity_binding(config)  # Phase 2
        >>> # Pass to MXC: mxc-exec --session {session_token} policy.json
    """
    raise NotImplementedError(
        "Phase 2 identity binding not implemented. "
        "Requires MXC session API + Entra binding (not GA yet). "
        f"Received config: agent_user_id={config.agent_user_id}, "
        f"tenant_id={config.tenant_id}"
    )
