#!/usr/bin/env bash
# Entrabot setup for macOS and Linux.
#
# This file only gets Python and a virtual environment in place, then starts the setup
# wizard (python -m entrabot.winsetup), the same wizard Windows runs from
# scripts\setup-windows.cmd. The wizard asks a few questions (each has a default), shows
# what it is about to do, and only then changes anything. Running it again is the repair.
#
# Every flag this script took before the wizard still works; see --help. Flags are passed
# to the wizard as arguments and never evaluated: a value can hold any character.
#
# Works with the bash 3.2 that ships with macOS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$PROJECT_ROOT/.venv/bin/python"

say()  { printf '%s\n' "$*"; }
warn() { printf 'NOTE: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

show_help() {
    cat <<'HELP'
Usage: ./scripts/setup.sh [OPTIONS]

Sets up an Entrabot agent on this Mac or Linux computer, or repairs the one that is here.
With no options it asks; press Enter to accept each suggested answer. Nothing is changed
until you confirm the review screen. Add --yes to run without questions.
A value may follow its option after "=" or after a space: --tenant=NAME or --tenant NAME.

Which agent:
  --new                    Create a new agent. Give it a name with --with-upn-suffix.
  --use-blueprint=APP_ID   Join an agent that already exists (a second computer), by its
                           Blueprint App ID. This computer gets its own certificate; every
                           other computer's is kept.
  --with-upn-suffix=NAME   Short name: the agent becomes entrabot-agent-NAME@<your domain>.
  --agent-user-upn=UPN     Use exactly this address for the agent.
  --sponsor=EMAIL          Who the agent takes instructions from in Teams. Repeat it, or
                           separate addresses with commas. They are added as sponsors in the
                           directory. Default: you.
  --teams-user=EMAIL       The older name for --sponsor.

Signing in:
  --tenant=NAME_OR_ID      Sign in to this tenant.
  --device-code            Sign in with a code instead of a browser window.
  --switch-user            Ask for a new sign-in even when an earlier one still works.

Memory:
  --keep-memory-local      Keep the agent's memory on this computer (the default on a first
                           run; on a later run, moves it back from Azure).
  --use-cloud-memory       Keep it in Azure Blob Storage, in your subscription.
  --with-storage-account=NAME, --with-container=NAME, --create-new-storage,
  --location=REGION, --subscription=ID_OR_NAME
                           Where the cloud memory goes. Only with --use-cloud-memory.
  A later run with neither memory flag keeps the setting it finds.

Word, Excel and OneDrive files:
  --configure-a365-work-iq Switch them on (Microsoft Agent 365 Work IQ). Nothing else to
                           install. The agent needs a Microsoft 365 Copilot licence.
  --with-a365-work-iq      The older name for the same thing.

Certificates:
  --remove-all-existing-certs
                           Replace EVERY certificate on the agent's Blueprint, other
                           computers' included, with a new one for this computer. Every
                           other computer stops signing in until setup is run on it again.
                           Asks first at a terminal; without one the flag is the consent.
                           Not with --new. Without this flag setup never removes a
                           certificate.
  --rotate-cert            Replace this computer's certificate only. The current one stays
                           in use until the new one is proven to work.

Other:
  --hosts=LIST             Assistants to register with: claude,copilot (default: both).
  --skip-smoke             Skip the end-to-end test at the end.
  --yes, -y                Never ask. A missing answer is an error (exit 2).
  --verbose                Show everything the helper scripts print.
  --log-file=PATH          Where to write the log (default: ~/.entrabot/logs).
  --diagnose               Health check of the setup that is here. Changes nothing.
  --status                 Show the agent's status (./status.sh). Other options after it
                           are passed on, for example: ./scripts/setup.sh --status --json
  --help, -h               Show this help.

Exit codes: 0 done, 1 failed, 2 usage or a missing answer under --yes, 3 set up but with
something to check (said on the last screen), 130 cancelled.

Persona and long-term memory (persona-sati, optional):
  If your assistant uses persona-sati, copy docs/clients/persona-sati-host-bootstrap.md
  into the assistant's global instruction file (Claude Code: ~/.claude/CLAUDE.md).
  FastMCP instructions are not enough: assistants do not put them into the model's
  prompt. A new session then calls bootstrap_session() first, and observe, reflect and
  recall as it works. When that answers mind_contract_available=false the agent works
  without the persona and does not impersonate it.

To disconnect this computer, or delete the agent: ./scripts/teardown.sh --help
HELP
}

