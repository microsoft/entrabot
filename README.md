# Entrabot: Identity Research for Microsoft 365 Agents


Entrabot is a Python MCP server that gives a device-local agent its own Entra **Agent ID** and **Agent User**. The agent can sign in autonomously, send and receive Teams messages from its own account, use its mailbox, work with Microsoft 365 files, and write audit events against its own object ID. It runs on macOS, Linux, and Windows and works with Claude Code, Copilot CLI, or any MCP-speaking client.

**All you need to get started is:**

* A Microsoft 365 development tenant where you can create app registrations and grant admin consent
* A license that includes Teams and Outlook (E3 or E5 dev tenant licenses work)
* Python 3.12 installed locally

The scripts will take care of the rest: provisioning the Agent Identity Blueprint, Agent Identity, and Agent User in Entra; uploading a self-signed certificate; assigning the license; and configuring the local MCP server.

**Microsoft Entra Agent ID** and **Microsoft Agent 365** — which enable these experiences — went GA on 2026-05-01. Entrabot is the reference implementation that pulls those primitives together on a real device, today.

---

## What this is

A device-local MCP server that turns an LLM agent into a first-class principal in Microsoft Entra. Three things change when you do this:

- **Attribution.** Every action — Teams message sent, file read, email drafted — is signed by the agent, not by the human who launched it. Sign-in logs distinguish them. Audit trails are honest.
- **Authorization.** Conditional Access, ID Protection, and DLP apply to the agent's own object. You can restrict what the agent can do without restricting yourself.
- **Autonomy.** No device-code prompt, no OBO, no human in the loop on every token refresh. The agent authenticates with its own certificate-backed credentials and minds its own session.

It is for developers building agents on Microsoft 365 who want the security posture to match the architecture. The agent's smarts are up to you. entrabot gives it a secure seat at the table and the keys to the kingdom; what it does with that power is your call.

The body prompt (`prompts/agent_system.md` plus `prompts/anatomy/*.md`) loads before any optional persona. Security rules, channel discipline, and external-content boundaries are enforced by the body and reinforced in code. These controls reduce instruction-injection risk; they are not an absolute guarantee against every jailbreak technique.

---

## The stack

entrabot is the device-side glue for a set of platform primitives Microsoft shipped at GA.

- **Entra Agent ID** — the four-object hierarchy: Agent Identity Blueprint → BlueprintPrincipal → Agent Identity → Agent User. Confidential clients only; no public-client flows; tokens carry `idtyp=user` for the Agent User leaf. ([platform learning](docs/platform-learnings/agent-id-blueprints-and-users.md))
- **Microsoft Agent 365** — the control plane: admin-center inventory, OTel observability, Work IQ MCP servers (Mail, Calendar, Teams, SharePoint, OneDrive, Word, User, Copilot, Dataverse), AI-teammate lifecycle. GA 2026-05-01. ([platform learning](docs/platform-learnings/microsoft-agent-365.md))
- **Conditional Access for agents** — GA. Apply CA policies to Agent Identity sign-ins the same way you apply them to users.
- **ID Protection for agents** — GA. Risk scoring and remediation against the agent's own object.
- **FastMCP** — the Python MCP server framework. entrabot registers every Teams, Outlook, Files, Word, audit, and identity tool through it.
- **Three-hop certificate chain** — Blueprint token (cert JWT) → Agent Identity token (federated identity credential) → Agent User token (`user_fic` grant). No client secret in flight. Private key in macOS Keychain, Windows CNG (TPM-backed when available), or Linux Secret Service.

entrabot connects these. The Blueprint is provisioned via Graph. The Agent User is licensed and visible in Teams. The MCP server runs locally, mints tokens against Entra without a human, and exposes the resulting capability surface to the agent.

---

## Architecture

