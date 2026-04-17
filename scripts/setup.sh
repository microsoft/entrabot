#!/usr/bin/env bash
# EntraClaw Identity Research — one-command setup
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
# State is persisted in .entraclaw-state.json so re-runs are idempotent.
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

for arg in "$@"; do
    case $arg in
        --switch-user)
            SWITCH_USER=true
            ;;
        --teams-user=*)
            TEAMS_USER_EMAIL="${arg#--teams-user=}"
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
    echo "  --switch-user          Sign in as a different user before setup."
    echo "                         The new user becomes the agent's owner and sponsor."
    echo "  --teams-user=EMAIL     Set a different user as the Teams chat recipient."
    echo "                         The az CLI user remains the admin/provisioner."
    echo "                         e.g., --teams-user=brandon@werner.ac"
    echo "  --help, -h             Show this help"
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
echo -e "${GREEN}║   EntraClaw Identity Research — Setup         ║${NC}"
echo -e "${GREEN}║   (Agent User — no OBO, no device-code flow) ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"

# ── Helper: read value from .entraclaw-state.json ───────────────────────────

read_state() {
    local key="$1"
    "$PYTHON" -c "
import json, pathlib, sys
state_file = pathlib.Path('$PROJECT_ROOT/.entraclaw-state.json')
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
# Step 6: Generate Blueprint certificate (for three-hop flow)
# ════════════════════════════════════════════════════════════════════════════
step 6 "Managing Blueprint certificate"

# Ensure venv + deps are available (cryptography + keyring needed for cert generation)
if [ ! -d ".venv" ]; then
    "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet -e ".[dev]" 2>/dev/null || pip install --quiet -e "." 2>/dev/null
VENV_PY="$PROJECT_ROOT/.venv/bin/python3"

# The Blueprint authenticates with a certificate, not a client secret.
# Private key is stored in the OS credential store (Keychain/TPM/Keyring).
# Only the public certificate is uploaded to the Blueprint app in Entra.
# See ADR-003 for rationale.
CERT_THUMBPRINT=$(read_state "BLUEPRINT_CERT_THUMBPRINT")

if [ -n "$CERT_THUMBPRINT" ]; then
    success "Using cached certificate (thumbprint: ${CERT_THUMBPRINT:0:16}...)"
else
    echo "  Generating self-signed certificate for Blueprint..."

    # Generate cert, store private key in keyring, upload public cert via
    # Provisioner token (NOT az CLI — Learning #1: az CLI tokens include
    # Directory.AccessAsUser.All which Agent Identity APIs reject).
    CERT_THUMBPRINT=$("$VENV_PY" -c "
import contextlib, sys, json, hashlib, base64
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from datetime import datetime, timedelta, timezone
import keyring, requests, pathlib

# --- Generate RSA 2048 key + self-signed cert ---
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, 'entraclaw-blueprint-$BLUEPRINT_APP_ID'),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'EntraClaw Device Agent'),
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
keyring.set_password('entraclaw', 'blueprint-private-key', pem_key)

# --- Upload public cert to Blueprint app via Graph API ---
# Uses Provisioner token (not az CLI) to avoid Directory.AccessAsUser.All rejection.
# get_graph_token() prints diagnostic lines to stdout (provisioner permission
# checks, admin-consent status, cached-secret notices). We redirect those to
# stderr so the shell-level \$(...) capture only sees the final thumbprint
# line — previously the diagnostic output leaked into .env as the literal
# ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT value and broke Hop 1 auth until
# manually repaired.
sys.path.insert(0, '$PROJECT_ROOT/scripts')
from entra_provisioning import get_graph_token
with contextlib.redirect_stdout(sys.stderr):
    token = get_graph_token(wait_for_propagation=False)

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
        'displayName': 'EntraClaw Device Certificate',
        'startDateTime': start_date,
        'endDateTime': end_date,
    }]},
)
if resp.status_code >= 400:
    print(f'ERROR: Failed to upload cert: {resp.status_code} {resp.text}', file=sys.stderr)
    sys.exit(1)

# --- Persist thumbprint in state file ---
state_file = pathlib.Path('$PROJECT_ROOT/.entraclaw-state.json')
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

pip install --quiet -e ".[dev]"
success "Installed dependencies (including dev)"

cat > .env << EOF
# EntraClaw Identity Research — generated by scripts/setup.sh
# Uses Agent User (three-hop flow) with certificate auth — no secrets on disk
# Private key stored in OS credential store (Keychain/TPM/Keyring)
# DO NOT commit this file (it is in .gitignore)

ENTRACLAW_TENANT_ID=$TENANT_ID
ENTRACLAW_BLUEPRINT_APP_ID=$BLUEPRINT_APP_ID
ENTRACLAW_BLUEPRINT_OBJECT_ID=$BLUEPRINT_OBJECT_ID
ENTRACLAW_BLUEPRINT_CERT_THUMBPRINT=$CERT_THUMBPRINT
ENTRACLAW_AGENT_ID=$AGENT_ID
ENTRACLAW_AGENT_OBJECT_ID=$AGENT_OBJECT_ID
ENTRACLAW_AGENT_USER_ID=${AGENT_USER_ID:-}
ENTRACLAW_AGENT_USER_UPN=${AGENT_USER_UPN:-}
ENTRACLAW_HUMAN_USER_ID=$HUMAN_USER_ID
ENTRACLAW_HUMAN_UPN=$HUMAN_UPN
ENTRACLAW_HUMAN_USER_IDS=$HUMAN_USER_IDS
ENTRACLAW_HUMAN_UPNS=$HUMAN_UPNS
ENTRACLAW_HUMAN_USER_TENANT_IDS=$HUMAN_USER_TENANT_IDS
ENTRACLAW_HUMAN_USER_MAILS=$HUMAN_USER_MAILS
ENTRACLAW_HUMAN_USER_TYPES=$HUMAN_USER_TYPES
ENTRACLAW_PROVISIONER_APP_ID=$PROV_CLIENT_ID
ENTRACLAW_LOG_LEVEL=INFO
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

# Write MCP server config to project root (.mcp.json)
# Claude Code picks this up automatically when opening the project.
# Uses the `entraclaw-mcp` console script installed by `pip install -e .[dev]`
# (defined as a [project.scripts] entry in pyproject.toml) — cleaner than
# invoking `python3 -m entraclaw.mcp_server` because it doesn't depend on
# the CLI having the right cwd.
ENTRACLAW_MCP_BIN="$PROJECT_ROOT/.venv/bin/entraclaw-mcp"
cat > "$PROJECT_ROOT/.mcp.json" << MCPEOF
{
  "mcpServers": {
    "entraclaw": {
      "type": "stdio",
      "command": "$ENTRACLAW_MCP_BIN",
      "args": [],
      "description": "EntraClaw Agent Identity — Teams tools + background DM/email poll"
    }
  }
}
MCPEOF
success "MCP server config written to .mcp.json"

echo -e "  ${YELLOW}NEXT STEPS:${NC}"
echo -e "  1. Wait 10-15 min for Teams/mailbox provisioning (if license was just assigned)"
echo -e "  2. Run tests: ${BLUE}$PROJECT_ROOT/.venv/bin/pytest -v${NC}"
echo -e "  3. Restart Claude Code / Copilot CLI in this project — the MCP server"
echo -e "     will be auto-discovered from ${BLUE}.mcp.json${NC}"
echo ""
