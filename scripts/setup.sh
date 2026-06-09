#!/usr/bin/env bash
# EntraBot Identity Research — one-command setup
#
# Thin orchestrator that calls Python scripts for Entra provisioning.
# No device-code flow.  No OBO.  The Agent User authenticates autonomously
# via the three-hop flow: Blueprint → Agent Identity → Agent User.
#
# Architecture (borrowed from a related internal project):
#   1. entra_provisioning.py  — creates/manages the dedicated provisioner app
#   2. create_entra_agent_ids.py — Blueprint + Agent Identity + Agent User
#   3. This script — venv + .env + tests
#
# State is persisted in .entrabot-state.json so re-runs are idempotent.
#
# NOTE: The Agent User requires a Teams-capable M365 license (E3/E5/Teams
# Enterprise) to be assigned AFTER this script runs. License assignment is
# a manual step in the Entra admin center or via Graph API.
set -euo pipefail

TOTAL_STEPS=8

# ── Argument parsing ───────────────────────────────────────────────────────

SWITCH_USER=false
TEAMS_USER_EMAIL=""
SHOW_HELP=false
DIAGNOSE=false
SKIP_SMOKE=false
# Local is the default for operational storage; users opt in to cloud
# via --use-cloud-memory. The old --keep-memory-local flag is accepted as a
# no-op alias so existing scripts keep working.
KEEP_MEMORY_LOCAL=true
USE_CLOUD_MEMORY=false
NEW_CHAIN=false
USE_BLUEPRINT=""
UPN_SUFFIX=""
AGENT_USER_UPN=""
WITH_STORAGE_ACCOUNT=""
WITH_CONTAINER=""
CREATE_NEW_STORAGE=false
WITH_A365_WORK_IQ=false
CONFIGURE_A365_WORK_IQ=false
A365_AGENT_NAME="EntraBot Code Agent"
A365_WORK_IQ_MCP_SERVERS=(mcp_WordServer mcp_ODSPRemoteServer)

SETUP_STATUS=false
STATUS_ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--status" ]; then
        SETUP_STATUS=true
    else
        STATUS_ARGS+=("$arg")
    fi
done

if [ "$SETUP_STATUS" = true ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    exec "$PROJECT_ROOT/status.sh" "${STATUS_ARGS[@]}"
fi

for arg in "$@"; do
    case $arg in
        --switch-user)
            SWITCH_USER=true
            ;;
        --teams-user=*)
            TEAMS_USER_EMAIL="${arg#--teams-user=}"
            ;;
        --use-cloud-memory)
            USE_CLOUD_MEMORY=true
            KEEP_MEMORY_LOCAL=false
            ;;
        --keep-memory-local)
            # Kept for backwards compatibility; local is the default now.
            KEEP_MEMORY_LOCAL=true
            USE_CLOUD_MEMORY=false
            ;;
        --new)
            NEW_CHAIN=true
            ;;
        --use-blueprint=*)
            USE_BLUEPRINT="${arg#--use-blueprint=}"
            ;;
        --with-upn-suffix=*)
            UPN_SUFFIX="${arg#--with-upn-suffix=}"
            ;;
        --agent-user-upn=*)
            AGENT_USER_UPN="${arg#--agent-user-upn=}"
            ;;
        --with-storage-account=*)
            WITH_STORAGE_ACCOUNT="${arg#--with-storage-account=}"
            ;;
        --with-container=*)
            WITH_CONTAINER="${arg#--with-container=}"
            ;;
        --create-new-storage)
            CREATE_NEW_STORAGE=true
            ;;
        --with-a365-work-iq)
            WITH_A365_WORK_IQ=true
            ;;
        --configure-a365-work-iq)
            WITH_A365_WORK_IQ=true
            CONFIGURE_A365_WORK_IQ=true
            ;;
        --a365-agent-name=*)
            A365_AGENT_NAME="${arg#--a365-agent-name=}"
            ;;
        --diagnose)
            DIAGNOSE=true
            ;;
        --status)
            # Handled before full setup argument parsing.
            ;;
        --skip-smoke)
            SKIP_SMOKE=true
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

# Mutex: --create-new-storage and --with-storage-account both pin the
# storage account name; only one can win.
if [ "$CREATE_NEW_STORAGE" = true ] && [ -n "$WITH_STORAGE_ACCOUNT" ]; then
    echo "ERROR: --create-new-storage and --with-storage-account are mutually exclusive." >&2
    exit 2
fi

if [ "$SHOW_HELP" = true ]; then
    echo "Usage: ./scripts/setup.sh [OPTIONS]"
    echo ""
    echo "Prerequisites (az CLI, Python 3.12+, git):"
    echo "  macOS:   ./scripts/prereqs-macos.sh"
    echo "  Windows: .\\scripts\\prereqs-windows.ps1"
    echo "  Linux:   install python3.12, git, and the Azure CLI via your package manager"
    echo ""
    echo "Options:"
    echo ""
    echo "Identity mode (one required):"
    echo "  --new                  Create a completely new Agent Identity chain."
    echo "                         Provisions fresh Blueprint, Agent Identity, Agent User."
    echo "                         The existing chain is NOT affected."
    echo "  --use-blueprint=ID     Attach to an existing Blueprint by App ID."
    echo "                         Generates a new cert for this machine and adds it"
    echo "                         to the Blueprint. Reuses existing Agent Identity"
    echo "                         and Agent User. Use when switching machines, OR"
    echo "                         when switching this machine to a different Blueprint"
    echo "                         (the stale Agent Identity / User / cert thumbprint"
    echo "                         are wiped from local state so create_entra_agent_ids.py"
    echo "                         rediscovers everything under the new Blueprint)."
    echo ""
    echo "  --with-upn-suffix=NAME Agent User UPN suffix (required with --new)."
    echo "                         e.g., --with-upn-suffix=sati-agent"
    echo "                         produces: entrabot-agent-sati-agent@yourdomain.com"
    echo "                         If omitted with --new, you will be prompted."
    echo "                         Also supported with --use-blueprint to select"
    echo "                         an existing suffixed Agent User under that Blueprint."
    echo "  --agent-user-upn=UPN   Explicit Agent User UPN. With --use-blueprint,"
    echo "                         selects an existing Agent User to reuse; with"
    echo "                         --new, creates exactly that UPN, e.g."
    echo "                         entrabot-agent-sati-agent@yourtenant.onmicrosoft.com."
    echo "  --switch-user          Sign in as a different user before setup."
    echo "                         The new user becomes the agent's owner and sponsor."
    echo "  --teams-user=EMAIL     Set a different user as the Teams chat recipient."
    echo "                         The az CLI user remains the admin/provisioner."
    echo "                         e.g., --teams-user=user@yourtenant.onmicrosoft.com"
    echo "  --use-cloud-memory     Opt in to Azure Blob Storage for operational data"
    echo "                         (interactions log, watched chats, email cursor)."
    echo "                         Provisions a resource group, storage account, and"
    echo "                         container scoped to the Agent User. Recommended"
    echo "                         for durable, cross-device operation. ADR-005."
    echo "                         Also unsets ENTRABOT_KEEP_MEMORY_LOCAL so the"
    echo "                         PreToolUse hook blocks local Claude Code auto-memory"
    echo "                         writes — mind content routes through persona-sati."
    echo "  --keep-memory-local    [default] Operational data stays on the local"
    echo "                         filesystem (~/.entrabot/data). Kept as an explicit"
    echo "                         flag for backwards compatibility; this is now the"
    echo "                         default behavior. Pass --use-cloud-memory to override."
    echo "                         Also sets ENTRABOT_KEEP_MEMORY_LOCAL=true, which"
    echo "                         opts the PreToolUse hook out so Claude Code's local"
    echo "                         auto-memory directory is writable again."
    echo ""
    echo "  Cloud storage targeting (used with --use-cloud-memory):"
    echo "  --with-storage-account=NAME"
    echo "                         Use the named Azure Storage Account instead of the"
    echo "                         deterministic per-tenant default (entclaw<hash>)."
    echo "                         The account is created if it doesn't exist, in"
    echo "                         resource group 'entrabot-rg'. Useful for attaching"
    echo "                         to a pre-existing account in a different naming"
    echo "                         convention. Mutually exclusive with"
    echo "                         --create-new-storage."
    echo "  --with-container=NAME  Use the named blob container instead of the"
    echo "                         agent-<oid> default. The Agent User still gets"
    echo "                         RBAC scoped to this container only."
    echo "  --create-new-storage   Force creation of a fresh randomly-suffixed"
    echo "                         storage account even when the deterministic-name"
    echo "                         one already exists. Use this when you want a"
    echo "                         second isolated cloud-memory store in the same"
    echo "                         tenant. Mutually exclusive with"
    echo "                         --with-storage-account."
    echo ""
    echo "  Agent 365 Work IQ:"
    echo "  --with-a365-work-iq    Install or update the Microsoft Agent 365"
    echo "                         DevTools CLI (a365) via dotnet global tools."
    echo "                         Requires a .NET SDK on PATH."
    echo "  --configure-a365-work-iq"
    echo "                         Run the interactive developer setup for Work IQ"
    echo "                         Word: a365 develop add-mcp-servers mcp_WordServer,"
    echo "                         a365 setup permissions mcp against the existing"
    echo "                         Entrabot Blueprint, then manifest validation."
    echo "  --a365-agent-name=NAME Deprecated compatibility flag; Work IQ setup now"
    echo "                         uses the existing Entrabot Blueprint from state."
    echo "  --help, -h             Show this help"
    echo ""
    echo "Diagnostics:"
    echo "  --status               Skip setup and run ./status.sh. Extra args are"
    echo "                         forwarded to show_agent_status.py, e.g."
    echo "                         ./scripts/setup.sh --status --json"
    echo "  --diagnose             Skip provisioning. Run a full health check"
    echo "                         (state file, certificate, three-hop token,"
    echo "                         Graph identity, Teams scope, MCP wiring) and"
    echo "                         print a pass/fail report. Use this to debug"
    echo "                         a previously-completed install. Exits non-zero"
    echo "                         if any check fails."
    echo "  --skip-smoke           Skip the post-setup smoke test (token + Graph"
    echo "                         identity + Teams scope). Useful in CI where"
    echo "                         no live tenant is reachable."
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
err()     { echo -e "  ${RED}❌ $1${NC}"; }
MIGRATION_FAILED=false
fail()    { echo -e "  ${RED}❌ $1${NC}"; exit 1; }

