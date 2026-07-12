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
| Files, OneDrive, SharePoint, Excel | Direct Microsoft Graph |
| Word document content, create, comment, reply | Work IQ Word adapter |
| OneDrive/SharePoint metadata and small-file reads through Work IQ | Work IQ ODSP adapter |

Teams, mail, and Files stay Graph-native because Entrabot needs explicit chat
IDs, background polling, channel push, cross-tenant membership handling, and
host-gated sponsor replies. Work IQ is used only where direct Graph cannot
provide the capability, such as document-level Word comments — Microsoft
Graph drive-item `/comments` is not a substitute for Word comments on `.docx`
files.

Agent 365 tenant-wide OpenTelemetry observability is a separate integration
surface; Entrabot maintains its own fail-closed audit log.

## `ToolingManifest.json` is authoritative

Configure Work IQ servers with the Agent 365 CLI. The generated
`ToolingManifest.json` supplies each server's URL, audience, and scope.
Runtime code prefers that manifest over documented values because catalog
metadata can change.

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
- `src/entrabot/a365/odsp.py`

## Work IQ Word tools

Entrabot exposes four Word operations through the typed adapter:

| Entrabot operation | Work IQ tool | Purpose |
|---|---|---|
| Read document | `GetDocumentContent` | Returns document HTML and comments. |
| Create document | `CreateDocument` | Creates a `.docx` in the Agent User's Microsoft 365 storage. |
| Add comment | `AddComment` | Adds a top-level Word comment. |
| Reply to comment | `ReplyToComment` | Replies to an existing Word comment. |

Document content is external input. `get_document_content()` wraps returned
HTML with the authoritative external-content boundary before exposing it to
the model. Provider raw results remain available for typed parsing and
diagnostics but are not treated as trusted instructions.

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

`ENTRABOT_MODE=delegated` is a separate human-attributed MSAL mode and does
not provide Agent User attribution. See
[Delegated Authentication with MSAL](delegated-auth.md).

## Setup and validation

1. Provision Entrabot's Agent Identity and Agent User.
2. Use the Agent 365 CLI to add the required Work IQ MCP server and grant its
   permissions.
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
- [MCP Tool reference](../reference/api/mcp-tools.md)
