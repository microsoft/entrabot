# Engineering Status

**Last reviewed:** 2026-07-10
**Runtime:** Python 3.12+ research project; no hosted production service

This page is the current shipped/in-progress view. Detailed backlog items live in [`TODOS.md`](https://github.com/microsoft/entrabot/blob/main/TODOS.md); architectural rationale lives in the ADRs and platform-learning documents.

## Shipped on `main`

### Identity and authentication

- Two runtime modes: certificate-backed `agent_user` and MSAL `delegated`.
- Autonomous three-hop Agent User flow: Blueprint certificate → Agent Identity FIC → Agent User `user_fic` token.
- Blueprint private keys live in macOS Keychain, Windows CNG/TPM or Software KSP, and Linux Secret Service through the `CredentialStore` abstraction.
- Provisioning creates Blueprint, BlueprintPrincipal, and Agent Identity through the dedicated Microsoft Graph v1.0 subtype endpoints; Agent User creation remains on Graph beta.
- `ENTRABOT_AGENT_UPN` is the canonical rename-safe identity setting. `ENTRABOT_AGENT_USER_UPN` remains a compatibility alias.

### Microsoft 365 and MCP

- 37 MCP tools across Teams, Outlook, Files, Agent 365 Work IQ, identity, audit, promises, interactions, and body bootstrap.
- Teams 1:1, group-chat, and cross-tenant membership flows use explicit `chat_id` values; there is no default group chat.
- Background Teams/email polling, chat discovery, daily summary scheduling, and Claude Code channel push.
- OneDrive/SharePoint read and sharing flows plus Work IQ Word create/read/comment operations.
- Agent actions emit audit events before returning; security-sensitive paths fail closed when auditing fails.

### Security and host behavior

- The non-overridable body prompt loads before the optional persona layer.
- Model-facing Teams, email, Files, and Work IQ content receives an authoritative `<external_content>` XPIA envelope. Envelope-looking attacker text is escaped and wrapped again rather than trusted.
- Sponsor-DM waiting is host-gated: Claude Code receives channel push; non-push hosts receive `sponsor_reply` inline from `send_teams_message`.
- Bot Gateway mode was removed by ADR-006; Teams communication is Graph-native.

### Storage, setup, and platforms

- Local operational storage is the default; Azure Blob is opt-in with `setup.sh --use-cloud-memory`.
- `LocalBackend`, `BlobBackend`, `PersonaBackend`, storage-token acquisition, migration helpers, and ETag concurrency are implemented.
- macOS, Linux, and Windows setup/teardown flows are present. Windows has a dedicated `status-windows.ps1` surface and has been acceptance-tested on Windows 11 ARM64.
- Consolidated status tooling covers identity objects, permissions, certificates, licenses, sponsors, and storage.

## In progress or under review

- **MXC/AppContainer sandbox integration:** PR #86 is open and currently conflicts with `main`. Sandbox tools must not be documented as shipped until that PR lands.
- **Windows command hardening:** PR #76 remains open and requires resolution before merge.
- **Long-session MCP disconnect:** the root cause remains open; read [`runbooks/mcp-disconnect-investigation.md`](runbooks/mcp-disconnect-investigation.md) before investigating.
- Script-toolkit documentation closeout, blob-environment test isolation, MCP server orphan cleanup, daily-summary scheduler fixes, and email cursor precision remain active backlog items.

## Known limitations

- Entrabot is a research implementation, not a production-ready tenant service.
- Live smoke testing provisions real Entra/Azure/Microsoft 365 resources and sends a real Teams message, so it remains manual unless a dedicated isolated tenant and non-human CI identity are provided.
- Windows end-to-end coverage is narrower than macOS coverage.
- Graph file comments do not provide Word document comments for `.docx`; use Work IQ Word comment tools.
- XPIA wrapping is defense in depth, not a proof that arbitrary model jailbreaks are impossible.

## Quality gates

Before commit:

```bash
pytest -v --tb=short
ruff check .
mkdocs build --strict
```

Use targeted tests first for changed behavior, then the full suite. Tests mirror the source tree and Graph calls are mocked with `respx` or focused fakes; destructive live smoke tests are not part of ordinary pull-request CI.
