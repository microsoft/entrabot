#!/usr/bin/env bash
# Start your Entrabot agent on macOS or Linux:   ./start-entrabot.sh
#
# It starts Claude Code in this folder with the Entrabot channel switched on, so the agent
# hears Teams messages as they arrive. Anything you add is passed on to Claude Code:
#     ./start-entrabot.sh --resume
#
# Run ./scripts/setup.sh once before the first start.
set -u

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# 1. Has setup been run in this folder?
if [ ! -x ".venv/bin/entrabot-mcp" ] || [ ! -f ".mcp.json" ]; then
    echo "Entrabot is not set up in this folder yet. Run ./scripts/setup.sh first." >&2
    exit 1
fi

# 2. Find Claude Code: on PATH, else where its own installer puts it.
CLAUDE="$(command -v claude || true)"
if [ -z "$CLAUDE" ]; then
    for candidate in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude"; do
        if [ -x "$candidate" ]; then
            CLAUDE="$candidate"
            break
        fi
    done
fi
if [ -z "$CLAUDE" ]; then
    echo "Claude Code is not installed. Install it from https://code.claude.com/docs/en/setup" >&2
    echo "and run this again." >&2
    exit 1
fi

# 3. Say what Claude Code asks the first time, in the terminal the questions appear in.
echo "Starting your Entrabot agent in Claude Code."
echo "First time? Claude Code asks whether to trust this folder, whether to use"
echo "the \"entrabot\" MCP server, and whether to load a development channel."
echo "Answer yes to each. Then say:  Who are you?"
echo

# 4. Start Claude Code in place of this script.
exec "$CLAUDE" --dangerously-load-development-channels server:entrabot "$@"
