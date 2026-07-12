# Configuration Reference

EntraBot is configured entirely through environment variables prefixed `ENTRABOT_`. There is no separate configuration-file schema — `.env` is just a plain `KEY=value` file that setup writes into the project root, and every value in it becomes a process environment variable.

`src/entrabot/config.py` loads that `.env` on first import (`_load_dotenv()`), one `KEY=value` line at a time, skipping blanks and `#` comments. Critically, it **never overwrites a variable that is already set in the process environment** — so an operator can override any `.env` value for a single run with `ENTRABOT_MODE=delegated python -m entrabot` (or similar) without editing the file.

Configuration is not a value frozen once at boot. `get_config()` calls `EntraBotConfig.from_env()` fresh every time it's invoked, building a new immutable `EntraBotConfig` dataclass instance from whatever is in `os.environ` **at that moment**. In practice this means:

- Values set before the process starts (shell export, `.env`, systemd `Environment=`, etc.) are what most code sees, because nothing in this codebase mutates `os.environ` after boot other than `_load_dotenv()` itself.
- Tests and scripts that call `os.environ[...] = ...` and then call `get_config()` again will see the change immediately — there is no cached singleton to invalidate.
- The returned `EntraBotConfig` instance itself is `frozen=True` (immutable) — once you have one, its fields cannot change. Call `get_config()` again to see a new read of the environment.

## Identity and tenant

| Variable | Description |
|---|---|
| `ENTRABOT_TENANT_ID` | Entra tenant GUID the Blueprint, Agent Identity, and Agent User all live in. |
| `ENTRABOT_BLUEPRINT_APP_ID` | Application (client) ID of the Agent Identity Blueprint app registration. |
| `ENTRABOT_BLUEPRINT_OBJECT_ID` | Directory object ID of the Blueprint **application object** (not the BlueprintPrincipal/service-principal object). This is the object ID you get back when creating the application registration itself. |
| `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` | Base64url-encoded SHA-256 DER thumbprint of the Blueprint's signing certificate. Used as the `x5t#S256` header on every JWT client-assertion, on every platform (macOS, Linux, Windows). |
| `ENTRABOT_BLUEPRINT_CERT_SHA1` | 40-character hex SHA-1 thumbprint of the same certificate. Used only on Windows to locate the certificate (and its non-exportable CNG private key) in the Windows Certificate Store — the store indexes by SHA-1, not SHA-256. |
| `ENTRABOT_BLUEPRINT_KSP` | Windows key-storage provider name/label recorded by setup, kept for certificate diagnostics on that platform. |
| `ENTRABOT_AGENT_ID` | Agent Identity's application (client) ID — the `client_id` used in the Hop 1 → Hop 2 token exchange. |
| `ENTRABOT_AGENT_OBJECT_ID` | Agent Identity's service-principal object ID. |
| `ENTRABOT_AGENT_USER_ID` | Agent User's directory object ID (the real Entra user account the agent authenticates as). |
| `ENTRABOT_AGENT_UPN` / `ENTRABOT_AGENT_USER_UPN` | The Agent User's UPN, used for stable self/peer identification in Teams (display names can change; UPN doesn't). `ENTRABOT_AGENT_UPN` is the canonical, rename-safe name; `ENTRABOT_AGENT_USER_UPN` is the historical alias kept for existing `.env` files. If both are set, `ENTRABOT_AGENT_UPN` wins. |
| `ENTRABOT_CLIENT_ID` | MSAL client ID used only in `delegated` mode (interactive/device-code auth as the human). |
| `ENTRABOT_AUTHORITY` | MSAL authority URL for delegated-mode auth. Defaults to `https://login.microsoftonline.com/common`. |

## Sponsor / human owner

The human "sponsor" behind an Agent User can be one person or several (e.g. a shared Agent User used by a small team). Each attribute has a singular and a plural form:

| Variable | Description |
|---|---|
| `ENTRABOT_HUMAN_USER_ID` | Single sponsor's Entra object ID. |
| `ENTRABOT_HUMAN_USER_IDS` | Comma-separated list of sponsor object IDs, for multi-sponsor setups. |
| `ENTRABOT_HUMAN_UPN` | Single sponsor's UPN. |
| `ENTRABOT_HUMAN_UPNS` | Comma-separated list of sponsor UPNs. |
| `ENTRABOT_HUMAN_USER_TENANT_IDS` | Comma-separated list of tenant IDs, index-aligned with the sponsor ID/UPN lists (for cross-tenant sponsors). |
| `ENTRABOT_HUMAN_USER_MAILS` | Comma-separated list of sponsor mail addresses. |
| `ENTRABOT_HUMAN_USER_TYPES` | Comma-separated list of sponsor account types (e.g. member/guest), index-aligned with the ID/UPN lists. |

