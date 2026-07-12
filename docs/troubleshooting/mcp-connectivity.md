# MCP connectivity

## The MCP host cannot start `entrabot`

Entrabot is a local stdio server. The host must launch the executable and keep
its stdin/stdout pipe open; there is no Entrabot HTTP listener.

Confirm the configured command exists:

=== "macOS or Linux"

    ```bash
    test -x .venv/bin/entrabot-mcp && .venv/bin/python -c "from entrabot import config; print(config.__file__)"
    ```

=== "Windows"

    ```powershell
    Test-Path .\.venv\Scripts\entrabot-mcp.exe
    .\.venv\Scripts\python.exe -c "from entrabot import config; print(config.__file__)"
    ```

The printed module path must belong to the repository the host is opening.
Use an absolute MCP command path when the host starts from a different working
directory.

## The server imports code from another worktree

Editable installs record a source path. Never run an editable install in a
worktree with the parent repository's `.venv`; that silently points the parent
environment at the worktree.

Each worktree that needs an install must have its own `.venv`. Verify the
active target:

```bash
.venv/bin/python -c "from entrabot import config; print(config.__file__)"
```

If the path is wrong, recreate the affected environment in the intended
repository and update the MCP command path.

## Tools load, but authentication fails

Transport health and authentication health are separate:

- A launch error, EOF, malformed JSON-RPC, or missing executable is an MCP
  transport problem.
- `UNAUTHENTICATED`, token-exchange errors, consent errors, and Graph 401/403
  responses are authentication or authorization problems.

Run the status command, then call `whoami`. If both work but a specific tool
fails, troubleshoot that resource rather than the stdio transport.

## Claude Code receives no background Teams or email messages

Launch with the channel flag:

```bash
claude --dangerously-load-development-channels server:entrabot
```

The MCP server name must match `.mcp.json`. Without the flag, tools can still
work while `notifications/claude/channel` is not surfaced as a new turn.

Copilot CLI and other non-channel hosts do not receive an out-of-band model
turn. Their `send_teams_message` path auto-waits and returns sponsor replies
inline instead.

## The server disconnects or exits without enough diagnostics

On macOS or Linux, temporarily point the MCP `command` to:

```text
scripts/entrabot-mcp-debug.sh
```

The wrapper:

- Copies stderr to `/tmp/entrabot-debug.log` while preserving normal stderr.
- Leaves stdout untouched for MCP JSON-RPC.
- Contains the shipped `# entrabot-self-ref-target:` marker so efferent-copy
  discovery recognizes the wrapper as the running Entrabot executable instead
  of spawning a duplicate.

Inspect the log:

```bash
tail -f /tmp/entrabot-debug.log
```

Restore the normal `.venv/bin/entrabot-mcp` command after diagnosis and remove
the unrotated diagnostic log:

```bash
rm -f /tmp/entrabot-debug.log
```

Treat the log as sensitive-adjacent even though normal Entrabot logging redacts
credential-bearing values.

## Optional observer discovery may be involved

Efferent-copy observer dispatch is off by default. If it is enabled in the
environment, isolate it during transport diagnosis:

```bash
EFFERENT_COPY_DISABLE=1 .venv/bin/entrabot-mcp
```

For an MCP host, set `EFFERENT_COPY_DISABLE=1` in the server environment and
restart the host. If the problem persists, the optional observer path is not
the cause.

## Debug output corrupts the MCP connection

Stdout is the stdio JSON-RPC transport. Do not add `print()` diagnostics to
stdout in the MCP server. Use structured logging, which writes to stderr and
the rotating `entrabot.log`, or use the debug wrapper above.

See [MCP Hosts and Transports](../platform-docs/mcp-hosts-and-transports.md),
[MCP Runtime](../architecture/mcp-runtime.md), and
[Debug Wrapper Reference](../reference/scripts/diagnostics/entrabot-mcp-debug-sh.md).
