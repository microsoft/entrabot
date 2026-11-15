# Headless and cloud operation: assessment and a first slice

**Status:** proposal, for the owner to decide. Nothing here is implemented.
**Date:** 2026-11-14. Assessed on `ws4/headless-design` (which carries PR #122's harness,
merged onto `main`, plus workstreams 1 and 3).

## The question

Can Entrabot run unattended, with only an LLM API key and no interactive CLI (no Claude
Code, no Copilot CLI session a person started), on a server or in a container, and still be
the same agent with its own Entra Agent ID and Agent User?

## What the harness already does

`src/entrabot/harness/` (PR #122, by Jacob Lyons and Rick Rybinski) is most of an unattended
runtime already:

- **It owns the model loop.** `session/core.py` drives a GitHub Copilot SDK session
  (`github-copilot-sdk`, `copilot.CopilotClient`): streaming turns, tools, MCP discovery from
  `.mcp.json`, slash commands, interrupt. It does not need the `entrabot-mcp` body: it
  excludes it and provides the Teams reply tools itself (`teams/tools.py`).
- **Teams in and out.** `teams/bridge.py` polls watched chats and injects new messages into
  the running turn (steering, `mode="immediate"`); `entrabot_send` replies.
- **The Teams token is wired.** `teams/auth.py` uses `ENTRABOT_GRAPH_TOKEN` when set, otherwise
  the three-hop Agent User flow (`acquire_agent_user_token`) with `exp`-based refresh. The
  harness README still calls this an "INTEGRATION POINT"; that line is stale.
- **Per-caller permissions.** `session/permissions.py` feeds the SDK's per-call permission
  hook from a `cli / sponsor / guest` matrix; sponsors come from the Agent Identity's
  directory sponsors.
- **A scheduler** (`scheduler/`) with interval, one-shot and cron tasks, persisted to
  `.entrabot/` on disk.
- **Agent 365 telemetry** (`observability/`): one content-suppressed `invoke_agent` span per
  turn, with the delegated token refreshed in the background.
- **Bring-your-own model.** The Copilot SDK's `ProviderConfig` (in the installed
  `copilot/session.py`) accepts `type` `openai`, `azure` or `anthropic`, a `base_url`, and an
  `api_key`, `api_key_command` or `bearer_token`. So "only an LLM API key" is possible without
  a GitHub Copilot subscription, once the harness passes a provider through (it does not
  today; it relies on the Copilot CLI's own sign-in).

## What is missing

1. **A non-interactive entry point.** `entrabot` starts a TUI or console UI. A headless run
   needs `entrabot run --headless`: no UI, logs to stdout, a health endpoint or file, clean
   shutdown on SIGTERM.
2. **A container image.** Python 3.12, the package, and the Copilot CLI runtime, which the SDK
   downloads on first use (`copilot/_cli_download.py`). A container should bake that binary in
   at build time, pinned and verified, and disable runtime downloads.
3. **A key source that is not a desktop keyring.** Today the Blueprint's key is in the macOS
   Keychain, the Linux Secret Service or the Windows certificate store. Microsoft Learn
   ("Create an agent identity blueprint") recommends a **managed identity as the Blueprint's
   federated identity credential** for production: no key at all, Hop 1 becomes a managed
   identity token exchanged for the Blueprint assertion. That needs:
   - a Hop-1 path in `entrabot.auth` that takes a managed identity assertion instead of a
     certificate JWT (the three-hop flow after it is unchanged);
   - setup that adds the managed identity as a federated credential on the Blueprint
     (`AgentIdentityBlueprint.AddRemoveCreds.All`).
   A certificate in Azure Key Vault (signing with Key Vault's `sign` operation, the key never
   leaving the HSM) is the fallback where a managed identity is not available.
4. **The LLM key's source.** `api_key_command` lets the runtime fetch the key per request;
   point it at Key Vault (managed identity), not an environment variable baked into an image.
5. **State that survives a restart.** The harness's Teams bridge keeps its "seen" set in memory
   and marks existing messages as seen on start, so a message that arrives while the process is
   down is never delivered. The MCP server solved this (`tools/chat_cursors.py`, `poll_state.py`,
   blob-backed with compare-and-swap). The harness should use the same backend. The scheduler
   writes to local disk, which a container loses; it should use the same backend too.
6. **The server-side controls the MCP body has.** The harness reads Teams through the body's
   `entrabot.tools.teams.read`, so inbound text arrives in the external-content envelope, and
   sends through `entrabot.tools.teams.send`, but it does not write the fail-closed audit
   events the body's tools now write (workstream 2) and has no path through the recipient
   policy. In a headless run nobody watches the terminal, so these matter more, not less.
   Either route the harness's tools through the body's implementations or call the same modules.
7. **Containment.** Workstream 3 contains `entrabot-mcp`. In a container the container is the
   first boundary; the same proxy allowlist (outbound to the Microsoft hosts only, plus the LLM
   provider's host) should apply to the harness process.

## Copilot SDK only, or another agent SDK too?

Stay on the Copilot SDK for the first slice. It is what the harness is built on and tested
against, it already gives per-call permission hooks, MCP discovery and BYOK providers, and it
keeps the colleagues' work as the base. An abstraction over a second SDK (the Claude Agent SDK,
the Microsoft Agent Framework, or the Microsoft 365 Agents SDK) is worth designing only after
the headless slice runs, around three seams that already exist: the model loop
(`session/core.py`), the tool surface (`teams/tools.py` plus MCP), and the permission hook
(`session/permissions.py`). The Microsoft 365 Agents SDK deserves a separate look for one
reason: Learn says it registers agents in the Agent 365 registry automatically, which Entrabot
now does only through an opt-in script.

## Recommended first slice

One process, one container, one agent, on Azure Container Apps (or any host with a managed
identity):

1. `entrabot run --headless` (no UI; SIGTERM-clean; logs to stdout).
2. Hop 1 from a managed identity federated credential, with setup adding that credential to the
   Blueprint. Keep the certificate path for desktops.
3. BYOK provider from configuration, `api_key_command` reading Key Vault.
4. The harness's Teams bridge on the body's cursor store (blob-backed), and its send tool on the
   body's recipient policy and fail-closed audit.
5. A Dockerfile that pins the Copilot CLI runtime and disables runtime downloads.

Out of the first slice: multiple agents per process, the TUI, a second agent SDK.

## Decisions for the owner

- Copilot SDK as the runtime for headless operation (recommended), or wait for a second SDK.
- Managed identity as the production Blueprint credential (recommended), or Key Vault-held
  certificates.
- Whether the harness's own Teams tools are replaced by the body's, or keep their own path and
  call the body's policy modules.
- Coordination with the PR #122 authors on all of the above, since it builds on their code.
