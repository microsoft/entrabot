# Changelog

## v0.1 — 2026-05-21

First public release. Reference implementation for Microsoft Entra Agent ID and Microsoft Agent 365 (GA 2026-05-01). MIT licensed. **Research repo, not production-ready** — see Known Limitations below.

### Added

**Identity & auth**
- Three-hop certificate chain: Blueprint → Agent Identity → Agent User. Private key in OS keystore (Keychain / Windows TPM via CNG / Linux Secret Service). No client secret in flight, no `.env` secrets on disk.
- Three auth modes: `agent_user` (full three-hop), `delegated` (MSAL interactive), `bot` (Bot Framework + M365 Agents SDK).
- Progressive identity state machine (UNAUTHENTICATED → DELEGATED → PROVISIONING → AGENT_USER).
- Cross-tenant federated B2B chat — guest UPN auto-decoded, home tenant resolved via OIDC discovery.
- Sponsor gate enforcement on every mutating tool (`share_file`, `add_teams_member`); requester must be an authorized sponsor of the chat.

**Agent capabilities (34 MCP tools)**
- Teams: send, read, create chat, list/add members, watch replies, placeholder + resolve, delete own message, image view.
- Outlook: `send_email` with thread-preserving replies, daily summary at 5pm PDT.
- Files: `resolve_file_url`, `list_recent_files`, `read_file`, `add_file_comment`.
- Agent 365 Work IQ Word: create/read documents, add/reply to comments.
- Promises (durable across restart): add, list, resolve.
- Identity: `whoami`, `audit_log`, on-demand daily summary.

**Channel push & polling fallback**
- `notifications/claude/channel` push extension — Claude Code gets inbound Teams DMs and emails as next-turn system reminders. The Teams conversation IS the conversation with the agent.
- `send_teams_message` auto-blocks on non-Claude-Code hosts (Copilot CLI, Codex, Cursor) for the sponsor's reply. Deterministic, host-detected, no parameter the model can disable.

**Body prompt architecture**
- Non-overridable body prompt at `prompts/agent_system.md` with `@include` expansion of `prompts/anatomy/*.md`. Security, channel discipline, identity/tools rules load below the persona line.
- Instruction-injection defense at the architectural level — an agent that runs on entraclaw cannot be jailbroken into impersonating its operator.

**Mind / persona (optional)**
- Persona-sati MCP integration. Body composes `body + persona` at boot when `PERSONA_SATI_MCP_URL` is set. Clean fallback to body-only mode when persona-sati is unreachable.

**Storage**
- ADR-005 Phases 1, 2, 5, 6a: opt-in cloud-hosted memory via Azure Blob. Per-Agent-User container, RBAC scoped to the agent's object ID. Local filesystem fallback.

**Agent 365 Work IQ provider**
- Reusable `entraclaw.a365` provider boundary for Work IQ MCP servers (Word, Mail, Copilot, Dataverse). Manifest loading, audience-specific token acquisition, MCP-client calls. Teams intentionally remains Graph-native.

**Discipline**
- 1,237 tests, ruff clean, 80% coverage threshold enforced.
- 66 hard-won learnings at `docs/runbooks/hard-won-learnings.md`.
- Full docs site at <https://microsoft.github.io/Entraclaw/> with API reference, script reference, ADRs, platform learnings, and runbooks.

### Known limitations

- **Windows port is acceptance-tested on one ARM64 VM only.** Verified end-to-end (cert provisioning, three-hop CNG signing, MCP registration, Teams DM round-trip) but not exercised on broad hardware, intel x64, or in long-running sessions.
- **Bot Gateway needs live Azure Bot Service registration.** The local server + Dev Tunnel path works; the productized "bot has its own Teams app identity" path needs an Azure Bot resource and Teams app manifest signed in a tenant.
- **Persona-sati 12h MCP bearer refresh** is open (draft PR persona-sati#47 paused at Agent Blueprint OAuth client constraints). Affects only sessions that connect to persona-sati after the 12h boundary; restart the MCP host to recover.
- **Agent Identity provisioning omits `Application.Read.All`**, which `wait_for_sponsor_dm` needs to call the sponsors API. Manual grant required as a workaround until `scripts/create_entra_agent_ids.py` is updated.
- **`add_file_comment` on `.docx` files returns 404** on Graph beta `/comments` — Microsoft does not expose Word document comments there. Use the Work IQ Word path instead (`add_word_comment` / `reply_to_word_comment`).
- **CLI commitment-language detection is not enforced.** Server-side detection fires on outbound Teams text; commitments made to the operator in the host terminal silently drift. Mitigation is body-prompt strengthening, not mechanical enforcement.

APIs may change in 0.x releases. Pin to a commit SHA or a release tag for stability.

### Provenance

History was rewritten with `git filter-repo` before this release to remove pre-sanitization PII commits (real Microsoft employee names, emails, production tenant GUIDs, internal codenames). The repo went from private to public at this release. Commits before `f2a3c18` no longer exist.
