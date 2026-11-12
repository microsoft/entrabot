# Entrabot enforcement registry

Single source of truth for every gate that enforces agent-body rules
mechanically. The pattern: prompt-text rules drift; mechanical
enforcement doesn't. Each row in the registry below names ONE gate
and points at exactly the file that implements it.

This file matters because there are now three places enforcement can
live (harness, server-side, process-boot) — and without a registry it
is too easy to grep one of them, miss the others, and conclude a rule
is unenforced when it actually is.

## Layers

| Layer | Where | Host coverage | Storage coverage |
|---|---|---|---|
| Harness (Claude Code hooks) | `scripts/hooks/*.py`, started by `scripts/hooks/run-hook.sh`, wired in `.claude/settings.json` | Claude Code only (macOS, Linux, Windows) | n/a |
| Server-side (MCP tool wrappers) | `src/entrabot/mcp_server.py` + `src/entrabot/tools/*.py` | All hosts that speak MCP | All backends (Local/Blob) |
| Process-boot (fail-closed gates) | `src/entrabot/singleton.py`, `src/entrabot/tools/files.py` | All hosts | n/a (in-process) |

The architectural preference is **server-side over harness** wherever
feasible: server-side gates apply on Copilot CLI, Codex, Cursor, and
any other host that speaks MCP — harness gates only apply on Claude
Code. `share_file`'s two-gate sponsor check (Learning #59) is the
canonical example of the server-side pattern; the two new gates
shipped in this PR (placeholder discipline, commitment-language)
follow it.

## Registry