# ── Resolve project root ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   EntraBot Identity Research — Setup         ║${NC}"
echo -e "${GREEN}║   (Agent User — no OBO, no device-code flow) ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"

# ── --diagnose short-circuit ────────────────────────────────────────────────
# Read existing .env + state, run preflight.run_diagnostics, exit. No
# provisioning happens. Use this to debug a previously-completed install.
if [ "$DIAGNOSE" = true ]; then
    echo ""
    echo -e "${BLUE}Running diagnostics (provisioning skipped)...${NC}"
    if [ ! -d "$PROJECT_ROOT/.venv" ]; then
        fail "No .venv found. Run setup.sh once before --diagnose."
    fi
    set +e
    # shellcheck disable=SC1091
    if [ -f "$PROJECT_ROOT/.env" ]; then
        set -a; . "$PROJECT_ROOT/.env"; set +a
    fi
    "$PROJECT_ROOT/.venv/bin/python" - <<PY
import sys
from pathlib import Path

from entrabot.config import EntraBotConfig
from entrabot.preflight import format_report, overall_exit_code, run_diagnostics

config = EntraBotConfig.from_env()
project_root = Path("$PROJECT_ROOT")
checks = run_diagnostics(
    config,
    state_path=project_root / ".entrabot-state.json",
    expected_binary=project_root / ".venv" / "bin" / "entrabot-mcp",
    claude_mcp_path=project_root / ".mcp.json",
    copilot_mcp_path=Path.home() / ".copilot" / "mcp-config.json",
)
print(format_report(checks, color=sys.stdout.isatty()))
sys.exit(overall_exit_code(checks))
PY
    DIAG_EXIT=$?
    set -e
    echo ""
    if [ "$DIAG_EXIT" -eq 0 ]; then
        echo -e "${GREEN}Diagnostics: all checks passed.${NC}"
    else
        echo -e "${RED}Diagnostics: one or more checks failed (see above).${NC}"
    fi
    exit "$DIAG_EXIT"
fi

# ── Helper: read value from .entrabot-state.json ───────────────────────────

