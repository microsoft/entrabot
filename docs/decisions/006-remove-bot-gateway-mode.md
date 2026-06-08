# ADR-006: Remove the Teams Bot Gateway Auth Mode

**Date:** 2026-06-08
**Status:** Accepted (shipped). The `bot` mode and `src/entrabot/bot/` are deleted.
**Deciders:** the user
**Context:** Reducing complexity by removing the off-thesis Bot Framework gateway

## Context

entrabot shipped three auth modes via `ENTRABOT_MODE`:

- `agent_user` — three-hop Agent User flow (Blueprint cert → Agent Identity FIC → Agent User). The agent acts as its **own** Entra principal.
- `delegated` — MSAL interactive auth using the human's token.
- `bot` — an M365 Agents SDK / Bot Framework gateway (`src/entrabot/bot/`) with a local aiohttp server, JSONL IPC, and a Dev Tunnel. The agent appeared in Teams as a **Bot Framework bot**, not as its Agent User.

Bot mode was added (see the archived `DESIGN-teams-bot-gateway.md`) to get a Teams identity with zero provisioning delay and no M365 license. In practice it pulled the project away from its thesis and added standing complexity.

## Decision

**Remove `bot` mode entirely.** Full excision in one change: the `bot/` package, its 45 tests, the `aiohttp` + `botbuilder-core` + `botbuilder-integration-aiohttp` dependencies, the `bot_*` config fields and `ENTRABOT_BOT_*` env vars, the `mode == "bot"` branches in `mcp_server.py`, and `scripts/setup_bot.sh` / `scripts/start_bot.sh`.

### Why

1. **Off-thesis.** The project exists to make the agent a first-class Entra principal — its own Agent ID and Agent User (Attribution / Authorization / Autonomy). Bot mode is the one mode that **bypasses Agent Identity**: messages show as a Bot Framework bot, not the Agent User. It contradicts the reason the repo exists.
2. **Redundant with a shipped product.** Microsoft Agent 365 GA'd a managed "AI teammate" (2026-05-01). The repo's own platform-learning called bot mode the "self-built equivalent." Microsoft now ships the managed version.
3. **Complexity tax.** ~921 LOC of source + 602 LOC of tests, three heavy dependencies used **only** in `bot/`, and ~6 conditional branches threaded through `mcp_server.py`.

### Fail loud, not silent

Removing `bot` from `VALID_MODES` would make `_validate_mode` silently fall back to `auto` for an existing `ENTRABOT_MODE=bot` config — a silent identity-mode switch, which the project's "zero silent failures" rule forbids. Instead, `_validate_mode` raises `RemovedModeError` for `bot` with a migration message pointing at `agent_user` / `delegated`. Other unrecognized values still fall back to `auto`.

### Accepted tradeoff

Bot mode was the only path that did Teams I/O **without** a Graph token (it wrote to `outbound.jsonl` for the bot server). Removing it eliminates that fallback. This is accepted — a Graph-tokenless Teams path is precisely the off-thesis behavior being removed.

## Consequences

- Two auth modes remain, and both use Agent Identity end to end.
- Attack surface shrinks: no local aiohttp bot server, no Dev Tunnel public HTTPS endpoint.
- The full Bot Framework implementation remains retrievable from git history (commit prior to `refactor/remove-bot-mode`) if ever needed as a reference.

## References

- The original design (now removed): `DESIGN-teams-bot-gateway.md` — see git history.
- `docs/platform-learnings/microsoft-agent-365.md` — the managed AI-teammate that supersedes this.
- `docs/platform-learnings/teams-bot-framework.md` — retained research on the Bot Framework platform itself.
