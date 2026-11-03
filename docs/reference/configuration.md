# Configuration

Compact lookup of every `ENTRABOT_*` environment variable read by `EntraBotConfig.from_env()` (`src/entrabot/config.py`). For descriptions, parsing rules, and platform-path defaults, follow each group's link to the matching section of the [Configuration guide](../guides/configuration.md).

`.env` precedence: `src/entrabot/config.py` loads a `.env` file from the project root once, on first import, and never overwrites a variable that is already set in the process environment. A shell-exported value always wins over `.env`. `get_config()` re-reads `os.environ` on every call — there is no cached singleton.

## Identity and tenant

| Variable | Default / required |
|---|---|
| `ENTRABOT_TENANT_ID` | Unset by default; required for the Agent User three-hop flow. |
| `ENTRABOT_BLUEPRINT_APP_ID` | Unset by default; required for the Agent User three-hop flow. |
| `ENTRABOT_BLUEPRINT_OBJECT_ID` | Unset by default; used by provisioning/diagnostics. |
| `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` | Unset by default; required (SHA-256, base64url) for the certificate JWT assertion. |
| `ENTRABOT_BLUEPRINT_CERT_SHA1` | Unset by default; Windows-only, used to locate the certificate in the Windows Certificate Store. |
| `ENTRABOT_BLUEPRINT_KSP` | Unset by default; Windows-only diagnostics label. |
| `ENTRABOT_AGENT_ID` | Unset by default; required for the Agent User three-hop flow. |
| `ENTRABOT_AGENT_OBJECT_ID` | Unset by default; used by provisioning/diagnostics. |
| `ENTRABOT_AGENT_USER_ID` | Unset by default; required for the Agent User three-hop flow. |
| `ENTRABOT_AGENT_UPN` / `ENTRABOT_AGENT_USER_UPN` | Unset by default. Aliases for the same value; `ENTRABOT_AGENT_UPN` takes precedence when both are set. |
| `ENTRABOT_CLIENT_ID` | Unset by default; required for delegated (MSAL) auth. |
| `ENTRABOT_AUTHORITY` | Defaults to `https://login.microsoftonline.com/common`. |

See [Configuration guide § Identity and tenant](../guides/configuration.md#identity-and-tenant).

## Sponsors

| Variable | Default / required |
|---|---|
| `ENTRABOT_HUMAN_USER_ID` / `ENTRABOT_HUMAN_USER_IDS` | Unset by default; at least one sponsor identifier is expected in practice. Plural CSV takes precedence over the singular. |
| `ENTRABOT_HUMAN_UPN` / `ENTRABOT_HUMAN_UPNS` | Unset by default. Plural CSV takes precedence over the singular. |
| `ENTRABOT_HUMAN_USER_TENANT_IDS` | Unset by default; optional, index-aligned with the sponsor ID/UPN lists (cross-tenant sponsors). |
| `ENTRABOT_HUMAN_USER_MAILS` | Unset by default; optional. |
| `ENTRABOT_HUMAN_USER_TYPES` | Unset by default; optional, index-aligned with the sponsor ID/UPN lists (e.g. member/guest). |

See [Configuration guide § Sponsor / human owner](../guides/configuration.md#sponsor-human-owner).

## Authentication and runtime

| Variable | Default / required |
|---|---|
| `ENTRABOT_MODE` | Defaults to `auto`. Validated (`auto`, `delegated`, `agent_user`) but **not currently consumed** by `_init_auth` as an auth selector — see the guide for what actually decides the session type. |
| `ENTRABOT_SKIP_PROVISIONING` | Defaults to falsy. Truthy (`true`/`1`/`yes`) skips the three-hop fast path and goes straight to delegated auth. |
| `ENTRABOT_LOG_LEVEL` | Defaults to `INFO`. |
| `ENTRABOT_XPIA_WRAP_ENABLE` | Defaults to enabled (`true`). Set to `false`/`0`/`no`/`off` to disable. See the [Security (XPIA) API Reference](api/security.md). |
| `ENTRABOT_FILES_MAX_WORKBOOK_CELLS` | Most cells one `read_workbook_range` call can read. Defaults to `5000`, which is also used for a zero, negative, or unparseable value, with a logged warning. |

See [Configuration guide § Runtime mode and behavior](../guides/configuration.md#runtime-mode-and-behavior).

## Local paths

| Variable | Default / required |
|---|---|
| `ENTRABOT_LOG_DIR` | Defaults to a platform-specific `logs` subdirectory. |
| `ENTRABOT_AUDIT_DIR` | Defaults to a platform-specific `audit` subdirectory. |
| `ENTRABOT_DATA_DIR` | Defaults to a platform-specific `data` subdirectory. Root for `LocalBackend` state, including the `watched_chats` list and the email cursor. With `BlobBackend` active, those two live in Blob and any local copies are ignored. |

Platform defaults: `~/.entrabot/{logs,audit,data}` on macOS/Linux; `%LOCALAPPDATA%\entrabot\{logs,audit,data}` on Windows (falling back to `<home>\AppData\Local\entrabot\...`).

See [Configuration guide § Local paths](../guides/configuration.md#local-paths).

## Storage backend

| Variable | Default / required |
|---|---|
| `ENTRABOT_KEEP_MEMORY_LOCAL` | Defaults to falsy. Truthy (`true`/`1`/`yes`) forces `LocalBackend` even when blob variables are set. |
| `ENTRABOT_BLOB_ENDPOINT` | Unset by default. Must be set together with `ENTRABOT_BLOB_CONTAINER` to select `BlobBackend`. |
| `ENTRABOT_BLOB_CONTAINER` | Unset by default. Must be set together with `ENTRABOT_BLOB_ENDPOINT` to select `BlobBackend`. |

Setting exactly one of `ENTRABOT_BLOB_ENDPOINT` / `ENTRABOT_BLOB_CONTAINER` fails closed (`BackendMisconfiguredError`) instead of silently falling back to local. `MemoryBackend` (local or blob) is where interaction logs, daily summaries, promises, per-chat delivery cursors, the `watched_chats` list, and the email cursor live. Only the per-machine `.singleton.lock` always stays on local disk under `ENTRABOT_DATA_DIR`.

See [Configuration guide § Storage backend](../guides/configuration.md#storage-backend).
