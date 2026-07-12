# `scripts/read_email.py`

## Purpose

One-off fetch of the Agent User's most recent mailbox messages whose
subject contains a given substring, printed to stdout — for checking mail
outside of an MCP session or the 60-second background email poll.

## Requirements

- Python 3.12+ with `entrabot` installed.
- Agent User three-hop credentials configured (same as
  [`catch_up.py`](catch-up-py.md#requirements)).
- Works on macOS, Linux, and Windows.

## Usage

```bash
python scripts/read_email.py "Project Apollo"
python scripts/read_email.py "Project Apollo" 20
python scripts/read_email.py
```

## Options

Two optional positional arguments, read directly from `sys.argv` (there
is no `argparse`):

1. **subject substring** — defaults to the literal string `"Project
   Apollo"` if omitted. This is a leftover example value, not a sentinel
   meaning "all mail" — always pass an explicit subject in real use.
2. **top** — maximum number of messages to return, parsed as `int`,
   defaults to `5`.

## Effects

- Acquires an Agent User token via `acquire_agent_user_token`.
- GETs `/me/messages` with `$filter=contains(subject, '<substring>')`,
  `$orderby=receivedDateTime desc`, `$top=<top>`, and
  `$select=subject,from,receivedDateTime,body,bodyPreview,hasAttachments`.
- For each match, strips HTML tags and collapses whitespace in the body,
  then prints a `======== [timestamp] sender ========` banner, the
  subject, and up to 3000 characters of the plain-text body to stdout.
- Entirely read-only against Graph — nothing is written, and messages are
  not marked as read.

## Exit behavior

No explicit exit codes or Graph status-code checking in source: the
response is passed straight to `r.json()` and iterated via
`.get("value", [])`. A non-2xx Graph response (for example a 401 from an
expired token) yields a JSON error body without a `"value"` key, so the
loop silently prints nothing and the script still exits `0` — there is no
visible error in that case. Only a failure before that point (token
acquisition raising, a network error) surfaces as an uncaught exception
and Python's default non-zero exit.

## Common failures

- **No output, exit code 0** — the Graph response was likely an error
  (most often an expired or invalid token) that this script doesn't
  surface. Re-run, or check
  [`show_agent_status.py`](show-agent-status-py.md) /
  [`show_permissions.py`](show-permissions-py.md) to confirm the Agent
  User credentials are still valid.
- **Uncaught `AgentIDNotAvailable`** — Agent User credentials aren't
  configured; run `setup.sh` first.
- **No matches** — the subject substring doesn't appear in the messages
  Graph returns for the given `$top`; widen `top` or double-check the
  substring.

## Related commands

- [`catch_up.py`](catch-up-py.md) — pulls the raw inbox (and watched chats) instead of filtering by subject.
- [`dm.py`](dm-py.md) — send a Teams message instead of reading mail.
- Guides: [Email Workflows](../../../guides/email-workflows.md)
- [Operations scripts index](../index.md#operations)