# ── --status: nothing here is needed for it ──────────────────────────────────

STATUS=false
STATUS_ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--status" ]; then
        STATUS=true
    else
        STATUS_ARGS+=("$arg")
    fi
done
if [ "$STATUS" = true ]; then
    exec "$PROJECT_ROOT/status.sh" ${STATUS_ARGS[@]+"${STATUS_ARGS[@]}"}
fi

# ── Flags: translated for the wizard, one for one ────────────────────────────

COMMAND="setup"
ARGS=()
value_of() { printf '%s' "${1#*=}"; }

# An option's value may follow it after a space as well as after "=":
#   --tenant contoso.com   is   --tenant=contoso.com
VALUE_OPTIONS=" --use-blueprint --with-upn-suffix --agent-user-upn --sponsor --teams-user --tenant --with-storage-account --with-container --location --subscription --a365-agent-name --hosts --log-file "
GIVEN=()
waiting_for_value=""
for arg in "$@"; do
    if [ -n "$waiting_for_value" ]; then
        case "$arg" in
            -*) die "$waiting_for_value needs a value, for example $waiting_for_value=VALUE" 2 ;;
        esac
        GIVEN+=("$waiting_for_value=$arg")
        waiting_for_value=""
    elif [ "${VALUE_OPTIONS#*" $arg "}" != "$VALUE_OPTIONS" ]; then
        waiting_for_value="$arg"
    else
        GIVEN+=("$arg")
    fi
done
if [ -n "$waiting_for_value" ]; then
    die "$waiting_for_value needs a value, for example $waiting_for_value=VALUE" 2
fi

for arg in ${GIVEN[@]+"${GIVEN[@]}"}; do
    case "$arg" in
        --help|-h)                   show_help; exit 0 ;;
        --diagnose)                  COMMAND="diagnose" ;;
        --rotate-cert)               COMMAND="rotate-cert" ;;
        --new)                       ARGS+=(--new-chain) ;;
        --use-blueprint=*)           ARGS+=(--use-blueprint "$(value_of "$arg")") ;;
        --with-upn-suffix=*)         ARGS+=(--upn-suffix "$(value_of "$arg")") ;;
        --agent-user-upn=*)          ARGS+=(--agent-user-upn "$(value_of "$arg")") ;;
        --sponsor=*|--teams-user=*)  ARGS+=(--sponsor "$(value_of "$arg")") ;;
        --tenant=*)                  ARGS+=(--tenant "$(value_of "$arg")") ;;
        --device-code)               ARGS+=(--device-code) ;;
        --switch-user)               ARGS+=(--switch-user) ;;
        --use-cloud-memory)          ARGS+=(--memory cloud) ;;
        --keep-memory-local)         ARGS+=(--memory local) ;;
        --with-storage-account=*)    ARGS+=(--storage-account "$(value_of "$arg")") ;;
        --with-container=*)          ARGS+=(--container "$(value_of "$arg")") ;;
        --create-new-storage)        ARGS+=(--create-new-storage) ;;
        --location=*)                ARGS+=(--location "$(value_of "$arg")") ;;
        --subscription=*)            ARGS+=(--subscription "$(value_of "$arg")") ;;
        --configure-a365-work-iq)    ARGS+=(--configure-a365-work-iq) ;;
        --with-a365-work-iq)
            warn "--with-a365-work-iq is now the same as --configure-a365-work-iq: setup switches Word, Excel and OneDrive files on itself, with no Agent 365 CLI to install."
            ARGS+=(--configure-a365-work-iq)
            ;;
        --a365-agent-name=*)
            warn "--a365-agent-name is no longer used and is ignored: Work IQ uses the agent's own Blueprint."
            ;;
        --remove-all-existing-certs) ARGS+=(--remove-all-existing-certs) ;;
        --hosts=*)                   ARGS+=(--hosts "$(value_of "$arg")") ;;
        --skip-smoke)                ARGS+=(--skip-smoke) ;;
        --yes|-y)                    ARGS+=(--yes) ;;
        --verbose)                   ARGS+=(--verbose) ;;
        --log-file=*)                ARGS+=(--log-file "$(value_of "$arg")") ;;
        *)
            printf 'ERROR: Unknown option: %s\n\n' "$arg" >&2
            show_help >&2
            exit 2
            ;;
    esac
done

# The health check and certificate rotation are for a setup that is already here. They
# install nothing, so without a virtual environment there is nothing for them to look at.
if [ "$COMMAND" != "setup" ] && [ ! -x "$VENV_PY" ]; then
    die "This computer has not been set up yet. Run ./scripts/setup.sh first."