```mermaid
flowchart LR
    subgraph Device["Local device — Mac / Windows / Linux"]
        Client["MCP client<br/>(Claude Code, Copilot CLI)"]
        Server["entrabot MCP server<br/>(FastMCP)"]
        Body["Body prompt<br/>(non-overridable)"]
        Keys[("OS keystore<br/>Keychain / TPM / Keyring")]
    end

    subgraph Entra["Microsoft Entra ID"]
        AgentID["Blueprint →<br/>Agent Identity →<br/>Agent User"]
    end

    subgraph M365["Microsoft 365 / Azure"]
        Graph["Graph API<br/>(Teams / Outlook / Files)"]
        A365["Agent 365 Work IQ<br/>(Word, Mail, Copilot)"]
        Blob[("Azure Blob<br/>(optional)")]
    end

    Client <-->|stdio| Server
    Server --> Body
    Server --> Keys
    Server -->|3-hop cert chain| AgentID
    AgentID -->|idtyp=user| Graph
    AgentID -->|delegated| A365
    AgentID -->|user_fic| Blob
```

The agent talks to the MCP server over stdio. The server reads the Blueprint's private key from the OS keystore, walks the three-hop chain to produce a delegated user token, and uses that token for every Graph and Work IQ call.

**Inbound delivery differs by host.** On **Claude Code**, the server's background poll pushes every inbound Teams message and email directly into the LLM as a `notifications/claude/channel` system reminder — the agent sees a DM the moment it lands, with no tool call and no human prompt required. The conversation in Teams becomes the conversation with the agent. On **Copilot CLI, Codex, Cursor, and any MCP host that doesn't implement the channel-push extension**, the same background poll runs server-side, but messages accumulate in the interaction log instead of streaming in. The agent reads them on demand via `read_teams_messages`, `send_teams_message` auto-blocks for the sponsor's reply when push is unavailable, and `scripts/catch_up.py` prints recent activity from the CLI. Channel push is the better UX; the polling fallback is a working second-class path for hosts that haven't shipped the extension yet.

Operational state (interaction log, daily summaries, watched chats) lives locally by default, or in Azure Blob Storage scoped to the Agent User's object ID when cloud memory is enabled.

Full walkthrough in [`docs/architecture/system-overview.md`](docs/architecture/system-overview.md). The module-by-module breakdown lives in [`docs/architecture/layers/`](docs/architecture/layers).

---

## Quickstart

Mac or Linux:

```bash
git clone https://github.com/microsoft/entrabot.git
cd entrabot
./scripts/prereqs-macos.sh   # macOS; see INSTALL.md for Linux/Windows
./scripts/setup.sh --new --with-upn-suffix=workstation
claude --dangerously-load-development-channels server:entrabot
```

`setup.sh` is idempotent. It provisions the Blueprint, BlueprintPrincipal, Agent Identity, and Agent User; assigns a Teams-capable license; uploads a self-signed certificate to Entra; and writes `.env` plus `.mcp.json` with no secrets on disk. Full walkthrough — including Windows, cloud memory, cross-tenant group chats, and the Work IQ Word setup — is in [`docs/getting-started/quickstart.md`](docs/getting-started/quickstart.md) and [`INSTALL.md`](INSTALL.md).

### Launching the agent

The repo isn't published to npm/pypi — your host CLI loads the local stdio MCP server from `.mcp.json` in the cwd. No flag needed for that; it's auto-discovered. What differs between hosts is **how inbound Teams DMs reach the agent**.

**Claude Code (recommended).** Channel push: inbound Teams messages and emails arrive as next-turn system reminders without a tool call. Requires the dev-channel allowlist flag:

```bash
claude --dangerously-load-development-channels server:entrabot
```

