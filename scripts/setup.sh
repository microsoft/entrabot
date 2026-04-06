#!/usr/bin/env bash
# Openclaw Identity Research — one-command setup
#
# Thin orchestrator that calls Python scripts for Entra provisioning.
# No device-code flow.  No OBO.  The Agent User authenticates autonomously
# via the three-hop flow: Blueprint → Agent Identity → Agent User.
#
# Architecture (borrowed from agent-foundry-poc):
#   1. entra_provisioning.py  — creates/manages the dedicated provisioner app
#   2. create_entra_agent_ids.py — Blueprint + Agent Identity + Agent User
#   3. This script — venv + .env + tests
#
# State is persisted in .openclaw-state.json so re-runs are idempotent.
#
# NOTE: The Agent User requires a Teams-capable M365 license (E3/E5/Teams
# Enterprise) to be assigned AFTER this script runs. License assignment is
# a manual step in the Entra admin center or via Graph API.
set -euo pipefail

TOTAL_STEPS=8

# ── Argument parsing ───────────────────────────────────────────────────────

OVERRIDE_HUMAN_USER=""
SHOW_HELP=false

for arg in "$@"; do
    case $arg in
        --human-user=*)
            OVERRIDE_HUMAN_USER="${arg#--human-user=}"
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
    echo "Usage: ./scripts/setup.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --human-user=EMAIL   Override the human user (default: signed-in az CLI user)"
    echo "                       e.g., --human-user=brandon@other.com"
    echo "  --help, -h           Show this help"
    exit 0
fi

# ── Colored output helpers ──────────────────────────────────────────────────

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

