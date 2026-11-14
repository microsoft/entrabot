#!/usr/bin/env bash
# Entrabot teardown for macOS and Linux.
#
# By default this DISCONNECTS THIS COMPUTER and nothing else: it starts the same teardown
# Windows runs (python -m entrabot.winsetup teardown), which takes this computer's
# certificate off the agent's Blueprint, deletes it here with its private key, removes the
# entrabot entry from the assistants' configs, and backs up and removes .env and
# .entrabot-state.json. The agent, its memory and every other computer are untouched.
# --delete-agent also deletes the agent from the tenant.
#
# The older, tenant-wide teardown is still here for test tenants and wrappers. It runs when
# --tenant-wide or one of its own flags is given (--agent-user-upn, --dry-run,
# --preserve-provisioner, --preserve-local-state) and removes everything setup creates:
#   1. Agent User (must go first — child of Agent Identity)
#   2. Agent Identity (service principal)
#   3. Blueprint (app registration — also deletes BlueprintPrincipal)
#   4. Provisioner app registration: the one recorded for this machine in
#      .entrabot-state.json, by its application ID, never one found by name
#   5. Local state (.env, .entrabot-state.json, key store entries)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

VENV_PY="$PROJECT_ROOT/.venv/bin/python3"

TARGET_AGENT_USER_UPNS=()
ASSUME_YES=false
DRY_RUN=false
DELETE_CLOUD_STORAGE=false
PRESERVE_PROVISIONER=false
PRESERVE_LOCAL_STATE=false
SHOW_HELP=false
UNKNOWN_ARGUMENT=false
TARGETED_DEPROVISION_DONE=false
# Which teardown runs. The tenant-wide one only when one of its own flags asks for it.
TENANT_WIDE=false
TENANT_WIDE_FLAG=""
WIZARD_ARGS=()
WIZARD_FLAG=""

# A value without its leading and trailing whitespace.
trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    printf '%s' "${value%"${value##*[![:space:]]}"}"
}

# An option's value may follow it after a space as well as after "=":
#   --log-file /tmp/t.log   is   --log-file=/tmp/t.log
GIVEN=()
WAITING_FOR_VALUE=""
for arg in "$@"; do
    if [ -n "$WAITING_FOR_VALUE" ]; then
        case "$arg" in
            -*)
                echo "ERROR: $WAITING_FOR_VALUE needs a value, for example $WAITING_FOR_VALUE=VALUE" >&2
                exit 2
                ;;
        esac
        GIVEN+=("$WAITING_FOR_VALUE=$arg")
        WAITING_FOR_VALUE=""
    elif [ "$arg" = "--log-file" ] || [ "$arg" = "--agent-user-upn" ]; then
        WAITING_FOR_VALUE="$arg"
    else
        GIVEN+=("$arg")
    fi
done
if [ -n "$WAITING_FOR_VALUE" ]; then
    echo "ERROR: $WAITING_FOR_VALUE needs a value, for example $WAITING_FOR_VALUE=VALUE" >&2
    exit 2
fi

