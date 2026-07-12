# `diagnose-chat.py`

## Purpose

Tests Teams chat creation directly against the Microsoft Graph API, bypassing the MCP server and its `create_chat` tool entirely. Use it when `create_chat` is failing (or behaving unexpectedly) inside a running MCP session and you need to see the raw three-hop token acquisition, the raw Graph request/response for chat creation, and the resulting member list without any MCP-layer wrapping in the way.

## Requirements

- Python 3.12+ with the project's virtualenv active, and `src/` importable (the script inserts `sys.path.insert(0, "src")` itself, so it can be run from the repository root without an editable install).
- A fully configured Agent User session: the three-hop token flow (`acquire_agent_user_token`) must succeed, which means `ENTRABOT_TENANT_ID`, the Blueprint app ID and certificate thumbprint, `ENTRABOT_AGENT_ID`, and `ENTRABOT_AGENT_USER_ID` must all be set (typically via `.env` written by `setup.sh`), and the Blueprint's private key must be present in the local OS keystore.
- At least one human recipient configured via `ENTRABOT_HUMAN_USER_ID(S)` (plus `ENTRABOT_HUMAN_USER_TYPES` / `ENTRABOT_HUMAN_USER_MAILS` / `ENTRABOT_HUMAN_USER_TENANT_IDS` for guest or cross-tenant recipients) — see the [configuration reference](../../../guides/configuration.md) for the full variable set and precedence rules.

## Usage

```bash
python scripts/diagnose-chat.py
```

The script takes no command-line arguments and reads everything from the environment via `EntraBotConfig.from_env()`. There is no dry-run flag.

## Effects

**This script has real side effects — it is not read-only.** Each run:

1. Acquires a fresh Agent User token via the three-hop flow.
2. Calls the same `create_or_find_chat()` used by the MCP server's `create_chat` tool. For a single configured human recipient this creates (or resumes, since Graph's `oneOnOne` chat creation is idempotent) a real 1:1 Teams chat between the Agent User and that recipient. For multiple recipients it creates a real `group` chat with a topic.
3. Fetches the chat's member list twice — once via `GET /chats/{id}?$expand=members` and once via `GET /chats/{id}/members` — to compare the two shapes side by side (they have historically diverged on which fields are populated).
4. Sends an actual Teams message ("🔧 Diagnostic test message from diagnose-chat.py") into that chat. **This step runs unconditionally and is not idempotent** — every invocation sends a new message that the recipient(s) will see in Teams.

Because of step 4, do not run this script against a chat with a human sponsor who isn't expecting a test message, and do not run it repeatedly in a loop — each run is a new, real notification.

## Exit behavior

The script always exits `0`. `main()` prints `✅`/`❌`/`⚠️` markers and returns early on failure (token acquisition failure, chat creation failure, non-200 member-list responses), but nothing in the script calls `sys.exit()` with a non-zero code — the process exit status does not reflect success or failure. Read the printed markers, not the shell exit code, to determine whether the run succeeded. Standard library `logging` is configured at `DEBUG` level, so verbose HTTP client logging accompanies the script's own print output.

## Related commands

- [`entrabot-mcp-debug.sh`](entrabot-mcp-debug-sh.md) — captures the MCP server's own stderr if you need to compare its `create_chat` behavior against this script's direct-Graph path.
- [`list_sponsors.py`](list-sponsors-py.md) and [`list_agent_identities.py`](list-agent-identities-py.md) — confirm the Agent Identity and sponsor configuration this script's token acquisition depends on.
- [Teams Graph API — Chat operations](../../../platform-docs/teams-graph-api.md#chat-operations) — the Graph semantics behind `create_or_find_chat()`, including the guest/cross-tenant member payload shape this script exercises.
- [Agent Users — Authentication: the three-hop token flow](../../../platform-docs/entra-agent-users.md#authentication-the-three-hop-token-flow) — what has to succeed before step 2 can run.
- [Scripts reference: Diagnostics](../index.md#diagnostics) — the other read-only diagnostics in this set (this is the one exception that isn't read-only).
