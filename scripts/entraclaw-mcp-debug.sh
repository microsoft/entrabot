#!/bin/bash
# Debug wrapper for entraclaw-mcp.
#
# Tees the server's stderr to /tmp/entraclaw-debug.log so we can read it
# AFTER a crash without needing to re-run `claude --debug` in-terminal.
# stderr is ALSO passed through to the parent (Claude Code) so normal
# error reporting stays intact.
#
# Replace .mcp.json's "command" with this script to enable capture:
#   /Volumes/Development HD/entraclaw-identity-research/scripts/entraclaw-mcp-debug.sh
set -u

LOG=/tmp/entraclaw-debug.log
BIN="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/entraclaw-mcp"

# Marker line so we can tell restarts apart in the shared log.
printf '\n===== wrapper start %s pid=%s =====\n' "$(date -u +%FT%TZ)" "$$" >> "$LOG"

# exec replaces this shell with entraclaw-mcp so signals propagate cleanly.
# The 2> >(tee -a ... >&2) pattern copies stderr to the log while still
# forwarding it to Claude Code's stderr.
exec "$BIN" 2> >(tee -a "$LOG" >&2)