for arg in ${GIVEN[@]+"${GIVEN[@]}"}; do
    case $arg in
        --delete-agent|--purge-data|--keep-setup-credential|--verbose)
            WIZARD_ARGS+=("$arg")
            WIZARD_FLAG="$arg"
            ;;
        --log-file=*)
            WIZARD_ARGS+=(--log-file "${arg#--log-file=}")
            WIZARD_FLAG="--log-file"
            ;;
        --tenant-wide)
            TENANT_WIDE=true
            TENANT_WIDE_FLAG="$arg"
            ;;
        --agent-user-upn=*)
            TENANT_WIDE=true
            TENANT_WIDE_FLAG="--agent-user-upn"
            # Comma-separated. Each UPN is trimmed and otherwise kept exactly as given: it is only
            # ever passed on as one argument.
            IFS=',' read -ra UPN_PARTS <<< "${arg#--agent-user-upn=}"
            for upn in ${UPN_PARTS[@]+"${UPN_PARTS[@]}"}; do
                upn="$(trim "$upn")"
                if [ -n "$upn" ]; then
                    TARGET_AGENT_USER_UPNS+=("$upn")
                fi
            done
            ;;
        --yes|-y)
            ASSUME_YES=true
            ;;
        --dry-run)
            TENANT_WIDE=true
            TENANT_WIDE_FLAG="$arg"
            DRY_RUN=true
            ;;
        --delete-cloud-storage)
            TENANT_WIDE=true
            TENANT_WIDE_FLAG="$arg"
            DELETE_CLOUD_STORAGE=true
            ;;
        --preserve-provisioner)
            TENANT_WIDE=true
            TENANT_WIDE_FLAG="$arg"
            PRESERVE_PROVISIONER=true
            ;;
        --preserve-local-state)
            TENANT_WIDE=true
            TENANT_WIDE_FLAG="$arg"
            PRESERVE_LOCAL_STATE=true
            ;;
        --help|-h)
            SHOW_HELP=true
            ;;
        *)
            echo "ERROR: Unknown argument: $arg" >&2
            SHOW_HELP=true
            UNKNOWN_ARGUMENT=true
            ;;
    esac
done

if [ "$SHOW_HELP" = true ]; then
    echo "Usage: ./scripts/teardown.sh [OPTIONS]"
    echo ""
    echo "Disconnects THIS computer from its agent. The agent itself, its memory and every"
    echo "other computer that uses it are kept. It says what it will do and asks first."
    echo ""
    echo "Options:"
    echo "  --delete-agent         Also delete the agent from the tenant, for every computer."
    echo "                         Cannot be undone."
    echo "  --purge-data           Also delete Entrabot's data folder on this computer"
    echo "                         (~/.entrabot: memory kept here, logs, its private Azure CLI)."
    echo "  --keep-setup-credential"
    echo "                         Keep this computer's setup (Provisioner) certificate, for"
    echo "                         setting up again soon. By default it is retired once no"
    echo "                         other Entrabot agent here needs it."
    echo "  --yes, -y              Don't ask. Only what the other flags name is removed."
    echo "  --verbose              Show everything the helper scripts print."
    echo "  --log-file=PATH        Where to write the log (default: ~/.entrabot/logs)."
    echo ""
    echo "Exit codes: 0 done or nothing to do, 1 finished with something left to do,"
    echo "2 usage, 130 cancelled."
    echo ""
    echo "Tenant-wide teardown, for test tenants and wrappers. Any of these flags selects it."
    echo "It deletes the agent's whole chain and, unless told otherwise, this computer's"
    echo "Provisioner app and local state. It cannot be combined with the options above"
    echo "(--yes works with both)."
    echo "  --tenant-wide          Run this teardown with none of the changes below: what"
    echo "                         ./scripts/teardown.sh did before it had the default above."
    echo "  --agent-user-upn=UPN   Target a specific Agent User UPN to deprovision."
    echo "                         May be repeated or comma-separated."
    echo "  --dry-run              Print what would be deleted, and delete nothing."
    echo "                         With --agent-user-upn, each Agent User's chain is"
    echo "                         looked up in Entra (read-only) and printed."
    echo "  --delete-cloud-storage Reserved explicit storage teardown switch."
    echo "                         Current script refuses this switch; cloud storage"
    echo "                         must be deleted manually after a backup."
    echo "  --preserve-provisioner"
    echo "                         Do not delete the shared Provisioner app. Intended"
    echo "                         for targeted smoke-test teardowns."
    echo "  --preserve-local-state"
    echo "                         Do not remove .env, .entrabot-state.json, or local"
    echo "                         keychain entries. Intended for wrapper-managed tests."
    echo "  --help, -h             Show this help."
    echo ""
    echo "Cloud storage is not deleted by teardown.sh."
    # Help after a mistyped option is a usage error, as in setup.sh.
    if [ "$UNKNOWN_ARGUMENT" = true ]; then
        exit 2
    fi
    exit 0
fi

# ── Which teardown ──────────────────────────────────────────────────────────