The double-dash matters — single-dash silently treats `server:entrabot` as prompt text (Learning #44). `server:entrabot` is the MCP server name from `.mcp.json`, not a publication identifier.

**GitHub Copilot CLI, Codex, Cursor, other non-Claude hosts.** MCP tools work, but there's no `notifications/claude/channel` equivalent — channel push is silently absent. Inbound Teams messages instead arrive **inline** as `sponsor_reply` on `send_teams_message`, which auto-blocks until the sponsor replies (host-detected, server-side).

```bash
copilot   # or: codex, cursor, etc. — no flag, just launch from the repo dir
```

While the agent is blocked waiting on a Teams reply (any host that calls `wait_for_sponsor_dm` explicitly), the host CLI shows a heartbeat animation so you know it's listening to Teams, not your keyboard:

```
           __
      (___()'`;  woof! 🐕
      /,    /`
      \"--\

(•ᴗ•) zZz... listening for Teams DM [42s] (Ctrl+C to break)
```

Frames cycle (`ʕ•ᴥ•ʔ waiting on sponsor`, `(´･ω･`) sponsor hasn't replied yet`, `(◕‿◕) still here, still waiting`, …) every ~30s with elapsed time. Ctrl+C breaks out cleanly. Full host-by-host protocol: [`docs/claude-copilot-cli-channel-port.md`](docs/claude-copilot-cli-channel-port.md) and [`prompts/anatomy/channel-discipline.md`](prompts/anatomy/channel-discipline.md). On Windows, use `pwsh -File status-windows.ps1` for the equivalent status surface.

After setup, use `./status.sh` as the canonical health and identity check:

```bash
./status.sh
./status.sh --health-only --strict
./scripts/setup.sh --status --json   # delegates to ./status.sh
```

---

## Documentation

The full doc site: **<https://microsoft.github.io/entrabot/>**

Direct pointers:

- [Quickstart](docs/getting-started/quickstart.md) — five minutes from clone to first Teams message
- [MCP tool reference](docs/reference/api/mcp-tools.md) — every tool, every parameter
- [Setup script reference](docs/reference/scripts/setup.md) — every `setup.sh` and `setup-windows.ps1` flag
- [Script reference](docs/reference/scripts/operations.md) — status, health, DM, email, setup, teardown, and diagnostic scripts
- [Token flows](docs/reference/token-flows.md) — the three hops, annotated
- [System overview](docs/architecture/system-overview.md) — how the modules fit together
- [Architecture decisions](docs/decisions/README.md) — ADRs 001–006
- [Platform learnings](docs/platform-learnings/) — Entra Agent ID constraints, Agent 365, MSAL, OS-specific notes
- [Hard-won learnings](docs/runbooks/hard-won-learnings.md) — non-obvious gotchas; read before changing auth or Teams code
- [Engineering status](docs/engineering-status.md) — what's shipped, what's open, what's next

---

## Status

This is a research repo, not a production service. It runs reliably on a developer's machine. It is not packaged for tenant-wide deployment.

**Shipped:**

- Two auth modes: `agent_user` (full three-hop), `delegated` (MSAL interactive for demos without an E5)
- Teams: 1:1 DMs, group chats, cross-tenant B2B group chats with federated home-tenant resolution
- Outlook: background email poll with Purview-encrypted detection, daily summary at 5pm PT
- Files: SharePoint / OneDrive read, write, upload, share — two-gate sponsor authorization on share
- Microsoft Agent 365 Work IQ Word: create, read, comment, reply-to-comment
- Storage: `LocalBackend` (default) and `BlobBackend` (Azure Blob Storage, opt-in via `setup.sh --use-cloud-memory`)
- Body-first prompt architecture with optional persona layer from a separate MCP (`persona-sati`)
- Audit fails closed: if the audit write fails, the action does not proceed
- `pytest -v --tb=short && ruff check .` gates every commit

**Persona-sati host bootstrap:** Hosts that attach persona-sati must call `bootstrap_session` before the first substantive answer because FastMCP instructions do not reliably reach the LLM prompt. If `mind_contract_available` is false, operate in body-only mode. When the mind contract is available, follow the per-turn cognition tools: `observe` around external tools, `reflect` for durable observations, and `recall` when a returned memory excerpt is insufficient.

**OS coverage:**

| OS | Status |
|---|---|
| macOS | Shipped — Keychain-backed cert storage, full three-hop flow |
| Linux | Works — Secret Service (libsecret) backend |
| Windows | Shipped, acceptance-tested on ARM64 Windows 11 — CNG cert storage with TPM-first/software fallback |

**Open:**

- MXC/AppContainer sandbox integration is under review and is not part of the current `main` runtime.
- Long-session MCP disconnect investigation and several scheduler/cursor precision fixes remain tracked in [`docs/engineering-status.md`](docs/engineering-status.md).

---

## Contributing, support, and license

- [Contributing](CONTRIBUTING.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Support](SUPPORT.md)
- [Security reporting](SECURITY.md)
- [MIT License](LICENSE)

This is a prototype. It is designed to show the pattern and make the implementation copyable, not to be run unchanged as a production platform.