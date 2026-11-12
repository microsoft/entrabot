# Entrabot hook launcher -- POSIX sh, LF line endings, ASCII, builtins only.
#
# Runs scripts/hooks/<name>.py with an interpreter that exists on THIS machine and
# hands the hook's exit code straight back to Claude Code (0 = allow, 2 = block).
#
# Why this exists: the hooks used to be `python3 scripts/hooks/<name>.py`. A standard
# Windows Python install ships python.exe and py.exe but no python3, so on Windows
# every hook failed to start and the security gates failed OPEN.
#
# Use from .claude/settings.json (shell form, "shell": "bash"). It is SOURCED, so it
# needs no executable bit and no `sh` on PATH (Git Bash on Windows does not always
# put its own bin directory on PATH):
#
#     ENTRABOT_HOOK=require_body_prompt.py; . "$CLAUDE_PROJECT_DIR/scripts/hooks/run-hook.sh"
#
# Direct use also works:  sh scripts/hooks/run-hook.sh require_body_prompt.py
#
# Interpreter order:
#   1. <project>/.venv/bin/python3          (macOS / Linux virtualenv)
#   2. <project>/.venv/Scripts/python.exe   (Windows virtualenv)
#   3. python3, then python, on PATH        (hooks are stdlib-only, so this is safe)
#   4. py -3                                (Windows launcher)
# Anything under a WindowsApps directory is skipped: that is the Microsoft Store
# alias, which is either a stub that only opens the Store or a sandboxed Python.
#
# Fails closed: a bad hook name, a missing hook script, or no interpreter at all
# prints one line to stderr and exits 2, which blocks a PreToolUse tool call.

# Claude Code writes hook input as UTF-8 JSON. Without this, Python on Windows decodes
# piped stdin in the ANSI code page: ordinary characters raise a decode error and a
# non-ASCII profile path arrives as mojibake. UTF-8 mode fixes stdin, stdout, stderr and
# file-system names in one go; the hooks also decode their input themselves.
PYTHONUTF8=1
export PYTHONUTF8
PYTHONIOENCODING=utf-8
export PYTHONIOENCODING

entrabot_hook="${ENTRABOT_HOOK:-${1:-}}"
entrabot_root="${CLAUDE_PROJECT_DIR:-.}"

case "$entrabot_hook" in
    '' | [!A-Za-z0-9_]* | *[!A-Za-z0-9_.]* | *..*)
        echo "entrabot hook launcher: invalid hook name; blocking" >&2
        exit 2
        ;;
esac
case "$entrabot_hook" in
    *.py) ;;
    *)
        echo "entrabot hook launcher: invalid hook name; blocking" >&2
        exit 2
        ;;
esac

entrabot_script="$entrabot_root/scripts/hooks/$entrabot_hook"
if [ ! -f "$entrabot_script" ]; then
    echo "entrabot hook launcher: hook script not found: $entrabot_script; blocking" >&2
    exit 2
fi

for entrabot_py in "$entrabot_root/.venv/bin/python3" "$entrabot_root/.venv/Scripts/python.exe"; do
    if [ -f "$entrabot_py" ] && [ -x "$entrabot_py" ]; then
        exec "$entrabot_py" "$entrabot_script"
    fi
done

for entrabot_name in python3 python; do
    entrabot_py="$(command -v "$entrabot_name")" || entrabot_py=""
    case "$entrabot_py" in
        '' | *WindowsApps*) ;;
        *) exec "$entrabot_py" "$entrabot_script" ;;
    esac
done

if command -v py >/dev/null; then
    exec py -3 "$entrabot_script"
fi

echo "entrabot hook launcher: no Python interpreter found for $entrabot_hook (no .venv in the project, no python3/python/py on PATH); blocking. Run Entrabot setup to create .venv." >&2
exit 2
