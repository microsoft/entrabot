# Delegated Authentication with MSAL

Entrabot supports two kinds of authenticated session. The **Agent User**
session uses the autonomous three-hop flow, where every action is attributed
to the Agent User. **Delegated mode** is the alternative: it authenticates the
signed-in human with MSAL and acts with the human's token. Use it for demos and
for environments without a provisioned Agent User.

In delegated mode there is no Agent User attribution. Graph sees the human, so
outbound Teams messages are prefixed `[EntraBot]` to make clear which messages
the agent sent on the human's behalf.

Delegated auth is implemented by `MsalDelegatedAuth` in
`src/entrabot/auth/delegated.py`.

## When delegated mode runs

Authentication is decided by `_init_auth`, not by branching on `ENTRABOT_MODE`.
`ENTRABOT_MODE` validates to `agent_user`, `delegated`, or `auto`, but that
value is not consulted as a gate at startup. The actual sequence is:

1. If `ENTRABOT_SKIP_PROVISIONING` is not set, and Blueprint credentials
   (`ENTRABOT_BLUEPRINT_APP_ID`) and a tenant ID are configured, Entrabot first
   tries the three-hop Agent User flow.
2. If that fast path is skipped (via `ENTRABOT_SKIP_PROVISIONING`, or missing
   Blueprint credentials/tenant) or it fails, Entrabot falls back to MSAL
   delegated auth whenever `ENTRABOT_CLIENT_ID` is configured.
3. If neither path succeeds, the identity state machine stays
   `UNAUTHENTICATED`.

To force delegated mode, set `ENTRABOT_SKIP_PROVISIONING` or simply omit the
Blueprint credentials — the delegated client remains available as the fallback
in either case.

On successful delegated auth the identity state machine transitions to
`DELEGATED` and records `attribution_type = "delegated-human"`.

## App registration

Delegated mode requires a **public-client** application registration — an
ordinary Entra `application` object, not an Agent Identity Blueprint. A Blueprint
cannot serve this role because Agent Blueprints cannot be OAuth public clients
(see [Microsoft Entra Agent ID: Blueprints, Identities, and Users](agent-id-blueprints-and-users.md)).

The registration needs:

- Public-client (native) platform with loopback redirect URIs
  (`http://localhost`, `http://127.0.0.1`), or `isFallbackPublicClient: true`.
- Delegated Microsoft Graph permissions matching the requested scopes.

Its application (client) ID is supplied to Entrabot as `ENTRABOT_CLIENT_ID`. The
authority is `https://login.microsoftonline.com/{tenant}`, where the tenant
defaults to `common` for multi-tenant sign-in.

## Authentication flow

`MsalDelegatedAuth` is built on `msal.PublicClientApplication` and acquires a
token in three stages, stopping at the first that succeeds:

1. **Silent cache acquisition** — `try_silent()` calls `acquire_token_silent`
   against the persisted cache for the first known account. If a valid or
   refreshable token exists, it is returned without any user interaction.
2. **Localhost redirect** — `acquire_token_interactive` opens the system browser
   and listens on **port 8400** (`LOCALHOST_PORT`) for the redirect, with a
   120-second timeout (`LOCALHOST_TIMEOUT`) and `prompt="select_account"`.
3. **Device-code fallback** — if the localhost redirect cannot complete (port in
   use, no browser available, or the user does not finish in time), the flow
   falls back to the device-code flow: MSAL prints a verification URL and code to
   stderr, and the user completes sign-in on another device.

The default scopes are `Chat.ReadWrite` and `User.Read` (`DEFAULT_SCOPES`).

Every result is validated before use: the code checks for an `error` key and a
missing `access_token`, raising `AuthCancelledError`, `AuthTimeoutError`, or
`MsalAuthError` as appropriate rather than returning a partial result.

## Token cache persistence

The MSAL token cache is backed by OS-encrypted persistence via `msal-extensions`:

- `build_encrypted_persistence()` wraps the OS keystore (Keychain on macOS,
  DPAPI on Windows, Secret Service on Linux) around a cache file named
  `entrabot_msal_cache`, stored under a stable per-user cache directory
  resolved via `platformdirs.user_cache_dir("entrabot")`. On Unix the parent
  directory is created with `0o700` permissions.
- The cache is exposed to MSAL through `PersistedTokenCache`, so tokens survive
  across restarts and silent acquisition can succeed without re-prompting.
- **In-memory fallback.** If persistent cache creation fails for any reason, the
  failure is logged and an in-memory `msal.SerializableTokenCache` is used
  instead. Authentication still works for the current process; only cross-restart
  persistence is lost.

## Attribution and the `[EntraBot]` prefix

Because delegated tokens belong to the human, delegated mode cannot distinguish
agent actions from human actions in Graph attribution. Entrabot compensates at
the application layer: outbound Teams messages sent in delegated mode are
prefixed with the literal `[EntraBot]`. This prefix is not a restart-safe
dedup mechanism on its own — inbound message content is XPIA-wrapped before the
literal-prefix check runs, so a wrapped agent message no longer starts with
`[EntraBot]` by the time the filter sees it. Effective protection against
reprocessing the agent's own messages instead comes from canonical sender
identity (`sender_upn`/`sender_id` matched against the agent's own identity),
the in-process set of sent-message IDs, and the persisted per-chat cursor that
tracks the last-seen message.

Adaptive Cards are an exception — they do not carry the `[EntraBot]` prefix
because the card itself identifies the sender.

## Configuration

| Variable | Purpose |
|---|---|
| `ENTRABOT_MODE` | Validated to `agent_user`, `delegated`, or `auto` (default); informational only — `_init_auth` selects the auth path from Blueprint credentials, `ENTRABOT_SKIP_PROVISIONING`, and `ENTRABOT_CLIENT_ID`, not from this value. |
| `ENTRABOT_CLIENT_ID` | Public-client application (client) ID for delegated auth. |
| `ENTRABOT_TENANT_ID` | Tenant for the authority; defaults to `common` when unset. |
| `ENTRABOT_SKIP_PROVISIONING` | When set, forces delegated-only auth. |

See the [Configuration guide](../guides/configuration.md) for the full
environment reference.

## Common failures and limitations

- **No Agent User attribution.** Every Graph call is the human. This is the
  fundamental limitation of delegated mode; use `agent_user` mode when actions
  must be attributed to the agent.
- **Interactive first sign-in.** The first run (or any run after the cache is
  cleared or the refresh token expires) requires a browser or device-code
  sign-in. Fully headless environments should rely on the device-code fallback.
- **Port 8400 in use.** If another process holds port 8400, the localhost
  redirect fails and the flow falls back to device code.
- **Cancelled or timed-out sign-in.** User cancellation raises
  `AuthCancelledError`; exceeding the 120-second window raises
  `AuthTimeoutError`.
- **Conditional Access.** Tenant Conditional Access policies (MFA, device
  compliance, IP restrictions) apply to the human account and can block or
  challenge the interactive sign-in.

## Related

- [Microsoft Entra Agent ID: Blueprints, Identities, and Users](agent-id-blueprints-and-users.md)
- [Agent Users](entra-agent-users.md)
- [Identity and Token Flow](../architecture/identity-and-token-flow.md)
- [Token Flows reference](../reference/token-flows.md)
- [Configuration guide](../guides/configuration.md)
