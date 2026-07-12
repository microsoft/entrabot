# `scripts/dm.py`

## Purpose

Send a single one-off Teams message to an explicit chat as the Agent
User, without starting an MCP session — for scripted notifications or ad
hoc replies from the command line.

## Requirements

- Python 3.12+ with `entrabot` installed.
- Agent User three-hop credentials configured (same as
  [`catch_up.py`](catch-up-py.md#requirements)).
- The target chat must already exist — created via `create_chat`,
  discovered by the running MCP server, or otherwise already known. This
  command does not create chats and there is **no default chat**; you
  must always pass `--chat` explicitly.
- Works on macOS, Linux, and Windows.

## Usage

```bash
python scripts/dm.py "Your message here" --chat 19:abcdefgh...@unq.gbl.spaces
python scripts/dm.py "Your message here" --chat myalias
```

## Options

- `message` (positional, required) — the text to send.
- `--chat` (required) — a Graph `chat_id`, or a key defined in the
  `CHAT_ALIASES` dict at the top of the script (empty by default; edit
  the script to add your own, e.g. `"alice": "19:xxxx@unq.gbl.spaces"`).
  If the value given isn't a key in `CHAT_ALIASES`, it is used verbatim
  as the chat_id.

## Effects

- Resolves `--chat` against `CHAT_ALIASES`, falling back to the literal
  value when there's no match.
- Acquires an Agent User token via `acquire_agent_user_token`.
- Calls `entrabot.tools.teams.send(chat_id=..., message=..., token=...)`,
  which POSTs an HTML-formatted chat message to Graph (`content_type`
  defaults to `"html"`).
- Prints the sent message's Graph `id` to stdout, or the literal string
  `ok` if the response has no `id`.
- This is a real, visible Graph side effect: the message is delivered to
  every member of the target chat immediately.

## Exit behavior

No explicit exit codes are set in source. Success exits `0` via
`asyncio.run(main())`. `--chat` is `required=True` in `argparse`, so
omitting it triggers argparse's own usage error and exit code `2` before
`main()`'s body runs. Any other failure — an invalid `chat_id`, missing
or expired Agent User credentials, a Graph error from `send()` — surfaces
as an uncaught exception and Python's default non-zero exit; the script
does not catch these itself.

## Common failures

- **Argparse usage error (exit 2)** — `--chat` was omitted; it's required
  on every invocation.
- **Uncaught `AgentIDNotAvailable`** — Agent User credentials aren't
  configured; run `setup.sh` first.
- **Uncaught Graph error (e.g. invalid `chat_id`)** — verify the chat_id
  with [`catch_up.py`](catch-up-py.md) or `show_agent_status.py`, or
  create the chat first via `create_chat`.

## Related commands

- [`catch_up.py`](catch-up-py.md) — see what's arrived in a chat before replying.
- [`read_email.py`](read-email-py.md) — the mail equivalent of a one-off check.
- Guides: [Teams and Chat Workflows](../../../guides/teams-and-chat-workflows.md)
- Architecture: [Messaging and Delivery](../../../architecture/messaging-and-delivery.md)
- [Operations scripts index](../index.md#operations)
