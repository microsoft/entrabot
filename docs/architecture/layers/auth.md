# Auth & Token Flows Layer

## Purpose

Everything that turns a Blueprint certificate into a usable Graph (or Storage) token lives here: the certificate-based JWT assertion, the three-hop Agent User exchange, the two-hop Agent Identity app-only exchange, and — for the delegated auth mode — MSAL interactive sign-in.

## Three-hop Agent User flow

Implemented as `acquire_agent_user_token()` in `src/entrabot/tools/teams.py`, using raw `httpx.Client` calls (no MSAL) with a 15-second timeout on every hop:

1. **Hop 1 — Blueprint `client_credentials`.** The Blueprint authenticates with a certificate-signed JWT assertion (built by `build_client_assertion()` in `src/entrabot/auth/certificate.py`) and requests a token scoped for Agent Identity impersonation via `fmi_path`.
2. **Hop 2 — Agent Identity FIC exchange.** The Agent Identity presents the Hop 1 token as its own client assertion and exchanges it for an Agent Identity token.
3. **Hop 3 — Agent User `user_fic` grant.** Using the Hop 1 and Hop 2 tokens together, Entrabot requests a delegated token for the Agent User, scoped to the target resource. The result carries `idtyp=user` and the Agent User's object ID.

`resource_scope` only changes what Hop 3 asks for — Hops 1 and 2 always exchange against `api://AzureADTokenExchange/.default`. `acquire_agent_user_storage_token()` is the same function called with `resource_scope="https://storage.azure.com/.default"` instead of the Graph default, for the Azure Blob Storage backend.

## Two-hop Agent Identity app-only flow

`acquire_agent_identity_token()` stops after Hop 2 — no `user_fic` grant, no Agent User. It's used by `entrabot.identity.sponsors` to read the Agent Identity's own `/sponsors` Graph relationship, which is an app-only read and doesn't need (or want) delegated user context.

## Certificate assertion building

`_build_blueprint_assertion()` in `teams.py` tries the keystore PEM first (macOS/Linux, and any Windows box a test fixture wrote a PEM into); if none is found and the process is on Windows, it falls back to the CNG path keyed by the Blueprint certificate's SHA-1 thumbprint. See [Platform](platform.md) for how the private key is actually stored per OS.

## Error handling

Every token response is checked for an `"error"` key before `"access_token"` is read — Entra returns error bodies with HTTP 200, not exceptions. `TokenExchangeError` carries which hop failed (`hop1:blueprint`, `hop2:agent_identity`, `hop3:agent_user`) plus Entra's `error` and `error_description`, so a failure in the chain is immediately attributable to a specific hop.

## Token lifecycle

Two independent mechanisms keep the active token usable:

- **Eager refresh** — `_ensure_valid_token()` in `mcp_server.py` refreshes whenever the cached token is older than 55 minutes (a 5-minute buffer on the 60-minute Entra expiry), dispatching to the three-hop flow in Agent User mode or an MSAL silent refresh in delegated mode.
- **Lazy retry** — `_with_token_retry()` wraps Graph calls and, on `TokenExpiredError`, clears the cached expiry, forces a refresh, and retries the call exactly once.

## Delegated mode

`src/entrabot/auth/delegated.py` implements `MsalDelegatedAuth`, used only when `ENTRABOT_MODE=delegated`. It tries silent token acquisition from the OS-encrypted MSAL cache first, then interactive sign-in via a localhost redirect, falling back to the device-code flow if the redirect can't complete (port in use, no browser, timeout). MSAL is not removed from the runtime — it's the entire auth path for delegated mode, just not used by the Agent User three-hop flow, which never depends on it.

See [Identity and Token Flow](../identity-and-token-flow.md) for the wire-level request/response shapes, and [Delegated Auth](../../platform-docs/delegated-auth.md) (forthcoming) for setup.