if [ "$TENANT_WIDE" = true ] && [ -n "$WIZARD_FLAG" ]; then
    echo "ERROR: $WIZARD_FLAG and $TENANT_WIDE_FLAG belong to two different teardowns and can't be combined." >&2
    echo "  See ./scripts/teardown.sh --help." >&2
    exit 2
fi

if [ "$TENANT_WIDE" = false ]; then
    if [ ! -x "$VENV_PY" ]; then
        if [ ! -f .env ] && [ ! -f .entrabot-state.json ]; then
            echo "Nothing to remove. This computer is not connected to an agent."
            exit 0
        fi
        echo "ERROR: Teardown needs the Python environment setup builds (.venv), and it is not here." >&2
        echo "  Run ./scripts/setup.sh --help to see how to rebuild it, or remove .env and" >&2
        echo "  .entrabot-state.json yourself if this folder is no longer used." >&2
        exit 1
    fi
    [ "$ASSUME_YES" = true ] && WIZARD_ARGS+=(--yes)
    exec "$VENV_PY" -m entrabot.winsetup teardown --project-root "$PROJECT_ROOT" \
        ${WIZARD_ARGS[@]+"${WIZARD_ARGS[@]}"}
fi

# Setup installs its own Azure CLI when the computer has none (~/.entrabot/tools/azure-cli).
# It is not on PATH, so it is used here when no other az is.
if ! command -v az >/dev/null && [ -x "${HOME:-}/.entrabot/tools/azure-cli/bin/az" ]; then
    PATH="$HOME/.entrabot/tools/azure-cli/bin:$PATH"
    export PATH
fi

if [ "$DELETE_CLOUD_STORAGE" = true ]; then
    echo "ERROR: --delete-cloud-storage is intentionally not implemented here." >&2
    echo "Cloud storage is not deleted by teardown.sh; delete containers/accounts manually after backup." >&2
    exit 2
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# ── Load IDs from all available sources ────────────────────────────────────

# One value from the state file (empty when the file, or python3, is missing). The key is an
# argument, never part of the program.
_read_state() {
    if [ -f .entrabot-state.json ] && command -v python3 >/dev/null; then
        python3 -c '
import json
import pathlib
import sys

state = json.loads(pathlib.Path(".entrabot-state.json").read_text(encoding="utf-8-sig"))
value = state.get(sys.argv[1])
print("" if value is None else value)
' "$1" || echo ""
    else
        echo ""
    fi
}

