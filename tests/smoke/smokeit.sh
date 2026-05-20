#!/usr/bin/env bash
# End-to-end destructive smoke test wrapper for macOS/Linux.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x ".venv/bin/python" ]; then
        PYTHON=".venv/bin/python"
    elif command -v python3.12 >/dev/null 2>&1; then
        PYTHON="python3.12"
    else
        PYTHON="python3"
    fi
fi

exec "$PYTHON" "$SCRIPT_DIR/smokeit.py" "$@"