| Gate | Layer | Trigger | Action | File | Bypass | Failure mode reference |
|---|---|---|---|---|---|---|
| `inject_body_prompt` | Harness | `SessionStart` | Inject `prompts/agent_system.md` into LLM context | `scripts/hooks/inject_body_prompt.py` | (none) | Body prompt missing → reduced rule context |
| `block_local_memory_write` | Harness | `PreToolUse(Write\|Edit\|NotebookEdit)` | Block writes to `~/.claude/projects/*/memory/**` | `scripts/hooks/block_local_memory_write.py` | `ENTRABOT_KEEP_MEMORY_LOCAL=true` | Routes memory writes to persona-sati |
| `require_body_prompt` | Harness | `PreToolUse(send_email\|send_teams_message\|send_card\|add_teams_member\|create_chat\|delete_teams_message)` | Block until body prompt loaded this session | `scripts/hooks/require_body_prompt.py` | `ENTRABOT_SKIP_BODY_PROMPT_GATE=true` | High-blast-radius tool used without rule context |
| persona-sati auto-allow | Harness | `PreToolUse(mcp__persona-sati__*)` | Print an `allow` permission decision so mind tools never prompt | `.claude/settings.json` (inline `echo`) | (none) | Permission prompts interrupting the cognition protocol |
| singleton flock | Process-boot | `main()` startup | Refuse second instance (Learning #56) | `src/entrabot/singleton.py` | (none) | Two MCP servers contend for `~/.entrabot/` state |
| `share_file` two-gate | Server-side | `share_file` invocation | Requester must be sponsor AND member of cited chat (Learning #59) | `src/entrabot/tools/files.py:share_file` | (none) | LLM fabricates sponsor email + chat to share data |
| Placeholder discipline | Server-side | `send_teams_message` invocation, message > ~200 chars | Block if no `post_thinking_placeholder` for this chat in last 5 min | `src/entrabot/mcp_server.py:send_teams_message` | `ENTRABOT_SKIP_PLACEHOLDER_CHECK=true` | Substantive Teams send without "thinking…" ack |
| Commitment-language | Server-side | `send_teams_message` invocation, message contains commitment phrase | WARN (not block): no recent `add_promise` found | `src/entrabot/mcp_server.py:send_teams_message` | `ENTRABOT_SKIP_COMMITMENT_CHECK=true` | Agent commits to "I'll do X later" without persistent promise |

## How harness hooks are launched (all platforms)

Every Python hook is wired the same way in `.claude/settings.json`:

```json
{
  "type": "command",
  "command": "ENTRABOT_HOOK=require_body_prompt.py; . \"$CLAUDE_PROJECT_DIR/scripts/hooks/run-hook.sh\"",
  "shell": "bash",
  "onFailure": "block"
}
```

`run-hook.sh` is a POSIX `sh` launcher (LF line endings, builtins only). It is
*sourced*, so it needs no executable bit and no `sh` on `PATH`. It picks the
first interpreter that exists and `exec`s the hook, so the hook's own exit code
(0 = allow, 2 = block) is what Claude Code sees:

1. `<project>/.venv/bin/python3` (macOS, Linux)
2. `<project>/.venv/Scripts/python.exe` (Windows)
3. `python3`, then `python`, on `PATH` — valid because the hooks are stdlib-only
4. `py -3` (the Windows launcher)

Anything under a `WindowsApps` directory is skipped: that is the Microsoft Store
alias, which is a stub that only opens the Store (or the sandboxed Store build).

Why not `python3 scripts/hooks/<name>.py`, as it used to be: a standard Windows
Python install has `python.exe` and `py.exe` but no `python3`. On Windows every
hook failed to start, and a hook that fails to start does not block — the gates
failed open.

Fail-closed rules:

- The launcher exits 2 (block) for a bad hook name, a missing hook script, or no
  interpreter at all.
- The two gate scripts only ever exit 0 (allow) or 2 (block). Input they cannot
  decode or parse, and any other unexpected error, is a block with one line on
  stderr, never exit 1. Exit 1 is "the hook broke", which Claude Code older than
  2.1.295 lets through.
- Hook input is UTF-8. The launcher exports `PYTHONUTF8=1` and
  `PYTHONIOENCODING=utf-8`, and each hook also reads `sys.stdin.buffer` and decodes
  UTF-8 itself. Without both, Python on Windows decodes stdin as cp1252: ordinary
  characters (curly quotes, emoji) crash the hook, and a non-ASCII profile path
  (`C:\Users\José`) turns into mojibake that no longer matches the real path, which
  let memory writes through and blocked every send.
- The two `PreToolUse` gates set `"onFailure": "block"`, so a hook that cannot
  start, crashes, or times out blocks the tool call instead of letting it
  through. This needs Claude Code 2.1.295 or later.
- `"shell": "bash"` pins the hooks to `sh -c` on macOS/Linux and Git Bash on
  Windows. **On Windows, Claude Code needs Git for Windows for these hooks.**
  Without it the gates block every gated tool call (fail closed) and the body
  prompt is not injected; install Git with
  `scripts\prereqs-windows.ps1 -WithGit`.
- `inject_body_prompt` is a `SessionStart` hook, which cannot block. If it does
  not run, `require_body_prompt` still keeps the high-blast-radius tools shut
  until the body prompt has been read.
- The persona-sati auto-allow hook is a bare `echo '<json>'`. Git Bash, `sh` and
  PowerShell all run it identically, so it is deliberately not pinned to a shell.

Tests: `tests/hooks/test_run_hook_launcher.py` runs the launcher with a real
`sh` (venv layouts, PATH fallbacks, Store alias, nothing found, exit-code
passthrough, a project path with a space) and executes every command string in
`.claude/settings.json` exactly as written. The Windows CI job runs the launcher
through Git Bash.

## Known coverage gaps

### CLI commitment-language detection — unenforced

Commitment phrases in CLI output (the agent talking to its operator
in the host terminal, not in Teams) are NOT detected by the
server-side commitment hook because the MCP server never sees CLI
text — only tool calls. Significant design conversation happens in
CLI; commitments made there ("I'll do X at the next pause") don't
become durable promises today.

Future fix paths considered:

1. **`Stop` hook (Claude Code only) reading the transcript.** Fast
   to implement but couples enforcement to one host. Not the
   architecturally correct default — drops Copilot CLI, Codex,
   Cursor, and direct MCP users.
2. **Push commitment discipline upstream via the body prompt.** Rely
   on the body-prompt rule that the agent should always
   `add_promise` on "I'll do X" language, regardless of channel.
   Today this drifts (the lapse on 2026-05-04 was exactly CLI
   commitment without `add_promise`).
3. **Body-prompt enrichment.** Add a TL;DR checklist to the top of
   `prompts/anatomy/channel-discipline.md` so the
   commitment-on-CLI rule is more salient.
4. **Per-host shims.** A small Stop-equivalent hook for each host
   that supports them (Claude Code today; others future). Defers
   the work and accepts duplication.

Tracked in `docs/project/status.md` under "Known Issues (Open)".

## Bypass env vars (summary)

| Env var | Effect |
|---|---|
| `ENTRABOT_KEEP_MEMORY_LOCAL=true` | Allow `Write`/`Edit` to `~/.claude/projects/*/memory/**` |
| `ENTRABOT_SKIP_BODY_PROMPT_GATE=true` | Skip body-prompt-loaded check on high-blast tools |
| `ENTRABOT_SKIP_PLACEHOLDER_CHECK=true` | Skip placeholder discipline for this MCP server boot |
| `ENTRABOT_SKIP_COMMITMENT_CHECK=true` | Skip commitment-language scan |
| `ENTRABOT_PLACEHOLDER_GRACE_SECONDS=N` | Grace window (seconds) for placeholder check (default 300) |

Bypass env vars are deliberately **server-side environment**, not
MCP tool parameters. LLMs that can flip a parameter will flip it to
skip enforcement. Operators who can flip an env var have full host
access already, so the gate they bypass was never the security
boundary.

## Adding a new gate

1. Decide the layer. Server-side first. Harness only when the trigger
   is a Claude Code surface that's not visible inside MCP (e.g.
   transcript text).
2. Add the gate to the registry table above with a row covering all
   seven columns.
3. If server-side, add the implementation in `mcp_server.py` (or the
   relevant `tools/*.py` module) and a JSON-error-blob catch in the
   wrapper that surfaces it. Mirror the existing `FilesError` and
   `MissingPlaceholderError` patterns.
4. If harness, add the script under `scripts/hooks/` (stdlib only; read
   `sys.stdin.buffer` and decode UTF-8; exit 0 to allow and 2 to block, never
   1, including on input it cannot parse) and wire the trigger in
   `.claude/settings.json` through `run-hook.sh`, with `"shell": "bash"`
   and, for a gate, `"onFailure": "block"`. See "How harness hooks are
   launched" above.
5. Add tests under `tests/` (top-level `test_*.py`, or under
   `tests/hooks/` for harness scripts).
6. If the gate has a known coverage gap (e.g. only fires on certain
   hosts or certain channels), add a "Known coverage gaps" entry
   above and cross-reference it from
   `docs/project/status.md`.