Precedence and list rules:

- The plural CSV variables (`_IDS`, `_UPNS`) take precedence when set. If a plural variable is empty or unset, it falls back to parsing the corresponding singular variable (`ENTRABOT_HUMAN_USER_ID` / `ENTRABOT_HUMAN_UPN`) as a one-item (or comma-separated) list.
- `ENTRABOT_HUMAN_USER_TENANT_IDS` and `ENTRABOT_HUMAN_USER_TYPES` are parsed preserving empty entries between commas (rather than dropping them), because they are **parallel lists**: position *N* in the tenant-IDs list corresponds to position *N* in the sponsor-IDs/UPNs list. Dropping an empty entry would silently misalign every sponsor after it.

## Runtime mode and behavior

| Variable | Description |
|---|---|
| `ENTRABOT_MODE` | Selects the auth mode. Valid values: `auto` (default), `delegated`, `agent_user`. The historical `bot` mode was removed (Bot Framework gateway bypassed the Agent Identity model) and setting `ENTRABOT_MODE=bot` now fails loudly with `RemovedModeError` rather than silently falling back. Any other unrecognized value falls back to `auto`. |
| `ENTRABOT_SKIP_PROVISIONING` | Truthy values `true`, `1`, or `yes` (case-insensitive) skip the provisioning check at boot. |
| `ENTRABOT_LOG_LEVEL` | Python logging level name. Defaults to `INFO`. |
| `ENTRABOT_XPIA_WRAP_ENABLE` | Enabled by default — external content (Teams, email, Files, Work IQ) is wrapped through the XPIA boundary. Set to `false`, `0`, `no`, or `off` (case-insensitive) to disable the wrap as a rollback path. See [Security Boundaries](../architecture/security-boundaries.md). |

## Local paths

| Variable | Description |
|---|---|
| `ENTRABOT_LOG_DIR` | Directory for log files. |
| `ENTRABOT_AUDIT_DIR` | Directory for audit-log output. |
| `ENTRABOT_DATA_DIR` | Root directory for `LocalBackend` operational data (interactions, watched chats, email cursor). |

If any of these is unset, it defaults to a platform-specific subdirectory:

- **macOS/Linux:** `~/.entrabot/logs`, `~/.entrabot/audit`, `~/.entrabot/data`.
- **Windows:** `%LOCALAPPDATA%\entrabot\logs`, `%LOCALAPPDATA%\entrabot\audit`, `%LOCALAPPDATA%\entrabot\data` (falling back to `<home>\AppData\Local\entrabot\...` if `%LOCALAPPDATA%` isn't set).

## Storage backend

| Variable | Description |
|---|---|
| `ENTRABOT_KEEP_MEMORY_LOCAL` | Truthy values `true`, `1`, or `yes` force the local filesystem backend even when blob settings are present. |
| `ENTRABOT_BLOB_ENDPOINT` | Azure Blob Storage account endpoint URL. |
| `ENTRABOT_BLOB_CONTAINER` | Azure Blob Storage container name. |

`get_backend()` in `src/entrabot/storage/backend.py` resolves the backend on every call using this exact order:

1. `ENTRABOT_KEEP_MEMORY_LOCAL` is truthy → `LocalBackend` (explicit escape hatch, checked first).
2. Both `ENTRABOT_BLOB_ENDPOINT` and `ENTRABOT_BLOB_CONTAINER` are set → `BlobBackend`, using the Agent User's storage-scope three-hop token.
3. Neither is set → `LocalBackend` rooted at `ENTRABOT_DATA_DIR` (or its platform default).
4. Exactly one of `ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER` is set → **fails closed** with `BackendMisconfiguredError` rather than silently falling back to local. A half-configured cloud setup is treated as a misconfiguration that must be fixed, not tolerated.

See [Storage Configuration and Migration](storage-configuration.md) for backend selection and migration guidance, and [Reference: Configuration](../reference/configuration.md) for the compact lookup in the Reference section.
