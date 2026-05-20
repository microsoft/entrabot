#!/usr/bin/env bash
# EntraClaw Identity Research — teardown
# Removes everything setup.sh creates:
#   1. Agent User (must go first — child of Agent Identity)
#   2. Agent Identity (service principal)
#   3. Blueprint (app registration — also deletes BlueprintPrincipal)
#   4. Provisioner app registration
#   5. Local state (.env, .entraclaw-state.json, legacy keychain)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

TARGET_AGENT_USER_UPNS=()
ASSUME_YES=false
DRY_RUN=false
DELETE_CLOUD_STORAGE=false
PRESERVE_PROVISIONER=false
PRESERVE_LOCAL_STATE=false
SHOW_HELP=false
TARGETED_DEPROVISION_DONE=false

for arg in "$@"; do
    case $arg in
        --agent-user-upn=*)
            IFS=',' read -ra UPN_PARTS <<< "${arg#--agent-user-upn=}"
            for upn in "${UPN_PARTS[@]}"; do
                upn="$(echo "$upn" | xargs)"
                [ -n "$upn" ] && TARGET_AGENT_USER_UPNS+=("$upn")
            done
            ;;
        --yes|-y)
            ASSUME_YES=true
            ;;
        --dry-run)
            DRY_RUN=true
            ;;
        --delete-cloud-storage)
            DELETE_CLOUD_STORAGE=true
            ;;
        --preserve-provisioner)
            PRESERVE_PROVISIONER=true
            ;;
        --preserve-local-state)
            PRESERVE_LOCAL_STATE=true
            ;;
        --help|-h)
            SHOW_HELP=true
            ;;
        *)
            echo "ERROR: Unknown argument: $arg" >&2
            SHOW_HELP=true
            ;;
    esac
done

if [ "$SHOW_HELP" = true ]; then
    echo "Usage: ./scripts/teardown.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --agent-user-upn=UPN   Target a specific Agent User UPN to deprovision."
    echo "                         May be repeated or comma-separated."
    echo "  --dry-run              Resolve and print targeted chains without deleting."
    echo "  --yes, -y              Skip confirmation prompt."
    echo "  --delete-cloud-storage Reserved explicit storage teardown switch."
    echo "                         Current script refuses this switch; cloud storage"
    echo "                         must be deleted manually after a backup."
    echo "  --preserve-provisioner"
    echo "                         Do not delete the shared Provisioner app. Intended"
    echo "                         for targeted smoke-test teardowns."
    echo "  --preserve-local-state"
    echo "                         Do not remove .env, .entraclaw-state.json, or local"
    echo "                         keychain entries. Intended for wrapper-managed tests."
    echo "  --help, -h             Show this help."
    echo ""
    echo "Cloud storage is not deleted by teardown.sh."
    exit 0
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

# Helper to read from state file (always available)
_read_state() {
    local key="$1"
    if [ -f .entraclaw-state.json ] && command -v python3 &>/dev/null; then
        python3 -c "
import json, pathlib
data = json.loads(pathlib.Path('.entraclaw-state.json').read_text())
print(data.get('$key', ''))
" || echo ""
    else
        echo ""
    fi
}

# Load from .env (non-fatal if missing)
# shellcheck disable=SC1091
if [ -f .env ]; then
    source .env
fi

# Merge state file values (state file takes precedence for new-format IDs)
AGENT_USER_ID="${ENTRACLAW_AGENT_USER_ID:-$(_read_state AGENT_USER_ID)}"
AGENT_OBJECT_ID="${ENTRACLAW_AGENT_OBJECT_ID:-$(_read_state AGENT_OBJECT_ID)}"
BLUEPRINT_APP_ID="${ENTRACLAW_BLUEPRINT_APP_ID:-$(_read_state BLUEPRINT_APP_ID)}"
BLUEPRINT_OBJECT_ID="${ENTRACLAW_BLUEPRINT_OBJECT_ID:-$(_read_state BLUEPRINT_OBJECT_ID)}"

# Check if there's anything to do
HAS_ENTRA_RESOURCES=false
HAS_LOCAL_STATE=false

