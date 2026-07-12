# `entrabot-mcp-debug.sh`

## Purpose

Debug wrapper around the `entrabot-mcp` binary. It captures the running server's stderr to a log file for post-crash inspection while still forwarding stderr to the parent process (Claude Code or another MCP host), so normal in-terminal error reporting is unaffected. Use it when a crash or hang needs to be diagnosed after the fact rather than watched live in a terminal.

The wrapper also carries a self-reference marker that lets efferent-copy peer discovery recognize it as pointing at the same `entrabot-mcp` entry point that is already running, so pointing `.mcp.json` at the wrapper does not cause the server to discover itself as a peer and spawn a duplicate instance of itself.

## Requirements

- macOS or Linux (the script uses `bash` process substitution — `2> >(tee ...)` — which is not available on Windows; use the Windows MCP registration directly instead).
- A working local install: `.venv/bin/entrabot-mcp` must already exist relative to the repository root (one directory up from `scripts/`). This is created by `setup.sh` / `pip install -e .`.
- No additional environment variables beyond whatever `entrabot-mcp` itself needs to boot (Blueprint/Agent Identity/Agent User config, or MSAL delegated config).

## Usage

Point the `entrabot` server's `command` in `.mcp.json` at the wrapper instead of the real binary:

```json
{
  "mcpServers": {
    "entrabot": {
      "command": "scripts/entrabot-mcp-debug.sh"
    }
  }
}
```

Then start (or restart) your MCP host as usual and tail the log in a separate terminal:

```bash
tail -f /tmp/entrabot-debug.log
```

The log path is fixed — there is no flag or environment variable to relocate it. To change it, edit the `LOG=` assignment near the top of the script.

## Effects

- Appends a `===== wrapper start <UTC timestamp> pid=<pid> =====` marker line to `/tmp/entrabot-debug.log` every time the wrapper starts, so repeated restarts (including the host's own crash-restart loop) are visible as distinct sections in one shared log rather than being interleaved without separators.
- `exec`s into the real `entrabot-mcp` binary, replacing the wrapper's shell process — the running process becomes `entrabot-mcp` itself (not a shell holding it open), so signals (including the host terminating the server on shutdown) propagate the same as they would without the wrapper.
- Every line the server writes to stderr is written to the log file **and** still passed through to the parent's stderr; stdout (the actual MCP JSON-RPC transport) is untouched.
- The wrapper performs no redaction of its own. It is a plain `tee`, so the log file contains byte-for-byte whatever `entrabot-mcp` writes to stderr. `entrabot-mcp` follows the project convention of overriding `__repr__` on token-bearing objects so access tokens do not appear in normal log output, and the debug log inherits that same protection — but if a future code path logs a raw string containing a secret, this wrapper will faithfully copy it to disk. Treat `/tmp/entrabot-debug.log` as sensitive-adjacent: it is not encrypted, is not automatically rotated or deleted, and should be cleaned up after use.

## Exit behavior

The wrapper's own exit code is whatever `entrabot-mcp` exits with, because `exec` replaces the shell process rather than waiting on a child — there is no separate wrapper-level exit path. If `.venv/bin/entrabot-mcp` does not exist at the resolved path, `exec` fails immediately and the shell reports a non-zero exit status (typically 127) before the log marker's timestamp is ever tailed by a running server.

## Related commands

- [`show-agent-status.py`](../operations/show-agent-status-py.md) — checks the same authentication config this wrapper's underlying server depends on, useful before assuming a captured crash is MCP-transport-specific.
- [MCP Hosts and Transports](../../../platform-docs/mcp-hosts-and-transports.md) and [MCP Runtime — Efferent-copy observer mechanism](../../../architecture/mcp-runtime.md#efferent-copy-observer-mechanism) — background on peer discovery and why a wrapper needs the self-reference marker at all.
- [Scripts reference: Diagnostics](../index.md#diagnostics) — the other read-only diagnostics in this set.
