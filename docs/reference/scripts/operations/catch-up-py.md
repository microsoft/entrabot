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
  environment (`entrabot.config` loads `.env` automatically on import, from
  the repository root regardless of the working directory), with the
  Blueprint certificate's private key present in the OS keystore. On a cloud
  deployment, `ENTRABOT_BLOB_ENDPOINT` and `ENTRABOT_BLOB_CONTAINER` from the
  same place select where the chat list is read from.
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
- Reads the chat IDs to poll from the configured storage backend, the same
  `watched_chats` list the running MCP server uses. With a local backend
  that is `ENTRABOT_DATA_DIR/watched_chats`. With Blob, any local copy is
  ignored.
- Also checks the backend for a legacy `chat_id` single-chat key and prepends
  its value to the chat list if present. No current code path writes this
  key (there is no default chat), so it is normally absent.
- For each chat ID, GETs `/chats/{id}/messages` (`$top=15`,
  `$orderby=createdDateTime desc`) and prints the sender, timestamp, and a
  300-character body preview for each message.
- GETs `/me/messages` (`$top=15`, `$orderby=receivedDateTime desc`) and
  prints the sender, timestamp, subject, an attachment marker, and a
  200-character preview for each message.
- Entirely read-only against Graph and storage: no messages are sent and no
  cursors or watched-chat state are updated.

## Exit behavior

`asyncio.run(main())` returning normally exits `0`. If the watched-chat list
can't be read, the script prints an error to stderr, still shows the inbox, and
exits `1`, so a storage outage doesn't look like "no chats". Per-chat and inbox
HTTP errors are caught individually
and printed as `ERROR <status>: <body>` without stopping the script or
raising — only failures before any request is made (e.g. `acquire_agent_user_token`
raising because credentials are missing) surface as an uncaught exception
and Python's default non-zero exit; the script does not catch or assign a
specific code to that path.

## Common failures

- **A chat prints nothing** — it isn't in `watched_chats` yet. Add it via
  `create_chat` or wait for the running MCP server's auto-discovery, then
  re-run.
- **`ERROR: could not read the watched-chat list`** — the configured storage
  backend couldn't be reached or denied the read. See
  [Storage troubleshooting](../../../troubleshooting/storage.md).
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