read_state() {
    local key="$1"
    "$PYTHON" -c "
import json, pathlib, sys
state_file = pathlib.Path('$PROJECT_ROOT/.entrabot-state.json')
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

if [ "$CONFIGURE_A365_WORK_IQ" = true ] && ! command -v pwsh &>/dev/null; then
    MISSING+=("PowerShell 7+ (pwsh) - macOS: brew install powershell")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    for m in "${MISSING[@]}"; do
        echo -e "  ${RED}✗ $m${NC}"
    done
    echo ""
    if [ "$(uname -s)" = "Darwin" ]; then
        echo -e "  ${YELLOW}One-shot installer for macOS:${NC}"
        echo -e "      ${YELLOW}./scripts/prereqs-macos.sh${NC}"
        echo -e "  ${YELLOW}(installs Xcode CLT, Python 3.12+, git, Azure CLI, and optionally${NC}"
        echo -e "  ${YELLOW}.NET SDK + a365 CLI + PowerShell via Homebrew. Pass --core-only${NC}"
        echo -e "  ${YELLOW}to skip the optional Microsoft Agent 365 tooling.)${NC}"
        echo ""
        echo -e "  ${YELLOW}Or install manually with Homebrew:${NC}"
        echo -e "      ${YELLOW}brew install python@3.12 git azure-cli${NC}"
    elif [ "$(uname -s)" = "Linux" ]; then
        echo -e "  ${YELLOW}Install via your distro's package manager:${NC}"
        echo -e "      ${YELLOW}# Debian/Ubuntu${NC}"
        echo -e "      ${YELLOW}sudo apt install python3.12 git${NC}"
        echo -e "      ${YELLOW}curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash${NC}"
    else
        echo -e "  ${YELLOW}For Windows, see scripts/prereqs-windows.ps1.${NC}"
    fi
    echo ""
    fail "Install the missing prerequisites above and re-run."
fi

success "az CLI found ($(az version --query '"azure-cli"' -o tsv || echo '?'))"
success "$PYTHON found ($PY_VER)"
success "git found ($(git --version | awk '{print $3}'))"

ensure_a365_cli() {
    if ! command -v dotnet &>/dev/null; then
        fail "--with-a365-work-iq requires a .NET SDK. Install it from https://dotnet.microsoft.com/download and re-run."
    fi

    export PATH="$PATH:$HOME/.dotnet/tools"
    if command -v a365 &>/dev/null; then
        echo "  Updating Microsoft Agent 365 DevTools CLI (a365)..."
        if dotnet tool update --global Microsoft.Agents.A365.DevTools.Cli; then
            success "a365 CLI updated"
        else
            warn "a365 is already on PATH, but dotnet global-tool update did not complete"
        fi
    else
        echo "  Installing Microsoft Agent 365 DevTools CLI (a365)..."
        dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli
        export PATH="$PATH:$HOME/.dotnet/tools"
        if ! command -v a365 &>/dev/null; then
            fail "a365 installed but is not on PATH. Add ~/.dotnet/tools to PATH and re-run."
        fi
        success "a365 CLI installed"
    fi
}

if [ "$WITH_A365_WORK_IQ" = true ]; then
    export ENTRABOT_ASSIGN_WORK_IQ_LICENSE=1
    ensure_a365_cli
fi

ensure_a365_tooling_manifest() {
    local manifest="$PROJECT_ROOT/ToolingManifest.json"
    if [ -f "$manifest" ]; then
        return
    fi
    printf '{"mcpServers":[]}\n' > "$manifest"
    success "Created minimal ToolingManifest.json for A365 Work IQ"
}

write_a365_config() {
    local client_app_id
    local agent_identity_display_name
    client_app_id=$(az ad app list --display-name "Agent 365 CLI" --query "[0].appId" -o tsv 2>/dev/null || echo "")
    if [ -z "$client_app_id" ]; then
        fail "Agent 365 CLI app was not found. Re-run and choose C during 'a365 setup requirements', or create the app before configuring Work IQ."
    fi
    if [ -z "${BLUEPRINT_APP_ID:-}" ]; then
        fail "Blueprint ID not found. Entrabot provisioning must complete before configuring A365 Work IQ."
    fi
    agent_identity_display_name=$(az ad sp show --id "$AGENT_ID" --query displayName -o tsv 2>/dev/null || echo "")
    if [ -z "$agent_identity_display_name" ]; then
        fail "Agent Identity display name not found for $AGENT_ID. Entrabot provisioning must complete before configuring A365 Work IQ."
    fi

    "$PYTHON" - "$PROJECT_ROOT" "$TENANT_ID" "$client_app_id" "$BLUEPRINT_APP_ID" "${BLUEPRINT_OBJECT_ID:-}" "${AGENT_ID:-}" "$agent_identity_display_name" <<'PY'
import json
import sys
from pathlib import Path

project_root = Path(sys.argv[1])
tenant_id, client_app_id, blueprint_app_id, blueprint_object_id, agent_id = sys.argv[2:7]
agent_identity_display_name = sys.argv[7]
config_path = project_root / "a365.config.json"

data = {}
if config_path.is_file():
    data = json.loads(config_path.read_text(encoding="utf-8"))

data.update(
    {
        "tenantId": tenant_id,
        "clientAppId": client_app_id,
        "agentBlueprintId": blueprint_app_id,
        "agentBlueprintDisplayName": "EntraBot Code Agent",
        "agentIdentityDisplayName": agent_identity_display_name,
        "deploymentProjectPath": str(project_root),
    }
)
if blueprint_object_id:
    data["agentBlueprintObjectId"] = blueprint_object_id
if agent_id:
    data["agentIdentityId"] = agent_id

config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
    success "A365 config points to existing Entrabot Blueprint"
}

configure_a365_work_iq() {
    echo "  Configuring Microsoft Agent 365 Work IQ Word + OneDrive/SharePoint..."
    echo "  This may open an interactive Microsoft sign-in/device-code flow."
    echo "  If the Agent 365 CLI app is missing, choose C when prompted to create it."
    ensure_a365_tooling_manifest
    write_a365_config
    if ! a365 setup requirements; then
        fail "a365 setup requirements failed"
    fi
    a365 develop add-mcp-servers "${A365_WORK_IQ_MCP_SERVERS[@]}" --project-path "$PROJECT_ROOT"
    "$SCRIPT_PYTHON" "$PROJECT_ROOT/scripts/ensure_a365_work_iq_permissions.py" \
        --blueprint-app-id "$BLUEPRINT_APP_ID"
    A365_PERMISSIONS_LOG=$(mktemp)
    set +e
    a365 setup permissions mcp 2>&1 | tee "$A365_PERMISSIONS_LOG"
    A365_PERMISSIONS_RC=${PIPESTATUS[0]}
    set -e
    if [ "$A365_PERMISSIONS_RC" -ne 0 ]; then
        rm -f "$A365_PERMISSIONS_LOG"
        fail "a365 setup permissions mcp failed"
    fi
    if grep -q "OAuth2 grants failed" "$A365_PERMISSIONS_LOG"; then
        rm -f "$A365_PERMISSIONS_LOG"
        fail "a365 setup permissions mcp reported OAuth2 grants failed; Work IQ permissions are incomplete. Resolve the Agent 365 Tools service principal/admin-consent issue, then rerun setup.sh."
    fi
    rm -f "$A365_PERMISSIONS_LOG"
    if ! "$SCRIPT_PYTHON" "$PROJECT_ROOT/scripts/spike_a365_work_iq.py"; then
        fail "A365 Work IQ manifest validation failed"
    fi
    success "A365 Work IQ Word manifest configured"
}

# ════════════════════════════════════════════════════════════════════════════
# Step 2: Verify Azure login
# ════════════════════════════════════════════════════════════════════════════
step 2 "Verifying Azure login"

if [ "$SWITCH_USER" = true ]; then
    echo "  Signing in as a new user (the new user will own/sponsor the agent)..."
    az login
fi

if ! az account show &>/dev/null; then
    fail "Not logged in to Azure CLI. Run 'az login' first."
fi

TENANT_ID=$(az account show --query "tenantId" -o tsv)
ACCOUNT_NAME=$(az account show --query "name" -o tsv)
HUMAN_UPN=$(az account show --query "user.name" -o tsv || echo "")
HUMAN_USER_ID=$(az ad signed-in-user show --query "id" -o tsv || echo "")

if [ -z "$HUMAN_USER_ID" ]; then
    fail "Could not determine signed-in user ID. Ensure 'az login' is done with a user account."
fi

# ── License preflight (informational, never blocks) ────────────────────────
# Check whether this tenant has Teams and, for A365 runs, Copilot seats *before* we provision
# anything. Warns are non-fatal — create_entra_agent_ids.py performs the actual
# assignment later, after the Agent User exists.
if command -v az >/dev/null 2>&1 && [ -d "$PROJECT_ROOT/.venv" ]; then
    GRAPH_TOKEN=$(az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv 2>/dev/null || echo "")
    if [ -n "$GRAPH_TOKEN" ]; then
        set +e
        "$PROJECT_ROOT/.venv/bin/python" - "$GRAPH_TOKEN" <<'PY' 2>&1 || true
import sys
from entrabot.preflight import (
    check_teams_license_availability,
    format_report,
)

token = sys.argv[1]
teams_result = check_teams_license_availability(token)
# Only print warn/skip — pass is silent so we don't add noise on green runs.
if teams_result.status == "pass":
    print(f"  \033[0;32m✅ Teams license available: {teams_result.detail}\033[0m")
else:
    print(format_report([teams_result], color=sys.stdout.isatty()))
PY
        if [ "$WITH_A365_WORK_IQ" = true ]; then
            "$PROJECT_ROOT/.venv/bin/python" - "$GRAPH_TOKEN" <<'PY' 2>&1 || true
import sys
from entrabot.preflight import (
    check_copilot_license_availability,
    format_report,
)

token = sys.argv[1]
copilot_result = check_copilot_license_availability(token)
if copilot_result.status == "pass":
    print(f"  \033[0;32m✅ Copilot license available: {copilot_result.detail}\033[0m")
else:
    print(format_report([copilot_result], color=sys.stdout.isatty()))
PY
        fi
        set -e
    fi
fi

# If --teams-user was specified, resolve user(s) for Teams (comma-separated for group chat)
HUMAN_USER_IDS=""
HUMAN_UPNS=""
HUMAN_USER_TENANT_IDS=""
HUMAN_USER_MAILS=""
HUMAN_USER_TYPES=""
if [ -n "$TEAMS_USER_EMAIL" ]; then
    IFS=',' read -ra TEAMS_USERS <<< "$TEAMS_USER_EMAIL"
    RESOLVED_IDS=()
    RESOLVED_UPNS=()
    RESOLVED_TENANT_IDS=()
    RESOLVED_MAILS=()
    RESOLVED_TYPES=()
    for TU in "${TEAMS_USERS[@]}"; do
        TU=$(echo "$TU" | xargs)  # trim whitespace
        # Query user details including userType and mail for guest detection
        TU_JSON=$(az ad user show --id "$TU" --query "{id:id, userType:userType, mail:mail, upn:userPrincipalName}" -o json 2>/dev/null) || true
        if [ -z "$TU_JSON" ]; then
            fail "Could not find Teams user '$TU' in Entra. Check the email/ID."
        fi
        TU_ID=$(echo "$TU_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
        TU_TYPE=$(echo "$TU_JSON" | python3 -c "import sys,json; v=json.load(sys.stdin)['userType']; print(v if v else '')")
        TU_MAIL=$(echo "$TU_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mail') or '')")
        TU_UPN=$(echo "$TU_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['upn'])")

        # If userType is null/empty, infer from UPN pattern (#EXT# = guest)
        if [ -z "$TU_TYPE" ]; then
            if echo "$TU_UPN" | grep -q '#EXT#'; then
                TU_TYPE="Guest"
            else
                TU_TYPE="Member"
            fi
        fi

        RESOLVED_IDS+=("$TU_ID")
        RESOLVED_UPNS+=("$TU")
        RESOLVED_MAILS+=("$TU_MAIL")
        RESOLVED_TYPES+=("$TU_TYPE")

        if [ "$TU_TYPE" = "Guest" ]; then
            success "  User '$TU' is a B2B Guest — will use federated chat (Example 7)"
            # Extract home domain from UPN: user_domain.com#EXT#@tenant.onmicrosoft.com
            HOME_DOMAIN=$(echo "$TU_UPN" | python3 -c "
import sys
upn = sys.stdin.read().strip()
# Pattern: user_domain.com#EXT#@tenant.onmicrosoft.com
if '#EXT#' in upn:
    local_part = upn.split('#EXT#')[0]  # user_domain.com
    # Domain is after the last underscore
    parts = local_part.rsplit('_', 1)
    print(parts[1] if len(parts) > 1 else '')
else:
    print('')
")
            if [ -n "$HOME_DOMAIN" ]; then
                # Look up home tenant GUID via OpenID discovery
                HOME_TENANT_ID=$(curl -s "https://login.microsoftonline.com/${HOME_DOMAIN}/.well-known/openid-configuration" \
                    | python3 -c "import sys,json; issuer=json.load(sys.stdin).get('issuer',''); parts=issuer.rstrip('/').split('/'); print(parts[-1] if len(parts)>3 else '')")
                if [ -n "$HOME_TENANT_ID" ]; then
                    success "  Guest '$TU' → home tenant: $HOME_DOMAIN ($HOME_TENANT_ID)"
                    RESOLVED_TENANT_IDS+=("$HOME_TENANT_ID")
                else
                    warn "Could not resolve home tenant for guest '$TU' (domain: $HOME_DOMAIN)"
                    RESOLVED_TENANT_IDS+=("")
                fi
            else
                warn "Could not extract home domain from guest UPN: $TU_UPN"
                RESOLVED_TENANT_IDS+=("")
            fi
        else
            # In-tenant member — no tenantId needed
            RESOLVED_TENANT_IDS+=("")
        fi
    done
    # Join arrays with commas
    HUMAN_USER_IDS=$(IFS=','; echo "${RESOLVED_IDS[*]}")
    HUMAN_UPNS=$(IFS=','; echo "${RESOLVED_UPNS[*]}")
    HUMAN_USER_TENANT_IDS=$(IFS=','; echo "${RESOLVED_TENANT_IDS[*]}")
    HUMAN_USER_MAILS=$(IFS=','; echo "${RESOLVED_MAILS[*]}")
    HUMAN_USER_TYPES=$(IFS=','; echo "${RESOLVED_TYPES[*]}")
    # First user is the primary (backward compat)
    HUMAN_USER_ID="${RESOLVED_IDS[0]}"
    HUMAN_UPN="${RESOLVED_UPNS[0]}"
    success "Admin:      $(az account show --query 'user.name' -o tsv) (provisioning)"
    if [ ${#TEAMS_USERS[@]} -gt 1 ]; then
        success "Teams users: $HUMAN_UPNS (group chat)"
    else
        success "Teams user: $HUMAN_UPN ($HUMAN_USER_ID) [type: ${RESOLVED_TYPES[0]}]"
    fi
else
    HUMAN_USER_IDS="$HUMAN_USER_ID"
    HUMAN_UPNS="$HUMAN_UPN"
    HUMAN_USER_TENANT_IDS=""
    HUMAN_USER_MAILS="$HUMAN_UPN"
    HUMAN_USER_TYPES="Member"
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

# ── Validate identity mode ────────────────────────────────────────────────
if [ "$NEW_CHAIN" = true ] && [ -n "$USE_BLUEPRINT" ]; then
    echo "ERROR: --new and --use-blueprint are mutually exclusive." >&2
    echo "  --new creates a fresh identity chain." >&2
    echo "  --use-blueprint attaches to an existing one." >&2
    exit 1
fi

if [ "$NEW_CHAIN" = false ] && [ -z "$USE_BLUEPRINT" ]; then
    echo ""
    echo "ERROR: No identity mode specified." >&2
    echo "" >&2
    echo "  Choose one:" >&2
    echo "    --new --with-upn-suffix=NAME    Create a fresh identity chain" >&2
    echo "    --use-blueprint=APP_ID          Attach to an existing Blueprint" >&2
    echo "" >&2
    exit 1
fi

# ── Handle --use-blueprint: reuse existing identity, add cert for this machine ─
#
# Two scenarios this has to handle cleanly:
#   (a) Fresh machine / no state — just record BLUEPRINT_APP_ID; the rest of
#       the script discovers everything under it.
#   (b) Existing state pointing at a DIFFERENT Blueprint — treat this as a
#       switch. create_entra_agent_ids.find_existing_blueprint() prefers the
#       cached BLUEPRINT_OBJECT_ID over the APP_ID filter, so if we only
#       rewrote BLUEPRINT_APP_ID the stale OBJECT_ID would silently pin us
#       back to the old Blueprint. Wipe all identity-derived state to force
#       fresh discovery against the new Blueprint. Keep PROVISIONER_* (the
#       helper app is machine-scoped, unaffected by the switch).
if [ -n "$USE_BLUEPRINT" ] && [ "$NEW_CHAIN" = false ]; then
    STATE_FILE="$PROJECT_ROOT/.entrabot-state.json"
    CURRENT_BP=$(read_state "BLUEPRINT_APP_ID")

    if [ -n "$CURRENT_BP" ] && [ "$CURRENT_BP" != "$USE_BLUEPRINT" ]; then
        BACKUP="$STATE_FILE.bak.$(date +%Y%m%d-%H%M%S)"
        cp "$STATE_FILE" "$BACKUP"
        echo -e "  ${YELLOW}Switching Blueprint: ${CURRENT_BP} → ${USE_BLUEPRINT}${NC}"
        echo -e "  ${YELLOW}Backed up prior state to $(basename "$BACKUP")${NC}"
        echo -e "  ${YELLOW}Note: this machine's cert on the OLD Blueprint is NOT revoked."
        echo -e "        Remove it manually with ./scripts/cleanup-orphans.sh or via"
        echo -e "        'az ad app credential delete' if you want a clean break.${NC}"
        "$SCRIPT_PYTHON" -c "
import json, pathlib
sf = pathlib.Path('$STATE_FILE')
data = json.loads(sf.read_text()) if sf.is_file() else {}
# Keep provisioner app + tenant; drop everything tied to the old chain
keep = {
    k: v for k, v in data.items()
    if k.startswith('PROVISIONER') or k == 'TENANT_ID'
}
keep['BLUEPRINT_APP_ID'] = '$USE_BLUEPRINT'
sf.write_text(json.dumps(keep, indent=2))
print('  Cleared stale Blueprint/Agent/User state (kept Provisioner + tenant)')
"
    else
        echo -e "  ${GREEN}Using existing Blueprint: ${USE_BLUEPRINT}${NC}"
        # Fresh machine or re-run with the same ID — just record it.
        "$SCRIPT_PYTHON" -c "
import json, pathlib
sf = pathlib.Path('$STATE_FILE')
data = json.loads(sf.read_text()) if sf.is_file() else {}
data['BLUEPRINT_APP_ID'] = '$USE_BLUEPRINT'
sf.write_text(json.dumps(data, indent=2))
"
        echo "  State file updated with Blueprint ID"
    fi
    if [ -n "$AGENT_USER_UPN" ]; then
        export ENTRABOT_AGENT_USER_UPN="$AGENT_USER_UPN"
        echo -e "  ${GREEN}Using existing Agent User UPN: ${AGENT_USER_UPN}${NC}"
    elif [ -n "$UPN_SUFFIX" ]; then
        export _ENTRABOT_UPN_SUFFIX="$UPN_SUFFIX"
        echo -e "  ${GREEN}Using existing Agent User suffix: ${UPN_SUFFIX}${NC}"
    fi
    # From here create_entra_agent_ids.py discovers Agent Identity + Agent User
    # under the chosen Blueprint. Step 6 generates/reuses a cert as appropriate.
fi

# ── Handle --new: back up state, force fresh identity chain ───────────────
if [ "$NEW_CHAIN" = true ]; then
    STATE_FILE="$PROJECT_ROOT/.entrabot-state.json"
    if [ -f "$STATE_FILE" ]; then
        BACKUP="$STATE_FILE.bak.$(date +%Y%m%d-%H%M%S)"
        cp "$STATE_FILE" "$BACKUP"
        echo -e "  ${YELLOW}--new: backed up state to $(basename "$BACKUP")${NC}"
        # Clear identity keys but keep the provisioner app (it can be reused)
        "$SCRIPT_PYTHON" -c "
import json, pathlib
sf = pathlib.Path('$STATE_FILE')
data = json.loads(sf.read_text()) if sf.is_file() else {}
# Keep provisioner app — it's a helper, not part of the agent identity
keep = {k: v for k, v in data.items() if k.startswith('PROVISIONER')}
sf.write_text(json.dumps(keep, indent=2))
print('  Cleared identity state (kept provisioner app)')
"
    fi
    # Resolve UPN — explicit UPN wins; otherwise suffix comes from flag or prompt.
    # Example: ./scripts/setup.sh --new --agent-user-upn=entrabot-agent@yourtenant.onmicrosoft.com
    if [ -n "$AGENT_USER_UPN" ]; then
        export ENTRABOT_AGENT_USER_UPN="$AGENT_USER_UPN"
        echo -e "  ${GREEN}--new: will create explicit Agent User UPN '${AGENT_USER_UPN}'${NC}"
    elif [ -z "$UPN_SUFFIX" ]; then
        echo ""
        echo -e "  ${YELLOW}--new requires a UPN suffix to avoid collision with existing agents.${NC}"
        echo "  This becomes part of the Agent User's email-like identity:"
        echo "    entrabot-<suffix>@yourdomain.com"
        echo ""
        echo "  Examples: sati-agent, dev-bot, test-agent"
        echo ""
        printf "  Enter UPN suffix: "
        read -r UPN_SUFFIX
        if [ -z "$UPN_SUFFIX" ]; then
            echo "ERROR: UPN suffix is required with --new" >&2
            exit 1
        fi
        export _ENTRABOT_UPN_SUFFIX="$UPN_SUFFIX"
    else
        export _ENTRABOT_UPN_SUFFIX="$UPN_SUFFIX"
    fi

    # Tell create_entra_agent_ids.py to skip display-name lookups
    export ENTRABOT_NEW_CHAIN=1
    if [ -n "$UPN_SUFFIX" ]; then
        echo -e "  ${YELLOW}--new: will create fresh identity chain with suffix '${UPN_SUFFIX}'${NC}"
    fi
fi

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

if [ "$CONFIGURE_A365_WORK_IQ" = true ]; then
    configure_a365_work_iq
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 6: Generate Blueprint certificate (for three-hop flow)
# ════════════════════════════════════════════════════════════════════════════
step 6 "Managing Blueprint certificate"

# Ensure venv + deps are available (cryptography + keyring needed for cert generation)
if [ ! -d ".venv" ]; then
    "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
# Python 3.12+ venv no longer seeds setuptools/wheel; editable install from
# pyproject.toml needs both. Seed them before the editable install.
pip install --quiet --upgrade pip setuptools wheel
pip install --quiet -e ".[dev]" 2>/dev/null || pip install --quiet -e "." 2>/dev/null
VENV_PY="$PROJECT_ROOT/.venv/bin/python3"

# The Blueprint authenticates with a certificate, not a client secret.
# Private key is stored in the OS credential store (Keychain/TPM/Keyring).
# Only the public certificate is uploaded to the Blueprint app in Entra.
# See ADR-003 for rationale.
CERT_THUMBPRINT=$(read_state "BLUEPRINT_CERT_THUMBPRINT")

if [ -n "$CERT_THUMBPRINT" ]; then
    # The state file has a thumbprint cached — but that's only trustworthy if
    # the Blueprint on Entra still has it registered. A teammate or another
    # machine could have run setup.sh since, replacing the keyCredentials
    # list. If so, trusting the cached thumbprint here would set up a silent
    # runtime auth failure ("invalid_client" with no useful hint). Verify
    # the cached thumbprint is still present; if not, clear it and fall
    # through to the regeneration path (which already warns + confirms).
    if PYTHONPATH="$PROJECT_ROOT/scripts" "$VENV_PY" \
        "$PROJECT_ROOT/scripts/verify_blueprint_cert.py" \
        "$BLUEPRINT_OBJECT_ID" "$CERT_THUMBPRINT" >/dev/null; then
        success "Using cached certificate (thumbprint: ${CERT_THUMBPRINT:0:16}...)"
    else
        echo ""
        echo -e "  ${YELLOW}Cached cert is no longer registered on the Blueprint.${NC}"
        echo -e "  Another machine replaced it since last run. Regenerating here."
        echo ""
        CERT_THUMBPRINT=""
    fi
fi

if [ -z "$CERT_THUMBPRINT" ]; then
    if RECOVERED_THUMBPRINT=$(PYTHONPATH="$PROJECT_ROOT/scripts" "$VENV_PY" \
        "$PROJECT_ROOT/scripts/find_local_blueprint_cert.py" "$BLUEPRINT_OBJECT_ID"); then
        CERT_THUMBPRINT="$RECOVERED_THUMBPRINT"
        "$PYTHON" -c "
import json, pathlib
state_file = pathlib.Path('$PROJECT_ROOT/.entrabot-state.json')
data = json.loads(state_file.read_text()) if state_file.is_file() else {}
data['BLUEPRINT_CERT_THUMBPRINT'] = '$CERT_THUMBPRINT'
data.pop('BLUEPRINT_SECRET', None)
state_file.write_text(json.dumps(data, indent=2) + '\n')
"
        success "Using registered local certificate (thumbprint: ${CERT_THUMBPRINT:0:16}...)"
    fi
fi

if [ -z "$CERT_THUMBPRINT" ]; then
    # Before generating — check if the Blueprint already has certs registered
    # (e.g. from a teammate's machine or from a prior install on another
    # laptop). setup.sh uploads the new cert via PATCH keyCredentials,
    # which is a list REPLACE, not an append — so any existing certs on
    # the Blueprint will be wiped. Warn loudly and confirm before proceeding;
    # otherwise we'd silently lock out whatever machines those certs came
    # from.
    # stdout: the numeric count (shell reads it into EXISTING_COUNT)
    # stderr: one human-readable line per cert (visible on the terminal, so
    #         the user sees WHICH certs will be replaced before confirming)
    EXISTING_COUNT=$(PYTHONPATH="$PROJECT_ROOT/scripts" "$VENV_PY" \
        "$PROJECT_ROOT/scripts/list_blueprint_certs.py" "$BLUEPRINT_OBJECT_ID")

    if [ "$EXISTING_COUNT" -gt 0 ] 2>/dev/null; then
        echo ""
        echo -e "  ${YELLOW}WARNING${NC}: Blueprint app already has ${YELLOW}$EXISTING_COUNT${NC} registered cert(s) (shown above)."
        echo -e "  Generating a new cert here will ${YELLOW}REPLACE${NC} that list (Graph PATCH semantics)."
        echo -e "  Any machine currently authenticating with one of those certs will stop"
        echo -e "  working until it re-runs setup.sh. EntraBot is designed to run from one"
        echo -e "  machine at a time, so this is usually what you want — but confirm."
        echo ""
        if [ -t 0 ]; then
            read -r -p "  Replace existing cert(s) and bind the Blueprint to this machine? [y/N] " CONFIRM
            if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
                fail "Cert replacement aborted by user"
            fi
        else
            echo -e "  ${BLUE}[non-interactive shell — proceeding]${NC}"
        fi
        echo ""
    fi

    echo "  Generating self-signed certificate for Blueprint..."

    # Generate cert, store private key in keyring, upload public cert via
    # Provisioner token (NOT az CLI — Learning #1: az CLI tokens include
    # Directory.AccessAsUser.All which Agent Identity APIs reject).
    CERT_THUMBPRINT=$("$VENV_PY" -c "
import contextlib, socket, sys, json, hashlib, base64
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from datetime import datetime, timedelta, timezone
import keyring, requests, pathlib

# Hostname tag so Entra-side cert listing identifies which machine owns
# each registered key (useful when rotating or revoking one device).
_HOST = socket.gethostname().split('.')[0] or 'unknown'

# --- Generate RSA 2048 key + self-signed cert ---
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, 'entrabot-blueprint-$BLUEPRINT_APP_ID'),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'EntraBot Device Agent'),
])
cert = (x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.now(timezone.utc))
    .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
    .sign(key, hashes.SHA256()))

# --- Compute thumbprint (SHA-256 of DER, base64url no padding) ---
der_bytes = cert.public_bytes(serialization.Encoding.DER)
thumbprint = base64.urlsafe_b64encode(hashlib.sha256(der_bytes).digest()).rstrip(b'=').decode()

# --- Store private key in OS credential store ---
pem_key = key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
keyring.set_password('entrabot', 'blueprint-private-key', pem_key)

# --- Upload public cert to Blueprint app via Graph API ---
# Uses Provisioner token (not az CLI) to avoid Directory.AccessAsUser.All rejection.
# get_bootstrap_graph_token() prints diagnostic lines to stdout (provisioner permission
# checks, admin-consent status, cached-secret notices). We redirect those to
# stderr so the shell-level \$(...) capture only sees the final thumbprint
# line — previously the diagnostic output leaked into .env as the literal
# ENTRABOT_BLUEPRINT_CERT_THUMBPRINT value and broke Hop 1 auth until
# manually repaired.
sys.path.insert(0, '$PROJECT_ROOT/scripts')
from entra_provisioning import get_bootstrap_graph_token
with contextlib.redirect_stdout(sys.stderr):
    token = get_bootstrap_graph_token(wait_for_propagation=False)

# Graph API: PATCH /applications/{id} with keyCredentials
# Dates MUST come from the cert itself and use Graph's 7-decimal-place format
cert_b64 = base64.b64encode(der_bytes).decode()
start_date = cert.not_valid_before_utc.strftime('%Y-%m-%dT%H:%M:%S.0000000Z')
end_date = cert.not_valid_after_utc.strftime('%Y-%m-%dT%H:%M:%S.0000000Z')

# Use v1.0 (not beta) for keyCredentials — more stable (Learning #15)
resp = requests.patch(
    'https://graph.microsoft.com/v1.0/applications/$BLUEPRINT_OBJECT_ID',
    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
    json={'keyCredentials': [{
        'type': 'AsymmetricX509Cert',
        'usage': 'Verify',
        'key': cert_b64,
        'displayName': f'EntraBot Device Certificate — {_HOST}',
        'startDateTime': start_date,
        'endDateTime': end_date,
    }]},
)
if resp.status_code >= 400:
    print(f'ERROR: Failed to upload cert: {resp.status_code} {resp.text}', file=sys.stderr)
    sys.exit(1)

# --- Persist thumbprint in state file ---
state_file = pathlib.Path('$PROJECT_ROOT/.entrabot-state.json')
data = json.loads(state_file.read_text()) if state_file.is_file() else {}
data['BLUEPRINT_CERT_THUMBPRINT'] = thumbprint
data.pop('BLUEPRINT_SECRET', None)  # clean up old secret if present
state_file.write_text(json.dumps(data, indent=2) + '\n')

print(thumbprint)
")

    if [ -z "$CERT_THUMBPRINT" ]; then
        fail "Could not generate Blueprint certificate"
    fi

    # Defense in depth: the thumbprint is SHA-256 DER base64url-no-padding
    # (43 chars, [A-Za-z0-9_-]). If anything else lands here, the .env file
    # is about to be corrupted — fail loudly instead of writing garbage.
    if ! [[ "$CERT_THUMBPRINT" =~ ^[A-Za-z0-9_-]{43}$ ]]; then
        fail "Captured thumbprint doesn't look like a SHA-256 base64url digest: '$CERT_THUMBPRINT'"
    fi

    success "Certificate generated, uploaded to Entra, private key stored in OS keyring"
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

# Python 3.12+ venv no longer seeds setuptools/wheel; editable install from
# pyproject.toml needs both. Seed them before the editable install.
pip install --quiet --upgrade pip setuptools wheel
pip install --quiet -e ".[dev]"
success "Installed dependencies (including dev)"

cat > .env << EOF
# EntraBot Identity Research — generated by scripts/setup.sh
# Uses Agent User (three-hop flow) with certificate auth — no secrets on disk
# Private key stored in OS credential store (Keychain/TPM/Keyring)
# DO NOT commit this file (it is in .gitignore)

ENTRABOT_TENANT_ID=$TENANT_ID
ENTRABOT_BLUEPRINT_APP_ID=$BLUEPRINT_APP_ID
ENTRABOT_BLUEPRINT_OBJECT_ID=$BLUEPRINT_OBJECT_ID
ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=$CERT_THUMBPRINT
ENTRABOT_AGENT_ID=$AGENT_ID
ENTRABOT_AGENT_OBJECT_ID=$AGENT_OBJECT_ID
ENTRABOT_AGENT_USER_ID=${AGENT_USER_ID:-}
ENTRABOT_AGENT_USER_UPN=${AGENT_USER_UPN:-}
ENTRABOT_HUMAN_USER_ID=$HUMAN_USER_ID
ENTRABOT_HUMAN_UPN=$HUMAN_UPN
ENTRABOT_HUMAN_USER_IDS=$HUMAN_USER_IDS
ENTRABOT_HUMAN_UPNS=$HUMAN_UPNS
ENTRABOT_HUMAN_USER_TENANT_IDS=$HUMAN_USER_TENANT_IDS
ENTRABOT_HUMAN_USER_MAILS=$HUMAN_USER_MAILS
ENTRABOT_HUMAN_USER_TYPES=$HUMAN_USER_TYPES
ENTRABOT_PROVISIONER_APP_ID=$PROV_CLIENT_ID
ENTRABOT_LOG_LEVEL=INFO
EOF

chmod 600 .env
success ".env file created (chmod 600)"

# ════════════════════════════════════════════════════════════════════════════
# Step 7b: Azure Blob Storage provisioning (ADR-005)
# ════════════════════════════════════════════════════════════════════════════
if [ "$KEEP_MEMORY_LOCAL" = true ]; then
    echo "" >> .env
    echo "# ADR-005: keep agent memory local (skip cloud sync)" >> .env
    echo "ENTRABOT_KEEP_MEMORY_LOCAL=true" >> .env
    success "Memory mode: LOCAL (--keep-memory-local set)"
elif [ -z "${AGENT_USER_ID:-}" ]; then
    warn "Skipping blob storage — no Agent User to scope RBAC against"
else
    echo ""
    echo -e "${BLUE}[7b]${NC} Provisioning Azure Blob Storage for agent memory (ADR-005)"

    PROVISION_ARGS=(
        --tenant-id "$TENANT_ID"
        --agent-user-object-id "$AGENT_USER_ID"
    )
    if [ -n "$WITH_STORAGE_ACCOUNT" ]; then
        PROVISION_ARGS+=(--with-storage-account "$WITH_STORAGE_ACCOUNT")
    fi
    if [ -n "$WITH_CONTAINER" ]; then
        PROVISION_ARGS+=(--with-container "$WITH_CONTAINER")
    fi
    if [ "$CREATE_NEW_STORAGE" = true ]; then
        PROVISION_ARGS+=(--create-new-storage)
    fi

    PROVISION_OUT=$("$PYTHON" "$PROJECT_ROOT/scripts/provision_blob_storage.py" \
        "${PROVISION_ARGS[@]}" \
        2>&1 1>/tmp/entrabot-provision-stdout.$$)
    PROVISION_RC=$?
    PROVISION_STDOUT=$(cat /tmp/entrabot-provision-stdout.$$)
    rm -f /tmp/entrabot-provision-stdout.$$
    # Echo the provisioner's progress lines (it prints them to stderr)
    echo "$PROVISION_OUT" | sed 's/^/  /'

    if [ $PROVISION_RC -ne 0 ]; then
        warn "Blob storage provisioning failed — falling back to local-only memory"
        echo "" >> .env
        echo "# ADR-005: provisioning failed, using local-only memory" >> .env
        echo "ENTRABOT_KEEP_MEMORY_LOCAL=true" >> .env
    else
        BLOB_ENDPOINT=$(echo "$PROVISION_STDOUT" | grep '^BLOB_ENDPOINT=' | cut -d= -f2-)
        BLOB_CONTAINER=$(echo "$PROVISION_STDOUT" | grep '^BLOB_CONTAINER=' | cut -d= -f2-)
        if [ -z "$BLOB_ENDPOINT" ] || [ -z "$BLOB_CONTAINER" ]; then
            warn "Provisioner returned no endpoint/container — using local-only memory"
            echo "" >> .env
            echo "ENTRABOT_KEEP_MEMORY_LOCAL=true" >> .env
        else
            echo "" >> .env
            echo "# ADR-005: cloud-hosted agent memory (Azure Blob Storage)" >> .env
            echo "ENTRABOT_BLOB_ENDPOINT=$BLOB_ENDPOINT" >> .env
            echo "ENTRABOT_BLOB_CONTAINER=$BLOB_CONTAINER" >> .env
            success "Blob storage ready: $BLOB_ENDPOINT/$BLOB_CONTAINER"

            # Migration prompt — upload existing local data + Claude Code
            # persona memory (ADR-005 Phase 6a), leave both trees in place.
            DATA_DIR="${ENTRABOT_DATA_DIR:-$HOME/.entrabot/data}"
            # Resolve Claude Code per-project memory dir (may be absent).
            PERSONA_DIR=$("$PYTHON" -c "
from pathlib import Path
from entrabot.storage.persona import claude_code_memory_dir
print(claude_code_memory_dir(Path('$PROJECT_ROOT')))
" 2>/dev/null || echo "")
            HAS_DATA=false
            HAS_PERSONA=false
            [ -d "$DATA_DIR" ] && [ -n "$(ls -A "$DATA_DIR" 2>/dev/null)" ] && HAS_DATA=true
            [ -n "$PERSONA_DIR" ] && [ -d "$PERSONA_DIR" ] && [ -n "$(ls -A "$PERSONA_DIR" 2>/dev/null)" ] && HAS_PERSONA=true

            if [ "$HAS_DATA" = true ] || [ "$HAS_PERSONA" = true ]; then
                TOTAL_KB=0
                DESC_PARTS=""
                if [ "$HAS_DATA" = true ]; then
                    DATA_KB=$(du -sk "$DATA_DIR" 2>/dev/null | awk '{print $1}')
                    TOTAL_KB=$((TOTAL_KB + DATA_KB))
                    DESC_PARTS="agent data"
                fi
                if [ "$HAS_PERSONA" = true ]; then
                    PERSONA_KB=$(du -sk "$PERSONA_DIR" 2>/dev/null | awk '{print $1}')
                    TOTAL_KB=$((TOTAL_KB + PERSONA_KB))
                    if [ -n "$DESC_PARTS" ]; then
                        DESC_PARTS="$DESC_PARTS + Claude Code persona memory"
                    else
                        DESC_PARTS="Claude Code persona memory"
                    fi
                fi
                echo ""
                echo -n "  Upload existing local memory (~${TOTAL_KB} KB, $DESC_PARTS) to blob? [y/N] "
                read -r MIGRATE_REPLY
                if [ "$MIGRATE_REPLY" = "y" ] || [ "$MIGRATE_REPLY" = "Y" ]; then
                    MIGRATION_RC=0
                    "$PYTHON" -c "
import sys
from pathlib import Path
from entrabot.storage.backend import get_backend
from entrabot.storage.migration import migrate_local_to_backend
RED = '\033[0;31m'
GREEN = '\033[0;32m'
NC = '\033[0m'
sources = []
if '$HAS_DATA' == 'true':
    sources.append((Path('$DATA_DIR'), ''))
if '$HAS_PERSONA' == 'true':
    sources.append((Path('$PERSONA_DIR'), 'claude_memory'))
report = migrate_local_to_backend(sources, get_backend())
print(f'  {GREEN}Copied:{NC} {report.copied} files ({report.bytes_copied} bytes)')
print(f'  Skipped (already in cloud): {report.skipped}')
if report.errors:
    print(f'  {RED}Errors: {len(report.errors)}{NC}')
    for k, e in report.errors[:5]:
        print(f'    {RED}- {k}:{NC} {e}')
    sys.exit(2)
" || MIGRATION_RC=$?
                    if [ "$MIGRATION_RC" -ne 0 ]; then
                        MIGRATION_FAILED=true
                        err "Migration FAILED — cloud memory is not in sync with local"
                        echo -e "    ${YELLOW}Hint:${NC} most common cause is missing storage consent for the"
                        echo -e "          Agent Identity. Re-run ${BLUE}./scripts/setup.sh${NC} (idempotent) or"
                        echo -e "          ${BLUE}python scripts/create_entra_agent_ids.py${NC} to grant it, then retry."
                    else
                        success "Migration complete (local files left untouched)"
                    fi
                else
                    echo "  Skipped migration. You can run it later with:"
                    echo "    .venv/bin/python scripts/claude_memory_sync.py push   # persona files"
                    echo "    .venv/bin/python -c 'from entrabot.storage.backend import get_backend; from entrabot.storage.migration import migrate_local_to_backend; from pathlib import Path; print(migrate_local_to_backend([(Path(\"$DATA_DIR\"), \"\")], get_backend()))'"
                fi
            fi
        fi
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 8: Summary
# ════════════════════════════════════════════════════════════════════════════
step 8 "Setup complete — summary"

# ── Post-setup smoke test ──────────────────────────────────────────────────
# Verify the agent user can actually mint a token, identify itself to Graph,
# and reach the Teams scope. This is the single check that distinguishes a
# "looks complete" setup from an "actually works" setup. Skip with
# --skip-smoke in CI environments where no live tenant is reachable.
SMOKE_FAILED=false
if [ "$SKIP_SMOKE" = true ]; then
    warn "Smoke test skipped (--skip-smoke)"
elif [ -d "$PROJECT_ROOT/.venv" ] && [ -f "$PROJECT_ROOT/.env" ]; then
    echo ""
    echo -e "  ${BLUE}Running smoke test (token + Graph identity + Teams scope)...${NC}"
    set +e
    set -a; . "$PROJECT_ROOT/.env"; set +a
    "$PROJECT_ROOT/.venv/bin/python" - <<'PY'
import sys
from entrabot.config import EntraBotConfig
from entrabot.preflight import format_report, overall_exit_code, run_smoke_checks

config = EntraBotConfig.from_env()
checks = run_smoke_checks(config)
print(format_report(checks, color=sys.stdout.isatty()))
sys.exit(overall_exit_code(checks))
PY
    SMOKE_EXIT=$?
    set -e
    if [ "$SMOKE_EXIT" -ne 0 ]; then
        SMOKE_FAILED=true
        warn "Smoke test failed. The agent provisioning succeeded, but the agent"
        warn "can't yet authenticate end-to-end. Most common cause: Teams license"
        warn "was just assigned and replication hasn't completed (10-15 min)."
        warn "Re-run ./scripts/setup.sh --diagnose later to confirm."
    fi
fi

echo ""
if [ "$MIGRATION_FAILED" = true ]; then
    echo -e "${RED}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  Setup INCOMPLETE — migration failed                         ║${NC}"
    echo -e "${RED}║  Cloud memory is not in sync with local disk.                ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════════════════════╝${NC}"
else
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  Setup complete!                                             ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
fi
echo ""
echo -e "  Provisioner: ${BLUE}$PROV_CLIENT_ID${NC}"
echo -e "  Blueprint:   ${BLUE}$BLUEPRINT_APP_ID${NC}"
echo -e "  Agent ID:    ${BLUE}$AGENT_ID${NC}"
echo -e "  Agent User:  ${BLUE}${AGENT_USER_UPN:-not created}${NC} (${AGENT_USER_ID:-n/a})"
echo -e "  Human User:  ${BLUE}$HUMAN_UPN${NC}"
echo -e "  Auth Flow:   ${BLUE}Three-hop (Blueprint cert → Agent Identity → Agent User)${NC}"
echo -e "  Credential:  ${BLUE}Certificate (private key in OS keyring, no secrets on disk)${NC}"
echo ""

if [ -z "$AGENT_USER_ID" ]; then
    echo -e "  ${YELLOW}⚠️  Agent User was not created — check permissions above${NC}"
    echo ""
fi

AGENT_LICENSE=$(read_state "AGENT_USER_LICENSE_SKU")
if [ -n "$AGENT_LICENSE" ]; then
    echo -e "  License:     ${BLUE}$AGENT_LICENSE${NC} (Teams provisioning in 10-15 min)"
fi

# Write MCP server config to both Claude Code and Copilot CLI config paths.
# Claude Code reads ``<project-root>/.mcp.json``. Copilot CLI reads
# ``$COPILOT_HOME/mcp-config.json`` (default ``~/.copilot/mcp-config.json``)
# and also honors the project-local ``.mcp.json``. Both entries are byte-
# identical; the leader/slave split is done inside the MCP server at
# session initialize via ``clientInfo.name``.
#
# The actual upsert (idempotent, preserves unrelated entries, backs up
# corrupt files) lives in ``scripts/mcp_config.py`` so it's unit-tested.
#
# To add persona-sati (mind server), see .mcp.json.example for the dual-
# server configuration. persona-sati is optional — entrabot works
# standalone as a Teams tool server without it.
ENTRABOT_MCP_BIN="$PROJECT_ROOT/.venv/bin/entrabot-mcp"
"$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/mcp_config.py" \
    --binary "$ENTRABOT_MCP_BIN" \
    --project-root "$PROJECT_ROOT"
success "MCP server config written (.mcp.json + ~/.copilot/mcp-config.json)"

echo -e "  ${YELLOW}NEXT STEPS:${NC}"
echo -e "  1. Wait 10-15 min for Teams/mailbox provisioning (if license was just assigned)"
echo -e "  2. Run tests: ${BLUE}$PROJECT_ROOT/.venv/bin/pytest -v${NC}"
echo -e "  3. Restart Claude Code / Copilot CLI in this project — the MCP server"
echo -e "     will be auto-discovered from ${BLUE}.mcp.json${NC}"
echo ""

echo -e "  ${YELLOW}Persona-sati host bootstrap:${NC}"
echo -e "  If this host uses persona-sati, copy ${BLUE}docs/clients/persona-sati-host-bootstrap.md${NC}"
echo -e "  into host global instruction file. FastMCP instructions are not enough — hosts"
echo -e "  do not inject them into the LLM system prompt."
echo -e "    - Claude Code: ${BLUE}~/.claude/CLAUDE.md${NC}"
echo -e "    - Copilot CLI: use configured global Copilot instruction location for this install"
echo -e "    - Repo-local fallback: ${BLUE}CLAUDE.md, AGENTS.md, .github/copilot-instructions.md${NC}"
echo -e "  Required first call in new sessions: ${BLUE}bootstrap_session()${NC}"
echo -e "  Per-turn cognition uses: ${BLUE}observe, reflect, recall${NC}"
echo -e "  If ${BLUE}mind_contract_available=false${NC}: body-only mode; do not impersonate persona."
echo ""

if [ "$MIGRATION_FAILED" = true ]; then
    echo -e "  ${RED}Re-run ${BLUE}./scripts/setup.sh${RED} after fixing the migration error above.${NC}"
    echo ""
    exit 2
fi

if [ "$SMOKE_FAILED" = true ]; then
    echo -e "  ${YELLOW}Smoke test failed — provisioning is done but auth path isn't healthy yet.${NC}"
    echo -e "  ${YELLOW}Re-run ${BLUE}./scripts/setup.sh --diagnose${YELLOW} after replication completes.${NC}"
    echo ""
    exit 3
fi
