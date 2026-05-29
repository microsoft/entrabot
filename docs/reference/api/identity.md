# Identity

Identity state machine and sponsor enforcement. Source under `src/entrabot/identity/`.

## `IdentityStateMachine`

`src/entrabot/identity/state_machine.py`. Manages identity state transitions with `asyncio.Lock` protection. The lock covers only the state mutation (microsecond hold time); auth and provisioning operations run outside the lock.

### States

```
UNAUTHENTICATED → DELEGATED       (browser sign-in)
UNAUTHENTICATED → AGENT_USER      (cert-auth fast path)
DELEGATED       → PROVISIONING    (mid-session promotion to Agent User)
DELEGATED       → UNAUTHENTICATED (sign-out)
PROVISIONING    → AGENT_USER
PROVISIONING    → ERROR
PROVISIONING    → DELEGATED       (rollback)
ERROR           → DELEGATED
ERROR           → UNAUTHENTICATED
AGENT_USER      → ERROR
AGENT_USER      → UNAUTHENTICATED
```

Invalid transitions raise `InvalidTransitionError`. Transitions that exceed `LOCK_TIMEOUT` (30s) raise `TransitionTimeoutError`.

### API

```python
class IdentityStateMachine:
    def __init__(self) -> None

    @property
    def state(self) -> IdentityState
    @property
    def session(self) -> IdentitySession

    def add_listener(self, callback: Callable[[IdentityState, IdentityState], Any]) -> None

    async def transition(
        self,
        to_state: IdentityState,
        *,
        callback: Callable[[], Awaitable[None]] | None = None,
    ) -> None

    def update_session(self, **kwargs: Any) -> None
```

The `callback` runs INSIDE the lock — keep it fast (no I/O). For I/O operations, do them before calling `transition`.

`add_listener` registers a callback fired with `(from_state, to_state)` on every transition. The MCP server uses listeners to update logging context and refresh the cached host.

## Sponsor enforcement

Sponsors are users authorized to give the Agent Identity operational instructions. The Agent Identity's `/sponsors` Graph relationship is the authoritative list. `wait_for_sponsor_dm` and the background poll filter inbound messages against it.

### `AgentIdentitySponsor`

`src/entrabot/identity/sponsors.py`:

```python
@dataclass(frozen=True)
class AgentIdentitySponsor:
    user_id: str
    user_principal_name: str | None
    mail: str | None
    proxy_addresses: tuple[str, ...]
    federated_emails: tuple[str, ...]

    def email_identifiers(self) -> tuple[str, ...]
```

Normalized view of a sponsor. `email_identifiers()` returns every email-shaped identifier (UPN, mail, proxy addresses, federated emails decoded from B2B ext UPNs).

### `SponsorGate`

```python
@dataclass(frozen=True)
class SponsorGate:
    user_ids: frozenset[str]
    upns: frozenset[str]
    mails: frozenset[str]

    @classmethod
    def from_agent_identity_sponsors(
        cls,
        sponsors: list[AgentIdentitySponsor],
    ) -> SponsorGate

    def with_chat_members(self, members: list[dict[str, Any]]) -> SponsorGate
    def with_watched_chat_ids(self, chat_ids: list[str], agent_user_id: str) -> SponsorGate

    def is_sponsor_message(
        self,
        from_user_id: str | None,
        from_email: str | None,
        from_upn: str | None,
    ) -> bool
```

Allow inbound Teams messages only from the Agent Identity's user sponsors.

- `from_agent_identity_sponsors()` builds the initial gate from Graph `/sponsors`.
- `with_chat_members()` adds chat-member user IDs only when their Graph email matches a sponsor identity.
- `with_watched_chat_ids()` extracts the cross-tenant sponsor's home-tenant userId from 1:1 chat IDs (`19:{user_a_id}_{user_b_id}@unq.gbl.spaces`) — Graph does not expose the cross-tenant guest's email in the chat-members API, so the chat_id is the only reliable carrier.

### `fetch_agent_identity_sponsors`

```python
def fetch_agent_identity_sponsors(
    config: EntraBotConfig,
    *,
    token_provider: Callable[[], str] | None = None,
) -> list[AgentIdentitySponsor]
```

Fetch the sponsors from Graph. Uses `acquire_agent_identity_token` (app-only) — the Agent User's delegated token cannot read `/sponsors` (Learning #20).

### `load_agent_identity_sponsor_gate`

```python
def load_agent_identity_sponsor_gate(config: EntraBotConfig) -> SponsorGate
```

The convenience constructor used by the MCP server at boot: fetches sponsors, builds the gate, then layers `with_chat_members` and `with_watched_chat_ids` for each watched 1:1 chat.

## Files-tool sponsor gate

`src/entrabot/tools/files.py` carries the same gating model for `share_file` and `add_teams_member`. Both require a `requester_email` argument and reject any requester that is not in the resolved sponsor allowlist. The recipient (`recipient_email` / `email`) is unrestricted — sponsors may share with anyone they choose.

Functions:

- `_get_sponsor_records()` — reads the live sponsor list.
- `_get_sponsor_allowlist()` — flattens into a normalized email set.

## Auth modes

`ENTRABOT_MODE` selects which identity path the MCP server runs:

| Mode | Description |
|------|-------------|
| `agent_user` | Three-hop cert flow. The Agent User authenticates autonomously. Default. |
| `delegated` | MSAL interactive auth with the human's token. Messages prefixed `[EntraBot]`. |
| `bot` | M365 Agents SDK bot server with JSONL IPC. Bot has its own Teams identity. |
| `auto` | Pick the best mode based on config. |

See `src/entrabot/config.py` for the env-var contract.

## Related

- [Auth](auth.md) — token acquisition.
- [Token Flows](../token-flows.md) — flow diagrams.
- ADR-002: Agent User over OBO.
- `docs/platform-learnings/entra-agent-users.md` — three-hop flow specifics.
- Learning #20: Agent Identity sponsors require app-only auth.