fi
# The maintenance commands take only these of the flags above.
if [ "$COMMAND" != "setup" ]; then
    KEPT=()
    skip_next=false
    for item in ${ARGS[@]+"${ARGS[@]}"}; do
        if [ "$skip_next" = true ]; then
            KEPT+=("$item")
            skip_next=false
            continue
        fi
        case "$item" in
            --log-file) KEPT+=("$item"); skip_next=true ;;
            --yes|--verbose) [ "$COMMAND" = "rotate-cert" ] && KEPT+=("$item") ;;
        esac
    done
    ARGS=(${KEPT[@]+"${KEPT[@]}"})
fi

# ── Python 3.12 or later ─────────────────────────────────────────────────────

is_new_enough() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
}

find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.14 python3; do
        if command -v "$candidate" >/dev/null && is_new_enough "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

python_missing() {
    say "Entrabot needs Python 3.12 or later, and it was not found on this computer."
    say ""
    if [ "$(uname -s)" = "Darwin" ]; then
        say "On a Mac, this installs it (with git and the Azure CLI) through Homebrew:"
        say "    ./scripts/prereqs-macos.sh"
        say "or, by hand:  brew install python@3.12"
    else
        say "Install it with your distribution's package manager, for example:"
        say "    sudo apt install python3.12 python3.12-venv      (Debian, Ubuntu)"
        say "    sudo dnf install python3.12                      (Fedora, RHEL)"
    fi
    say ""
    say "Then run ./scripts/setup.sh again."
    exit 1
}

# ── The virtual environment ──────────────────────────────────────────────────

# What the installed packages were built from. A changed pyproject.toml means new
# requirements, so the install runs again; otherwise a second run starts at once.
stamp_of_project() { cksum < "$PROJECT_ROOT/pyproject.toml" | tr -d ' \t'; }
STAMP_FILE="$PROJECT_ROOT/.venv/.entrabot-setup-stamp"

venv_is_ready() {
    [ -x "$VENV_PY" ] || return 1
    [ -f "$STAMP_FILE" ] && [ "$(cat "$STAMP_FILE")" = "$(stamp_of_project)" ] || return 1
    # The packages setup itself imports. Anything missing means the install did not finish.
    # Asked as a question, so a missing one prints nothing and nothing has to be hidden.
    "$VENV_PY" -c '
import importlib.util, sys
def there(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError:
        return False
wanted = ("azure.identity", "cryptography", "entrabot.winsetup", "httpx", "keyring", "requests")
sys.exit(0 if all(there(name) for name in wanted) else 1)'
}

build_venv() {
    local python
    if [ ! -x "$VENV_PY" ]; then
        python="$(find_python)" || python_missing
        say "Creating Entrabot's Python environment (.venv)..."
        if ! "$python" -m venv "$PROJECT_ROOT/.venv"; then
            say ""
            say "Python could not create a virtual environment."
            if [ "$(uname -s)" = "Linux" ]; then
                say "On Debian and Ubuntu that needs one more package:  sudo apt install python3-venv"
            fi
            die "Fix the problem above, then run ./scripts/setup.sh again."
        fi
    elif ! is_new_enough "$VENV_PY"; then
        die "The Python in .venv is older than 3.12. Move .venv aside and run ./scripts/setup.sh again."
    fi
    say "Installing what setup needs into .venv (a minute or two the first time)..."
    # Python 3.12 no longer puts setuptools and wheel in a new environment; an editable
    # install from pyproject.toml needs both.
    "$VENV_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip setuptools wheel \
        || die "pip could not be updated in .venv (see above). Check the network connection, then run ./scripts/setup.sh again."
    (cd "$PROJECT_ROOT" && "$VENV_PY" -m pip install --quiet --disable-pip-version-check -e ".[provisioning]") \
        || die "Entrabot's packages could not be installed (see above). Check the network connection, then run ./scripts/setup.sh again."
    stamp_of_project > "$STAMP_FILE"
}

if [ "$COMMAND" = "setup" ] && ! venv_is_ready; then
    build_venv
fi

# ── The wizard ───────────────────────────────────────────────────────────────

cd "$PROJECT_ROOT"
exec "$VENV_PY" -m entrabot.winsetup "$COMMAND" --project-root "$PROJECT_ROOT" \
    ${ARGS[@]+"${ARGS[@]}"}
