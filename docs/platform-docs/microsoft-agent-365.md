# Microsoft Agent 365 and Work IQ

Microsoft Agent 365 is the enterprise control plane for agents, built on
Microsoft Entra Agent ID. It provides:

1. **Identity and inventory** through Microsoft Entra Agent ID and the
   Microsoft 365 admin center.
2. **Governance and observability** through Microsoft 365, Purview, Defender,
   and OpenTelemetry-compatible signals.
3. **Work IQ MCP servers** for Microsoft 365 data and actions.
4. **AI teammate experiences** for agents that have their own Microsoft 365
   identity, mailbox, and Teams presence.

Entrabot builds the identity chain directly from Microsoft Graph primitives
and uses Work IQ where it closes a capability gap that direct Graph does not
cover — most notably Word document comments.

```text
Agent Identity Blueprint
  -> BlueprintPrincipal
  -> Agent Identity
  -> Agent User
```

Blueprint, BlueprintPrincipal, and Agent Identity creation use the dedicated
Microsoft Graph v1.0 subtype endpoints; Agent User creation is on Graph beta.
See [Microsoft Entra Agent ID: Blueprints, Identities, and Users](agent-id-blueprints-and-users.md).

## Work IQ versus direct Graph

Entrabot draws a deliberate boundary between the two surfaces:

