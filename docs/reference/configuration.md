# Configuration

Compact lookup of every `ENTRABOT_*` environment variable: those read by `EntraBotConfig.from_env()` (`src/entrabot/config.py`), and the containment, outbound and token settings read where they are used (`src/entrabot/sandbox/`, `src/entrabot/security/recipients.py`, `src/entrabot/mcp_server.py`). For descriptions, parsing rules, and platform-path defaults, follow each group's link to the matching section of the [Configuration guide](../guides/configuration.md).

`.env` precedence: `src/entrabot/config.py` loads configuration once, on first import, and never overwrites a variable that is already set in the process environment. It reads, in this order: `~/.entrabot/global.env` (`$ENTRABOT_HOME/global.env`; shared tenant and Blueprint keys, when present), then the first per-agent `.env` it finds among `$ENTRABOT_ENV_FILE`, the clone's own `.env`, `$ENTRABOT_HOME/.env` (default `~/.entrabot/.env`) and `./.entrabot/.env`. Because earlier values are never overwritten, a shell-exported value wins over both files, and `global.env` wins over the per-agent file for a key in both. Files are read as data (`KEY=value` lines), never executed. `ENTRABOT_ENV_FILE` names a per-agent `.env` explicitly; `ENTRABOT_HOME` moves `~/.entrabot`. `get_config()` re-reads `os.environ` on every call — there is no cached singleton.

## Identity and tenant

| Variable | Default / required |
|---|---|
| `ENTRABOT_TENANT_ID` | Unset by default; required for the Agent User three-hop flow. |
| `ENTRABOT_BLUEPRINT_APP_ID` | Unset by default; required for the Agent User three-hop flow. |
| `ENTRABOT_BLUEPRINT_OBJECT_ID` | Unset by default; used by provisioning/diagnostics. |
| `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT` | Unset by default; required (SHA-256, base64url) for the certificate JWT assertion. |
| `ENTRABOT_BLUEPRINT_CERT_SHA1` | Unset by default; setup writes it on every platform. Locates the certificate and its private key in the Windows Certificate Store, or in the macOS Keychain or Linux keyring. |
| `ENTRABOT_BLUEPRINT_KSP` | Unset by default; diagnostics label for where the key is (`tpm`, `software`, `keychain` or `keyring`). |
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
| `ENTRABOT_HUMAN_USER_MAILS` | Unset by default; optional. The first address gets the daily summary only when the directory gives no sponsor address. |
| `ENTRABOT_HUMAN_USER_TYPES` | Unset by default; optional, index-aligned with the sponsor ID/UPN lists (e.g. member/guest). |

See [Configuration guide § Sponsor / human owner](../guides/configuration.md#sponsor-human-owner).

## Authentication and runtime

| Variable | Default / required |
|---|---|
| `ENTRABOT_MODE` | Defaults to `auto`. Validated (`auto`, `delegated`, `agent_user`) but **not currently consumed** by `_init_auth` as an auth selector — see the guide for what actually decides the session type. |
| `ENTRABOT_SKIP_PROVISIONING` | Defaults to falsy. Truthy (`true`/`1`/`yes`) skips the three-hop fast path and goes straight to delegated auth. |
| `ENTRABOT_LOG_LEVEL` | Defaults to `INFO`. |
| `ENTRABOT_XPIA_WRAP_ENABLE` | Defaults to enabled (`true`). Set to `false`/`0`/`no`/`off` to disable. See the [Security (XPIA) API Reference](api/security.md). |
| `ENTRABOT_REACTION_NOTIFICATIONS` | Defaults to enabled (`true`). Set to `false`/`0`/`no`/`off` to stop the Teams poll pushing a notification when someone reacts to one of the agent's own messages. |
| `ENTRABOT_FILES_MAX_TEXT_BYTES` | Caps the text a Files read returns, in bytes. Defaults to `200000`, which is also used for a zero, negative, or unparseable value, with a logged warning. |
| `ENTRABOT_FILES_MAX_WORKBOOK_CELLS` | Most cells one `read_workbook_range` call can read. Defaults to `5000`, which is also used for a zero, negative, or unparseable value, with a logged warning. |

See [Configuration guide § Runtime mode and behavior](../guides/configuration.md#runtime-mode-and-behavior).

## Outbound policy, containment and telemetry

| Variable | Default / required |
|---|---|
| `ENTRABOT_ENV_FILE` | Unset. A per-agent `.env` to load before the usual locations (see the precedence note above). |
| `ENTRABOT_HOME` | `~/.entrabot`. Where `global.env`, the home-directory `.env` and (by default) state live. |
| `ENTRABOT_OUTBOUND_ALLOW` | Unset. Comma-separated addresses, or domains (`fabrikam.com` or `@fabrikam.com`), the agent may reach besides its sponsors, the requesting chat's members and its own tenant. See [Security Boundaries § Outbound recipient policy](../architecture/security-boundaries.md#outbound-recipient-policy). |
| `ENTRABOT_HOME_DOMAINS` | Unset. More domains counted as the agent's own tenant, besides the Agent User's domain and domains that resolve to its tenant ID. |
| `ENTRABOT_TOKEN_MAX_AGE_SECONDS` | `600`. How long the server reuses an access token before asking again (60 to 3300). Bounds how long a disabled agent keeps acting. |
| `ENTRABOT_CONTAINMENT` | `auto` (contain on macOS and Linux when a sandbox is available). `required`, `off`, `seatbelt`, `bwrap`, `mxc`. See [Security Boundaries § Process containment](../architecture/security-boundaries.md#process-containment). |
| `ENTRABOT_SANDBOX_EXTRA_HOSTS` | Unset. More hosts the contained server may reach on 443 (`*.example.com` covers subdomains). |
| `ENTRABOT_SANDBOX_EXTRA_READ` | Unset. More read-only paths for the contained server, `:`-separated. |
| `ENTRABOT_MXC_EXECUTOR` / `ENTRABOT_MXC_SHA256` | Unset. The MXC executor to use instead of the one on `PATH`, and the SHA-256 it must have. |
| `ENTRABOT_A365_OBSERVABILITY_ENABLED` / `ENTRABOT_A365_EXPORT_ENABLED` | `false`. Agent 365 telemetry and its export; setup's Agent 365 integration step writes both once the agent's token carries `Agent365.Observability.OtelWrite`. See [Telemetry and the Agent 365 registry](../platform-docs/microsoft-agent-365.md#telemetry-and-the-agent-365-registry). |
| `ENTRABOT_A365_CONSOLE_ENABLED` / `ENTRABOT_A365_SERVICE_NAMESPACE` | `false` / unset. Print spans to the server's standard error (never standard output, the MCP channel); the `service.namespace` resource attribute. |
| `MICROSOFT_OTEL_SDKSTATS_DISABLED` / `APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL` / `APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED` | Set to `true` by Entrabot when telemetry starts, unless already set. Read by the Microsoft OpenTelemetry Distro and its Azure Monitor exporter: the first two stop the distro's usage statistics to Application Insights, the third its settings lookup at `settings.sdk.monitor.azure.com`. Entrabot never enables the distro's Azure Monitor export. |

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
