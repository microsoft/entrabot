# `scripts/setup_delegated.sh`

Platforms: macOS, Linux.

## Purpose

`setup_delegated.sh` signs the operator in interactively via MSAL — browser
localhost redirect, falling back to device code — and caches the resulting
token through OS-encrypted persistence, so a later MCP server session can
pick it up silently. It is the setup step for **delegated mode**: the agent
acts with the human's own delegated permissions instead of provisioning a
separate Agent User chain. It does not provision any Entra Agent Identity
resources and does not start the MCP server itself.

## Requirements

- `ENTRABOT_CLIENT_ID` set in `.env` — the Application (client) ID of a
  multi-tenant app registration configured for delegated auth. The script
  exits `1` immediately if this is missing.
- `ENTRABOT_TENANT_ID` is optional; when unset, the sign-in authority
  defaults to `common` (any Entra tenant, work or personal Microsoft
  account, depending on the app registration's supported account types).
- A browser available for the localhost-redirect flow (port 8400), or a
  second device/terminal that can open a URL for the device-code fallback.
- An OS credential store reachable by `msal-extensions`
  (`build_encrypted_persistence` — Keychain on macOS, Secret Service/Keyring
  on Linux) for persisting the token cache. If building the encrypted
  persistence fails for any reason, the underlying `MsalDelegatedAuth`
  client logs a warning and falls back to an **in-memory** cache for that
  process only — the sign-in still completes, but nothing is persisted for
  next time.

## Usage

```bash
./scripts/setup_delegated.sh
```

The script takes no arguments and no flags.

## Effects

1. Loads `.env` (if present) into the environment and validates
   `ENTRABOT_CLIENT_ID` is set.
2. Activates `.venv` if `.venv/bin/activate` exists.
3. Constructs an `entrabot.auth.delegated.MsalDelegatedAuth` client for the
   configured client ID and tenant (`Chat.ReadWrite` and `User.Read` scopes).
4. **Tries a silent token acquisition first** (`try_silent()`), which checks
   for a cached MSAL account and requests a token from the cache without any
   user interaction. If that succeeds, it prints the signed-in identity and
   exits — no browser is opened.
5. If no cached token is available, calls `authenticate()`, which:
   - Attempts the localhost-redirect interactive flow (opens a browser on
     port 8400, 120-second timeout).
   - Falls back to the device-code flow if the localhost flow fails with a
     timeout or an OS-level error (for example, the port is already in use
     or no browser can be launched).
6. On success, prints the signed-in identity (`preferred_username`, `name`,
   tenant ID) and the **byte length** of the cached access token — never the
   token value itself.
7. Persists the MSAL token cache to the OS-encrypted store so a later
   `try_silent()` call (including from the MCP server) can reuse it without
   prompting again.
8. Does **not** register the `entrabot` MCP server, write `.env`, or
   provision any Blueprint / Agent Identity / Agent User resources — those
   belong to [`setup.sh`](setup-sh.md)'s Agent User path, which this script
   is an alternative to, not a complement of.

## Exit behavior

- `0` — a token was already cached and validated silently, or interactive
  sign-in completed successfully.
- `1` — `ENTRABOT_CLIENT_ID` is not set in `.env`.
- `1` — `authenticate()` raised (device-code initiation failed, MSAL
  returned an `"error"` field on either flow, the user cancelled consent, or
  both the localhost and device-code flows timed out); the script prints the
  exception text to stderr before exiting.

## Common failures

- **`ERROR: ENTRABOT_CLIENT_ID not set in .env`** — set it to your
  multi-tenant app registration's Application (client) ID and re-run.
- **Browser doesn't open / localhost port 8400 in use** — the script
  automatically falls back to the device-code flow; follow the printed code
  and URL in a browser on any device.
- **Sign-in appears to "hang"** — the localhost flow has a 120-second
  window; if the browser sign-in isn't completed in time, it times out and
  falls back to device code automatically.
- **Token not remembered next run** — if the OS credential store could not
  be opened (no Keychain/Secret Service available), the cache silently falls
  back to in-memory only; sign-in will be required again next run.

## Related commands

- [Script reference — Setup](../index.md#setup)
- [`setup.sh`](setup-sh.md) — the full Agent User provisioning path this
  script is an alternative to for delegated-mode operation.
- [Identity and token flow](../../../architecture/identity-and-token-flow.md).