# One value from .env, read as data the way entrabot.config reads the file: a UTF-8 byte-order
# mark and CRLF line ends are tolerated, each line is trimmed, blank lines, comments and lines
# with no "=" are skipped, the line is split at the FIRST "=", key and value are trimmed (a value
# is not unquoted), and the FIRST value of a repeated key wins. Prints the value and returns 0;
# returns 1 when the file or the key is absent. Nothing in the file is ever run: other tools write
# .env too, and running it as shell code would run a value holding $(...), backticks or ";".
env_file_value() {
    local file="$1" wanted="$2" line key first=true
    [ -f "$file" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$first" = true ]; then
            line="${line#$'\xef\xbb\xbf'}"
            first=false
        fi
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        case "$line" in
            '' | '#'*) continue ;;
            *=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        key="${key%"${key##*[![:space:]]}"}"
        if [ "$key" = "$wanted" ]; then
            line="${line#*=}"
            printf '%s' "${line#"${line%%[![:space:]]*}"}"
            return 0
        fi
    done < "$file"
    return 1
}

# A .env that is there but can't be read must not look empty: teardown would go on to delete
# local state without the IDs it holds.
if [ -f .env ] && [ ! -r .env ]; then
    echo "ERROR: .env exists but cannot be read, so teardown does not know what to delete. Nothing was deleted." >&2
    exit 1
fi

# A value .env holds wins over the state file, as it always has. Only these keys are read.
if value="$(env_file_value .env ENTRABOT_AGENT_USER_ID)"; then ENTRABOT_AGENT_USER_ID="$value"; fi
if value="$(env_file_value .env ENTRABOT_AGENT_OBJECT_ID)"; then ENTRABOT_AGENT_OBJECT_ID="$value"; fi
if value="$(env_file_value .env ENTRABOT_BLUEPRINT_APP_ID)"; then ENTRABOT_BLUEPRINT_APP_ID="$value"; fi
if value="$(env_file_value .env ENTRABOT_BLUEPRINT_OBJECT_ID)"; then ENTRABOT_BLUEPRINT_OBJECT_ID="$value"; fi

AGENT_USER_ID="${ENTRABOT_AGENT_USER_ID:-$(_read_state AGENT_USER_ID)}"
AGENT_OBJECT_ID="${ENTRABOT_AGENT_OBJECT_ID:-$(_read_state AGENT_OBJECT_ID)}"
BLUEPRINT_APP_ID="${ENTRABOT_BLUEPRINT_APP_ID:-$(_read_state BLUEPRINT_APP_ID)}"
BLUEPRINT_OBJECT_ID="${ENTRABOT_BLUEPRINT_OBJECT_ID:-$(_read_state BLUEPRINT_OBJECT_ID)}"

# Microsoft Entra object and application IDs are GUIDs.
is_guid() {
    [[ "$1" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]
}

# The Agent User is deleted with az ad user delete --id, which also takes a sign-in name: a
# recorded value that is not an object ID could name somebody else, so it is shown, never used.
# The Agent Identity and Blueprint IDs only ever go into a Graph URL, as one path segment.
AGENT_USER_ID_NOT_AN_ID=""
if [ -n "$AGENT_USER_ID" ] && ! is_guid "$AGENT_USER_ID"; then
    AGENT_USER_ID_NOT_AN_ID="$AGENT_USER_ID"
    AGENT_USER_ID=""
fi

# The Provisioner app setup created or took over for this machine, by the application ID recorded
# in .entrabot-state.json. It is the only app teardown deletes. Display names are not unique (anyone
# who may register applications can give an app the Provisioner's), so an app found by name may be
# somebody else's, and a Provisioner holds tenant-wide Microsoft Graph permissions.
PROV_APP_ID="$(_read_state PROVISIONER_CLIENT_ID)"
PROV_APP_ID_NOT_AN_ID=""
if [ -n "$PROV_APP_ID" ] && ! is_guid "$PROV_APP_ID"; then
    # az ad app delete --id also takes an identifier URI: hand it nothing but an application ID.
    PROV_APP_ID_NOT_AN_ID="$PROV_APP_ID"
    PROV_APP_ID=""
fi

# When no recorded Provisioner app is to be deleted: say so, and how to delete one by hand.
no_recorded_provisioner() {
    if [ -n "$PROV_APP_ID_NOT_AN_ID" ]; then
        echo -e "  ${YELLOW}⚠️  PROVISIONER_CLIENT_ID in .entrabot-state.json is not an application ID, so no Provisioner app is deleted.${NC}"
        printf '     It is: %s\n' "$PROV_APP_ID_NOT_AN_ID"
    else
        echo -e "  ${YELLOW}⚠️  No Provisioner app is recorded for this machine in .entrabot-state.json, so none is deleted.${NC}"
    fi
    echo "     Teardown never deletes an app found by its name: another app can have the same name."
    echo "     To delete this machine's Provisioner app yourself, find its application ID in the Entra"
    echo "     admin center (App registrations > All applications: 'EntraBot Agent ID Provisioner', or"
    echo "     'EntraBot Provisioner' from older setups), check that it is the one setup created for"
    echo "     this machine, then run:"
    echo "       az ad app delete --id <PROVISIONER_APP_ID>"
}

# The targeted teardown (deprovision_entra_agent_identity.py), one --agent-user-upn per UPN. It
# gets its own Provisioner token.
run_targeted_deprovision() {
    local args=("$@") upn
    for upn in "${TARGET_AGENT_USER_UPNS[@]}"; do
        args+=(--agent-user-upn "$upn")
    done
    "$VENV_PY" "$SCRIPT_DIR/deprovision_entra_agent_identity.py" "${args[@]}"
}

# Check if there's anything to do
HAS_ENTRA_RESOURCES=false
HAS_LOCAL_STATE=false

if [ -n "$AGENT_USER_ID" ] || [ -n "$AGENT_USER_ID_NOT_AN_ID" ] || [ -n "$AGENT_OBJECT_ID" ] \
    || [ -n "$BLUEPRINT_APP_ID" ] || [ ${#TARGET_AGENT_USER_UPNS[@]} -gt 0 ]; then
    HAS_ENTRA_RESOURCES=true
fi
if [ -f .env ] || [ -f .entrabot-state.json ]; then
    HAS_LOCAL_STATE=true
fi
if [ -n "$PROV_APP_ID" ]; then
    HAS_ENTRA_RESOURCES=true
fi

if [ "$HAS_ENTRA_RESOURCES" = false ] && [ "$HAS_LOCAL_STATE" = false ]; then
    echo -e "${GREEN}Nothing to clean up.${NC} No Entra resources or local state found."
    if [ "$PRESERVE_PROVISIONER" = false ]; then
        no_recorded_provisioner
    fi
    exit 0
fi

echo -e "${YELLOW}⚠️  This will delete the following:${NC}"
echo ""
if [ "$HAS_ENTRA_RESOURCES" = true ]; then
    echo "  Entra resources:"
    if [ ${#TARGET_AGENT_USER_UPNS[@]} -gt 0 ]; then
        for upn in "${TARGET_AGENT_USER_UPNS[@]}"; do
            echo "    Target Agent User UPN: $upn"
        done
        echo "    Targeted teardown removes assigned licenses, Agent User,"
        echo "    parent Agent Identity, and parent Blueprint."
    else
        # The chain local state names. A targeted teardown leaves it alone, so it is not listed.
        if [ -n "$AGENT_USER_ID" ]; then
            echo "    Agent User:     $AGENT_USER_ID"
        elif [ -n "$AGENT_USER_ID_NOT_AN_ID" ]; then
            printf '    Agent User:     %s (not an object ID, so not deleted)\n' "$AGENT_USER_ID_NOT_AN_ID"
        fi
        if [ -n "$AGENT_OBJECT_ID" ]; then
            echo "    Agent Identity: $AGENT_OBJECT_ID"
        fi
        if [ -n "$BLUEPRINT_APP_ID" ]; then
            echo "    Blueprint:      $BLUEPRINT_APP_ID"
        fi
    fi
    if [ -n "$PROV_APP_ID" ] && [ "$PRESERVE_PROVISIONER" = false ]; then
        echo "    Provisioner:    $PROV_APP_ID (recorded in .entrabot-state.json)"
    fi
fi
if [ "$HAS_LOCAL_STATE" = true ]; then
    echo "  Local state:"
    [ -f .env ]                  && echo "    .env"
    [ -f .entrabot-state.json ]  && echo "    .entrabot-state.json"
    [ "$PRESERVE_LOCAL_STATE" = true ] && echo "    (preserved by --preserve-local-state)"
fi
if [ -z "$PROV_APP_ID" ] && [ "$PRESERVE_PROVISIONER" = false ]; then
    echo "  Provisioner app: no application ID recorded for this machine, so none will be deleted (see below)"
fi
echo "  Cloud storage: not deleted by teardown.sh"
[ "$PRESERVE_PROVISIONER" = true ] && echo "  Provisioner app: preserved by --preserve-provisioner"
echo ""
if [ "$DRY_RUN" = true ]; then
    if [ ${#TARGET_AGENT_USER_UPNS[@]} -gt 0 ]; then
        # Read-only: the helper looks each Agent User's chain up in Entra, prints it, and deletes
        # nothing. Teardown itself gets no token and runs nothing else.
        if ! run_targeted_deprovision --dry-run; then
            echo -e "  ${RED}❌ Targeted dry run failed: see the error above${NC}"
            exit 1
        fi
        echo ""
    fi
    echo -e "${YELLOW}Dry run only — no tenant or local state will be deleted.${NC}"
    exit 0
elif [ "$ASSUME_YES" = false ]; then
    read -p "Are you sure? (y/N) " -n 1 -r
    echo

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""

# ── Get Provisioner token for Agent Identity API deletions ────────────────
# Learning #1: az CLI tokens include Directory.AccessAsUser.All which Agent
# Identity APIs reject. Must use the Provisioner's client_credentials token.
# Get this BEFORE deleting anything, since we need the Provisioner alive.
#
# The token is a secret. It never goes on a command line, where any local user can read it from
# the process list, or into program text. It lives in a shell variable that is never exported
# (unset first, in case the calling shell exported one of this name), is handed to each Graph
# delete in the environment of that one command, and is unset once those are done.

unset PROV_TOKEN
PROV_TOKEN=""

if [ -d "$PROJECT_ROOT/.venv" ] && [ -f "$PROJECT_ROOT/scripts/entra_provisioning.py" ]; then
    # Only the token goes to stdout. What the provisioning code prints, and why there is no token,
    # go to stderr, where they are seen. The scripts folder is an argument, never program text.
    PROV_TOKEN="$("$VENV_PY" -c '
import contextlib
import sys

sys.path.insert(0, sys.argv[1])
try:
    with contextlib.redirect_stdout(sys.stderr):
        from entra_provisioning import get_existing_graph_token

        token = get_existing_graph_token()
except Exception as exc:
    sys.exit(f"  Could not get provisioner token: {exc}")
sys.stdout.write(token)
' "$PROJECT_ROOT/scripts")" || PROV_TOKEN=""
fi

if [ -n "$PROV_TOKEN" ] && [ ${#PROV_TOKEN} -gt 100 ]; then
    echo -e "  ${GREEN}Got Provisioner token for Agent Identity API deletions${NC}"
else
    echo -e "  ${YELLOW}⚠️  No Provisioner token — manual cleanup may be required for Agent Identity APIs${NC}"
    PROV_TOKEN=""
fi

# DELETE https://graph.microsoft.com/beta/<collection>/<object ID> with the Provisioner token.
# Prints the HTTP status. Graph's error, or why Graph could not be reached, goes to stderr. The ID
# is an argument and becomes one path segment; the token is in this one command's environment,
# which the program takes it out of.
graph_delete() {
    PROVISIONER_TOKEN="$PROV_TOKEN" "$VENV_PY" -c '
import os
import sys
from urllib.parse import quote

import requests

collection, object_id = sys.argv[1], sys.argv[2]
token = os.environ.pop("PROVISIONER_TOKEN")
segment = quote(object_id, safe="")
url = f"https://graph.microsoft.com/beta/{collection}/{segment}"
try:
    response = requests.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
except requests.RequestException as exc:
    sys.exit(f"  Could not reach Microsoft Graph: {type(exc).__name__}: {exc}")
if response.status_code not in (200, 204, 404):
    print(f"  Microsoft Graph answered {response.status_code}: {response.text[:500]}", file=sys.stderr)
print(response.status_code)
' "$1" "$2"
}

# Does az's output say that the object demonstrably does not exist? (entra_provisioning.py reads
# az's answer the same way.)
az_says_not_found() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        *request_resourcenotfound* | *"does not exist or one of its queried reference-property objects are not present"*)
            return 0 ;;
    esac
    return 1
}

# ── Targeted Agent User teardown by UPN ─────────────────────────────────────

if [ ${#TARGET_AGENT_USER_UPNS[@]} -gt 0 ]; then
    if ! run_targeted_deprovision; then
        echo -e "  ${RED}❌ Targeted Agent User teardown failed${NC}"
        exit 1
    fi
    # The targeted helper already deleted the Agent User, parent Agent Identity,
    # and parent Blueprint. Avoid duplicate state-based deletes below.
    TARGETED_DEPROVISION_DONE=true
fi

# ── 1-3. Delete state-based identity chain if no targeted teardown ran ─────

if [ "$TARGETED_DEPROVISION_DONE" = false ]; then

# ── 1. Delete Agent User (child — must go before Agent Identity) ──────────
# Agent Users are regular Entra users — az CLI works fine here. Whatever az says is shown. An
# Agent User that is already gone is told from az's answer.

if [ -n "$AGENT_USER_ID" ]; then
    AZ_STATUS=0
    AZ_OUTPUT="$(az ad user delete --id "$AGENT_USER_ID" 2>&1)" || AZ_STATUS=$?
    if [ -n "$AZ_OUTPUT" ]; then
        printf '%s\n' "$AZ_OUTPUT" >&2
    fi
    if [ "$AZ_STATUS" -eq 0 ]; then
        echo -e "  ${GREEN}✅ Deleted Agent User ($AGENT_USER_ID)${NC}"
    elif az_says_not_found "$AZ_OUTPUT"; then
        echo -e "  ${YELLOW}⚠️  Agent User not found — already deleted${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Could not delete Agent User ($AGENT_USER_ID): see the az error above${NC}"
        echo "     Once that is fixed, delete it with: az ad user delete --id $AGENT_USER_ID"
    fi
elif [ -n "$AGENT_USER_ID_NOT_AN_ID" ]; then
    echo -e "  ${YELLOW}⚠️  The Agent User ID recorded for this machine is not an object ID, so no Agent User is deleted.${NC}"
    printf '     It is: %s\n' "$AGENT_USER_ID_NOT_AN_ID"
    echo "     Teardown deletes an Agent User only by its object ID: az ad user delete --id also takes a sign-in name."
else
    echo -e "  ${YELLOW}⚠️  No Agent User ID found — skipping${NC}"
fi

# ── 2. Delete Agent Identity (service principal) ──────────────────────────
# MUST use Provisioner token — az CLI is rejected by Agent Identity APIs

if [ -n "$AGENT_OBJECT_ID" ]; then
    if [ -n "$PROV_TOKEN" ]; then
        STATUS="$(graph_delete servicePrincipals "$AGENT_OBJECT_ID")" || STATUS="error"
        if [ "$STATUS" = "204" ] || [ "$STATUS" = "200" ]; then
            printf "  ${GREEN}✅ Deleted Agent Identity SP (%s)${NC}\n" "$AGENT_OBJECT_ID"
        elif [ "$STATUS" = "404" ]; then
            echo -e "  ${YELLOW}⚠️  Agent Identity SP not found — already deleted${NC}"
        else
            printf "  ${YELLOW}⚠️  Could not delete Agent Identity SP (%s, status: %s): see the error above${NC}\n" \
                "$AGENT_OBJECT_ID" "${STATUS:-error}"
        fi
    else
        echo -e "  ${YELLOW}⚠️  No Provisioner token — cannot delete Agent Identity SP via az CLI (Learning #1)${NC}"
        printf "  ${YELLOW}   Delete manually in Azure Portal → Entra ID → Enterprise applications → %s${NC}\n" "$AGENT_OBJECT_ID"
    fi
else
    echo -e "  ${YELLOW}⚠️  No Agent Identity object ID found — skipping${NC}"
fi

# ── 3. Delete Blueprint (app registration + BlueprintPrincipal cascade) ───
# MUST use Provisioner token — az CLI is rejected by Agent Identity APIs

if [ -n "$BLUEPRINT_OBJECT_ID" ]; then
    if [ -n "$PROV_TOKEN" ]; then
        STATUS="$(graph_delete applications "$BLUEPRINT_OBJECT_ID")" || STATUS="error"
        if [ "$STATUS" = "204" ] || [ "$STATUS" = "200" ]; then
            printf "  ${GREEN}✅ Deleted Blueprint app (%s)${NC}\n" "$BLUEPRINT_APP_ID"
        elif [ "$STATUS" = "404" ]; then
            echo -e "  ${YELLOW}⚠️  Blueprint not found — already deleted${NC}"
        else
            printf "  ${YELLOW}⚠️  Could not delete Blueprint (%s, status: %s): see the error above${NC}\n" \
                "$BLUEPRINT_OBJECT_ID" "${STATUS:-error}"
        fi
    else
        echo -e "  ${YELLOW}⚠️  No Provisioner token — cannot delete Blueprint via az CLI (Learning #1)${NC}"
        printf "  ${YELLOW}   Delete manually in Azure Portal → Entra ID → App registrations → %s${NC}\n" "$BLUEPRINT_OBJECT_ID"
    fi
else
    echo -e "  ${YELLOW}⚠️  No Blueprint ID found — skipping${NC}"
fi

else
    echo -e "  ${GREEN}✅ Targeted teardown already removed the identity chain${NC}"
fi

# The token is not needed after this point.
unset PROV_TOKEN

# ── 4. Delete Provisioner app LAST (needed it for steps 2-3) ─────────────
# Only the one recorded for this machine (PROV_APP_ID above), by its application ID.
# Provisioner is a regular app — az CLI works fine here

if [ "$PRESERVE_PROVISIONER" = true ]; then
    if [ -n "$PROV_APP_ID" ]; then
        echo -e "  ${YELLOW}⚠️  Preserving Provisioner app ($PROV_APP_ID)${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Preserving Provisioner app${NC}"
    fi
elif [ -n "$PROV_APP_ID" ]; then
    if az ad app delete --id "$PROV_APP_ID"; then
        echo -e "  ${GREEN}✅ Deleted Provisioner app ($PROV_APP_ID)${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Could not delete Provisioner app ($PROV_APP_ID): see the az error above${NC}"
        echo "     Once that is fixed, delete it with: az ad app delete --id $PROV_APP_ID"
    fi
else
    no_recorded_provisioner
fi

# ── 5. Clean up local state ───────────────────────────────────────────────

echo ""

if [ "$PRESERVE_LOCAL_STATE" = true ]; then
    echo -e "  ${YELLOW}⚠️  Preserving local state and keychain entries${NC}"
    echo ""
    echo -e "${GREEN}Done.${NC} Local state was preserved."
    exit 0
fi

# Keychain entries (certificate private key + legacy) — use venv Python for keyring
CLEANUP_PY="${VENV_PY:-python3}"
"$CLEANUP_PY" -c "
import keyring
cleared = []
for key in ['blueprint-private-key', 'blueprint-private-key.pending', 'blueprint_secret', 'human_refresh_token', 'agent_password']:
    try:
        keyring.delete_password('entrabot', key)
        cleared.append(key)
    except Exception:
        pass
if cleared:
    print(f'  ✅ Cleared keychain entries: {\", \".join(cleared)}')
else:
    print('  (no keychain entries found)')
" || true

# This agent's own key store item (the setup wizard keeps one per certificate, named by the
# thumbprint in .env). .env is read as data, never run. Another agent's item is not touched.
"$CLEANUP_PY" - "$PROJECT_ROOT/.env" <<'PY' || true
import sys
from pathlib import Path

try:
    from entrabot.platform import keycert
    from entrabot.winsetup.envfile import read_env
except ImportError:
    sys.exit(0)
values = read_env(Path(sys.argv[1]))
for key in ("ENTRABOT_BLUEPRINT_CERT_SHA1", "ENTRABOT_BLUEPRINT_CERT_RETIRING_SHA1"):
    sha1 = values.get(key, "").strip()
    if not sha1:
        continue
    try:
        if keycert.delete_cert(sha1):
            print("  ✅ Deleted this agent's certificate and its private key from the key store")
    except keycert.CertStoreError as exc:
        print(f"  ⚠️  Could not delete this agent's certificate from the key store: {exc}")
PY

if [ -f .env ]; then
    rm -f .env
    echo -e "  ${GREEN}✅ Removed .env${NC}"
fi

if [ -f .entrabot-state.json ]; then
    rm -f .entrabot-state.json
    echo -e "  ${GREEN}✅ Removed .entrabot-state.json${NC}"
fi

echo ""
echo -e "${GREEN}Done.${NC} Run ${YELLOW}./scripts/setup.sh${NC} to set up again."
