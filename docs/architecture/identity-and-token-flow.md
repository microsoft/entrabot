# Identity and Token Flow

## Actors

Entrabot's identity chain has four Entra resources, each scoped to the one before it:

| Actor | What it is | Role in the token flow |
|---|---|---|
| **Blueprint** | An Agent Identity Blueprint app registration. Has an app ID and a certificate credential — no client secret. | Hop 1: authenticates with a certificate-signed JWT assertion. |
| **BlueprintPrincipal** | The Blueprint's service principal — created explicitly as its own provisioning step, never automatically. | Not itself a token-flow party; it's what makes the Blueprint app resolvable as a principal in the tenant. |
| **Agent Identity** | A service principal scoped to the Blueprint, one per device. Has its own app/client ID (`config.agent_id`). | Hop 2: exchanges a Blueprint-issued token for its own token via FIC. Also used standalone for the two-hop app-only flow. |
| **Agent User** | A real Entra user object linked to the Agent Identity (`config.agent_user_id`) — the thing with Teams presence, a mailbox, and (if licensed) an AI-agent badge. | Hop 3: the identity a `user_fic` grant mints a delegated token for. |

Object IDs vs. app (client) IDs only matter where the flow cites them explicitly: `config.agent_id` is the Agent Identity's **client ID**, used as `client_id` in Hops 2 and 3; `config.agent_user_id` is the Agent User's **object ID**, used as `user_id` in Hop 3. The Blueprint's `client_id` is its own app ID, distinct from both.

See [System Overview](system-overview.md) for how these four resources fit into the rest of Entrabot, and the [Identity Lifecycle guide](../guides/identity-lifecycle.md) for the provisioning order and teardown.

## The three-hop Agent User flow

Implemented as `acquire_agent_user_token()` in `src/entrabot/tools/teams.py`. Every hop posts to the same tenant v2 token endpoint (`https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`) over a plain `httpx.Client` with a 15-second timeout — no MSAL involved.

All three requests go to the tenant's `/oauth2/v2.0/token` endpoint:

| Hop | Request fields | Result |
|---|---|---|
| **1 — Blueprint `client_credentials`** | `client_id` = Blueprint app ID; certificate-signed JWT as `client_assertion`; `scope=api://AzureADTokenExchange/.default`; `fmi_path=config.agent_id`; `grant_type=client_credentials`. | **T1** |
| **2 — Agent Identity FIC exchange** | `client_id=config.agent_id`; T1 as `client_assertion`; `scope=api://AzureADTokenExchange/.default`; `grant_type=client_credentials`; **no `fmi_path`**. | **T2** |
| **3 — Agent User `user_fic` grant** | `client_id=config.agent_id`; T1 as `client_assertion`; T2 as `user_federated_identity_credential`; `user_id=config.agent_user_id`; `requested_token_use=on_behalf_of`; `grant_type=user_fic`; target resource `scope`. | Resource token with `idtyp=user` |

Hop 3's `scope` — and only Hop 3's — selects the target resource; it defaults to `https://graph.microsoft.com/.default`.

The resulting token carries `idtyp=user` and the Agent User's object ID (`oid`). This is the load-bearing difference from human delegated auth: Graph sees a first-class user principal that happens to be the agent, not an app-only service principal and not the human sitting at the keyboard. Every Teams/email/Files call the agent makes in Agent User mode uses this token, so Graph attribution — and the audit trail built on top of it — points at the Agent User, never the human.

### Storage token variant

`acquire_agent_user_storage_token()` (also in `tools/teams.py`) is the same three-hop function with Hop 3's `scope` swapped from Graph to `https://storage.azure.com/.default`. Hops 1 and 2 are unchanged — only the resource requested at the last hop differs. This is what backs the Azure Blob Storage memory backend when cloud memory is enabled.

### Two-hop Agent Identity flow (sponsor reads)

