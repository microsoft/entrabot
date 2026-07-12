# Identity

Identity state machine and sponsor enforcement. Source under `src/entrabot/identity/`.

## `IdentityStateMachine`

`src/entrabot/identity/state_machine.py`. Manages identity state transitions with `asyncio.Lock` protection. The lock covers state transitions and `update_session()` calls; auth and provisioning I/O should run outside the lock and be passed in via closures.

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

    async def update_session(self, **kwargs: Any) -> None
```

The `callback` runs INSIDE the lock — keep it fast (no I/O). For I/O operations, do them before calling `transition`.

`add_listener` registers a callback fired with `(from_state, to_state)` on every transition. The MCP server uses listeners to update logging context and refresh the cached host.

`update_session()` also acquires the lock, so it must never be awaited from inside a `transition()` callback — the callback already runs while `transition()` holds the lock, and `asyncio.Lock` is not reentrant. Mutate the session directly inside the callback instead.

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
    other_mails: tuple[str, ...] = ()
    proxy_addresses: tuple[str, ...] = ()
    federated_emails: tuple[str, ...] = ()

    def email_identifiers(self) -> frozenset[str]
```

Normalized view of a sponsor. `email_identifiers()` returns every email-shaped identifier (UPN, decoded B2B ext UPN, mail, other mails, proxy addresses, federated emails), normalized and deduplicated.

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
    def with_watched_chat_ids(
        self,
        chat_members_by_id: dict[str, list[dict[str, Any]]],
        agent_user_id: str,
    ) -> SponsorGate

    def accepts(self, message: dict[str, Any]) -> bool
```

Allow inbound Teams messages only from the Agent Identity's user sponsors.

- `from_agent_identity_sponsors()` builds the initial gate from Graph `/sponsors`.
- `with_chat_members()` adds chat-member user IDs only when their Graph email matches a sponsor identity.
- `with_watched_chat_ids()` extracts the cross-tenant sponsor's home-tenant userId from 1:1 chat IDs (`19:{user_a_id}_{user_b_id}@unq.gbl.spaces`) — Graph does not expose the cross-tenant guest's email in the chat-members API, so the chat_id is the only reliable carrier. It promotes the counterparty to a sponsor only for chats already verified to contain a known sponsor; unverifiable chats (for example, an empty member list) are skipped rather than trusted.
- `accepts()` checks an inbound message dict's `sender_id`/`sender` fields against `user_ids`, `upns`, and `mails`.

### `fetch_agent_identity_sponsors`

```python
def fetch_agent_identity_sponsors(
    config: EntraBotConfig,
    *,
    token_provider: Callable[[EntraBotConfig], str] = acquire_agent_identity_token,
    user_token_provider: Callable[[EntraBotConfig], str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[AgentIdentitySponsor]
```

Fetch the sponsors from Graph. Two Graph calls happen with different scope requirements: reading the Agent Identity's `/sponsors` relationship needs an app-only token (`token_provider`, which defaults to `acquire_agent_identity_token` since the Agent User's delegated token cannot read `/sponsors`), while enriching each sponsor with email fields needs `User.Read.All`. When `user_token_provider` is supplied (for example `acquire_agent_user_token`), the enrichment call uses that token instead; otherwise both calls reuse the same Agent Identity token, which is sufficient for `user_id`-only matching.

### `load_agent_identity_sponsor_gate`

```python
def load_agent_identity_sponsor_gate(config: EntraBotConfig) -> SponsorGate
```

The convenience constructor used by the MCP server at boot: fetches sponsors, builds the gate, then layers `with_chat_members` and `with_watched_chat_ids` for each watched 1:1 chat.

## Files-tool sponsor gate

`src/entrabot/tools/files.py` defines the sponsor allowlist helpers (`_get_sponsor_records`, `_get_sponsor_allowlist`) and uses them to gate `share_file`. `entrabot.tools.teams.add_member` (the implementation behind the `add_teams_member` MCP tool) re-exports `_get_sponsor_records` from `files.py` so both tools share the same allowlist logic. Both `share_file` and `add_member` require a `requester_email` argument and reject any requester that is not in the resolved sponsor allowlist. The recipient (`recipient_email` / `email`) is unrestricted — sponsors may share with anyone they choose.

Functions:

- `_get_sponsor_records()` — reads the live sponsor list.
- `_get_sponsor_allowlist()` — flattens into a normalized email set.

## Authenticated session types

`_init_auth` selects the identity path by credential presence and `ENTRABOT_SKIP_PROVISIONING`, not by `ENTRABOT_MODE` (which is validated but not currently consumed): with a Blueprint app ID + tenant ID and provisioning enabled it tries three-hop first, then falls back to MSAL delegated when `ENTRABOT_CLIENT_ID` is set.

| Session type | Description |
|------|-------------|
| `agent_user` | Three-hop cert flow. The Agent User authenticates autonomously. |
| `delegated` | MSAL interactive auth with the human's token. Messages prefixed `[EntraBot]`. |

See [Delegated Authentication](../../platform-docs/delegated-auth.md) for detail.

See [Configuration](../configuration.md) for the full env-var contract.

## Related

- [Auth](auth.md) — token acquisition.
- [Token Flows](../token-flows.md) — flow diagrams.
- [Identity and Token Flow](../../architecture/identity-and-token-flow.md) — Entrabot's implementation of the identity model.
- [Agent Users](../../platform-docs/entra-agent-users.md) — three-hop flow specifics.
- [Configuration](../configuration.md) — the full environment variable reference.
