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

Agent 365 tenant-wide OpenTelemetry observability is a separate integration
surface; Entrabot maintains its own fail-closed audit log.

## `ToolingManifest.json` is authoritative

The Agent 365 CLI (`a365 develop add-mcp-servers`) generates
`ToolingManifest.json`. Windows setup writes the same three entries (Word,
Excel and OneDrive/SharePoint) itself, without the CLI, from the package's
catalog (`src/entrabot/a365/catalog.py`), and keeps any other entries in the
file. The manifest supplies each server's URL, audience, and scope. Runtime
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
server return `403`. On macOS and Linux, the later `a365 setup permissions mcp`
step requires PowerShell 7+ and stops at its requirements check without it;
Windows setup does not run it.

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
honour a new entry, so Windows setup checks the agent's tokens for each
audience for about two minutes, and `scripts\setup-windows.cmd -Diagnose` runs
the same check.

## Setup and validation

1. Provision Entrabot's Agent Identity and Agent User.
2. Add the Work IQ MCP servers to `ToolingManifest.json` and grant their
   permissions, including the Blueprint's inheritable permissions. On
   Windows, setup does this itself (answer yes to the Word, Excel and OneDrive
   question, or pass `-ConfigureA365WorkIq`); on macOS and Linux,
   `scripts/setup.sh --configure-a365-work-iq` does it with the Agent 365 CLI.
3. Keep the generated `ToolingManifest.json` in a location Entrabot
   discovers, or set the configured manifest path.
4. Run the relevant Word MCP tool and confirm the audit event identifies the
   Agent action.
5. Confirm returned document content carries the authoritative
   external-content envelope.

Use the generated manifest and live tenant consent state as the source of
truth. Server availability, license requirements, and admin-center
presentation vary by tenant.

## Related

- [Microsoft Entra Agent ID: Blueprints, Identities, and Users](agent-id-blueprints-and-users.md)
- [Agent Users](entra-agent-users.md)
- [Files and Work IQ guide](../guides/files-and-work-iq.md)
- [Files and SharePoint Graph API](files-graph-api.md)
- [Token Flows reference](../reference/token-flows.md)
- [MCP Tool reference](../reference/mcp-tools.md)