if [ -n "$AGENT_USER_ID" ] || [ -n "$AGENT_OBJECT_ID" ] || [ -n "$BLUEPRINT_APP_ID" ] || [ ${#TARGET_AGENT_USER_UPNS[@]} -gt 0 ]; then
    HAS_ENTRA_RESOURCES=true
fi
if [ -f .env ] || [ -f .entraclaw-state.json ]; then
    HAS_LOCAL_STATE=true
fi

# Check for provisioner apps in Entra (only if logged in)
PROV_FOUND=false
if az account show &>/dev/null; then
    for PROV_NAME in "EntraClaw Provisioner" "EntraClaw Agent ID Provisioner"; do
        PROV_CHECK=$(az ad app list --display-name "$PROV_NAME" --query "[0].id" -o tsv) || true
        if [ -n "$PROV_CHECK" ]; then
            PROV_FOUND=true
            HAS_ENTRA_RESOURCES=true
        fi
    done
fi

if [ "$HAS_ENTRA_RESOURCES" = false ] && [ "$HAS_LOCAL_STATE" = false ]; then
    echo -e "${GREEN}Nothing to clean up.${NC} No Entra resources or local state found."
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
    fi
    [ -n "$AGENT_USER_ID" ]    && echo "    Agent User:     $AGENT_USER_ID"
    [ -n "$AGENT_OBJECT_ID" ]  && echo "    Agent Identity: $AGENT_OBJECT_ID"
    [ -n "$BLUEPRINT_APP_ID" ] && echo "    Blueprint:      $BLUEPRINT_APP_ID"
    [ "$PROV_FOUND" = true ]   && echo "    Provisioner:    (found by name)"
fi
if [ "$HAS_LOCAL_STATE" = true ]; then
    echo "  Local state:"
    [ -f .env ]                  && echo "    .env"
    [ -f .entraclaw-state.json ]  && echo "    .entraclaw-state.json"
    [ "$PRESERVE_LOCAL_STATE" = true ] && echo "    (preserved by --preserve-local-state)"
fi
echo "  Cloud storage: not deleted by teardown.sh"
[ "$PRESERVE_PROVISIONER" = true ] && echo "  Provisioner app: preserved by --preserve-provisioner"
echo ""
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}Dry run only — no tenant or local state will be deleted.${NC}"
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

VENV_PY="$PROJECT_ROOT/.venv/bin/python3"
PROV_TOKEN=""

if [ -d "$PROJECT_ROOT/.venv" ] && [ -f "$PROJECT_ROOT/scripts/entra_provisioning.py" ]; then
    PROV_TOKEN=$("$VENV_PY" -c "
import sys
sys.path.insert(0, '$PROJECT_ROOT/scripts')
try:
    from entra_provisioning import get_existing_graph_token
    print(get_existing_graph_token())
except Exception as e:
    print('', end='')
    print(f'  Could not get provisioner token: {e}', file=sys.stderr)
" 2>&1 | head -1) || true
fi

if [ -n "$PROV_TOKEN" ] && [ ${#PROV_TOKEN} -gt 100 ]; then
    echo -e "  ${GREEN}Got Provisioner token for Agent Identity API deletions${NC}"
else
    echo -e "  ${YELLOW}⚠️  No Provisioner token — will try az CLI (may fail for Agent Identity APIs)${NC}"
    PROV_TOKEN=""
fi

# ── Targeted Agent User teardown by UPN ─────────────────────────────────────

if [ ${#TARGET_AGENT_USER_UPNS[@]} -gt 0 ]; then
    TARGET_ARGS=()
    [ "$DRY_RUN" = true ] && TARGET_ARGS+=(--dry-run)
    for upn in "${TARGET_AGENT_USER_UPNS[@]}"; do
        TARGET_ARGS+=(--agent-user-upn "$upn")
    done
    if ! "$VENV_PY" "$SCRIPT_DIR/deprovision_entra_agent_identity.py" "${TARGET_ARGS[@]}"; then
        echo -e "  ${RED}❌ Targeted Agent User teardown failed${NC}"
        exit 1
    fi
    if [ "$DRY_RUN" = true ]; then
        echo ""
        echo -e "${GREEN}Dry run complete.${NC}"
        exit 0
    fi
    # The targeted helper already deleted the Agent User, parent Agent Identity,
    # and parent Blueprint. Avoid duplicate state-based deletes below.
    TARGETED_DEPROVISION_DONE=true
fi

# ── 1-3. Delete state-based identity chain if no targeted teardown ran ─────

if [ "$TARGETED_DEPROVISION_DONE" = false ]; then

# ── 1. Delete Agent User (child — must go before Agent Identity) ──────────

if [ -n "$AGENT_USER_ID" ]; then
    # Agent Users are regular Entra users — az CLI works fine here
    if az ad user delete --id "$AGENT_USER_ID" 2>/dev/null; then
        echo -e "  ${GREEN}✅ Deleted Agent User ($AGENT_USER_ID)${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Could not delete Agent User — may already be deleted${NC}"
    fi
else
    echo -e "  ${YELLOW}⚠️  No Agent User ID found — skipping${NC}"
fi

# ── 2. Delete Agent Identity (service principal) ──────────────────────────
# MUST use Provisioner token — az CLI is rejected by Agent Identity APIs

if [ -n "$AGENT_OBJECT_ID" ]; then
    if [ -n "$PROV_TOKEN" ]; then
        STATUS=$("$VENV_PY" -c "
import requests
resp = requests.delete(
    'https://graph.microsoft.com/beta/servicePrincipals/$AGENT_OBJECT_ID',
    headers={'Authorization': 'Bearer $PROV_TOKEN'},
)
print(resp.status_code)
" 2>/dev/null) || STATUS="error"
        if [ "$STATUS" = "204" ] || [ "$STATUS" = "200" ]; then
            echo -e "  ${GREEN}✅ Deleted Agent Identity SP ($AGENT_OBJECT_ID)${NC}"
        elif [ "$STATUS" = "404" ]; then
            echo -e "  ${YELLOW}⚠️  Agent Identity SP not found — already deleted${NC}"
        else
            echo -e "  ${YELLOW}⚠️  Could not delete Agent Identity SP (status: $STATUS)${NC}"
        fi
    else
        echo -e "  ${YELLOW}⚠️  No Provisioner token — cannot delete Agent Identity SP via az CLI (Learning #1)${NC}"
        echo -e "  ${YELLOW}   Delete manually in Azure Portal → Entra ID → Enterprise applications → $AGENT_OBJECT_ID${NC}"
    fi
else
    echo -e "  ${YELLOW}⚠️  No Agent Identity object ID found — skipping${NC}"
fi

# ── 3. Delete Blueprint (app registration + BlueprintPrincipal cascade) ───
# MUST use Provisioner token — az CLI is rejected by Agent Identity APIs

if [ -n "$BLUEPRINT_OBJECT_ID" ]; then
    if [ -n "$PROV_TOKEN" ]; then
        STATUS=$("$VENV_PY" -c "
import requests
resp = requests.delete(
    'https://graph.microsoft.com/beta/applications/$BLUEPRINT_OBJECT_ID',
    headers={'Authorization': 'Bearer $PROV_TOKEN'},
)
print(resp.status_code)
" 2>/dev/null) || STATUS="error"
        if [ "$STATUS" = "204" ] || [ "$STATUS" = "200" ]; then
            echo -e "  ${GREEN}✅ Deleted Blueprint app ($BLUEPRINT_APP_ID)${NC}"
        elif [ "$STATUS" = "404" ]; then
            echo -e "  ${YELLOW}⚠️  Blueprint not found — already deleted${NC}"
        else
            echo -e "  ${YELLOW}⚠️  Could not delete Blueprint (status: $STATUS)${NC}"
        fi
    else
        echo -e "  ${YELLOW}⚠️  No Provisioner token — cannot delete Blueprint via az CLI (Learning #1)${NC}"
        echo -e "  ${YELLOW}   Delete manually in Azure Portal → Entra ID → App registrations → $BLUEPRINT_OBJECT_ID${NC}"
    fi
else
    echo -e "  ${YELLOW}⚠️  No Blueprint ID found — skipping${NC}"
fi

else
    echo -e "  ${GREEN}✅ Targeted teardown already removed the identity chain${NC}"
fi

# ── 4. Delete Provisioner app LAST (needed it for steps 2-3) ─────────────
# Provisioner is a regular app — az CLI works fine here

for PROV_NAME in "EntraClaw Provisioner" "EntraClaw Agent ID Provisioner"; do
    if [ "$PRESERVE_PROVISIONER" = true ]; then
        echo -e "  ${YELLOW}⚠️  Preserving Provisioner app ($PROV_NAME)${NC}"
        continue
    fi
    PROV_OBJ=$(az ad app list --display-name "$PROV_NAME" \
        --query "[0].id" -o tsv 2>/dev/null) || true
    if [ -n "$PROV_OBJ" ]; then
        if az ad app delete --id "$PROV_OBJ" 2>/dev/null; then
            echo -e "  ${GREEN}✅ Deleted Provisioner app ($PROV_NAME)${NC}"
        else
            echo -e "  ${YELLOW}⚠️  Could not delete Provisioner app ($PROV_NAME)${NC}"
        fi
    fi
done

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
for key in ['blueprint-private-key', 'blueprint_secret', 'human_refresh_token', 'agent_password']:
    try:
        keyring.delete_password('entraclaw', key)
        cleared.append(key)
    except Exception:
        pass
if cleared:
    print(f'  ✅ Cleared keychain entries: {\", \".join(cleared)}')
else:
    print('  (no keychain entries found)')
" || true

if [ -f .env ]; then
    rm -f .env
    echo -e "  ${GREEN}✅ Removed .env${NC}"
fi

if [ -f .entraclaw-state.json ]; then
    rm -f .entraclaw-state.json
    echo -e "  ${GREEN}✅ Removed .entraclaw-state.json${NC}"
fi

echo ""
echo -e "${GREEN}Done.${NC} Run ${YELLOW}./scripts/setup.sh${NC} to set up again."