step()    { echo -e "\n${BLUE}[$1/$TOTAL_STEPS]${NC} $2"; }
success() { echo -e "  ${GREEN}✅ $1${NC}"; }
warn()    { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
fail()    { echo -e "  ${RED}❌ $1${NC}"; exit 1; }

# ── Resolve project root ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Openclaw Identity Research — Setup         ║${NC}"
echo -e "${GREEN}║   (Agent User — no OBO, no device-code flow) ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"

# ── Helper: read value from .openclaw-state.json ───────────────────────────

read_state() {
    local key="$1"
    "$PYTHON" -c "
import json, pathlib, sys
state_file = pathlib.Path('$PROJECT_ROOT/.openclaw-state.json')
if not state_file.is_file():
    sys.exit(0)
data = json.loads(state_file.read_text())
val = data.get('$key', '')
if val:
    print(val)
" || echo ""
}

# ════════════════════════════════════════════════════════════════════════════
# Step 1: Verify prerequisites
# ════════════════════════════════════════════════════════════════════════════
step 1 "Verifying prerequisites"

MISSING=()

if ! command -v az &>/dev/null; then
    MISSING+=("az (Azure CLI — https://aka.ms/install-az)")
fi

PYTHON=""
for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" &>/dev/null; then
        PY_VER=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        if [ "$(echo "$PY_VER >= 3.12" | bc || python3 -c "print(int($PY_VER >= 3.12))")" = "1" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    MISSING+=("python3.12+ (https://www.python.org/downloads/)")
fi

if ! command -v git &>/dev/null; then
    MISSING+=("git")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    for m in "${MISSING[@]}"; do
        echo -e "  ${RED}✗ $m${NC}"
    done
    fail "Install the missing prerequisites above and re-run."
fi

success "az CLI found ($(az version --query '"azure-cli"' -o tsv || echo '?'))"
success "$PYTHON found ($PY_VER)"
success "git found ($(git --version | awk '{print $3}'))"

# ════════════════════════════════════════════════════════════════════════════
# Step 2: Verify Azure login
# ════════════════════════════════════════════════════════════════════════════
step 2 "Verifying Azure login"

if ! az account show &>/dev/null; then
    fail "Not logged in to Azure CLI. Run 'az login' first."
fi

TENANT_ID=$(az account show --query "tenantId" -o tsv)
ACCOUNT_NAME=$(az account show --query "name" -o tsv)

if [ -n "$OVERRIDE_HUMAN_USER" ]; then
    HUMAN_UPN="$OVERRIDE_HUMAN_USER"
    # Resolve UPN to object ID via Graph
    HUMAN_USER_ID=$(az ad user show --id "$HUMAN_UPN" --query "id" -o tsv) || true
    if [ -z "$HUMAN_USER_ID" ]; then
        fail "Could not find user '$HUMAN_UPN' in Entra. Check the email address."
    fi
    success "Human user (override): $HUMAN_UPN ($HUMAN_USER_ID)"
else
    HUMAN_UPN=$(az account show --query "user.name" -o tsv || echo "")
    HUMAN_USER_ID=$(az ad signed-in-user show --query "id" -o tsv || echo "")
    if [ -z "$HUMAN_USER_ID" ]; then
        fail "Could not determine signed-in user ID. Ensure 'az login' is done with a user account."
    fi
    success "Human user: $HUMAN_UPN ($HUMAN_USER_ID)"
fi

success "Tenant:     $TENANT_ID"
success "Account:    $ACCOUNT_NAME"

# ════════════════════════════════════════════════════════════════════════════
# Step 3: Ensure Python dependencies for provisioning scripts
# ════════════════════════════════════════════════════════════════════════════
step 3 "Ensuring Python dependencies for provisioning scripts"

if [ -d "$PROJECT_ROOT/.venv" ]; then
    SCRIPT_PYTHON="$PROJECT_ROOT/.venv/bin/python3"
    if [ ! -f "$SCRIPT_PYTHON" ]; then
        SCRIPT_PYTHON="$PYTHON"
    fi
else
    SCRIPT_PYTHON="$PYTHON"
fi

"$SCRIPT_PYTHON" -m pip install --quiet azure-identity requests 2>&1 | tail -1 || true
success "azure-identity and requests available"

# ════════════════════════════════════════════════════════════════════════════
# Step 4: Bootstrap provisioner app (Python)
# ════════════════════════════════════════════════════════════════════════════
step 4 "Bootstrapping Entra provisioner app"

echo "  Running entra_provisioning.py..."
if ! "$SCRIPT_PYTHON" "$SCRIPT_DIR/entra_provisioning.py"; then
    fail "Provisioner bootstrap failed. Check errors above."
fi
success "Provisioner app ready"

# ════════════════════════════════════════════════════════════════════════════
# Step 5: Create Blueprint + Agent Identity + Agent User (Python)
# ════════════════════════════════════════════════════════════════════════════
step 5 "Creating Entra Agent Identities + Agent User"

echo "  Running create_entra_agent_ids.py..."
if ! "$SCRIPT_PYTHON" "$SCRIPT_DIR/create_entra_agent_ids.py"; then
    fail "Agent Identity provisioning failed. Check errors above."
fi

# Read back IDs from state file
BLUEPRINT_APP_ID=$(read_state "BLUEPRINT_APP_ID")
BLUEPRINT_OBJECT_ID=$(read_state "BLUEPRINT_OBJECT_ID")
AGENT_ID=$(read_state "AGENT_ID")
AGENT_OBJECT_ID=$(read_state "AGENT_OBJECT_ID")
AGENT_USER_ID=$(read_state "AGENT_USER_ID")
AGENT_USER_UPN=$(read_state "AGENT_USER_UPN")
PROV_CLIENT_ID=$(read_state "PROVISIONER_CLIENT_ID")

if [ -z "$BLUEPRINT_APP_ID" ] || [ -z "$AGENT_ID" ]; then
    fail "Agent Identity provisioning completed but IDs not found in state file"
fi

success "Blueprint: $BLUEPRINT_APP_ID"
success "Agent ID:  $AGENT_ID"
success "Agent User: ${AGENT_USER_UPN:-not created} (${AGENT_USER_ID:-n/a})"

# ════════════════════════════════════════════════════════════════════════════
# Step 6: Create Blueprint client secret (for three-hop flow)
# ════════════════════════════════════════════════════════════════════════════
step 6 "Managing Blueprint client secret"

# The Blueprint needs a client secret for Hop 1 of the three-hop flow.
# In production, use a certificate or Managed Identity FIC instead.
BLUEPRINT_SECRET=$(read_state "BLUEPRINT_SECRET")

if [ -n "$BLUEPRINT_SECRET" ]; then
    success "Using cached Blueprint secret from state file"
else
    echo "  Creating new client secret on Blueprint..."
    BP_CRED_JSON=$(az ad app credential reset \
        --id "$BLUEPRINT_OBJECT_ID" \
        --display-name "Openclaw Device" \
        --append \
        -o json)
    BLUEPRINT_SECRET=$("$PYTHON" -c "import sys,json; print(json.loads(sys.stdin.read())['password'])" <<< "$BP_CRED_JSON")

    if [ -z "$BLUEPRINT_SECRET" ]; then
        fail "Could not create Blueprint client secret"
    fi

    # Persist in state file (not keychain — simpler, same security for dev)
    "$PYTHON" -c "
import json, pathlib
state_file = pathlib.Path('$PROJECT_ROOT/.openclaw-state.json')
data = json.loads(state_file.read_text()) if state_file.is_file() else {}
data['BLUEPRINT_SECRET'] = '$BLUEPRINT_SECRET'
state_file.write_text(json.dumps(data, indent=2) + '\n')
"
    success "Blueprint secret created and stored in state file"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 7: Python venv + dependencies + .env
# ════════════════════════════════════════════════════════════════════════════
step 7 "Setting up Python virtual environment and writing .env"

if [ ! -d ".venv" ]; then
    "$PYTHON" -m venv .venv
    success "Created .venv"
else
    success "Virtual environment .venv already exists"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

pip install --quiet -e ".[dev]"
success "Installed dependencies (including dev)"

cat > .env << EOF
# Openclaw Identity Research — generated by scripts/setup.sh
# Uses Agent User (three-hop flow) — no OBO, no device-code
# DO NOT commit this file (it is in .gitignore)

OPENCLAW_TENANT_ID=$TENANT_ID
OPENCLAW_BLUEPRINT_APP_ID=$BLUEPRINT_APP_ID
OPENCLAW_BLUEPRINT_OBJECT_ID=$BLUEPRINT_OBJECT_ID
OPENCLAW_BLUEPRINT_SECRET=$BLUEPRINT_SECRET
OPENCLAW_AGENT_ID=$AGENT_ID
OPENCLAW_AGENT_OBJECT_ID=$AGENT_OBJECT_ID
OPENCLAW_AGENT_USER_ID=${AGENT_USER_ID:-}
OPENCLAW_AGENT_USER_UPN=${AGENT_USER_UPN:-}
OPENCLAW_HUMAN_USER_ID=$HUMAN_USER_ID
OPENCLAW_HUMAN_UPN=$HUMAN_UPN
OPENCLAW_PROVISIONER_APP_ID=$PROV_CLIENT_ID
OPENCLAW_LOG_LEVEL=INFO
EOF

chmod 600 .env
success ".env file created (chmod 600)"

# ════════════════════════════════════════════════════════════════════════════
# Step 8: Summary
# ════════════════════════════════════════════════════════════════════════════
step 8 "Setup complete — summary"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Setup complete!                                             ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Provisioner: ${BLUE}$PROV_CLIENT_ID${NC}"
echo -e "  Blueprint:   ${BLUE}$BLUEPRINT_APP_ID${NC}"
echo -e "  Agent ID:    ${BLUE}$AGENT_ID${NC}"
echo -e "  Agent User:  ${BLUE}${AGENT_USER_UPN:-not created}${NC} (${AGENT_USER_ID:-n/a})"
echo -e "  Human User:  ${BLUE}$HUMAN_UPN${NC}"
echo -e "  Auth Flow:   ${BLUE}Three-hop (Blueprint → Agent Identity → Agent User)${NC}"
echo ""

if [ -z "$AGENT_USER_ID" ]; then
    echo -e "  ${YELLOW}⚠️  Agent User was not created — check permissions above${NC}"
    echo ""
fi

AGENT_LICENSE=$(read_state "AGENT_USER_LICENSE_SKU")
if [ -n "$AGENT_LICENSE" ]; then
    echo -e "  License:     ${BLUE}$AGENT_LICENSE${NC} (Teams provisioning in 10-15 min)"
fi

echo -e "  ${YELLOW}NEXT STEPS:${NC}"
echo -e "  1. Wait 10-15 min for Teams/mailbox provisioning (if license was just assigned)"
echo -e "  2. Run tests: ${BLUE}pytest -v${NC}"
echo -e "  3. Start the MCP server via Copilot CLI config:"
echo ""
echo -e "     ${BLUE}~/.copilot/mcp-config.json${NC}"
echo ""
echo '     {'
echo '       "mcpServers": {'
echo '         "openclaw": {'
echo "           \"command\": \"$PYTHON\","
echo '           "args": ["-m", "openclaw.mcp_server"],'
echo "           \"cwd\": \"$PROJECT_ROOT\","
echo '           "env": {}'
echo '         }'
echo '       }'
echo '     }'
echo ""
