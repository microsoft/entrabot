#!/usr/bin/env bash
# EntraClaw status wrapper: ensure the local Python environment exists, then
# run the consolidated Agent Identity status command.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

find_python() {
    local candidate
    for candidate in python3.12 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
            then
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if [ ! -x "$PROJECT_ROOT/.venv/bin/python3" ]; then
    PYTHON="$(find_python || true)"
    if [ -z "$PYTHON" ]; then
        echo "ERROR: Python 3.12+ is required to run EntraClaw status." >&2
        exit 1
    fi
    echo "Creating local Python environment at .venv..." >&2
    "$PYTHON" -m venv "$PROJECT_ROOT/.venv"
fi

VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python3"

if ! "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1
import azure.identity
import entraclaw
PY
then
    echo "Installing EntraClaw status dependencies into .venv..." >&2
    "$VENV_PYTHON" -m pip install -e ".[provisioning]"
fi

if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$PROJECT_ROOT/.env"
    set +a
fi

exec "$VENV_PYTHON" "$PROJECT_ROOT/scripts/show_agent_status.py" "$@"