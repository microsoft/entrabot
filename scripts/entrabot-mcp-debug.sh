#!/bin/bash
# Debug wrapper for entrabot-mcp.
#
# Tees the server's stderr to /tmp/entrabot-debug.log so we can read it
# AFTER a crash without needing to re-run `claude --debug` in-terminal.
# stderr is ALSO passed through to the parent (Claude Code) so normal
# error reporting stays intact.
#
# Replace .mcp.json's "command" with this script to enable capture:
#   scripts/entrabot-mcp-debug.sh
#
# entrabot-self-ref-target: ../.venv/bin/entrabot-mcp
#   ^ Tells efferent_copy._is_self_referential_peer that this wrapper
#     execs into the same entry point as the running entrabot-mcp,
#     so peer discovery skips us and avoids spawning a duplicate
#     entrabot-mcp. Without this marker, swapping .mcp.json's command
#     to this wrapper reintroduces the self-spawn cascade originally
#     fixed by PR #36 / commit 8a00939. See Learning #45 for the writeup.
set -u

LOG=/tmp/entrabot-debug.log
BIN="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/entrabot-mcp"

# Marker line so we can tell restarts apart in the shared log.
printf '\n===== wrapper start %s pid=%s =====\n' "$(date -u +%FT%TZ)" "$$" >> "$LOG"

# exec replaces this shell with entrabot-mcp so signals propagate cleanly.
# The 2> >(tee -a ... >&2) pattern copies stderr to the log while still
# forwarding it to Claude Code's stderr.
exec "$BIN" 2> >(tee -a "$LOG" >&2)