`acquire_agent_identity_token()` stops after Hop 2 — no `user_fic` grant, no Agent User involved. It's an app-only token for the Agent Identity itself, used by `entrabot.identity.sponsors` to read the Agent Identity's `/sponsors` Graph relationship, which requires app-only auth (the Agent User's delegated token cannot read it).

## Certificate handling per OS

`_build_blueprint_assertion()` in `tools/teams.py` decides which signing path to use:

- **macOS and Linux** retrieve the Blueprint's PEM private key from the OS keystore (Keychain / Secret Service) into the process, then sign the JWT in-process via `cryptography`'s `load_pem_private_key` and PyJWT.
- **Windows** has no PEM in the keystore. It locates the certificate by its 40-character SHA-1 thumbprint in `Cert:\CurrentUser\My` and signs through `ncrypt.dll` (CNG) against the non-exportable private key — the key material itself is never exported or read into the process; only the signature comes back.

Both paths produce the same JWT shape: header carries `x5t#S256` (the base64url SHA-256 thumbprint of the DER certificate, RFC 7515 §4.1.8), and the assertion is valid for 10 minutes (`ASSERTION_LIFETIME_SECONDS`). See `src/entrabot/auth/certificate.py` for `build_client_assertion()` and `compute_cert_thumbprint()`.

## Error handling

Every token response is parsed and checked for an `"error"` key **before** `"access_token"` is read (`_check_token_response()` in `teams.py`) — Entra can return an error body on a response that isn't necessarily HTTP 200, so status-code checking alone is not sufficient. A hop-specific `TokenExchangeError` is raised, carrying:

- `hop` — which leg failed (`"hop1:blueprint"`, `"hop2:agent_identity"`, or `"hop3:agent_user"`)
- `error` / `description` — Entra's own error code and description, or `"missing_token"` / `"non_json_response"` for malformed responses

This makes a failure in the chain immediately attributable to a specific hop rather than a generic "auth failed."

## Token lifecycle

Two independent mechanisms in `mcp_server.py` keep the active token usable without a per-call round trip to Entra:

- **Eager refresh** — `_ensure_valid_token()` refreshes whenever the cached token is older than 55 minutes (`TOKEN_REFRESH_THRESHOLD`, a 5-minute buffer on the 60-minute Entra expiry). Dispatches to the three-hop flow in Agent User mode, or an MSAL silent refresh in delegated mode.
- **Lazy retry** — `_with_token_retry()` wraps Graph calls; on `TokenExpiredError` it clears the cached expiry, forces one refresh via `_ensure_valid_token()`, and retries the call exactly once.

## Delegated mode

`ENTRABOT_MODE=delegated` (or `auto` falling back after the three-hop fast path fails) uses `MsalDelegatedAuth` in `src/entrabot/auth/delegated.py`. It tries silent token acquisition from an OS-encrypted MSAL cache first, then interactive sign-in via a localhost redirect (port 8400, 120s timeout), falling back to the device-code flow if the redirect can't complete. The resulting token belongs to the signed-in human — there is no Agent User attribution in this mode. The state machine records `attribution_type = "delegated-human"`, and outbound Teams messages are prefixed `[EntraBot]` so the human can tell which messages the agent sent.

## Identity state machine

`IdentityStateMachine` (`src/entrabot/identity/state_machine.py`) is a non-linear state graph, not a simple ladder — each state has its own set of valid destinations, validated against `VALID_TRANSITIONS` and applied under an `asyncio.Lock`:

| From | Allowed transitions to |
|---|---|
| `UNAUTHENTICATED` | `DELEGATED`, `AGENT_USER` |
| `DELEGATED` | `PROVISIONING`, `UNAUTHENTICATED` |
| `PROVISIONING` | `AGENT_USER`, `ERROR`, `DELEGATED` |
| `AGENT_USER` | `ERROR`, `UNAUTHENTICATED` |
| `ERROR` | `DELEGATED`, `UNAUTHENTICATED` |

At MCP server boot, `_init_auth()` takes the fast path directly from `UNAUTHENTICATED` to `AGENT_USER` when Blueprint credentials are configured, falling back to `DELEGATED` via MSAL if the three-hop flow fails, and remaining `UNAUTHENTICATED` if both fail. `PROVISIONING` is modeled in `VALID_TRANSITIONS` for a delegated-to-Agent-User promotion and rollback, but the current `mcp_server.py` runtime has no call site that enters it; the boot path transitions directly and does not use this state.

Rollback is not a side note — it's built into `transition()`. The state machine snapshots the entire `IdentitySession` (not just the state field) after acquiring the lock. If the caller's transition callback raises, the whole session — including any `update_session()` mutations made before the transition started — is restored to that snapshot, and an `InvalidTransitionError`/`TransitionError` propagates. A transition attempt never leaves the session in a partially-updated state.

`attribution_type` is set as a side effect of certain transitions: `"delegated-human"` on entering `DELEGATED`, `"agent"` on entering `AGENT_USER`, `"none"` on entering `UNAUTHENTICATED`. This is what the audit layer reads to attribute an action.

## See also

- [Auth & Token Flows Layer](layers/auth.md) — the same flow, framed as a component layer.
- [Teams Integration Layer](layers/teams.md) — how the resulting token is used to send and read messages.
- [Identity Lifecycle guide](../guides/identity-lifecycle.md) — provisioning order, certificate rotation, deprovisioning.
- [Identity API reference](../reference/api/identity.md) — the state machine and sponsor-gate API surface.
- [Token Flows reference](../reference/token-flows.md) — wire-level request/response shapes for every hop.
- [MCP Runtime](mcp-runtime.md) — how token refresh fits into the server's initialization lifecycle.
