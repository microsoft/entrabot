# `scripts/catch_up.py`

## Purpose

One-off pull of recent messages from every watched Teams chat plus the
Agent User's inbox, printed to stdout. Useful for seeing what arrived
while the MCP server's background poll wasn't running, or on hosts that
don't support the `notifications/claude/channel` push extension and so
never surface polled messages to the LLM automatically.

## Requirements

- Python 3.12+ with `entrabot` installed.
- Agent User three-hop credentials configured — `ENTRABOT_TENANT_ID`,
  `ENTRABOT_BLUEPRINT_APP_ID`, `ENTRABOT_BLUEPRINT_CERT_THUMBPRINT`,
  `ENTRABOT_AGENT_ID`, and `ENTRABOT_AGENT_USER_ID` set via `.env` or the
  environment (`entrabot.config` loads `.env` automatically on import),
  with the Blueprint certificate's private key present in the OS keystore.
- Works on macOS, Linux, and Windows.

## Usage

```bash
python scripts/catch_up.py
python scripts/catch_up.py 12
```

## Options

One optional positional argument, parsed as an integer and stored as
`_hours`. It is accepted but **not currently used to filter anything** —
the source marks it `# noqa: F841 — reserved for future filtering`.
Passing it does not change today's output.

## Effects

- Acquires an Agent User token via `acquire_agent_user_token`.
- Reads chat IDs to poll from `~/.entrabot/data/watched_chats` (one chat
  ID per line). This path is **hardcoded to the user's home directory**
  and does not honor `ENTRABOT_DATA_DIR` or the Windows data-directory
  override that `entrabot.config` otherwise applies — on a machine using
  a non-default data directory, this script may read a different (or
  empty) `watched_chats` file than the running MCP server writes.
- Also checks for a legacy `~/.entrabot/data/chat_id` single-chat file and
  prepends its contents to the chat list if present. No current code path
  writes this file (there is no default chat), so it is normally absent.
- For each chat ID, GETs `/chats/{id}/messages` (`$top=15`,
  `$orderby=createdDateTime desc`) and prints the sender, timestamp, and a
  300-character body preview for each message.
- GETs `/me/messages` (`$top=15`, `$orderby=receivedDateTime desc`) and
  prints the sender, timestamp, subject, an attachment marker, and a
  200-character preview for each message.
- Entirely read-only against Graph: no messages are sent and no cursors
  or watched-chat state are updated.

## Exit behavior

No explicit exit codes are set in source. `asyncio.run(main())` returning
normally exits `0`. Per-chat and inbox HTTP errors are caught individually
and printed as `ERROR <status>: <body>` without stopping the script or
raising — only failures before any request is made (e.g. `acquire_agent_user_token`
raising because credentials are missing) surface as an uncaught exception
and Python's default non-zero exit; the script does not catch or assign a
specific code to that path.

## Common failures

- **A chat prints nothing** — it isn't in `watched_chats` yet. Add it via
  `create_chat` or wait for the running MCP server's auto-discovery, then
  re-run.
- **Uncaught traceback from `acquire_agent_user_token`** — Agent User
  credentials aren't configured; run `setup.sh` first.
- **`ERROR 401` per chat or inbox section** — the Agent User token
  expired mid-run or lacks the required Teams/Mail scope; re-run to mint
  a fresh token, or check [`show_permissions.py`](show-permissions-py.md).

## Related commands

- [`dm.py`](dm-py.md) — send a message to a watched chat as the Agent User.
- [`read_email.py`](read-email-py.md) — fetch mail by subject instead of the raw inbox dump.
- [`show_agent_status.py`](show-agent-status-py.md) — confirm the Agent Identity chain is healthy before troubleshooting message delivery.
- Architecture: [Messaging and Delivery](../../../architecture/messaging-and-delivery.md)
- [Operations scripts index](../index.md#operations)