| Capability | Surface Entrabot uses |
|---|---|
| Teams chat send/read/members | Direct Microsoft Graph |
| Outlook mail | Direct Microsoft Graph |
| Files, OneDrive, SharePoint | Direct Microsoft Graph |
| Excel worksheet and range reads | Direct Microsoft Graph (`list_workbook_worksheets`, `read_workbook_range`) |
| Word document content, create, comment, reply | Work IQ Word adapter |
| OneDrive/SharePoint metadata and small-file reads through Work IQ | Work IQ ODSP adapter |
| Whole-workbook reads, workbook creation from CSV, cell comments and replies | Work IQ Excel adapter (see [Work IQ Excel tools](#work-iq-excel-tools)) |

Teams, mail, and Files stay Graph-native because Entrabot needs explicit chat
IDs, background polling, channel push, cross-tenant membership handling, and
host-gated sponsor replies. Work IQ is used only where direct Graph cannot
provide the capability, such as document-level Word comments — Microsoft
Graph drive-item `/comments` is not a substitute for Word comments on `.docx`
files.

Agent 365 integration (telemetry through OpenTelemetry, and the agent's entry in
the Agent 365 registry) is optional and separate from Work IQ; see
[Telemetry and the Agent 365 registry](#telemetry-and-the-agent-365-registry).
It does not replace Entrabot's own audit log.

## `ToolingManifest.json` is authoritative

The Agent 365 CLI (`a365 develop add-mcp-servers`) generates
`ToolingManifest.json`. Setup, on Windows, macOS and Linux, writes the same three
entries (Word, Excel and OneDrive/SharePoint) itself, without the CLI, from the
package's catalog (`src/entrabot/a365/catalog.py`), and keeps any other entries
in the file. The manifest supplies each server's URL, audience, and scope. Runtime
code prefers that manifest over documented values because catalog metadata
can change; if Microsoft changes a server's URL, audience, or scope, the
package's catalog has to change with it.

Representative manifest shape:

```json
{
  "mcpServers": [
    {
      "mcpServerName": "mcp_WordServer",
      "mcpServerUniqueName": "mcp_WordServer",
      "url": "https://agent365.svc.cloud.microsoft/agents/servers/mcp_WordServer",
      "scope": "Tools.ListInvoke.All",
      "audience": "{manifest-provided-audience}",
      "publisher": "Microsoft"
    }
  ]
}
```

Entrabot loads the manifest through `src/entrabot/a365/manifest.py`. Missing,
malformed, or incomplete manifest entries raise typed errors; the provider
does not silently invent credentials or scopes.

## Runtime architecture

`WorkIqProvider` is the audited boundary for Work IQ calls:

```text
MCP tool
  -> typed adapter (Word or ODSP)
  -> WorkIqProvider
  -> audience-specific Agent User token
  -> Work IQ MCP client
  -> Microsoft-hosted MCP server
```

The provider:

- resolves server metadata from `ToolingManifest.json`;
- acquires a token for the manifest-provided audience through Entrabot's
  three-hop Agent User flow;
- calls the Microsoft-hosted MCP server;
- emits pending/success/failure audit events without logging customer
  arguments or token material.

The implementation lives in:

- `src/entrabot/a365/manifest.py`
- `src/entrabot/a365/tokens.py`
- `src/entrabot/a365/mcp_client.py`
- `src/entrabot/a365/provider.py`
- `src/entrabot/a365/word.py`
- `src/entrabot/a365/excel.py`
- `src/entrabot/a365/odsp.py`

## Work IQ Word tools

Entrabot exposes four Word operations through the typed adapter:

| Entrabot operation | Work IQ tool | Purpose |
|---|---|---|
| Read document | `GetDocumentContent` | Returns document HTML and comments. |
| Create document | `CreateDocument` | Creates a `.docx` in the Agent User's Microsoft 365 storage. |
| Add comment | `AddComment` | Adds a top-level Word comment. |
| Reply to comment | `ReplyToComment` | Replies to an existing Word comment. |

Document content is external input. `get_document_content()` wraps the returned
HTML, comments, and any sensitivity label with the authoritative external-content
boundary before exposing them to the model. `read_word_document` also returns
the `drive_id` and `document_id` the comment operations take. The adapters keep
provider responses for typed parsing only: no MCP tool returns them, and they are
excluded from `repr`.

## Work IQ Excel tools

Entrabot exposes four Excel operations through the typed adapter:

| Entrabot tool | Work IQ tool | Purpose |
|---|---|---|
| `read_excel_workbook` | `GetDocumentContent` | Returns a workbook's cell text and comments from a sharing URL, plus the IDs needed to comment. |
| `create_excel_workbook` | `CreateWorkbook` | Creates a workbook from CSV in the root of the Agent User's OneDrive. |
| `add_excel_comment` | `CreateComment` | Comments on one cell, addressed as `Sheet1!A1`. |
| `reply_to_excel_comment` | `ReplyToComment` | Replies to an existing workbook comment. |

Use `read_excel_workbook` for the whole workbook as text or for its comments.
Use the direct Graph `read_workbook_range` for a bounded range or to tell
formulas from values.

The adapter differs from the Word adapter in three ways:

- **Everything from the workbook stays in the envelope.** The file name,
  comments, and cell text sit inside one external-content envelope. Results
  have no `raw` field, because the `CreateWorkbook` response includes a
  pre-authenticated `@microsoft.graph.downloadUrl`.
- **`shareWith` is intentionally not exposed.** `CreateWorkbook` accepts an
  optional email address to share with, which would skip the sponsor, channel,
  and membership checks in `share_file`. Entrabot never sends it, so new
  workbooks stay unshared until `share_file` shares them.
- **Reads are capped.** Workbook text is truncated to
  `ENTRABOT_FILES_MAX_TEXT_BYTES`, comments first so they survive, and
  `truncated` reports when that happened. The truncated `text` is no longer
  valid JSON.

CSV cells arrive as text, so numbers in a new workbook come back as strings.

`mcp_ExcelServer` appears in the catalog returned by
`a365 develop list-available`, but Microsoft Learn doesn't document it, so its
tool names and response shapes come from the server's `tools/list` and may
change. When adding it, the Agent 365 CLI warned that it "uses a legacy ATG
audience and may not work correctly".

### Excel setup

Setup adds `mcp_ExcelServer` to `ToolingManifest.json`, and
`scripts/ensure_a365_work_iq_permissions.py` adds `McpServers.Excel.All` to the
Blueprint's grant on the Agent 365 Tools resource and to the Blueprint's
inheritable permissions for it. Until that scope is granted, calls to the
server return `403`. Setup does not run the Agent 365 CLI on any system, so it
needs neither PowerShell 7 nor the .NET SDK.

## Authentication

Work IQ servers have audience-specific OAuth resources. Entrabot reads the
audience from the manifest and requests `{audience}/.default` through the
existing Agent User three-hop flow, preserving Agent User attribution rather
than falling back to the human operator.

Entrabot never:

- hard-codes a stale Work IQ audience or legacy `McpServers.*` scope;
- turns the Blueprint into an OAuth public client;
- uses the signed-in human's delegated token in `agent_user` mode;
- logs bearer tokens or complete Work IQ responses.

A delegated MSAL session is human-attributed: Graph attributes calls to the
signed-in human, not to the Agent User, so it does not provide Agent User
attribution. See [Delegated Authentication with MSAL](delegated-auth.md).

### Inheritable permissions

The Blueprint-wide OAuth2 grants are not enough for the Agent User's token.
The agent inherits the scopes only through the Blueprint's inheritable
permissions: one entry per resource app, in
`/v1.0/applications/microsoft.graph.agentIdentityBlueprint/{id}/inheritablePermissions`.
Without them, the token request for a Work IQ audience fails with
`AADSTS65001` even with every grant in place. Inherited scopes appear in the
token's `scp` claim. See Microsoft Learn,
[Configure inheritable permissions for agent identity blueprints](https://learn.microsoft.com/entra/agent-id/configure-inheritable-permissions-blueprints).

`scripts/ensure_a365_work_iq_permissions.py` writes these entries least
privilege: only the scopes the manifest needs (`enumeratedScopes`) and no app
roles (`noRoles`). An entry that already covers them, such as the CLI's
`allAllowed` one, is left alone; one with fewer scopes is widened, never
narrowed. On a real tenant, Microsoft's token service took 20 to 45 seconds to
honour a new entry, so setup checks the agent's tokens for each audience for
about two minutes, and the health check (`scripts\setup-windows.cmd -Diagnose`,
or `./scripts/setup.sh --diagnose` on macOS and Linux) runs the same check.

## Setup and validation

1. Provision Entrabot's Agent Identity and Agent User.
2. Add the Work IQ MCP servers to `ToolingManifest.json` and grant their
   permissions, including the Blueprint's inheritable permissions. Setup does
   this itself, with no Agent 365 CLI, .NET SDK, PowerShell 7 or
   `a365.config.json`: answer yes to the Word, Excel and OneDrive question, or
   pass `-ConfigureA365WorkIq` on Windows and `--configure-a365-work-iq` to
   `./scripts/setup.sh` on macOS and Linux. On Linux this path has been run
   against a real tenant (Ubuntu 24.04); on macOS it has not.
3. Keep the generated `ToolingManifest.json` in a location Entrabot
   discovers, or set the configured manifest path.
4. Run the relevant Word MCP tool and confirm the audit event identifies the
   Agent action.
5. Confirm returned document content carries the authoritative
   external-content envelope.

Use the generated manifest and live tenant consent state as the source of
truth. Server availability, license requirements, and admin-center
presentation vary by tenant.

## Telemetry and the Agent 365 registry

Agent 365 shows an agent's activity in Microsoft Defender, the Microsoft 365
admin center and Microsoft Purview from OpenTelemetry spans the agent sends,
and lists the agent in the Agent 365 registry (Microsoft 365 admin center >
Agents > All agents > Registry) only once it is registered. These are two
separate things in Microsoft's terms. Setup does both with one question,
**Agent 365 integration** (default no): telemetry first, then the
registration. Each half reports its own result, and a failure in either never
stops setup or undoes the other.

**Which SDK.** Entrabot uses Microsoft's OpenTelemetry Distro,
[`microsoft-opentelemetry`](https://learn.microsoft.com/microsoft-agent-365/developer/microsoft-opentelemetry)
(its Agent 365 exporter and scopes), which Microsoft Learn recommends for new
Agent 365 observability integrations; the older Agent 365 Observability SDK is
deprecated. Entrabot does not use the Agent 365 tooling SDK
(`microsoft-agents-a365-tooling`): Work IQ is called by Entrabot's own MCP
client in `src/entrabot/a365/`, so that every call goes through the audit log
and the external-content envelope.

**What is sent.** Content is suppressed: each request and response is
`[content suppressed]`, tool spans carry only the tool's name, and errors only
"failed". The spans carry the agent's IDs, the conversation (chat) ID, the
channel and, for a Teams message, the sender's Entra object ID.

- The harness (`entrabot`, from PR #122) sends one `invoke_agent` span per
  turn.
- The MCP server sends them too, from what it can see, because under Claude
  Code or Copilot CLI the host runs the model loop: an inbound Teams message
  or email pushed to the model starts a run, each tool call is an
  `execute_tool` span in it, and a tool call with no run open starts one in
  the `cli` channel. A run ends at the next inbound message, after five quiet
  minutes, or when the server stops (`src/entrabot/observability/mcp_runs.py`).

It uses the delegated route: the agent's own Agent User token for
`Agent365.Observability.OtelWrite` on the Agent 365 Observability resource
(`9b975845-388f-4429-889e-eab1ef63949c`), kept fresh in the background.

**Switching it on.** Answer yes to the Agent 365 integration question in
setup, or pass `--configure-a365` to `./scripts/setup.sh` (macOS, Linux) or
`-ConfigureA365` to `scripts\setup-windows.cmd`. The 0.2.0 names,
`--configure-a365-observability` and `-ConfigureA365Observability`, still work
and now mean the same: telemetry and the registry. For telemetry, setup runs
`scripts/ensure_a365_observability_permissions.py` with the setup credential
(the Blueprint principal's tenant-wide grant, the Blueprint's inheritable
permission and the Agent Identity's grant for its Agent User), checks as the
agent that its token carries the scope, and only then writes
`ENTRABOT_A365_OBSERVABILITY_ENABLED=true` and
`ENTRABOT_A365_EXPORT_ENABLED=true` to `.env`. A failure never stops setup.
`--diagnose` (`-Diagnose`) reports whether telemetry is on and whether the
agent's token carries the scope ("Agent 365 telemetry"), and on the next line
whether the agent is registered ("Agent 365 registry").

**No Azure Monitor.** Entrabot exports only to Agent 365 and never enables the
distro's Azure Monitor export. The distro still makes two calls of its own:
usage statistics to an Application Insights ingestion host, and a settings
lookup at `settings.sdk.monitor.azure.com` by the Azure Monitor exporter it
loads. Before it starts the distro, Entrabot sets
`MICROSOFT_OTEL_SDKSTATS_DISABLED`, `APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL`
and `APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED` to `true`, which stops both. A
value you have already set is kept (`src/entrabot/observability/runtime.py`).

**Licence.** Microsoft accepts the spans with `200 OK` but drops them
(`tenant_not_licensed`) unless at least one user in the tenant has a
**Microsoft 365 E7 or Microsoft Agent 365 licence assigned**
([drop conditions](https://learn.microsoft.com/microsoft-agent-365/developer/observability-concepts#limits-and-drop-conditions)).
Neither setup nor diagnose can see that; both say so. Because the status code is
`200` either way, look at the response body: in a run against a licensed tenant
it listed each span with status `sent` for each of Agent 365's sinks and
`rejectedSpans: 0` (see the [Verification Record](../project/verification.md)).

**The registry.** Learn: "Creating a Microsoft Entra identity alone doesn't
register the instance." An agent whose Blueprint was made through Microsoft
Graph, as Entrabot's is, registers with the Agent Registry API
(`POST https://graph.microsoft.com/beta/copilot/agentRegistrations`,
`AgentRegistration.ReadWrite.All`). With Agent 365 integration switched on,
setup does it in two steps:

1. **The permission.** While it brings the setup (Provisioner) app's
   permissions up to date, setup adds the Microsoft Graph application
   permission `AgentRegistration.ReadWrite.All` to that app and grants admin
   consent for it. It does this the way it gives the app its other
   permissions: through the signed-in administrator's Azure CLI
   (`az ad app permission add`, then `az ad app permission admin-consent`),
   so the grant needs a Global Administrator or Privileged Role
   Administrator. The setup app never grants it to itself. A setup that
   answers no never adds it. Setup never removes it either: the setup app is
   shared by every Entrabot agent in the tenant, and another agent's
   registration may need it. Consent answering success is not taken as
   proof: right after the permission is added, Microsoft can answer success
   and grant nothing. Setup checks that the grant shows on the setup app and
   consents again until it does, for about a minute and a half at most. If it
   cannot be added or never shows, setup says why and carries on.
2. **The registration**, after telemetry:
   `scripts/register_agent_instance.py --apply`, with the setup app's token.
   The request body is built in one place
   (`entrabot.a365.registry.build_registration`): the Agent Identity's app ID
   as `agentIdentityId` and `sourceAgentId`, the Blueprint's app ID as
   `agentIdentityBlueprintId`, the agent's sponsors as owners, and as display
   name `ENTRABOT_AGENT_DISPLAY_NAME` from `.env` when set, else the part of
   the agent's address before the `@`. The registration's ID is kept in
   `.entrabot-state.json` (`AGENT_REGISTRATION_ID`), so a later run reads and
   updates it instead of creating another; if Graph says it is gone (404), a
   new one is created; any other answer creates nothing.

The API is beta and has no list call: nothing can be looked up by agent, so a
computer knows only what it recorded. A computer that **joined** an agent that
already existed (`--use-blueprint`; setup records `JOINED_EXISTING_AGENT` in
its state file) does not create a registration, because another computer may
have made one: setup says so and shows the two commands to use instead,
`register_agent_instance.py --registration-id <ID> --apply` to record and
update the one another computer made (its `--diagnose` shows the ID), or
`register_agent_instance.py --apply` if none did. A computer joined with an
older setup has no such record and cannot be told apart from the one that
created the agent.

Teardown with `--delete-agent` (`-DeleteAgent`) deletes the registration this
computer recorded (`DELETE .../agentRegistrations/{id}`), after the agent,
best-effort; one it cannot delete is a to-do with where to delete it. A
disconnect alone leaves the registration alone.

Run for real on Linux (see the [Verification Record](../project/verification.md)):
setup registered the agent, Graph accepted the app IDs above, and for an Entra
agent identity the registration ID Graph returned was the agent identity's ID.
About a minute later the admin center listed the agent with platform
"Entrabot", the sponsor as owner and the Entra agent ID. Not yet run on Windows
or macOS, nor teardown's delete.

Microsoft's other route, the Agent 365 CLI's `a365 setup all`, creates its own
Blueprint with a client secret and an Azure App Service. Do not run it against
an Entrabot agent: that is how an earlier attempt ended up with a second
Blueprint holding a secret and broad grants.

## Related

- [Microsoft Entra Agent ID: Blueprints, Identities, and Users](agent-id-blueprints-and-users.md)
- [Agent Users](entra-agent-users.md)
- [Files and Work IQ guide](../guides/files-and-work-iq.md)
- [Files and SharePoint Graph API](files-graph-api.md)
- [Token Flows reference](../reference/token-flows.md)
- [MCP Tool reference](../reference/mcp-tools.md)
