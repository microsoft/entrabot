#!/usr/bin/env bash
# Openclaw Identity Research — one-command setup
# Creates an Entra Agent Identity Blueprint + Agent Identity (service principal),
# runs a one-time human device-code auth, caches the refresh token in the OS
# keychain, installs dependencies, and writes .env.
# Idempotent: safe to re-run — detects existing resources and skips.
set -euo pipefail

TOTAL_STEPS=15
GRAPH_API_ID="00000003-0000-0000-c000-000000000000"
PROVISIONER_APP_NAME="Openclaw Agent ID Provisioner"
BLUEPRINT_DISPLAY_NAME="Openclaw Code Agent"

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

# ── Helper: resolve project root (script may be invoked from anywhere) ──────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Openclaw Identity Research — Setup     ║${NC}"
echo -e "${GREEN}║   (Entra Agent Identity — no fake users) ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"

# ════════════════════════════════════════════════════════════════════════════
# Step 1: Verify prerequisites
# ════════════════════════════════════════════════════════════════════════════
step 1 "Verifying prerequisites"

MISSING=()

if ! command -v az &>/dev/null; then
    MISSING+=("az (Azure CLI — https://aka.ms/install-az)")
fi

# Accept python3.12, python3.13, … or plain python3 ≥ 3.12
PYTHON=""
for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" &>/dev/null; then
        PY_VER=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        if [ "$(echo "$PY_VER >= 3.12" | bc 2>/dev/null || python3 -c "print(int($PY_VER >= 3.12))")" = "1" ]; then
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

success "az CLI found ($(az version --query '\"azure-cli\"' -o tsv 2>/dev/null || echo '?'))"
success "$PYTHON found ($PY_VER)"
success "git found ($(git --version | awk '{print $3}'))"

# ════════════════════════════════════════════════════════════════════════════
# Step 2: Discover Azure subscription and tenant
# ════════════════════════════════════════════════════════════════════════════
step 2 "Discovering Azure subscription and tenant"

if ! az account show &>/dev/null; then
    fail "Not logged in to Azure CLI. Run 'az login' first."
fi

SUBSCRIPTION_ID=$(az account show --query "id" -o tsv)
TENANT_ID=$(az account show --query "tenantId" -o tsv)
ACCOUNT_NAME=$(az account show --query "name" -o tsv)

# Discover the signed-in human user's info
HUMAN_UPN=$(az account show --query "user.name" -o tsv 2>/dev/null || echo "")
HUMAN_USER_ID=$(az ad signed-in-user show --query "id" -o tsv 2>/dev/null || echo "")

success "Subscription: $ACCOUNT_NAME ($SUBSCRIPTION_ID)"
success "Tenant:       $TENANT_ID"
success "Human user:   $HUMAN_UPN ($HUMAN_USER_ID)"

# ════════════════════════════════════════════════════════════════════════════
# Step 3: Create / find Provisioner app registration
# ════════════════════════════════════════════════════════════════════════════
step 3 "Creating/finding Provisioner app registration"

# A dedicated app for Agent ID provisioning (Azure CLI tokens include
# Directory.AccessAsUser.All which the Agent Identity APIs reject).
EXISTING_PROV=$(az ad app list --display-name "$PROVISIONER_APP_NAME" --query "[0].appId" -o tsv 2>/dev/null)
if [ -n "$EXISTING_PROV" ]; then
    success "Found existing provisioner app: $EXISTING_PROV"
    PROV_CLIENT_ID="$EXISTING_PROV"
    PROV_OBJECT_ID=$(az ad app list --display-name "$PROVISIONER_APP_NAME" --query "[0].id" -o tsv)
else
    echo "  Creating provisioner app registration..."
    PROV_JSON=$(az ad app create \
        --display-name "$PROVISIONER_APP_NAME" \
        --sign-in-audience AzureADMyOrg \
        --query "{appId: appId, id: id}" -o json)
    PROV_CLIENT_ID=$(echo "$PROV_JSON" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['appId'])")
    PROV_OBJECT_ID=$(echo "$PROV_JSON" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['id'])")
    success "Created provisioner app: $PROV_CLIENT_ID"
fi

# Ensure service principal exists for provisioner
EXISTING_PROV_SP=$(az ad sp list --filter "appId eq '$PROV_CLIENT_ID'" --query "[0].id" -o tsv 2>/dev/null)
if [ -z "$EXISTING_PROV_SP" ]; then
    az ad sp create --id "$PROV_CLIENT_ID" -o none 2>/dev/null
    success "Provisioner service principal created"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 4: Create Agent Identity Blueprint via Graph beta API
# ════════════════════════════════════════════════════════════════════════════
step 4 "Creating/finding Agent Identity Blueprint"

# Check for existing blueprint
EXISTING_BP=$(az ad app list --display-name "$BLUEPRINT_DISPLAY_NAME" --query "[0].appId" -o tsv 2>/dev/null)
if [ -n "$EXISTING_BP" ]; then
    success "Found existing blueprint: $EXISTING_BP"
    BLUEPRINT_APP_ID="$EXISTING_BP"
    BLUEPRINT_OBJECT_ID=$(az ad app list --display-name "$BLUEPRINT_DISPLAY_NAME" --query "[0].id" -o tsv)
else
    echo "  Creating Agent Identity Blueprint via Graph beta API..."
    BP_JSON=$(az rest --method POST \
        --uri "https://graph.microsoft.com/beta/applications" \
        --headers "Content-Type=application/json" \
        --body "{
            \"@odata.type\": \"Microsoft.Graph.AgentIdentityBlueprint\",
            \"displayName\": \"$BLUEPRINT_DISPLAY_NAME\",
            \"description\": \"Agent Identity Blueprint for Openclaw device agents\",
            \"sponsors@odata.bind\": [\"https://graph.microsoft.com/beta/users/$HUMAN_USER_ID\"]
        }" 2>/dev/null || echo "FALLBACK")

    if [ "$BP_JSON" = "FALLBACK" ]; then
        warn "Graph beta Agent Identity API not available — falling back to standard app registration"
        BP_JSON=$(az ad app create \
            --display-name "$BLUEPRINT_DISPLAY_NAME" \
            --sign-in-audience AzureADMyOrg \
            --query "{appId: appId, id: id}" -o json)
    fi

    BLUEPRINT_APP_ID=$(echo "$BP_JSON" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('appId', d.get('appId','')))")
    BLUEPRINT_OBJECT_ID=$(echo "$BP_JSON" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))")
    success "Created blueprint: $BLUEPRINT_APP_ID"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 5: Expose custom API scope on the blueprint (access_as_user)
# ════════════════════════════════════════════════════════════════════════════
step 5 "Exposing custom API scope (api://$BLUEPRINT_APP_ID/access_as_user)"

APP_ID_URI=$(az ad app show --id "$BLUEPRINT_OBJECT_ID" --query "identifierUris[0]" -o tsv 2>/dev/null)
if [ -z "$APP_ID_URI" ] || [ "$APP_ID_URI" = "None" ]; then
    az rest --method PATCH \
        --uri "https://graph.microsoft.com/v1.0/applications/$BLUEPRINT_OBJECT_ID" \
        --headers "Content-Type=application/json" \
        --body "{\"identifierUris\":[\"api://$BLUEPRINT_APP_ID\"]}" 2>/dev/null
    success "Set Application ID URI: api://$BLUEPRINT_APP_ID"
else
    success "Application ID URI already set: $APP_ID_URI"
fi

EXISTING_SCOPE=$(az ad app show --id "$BLUEPRINT_OBJECT_ID" \
    --query "api.oauth2PermissionScopes[?value=='access_as_user'].id" -o tsv 2>/dev/null)
if [ -z "$EXISTING_SCOPE" ]; then
    SCOPE_ID=$("$PYTHON" -c "import uuid; print(uuid.uuid4())")
    az rest --method PATCH \
        --uri "https://graph.microsoft.com/v1.0/applications/$BLUEPRINT_OBJECT_ID" \
        --headers "Content-Type=application/json" \
        --body "{
            \"api\": {
                \"oauth2PermissionScopes\": [{
                    \"adminConsentDescription\": \"Allow Openclaw agent to act on behalf of the user\",
                    \"adminConsentDisplayName\": \"Access as user\",
                    \"id\": \"$SCOPE_ID\",
                    \"isEnabled\": true,
                    \"type\": \"User\",
                    \"userConsentDescription\": \"Allow Openclaw agent to act on your behalf\",
                    \"userConsentDisplayName\": \"Access as user\",
                    \"value\": \"access_as_user\"
                }]
            }
        }"
    success "Created scope: access_as_user ($SCOPE_ID)"
else
    success "Scope access_as_user already exists ($EXISTING_SCOPE)"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 6: Add Graph API delegated permissions to the blueprint
# ════════════════════════════════════════════════════════════════════════════
step 6 "Adding Graph API delegated permissions"

az ad app permission add --id "$BLUEPRINT_APP_ID" \
    --api "$GRAPH_API_ID" \
    --api-permissions \
        e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope \
        9ff7295e-131b-4d94-90e1-69fde507ac11=Scope \
        116b7235-7cc6-461e-b163-8e55691d839e=Scope \
        7427e0e9-2fba-42fe-b0c0-848c9e6a8182=Scope 2>/dev/null || true

success "Delegated permissions: User.Read, Chat.Create, ChatMessage.Send, Chat.ReadWrite"

# ════════════════════════════════════════════════════════════════════════════
# Step 7: Create service principal for blueprint + grant admin consent
# ════════════════════════════════════════════════════════════════════════════
step 7 "Creating blueprint service principal and granting admin consent"

EXISTING_BP_SP=$(az ad sp list --filter "appId eq '$BLUEPRINT_APP_ID'" --query "[0].id" -o tsv 2>/dev/null)
if [ -n "$EXISTING_BP_SP" ]; then
    success "Blueprint service principal already exists ($EXISTING_BP_SP)"
else
    az ad sp create --id "$BLUEPRINT_APP_ID" -o none 2>/dev/null
    success "Blueprint service principal created"
fi

CONSENT_GRANTED=false
for i in 1 2 3; do
    if az ad app permission admin-consent --id "$BLUEPRINT_APP_ID" 2>&1; then
        CONSENT_GRANTED=true
        break
    else
        if [ "$i" -lt 3 ]; then
            warn "Consent attempt $i failed, retrying in 5 seconds..."
            sleep 5
        fi
    fi
done

if [ "$CONSENT_GRANTED" = true ]; then
    success "Admin consent granted"
else
    warn "Admin consent failed. Grant manually:"
    warn "  az ad app permission admin-consent --id $BLUEPRINT_APP_ID"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 8: Create Agent Identity (service principal linked to blueprint)
# ════════════════════════════════════════════════════════════════════════════
step 8 "Creating/finding Agent Identity"

HOSTNAME_SHORT=$(hostname -s 2>/dev/null || hostname)
AGENT_DISPLAY_NAME="Openclaw Agent - $HOSTNAME_SHORT"

# Check for existing agent identity by display name
EXISTING_AGENT=$(az ad sp list --display-name "$AGENT_DISPLAY_NAME" --query "[0].appId" -o tsv 2>/dev/null)
if [ -n "$EXISTING_AGENT" ]; then
    success "Found existing agent identity: $EXISTING_AGENT"
    AGENT_ID="$EXISTING_AGENT"
    AGENT_OBJECT_ID=$(az ad sp list --display-name "$AGENT_DISPLAY_NAME" --query "[0].id" -o tsv)
else
    echo "  Creating Agent Identity via Graph beta API..."
    AGENT_JSON=$(az rest --method POST \
        --uri "https://graph.microsoft.com/beta/servicePrincipals" \
        --headers "Content-Type=application/json" \
        --body "{
            \"@odata.type\": \"Microsoft.Graph.AgentIdentity\",
            \"displayName\": \"$AGENT_DISPLAY_NAME\",
            \"agentIdentityBlueprintId\": \"$BLUEPRINT_APP_ID\",
            \"sponsors@odata.bind\": [\"https://graph.microsoft.com/beta/users/$HUMAN_USER_ID\"]
        }" 2>/dev/null || echo "FALLBACK")

    if [ "$AGENT_JSON" = "FALLBACK" ]; then
        warn "Graph beta Agent Identity API not available — using standard service principal"
        # Create a standard service principal as fallback
        AGENT_JSON=$(az rest --method POST \
            --uri "https://graph.microsoft.com/v1.0/servicePrincipals" \
            --headers "Content-Type=application/json" \
            --body "{
                \"displayName\": \"$AGENT_DISPLAY_NAME\",
                \"appId\": \"$BLUEPRINT_APP_ID\",
                \"notes\": \"Agent Identity for Openclaw - sponsored by $HUMAN_UPN\"
            }" 2>/dev/null || echo "{}")
    fi

    AGENT_ID=$(echo "$AGENT_JSON" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('appId',''))" 2>/dev/null || echo "$BLUEPRINT_APP_ID")
    AGENT_OBJECT_ID=$(echo "$AGENT_JSON" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")

    if [ -n "$AGENT_ID" ]; then
        success "Created agent identity: $AGENT_DISPLAY_NAME ($AGENT_ID)"
    else
        AGENT_ID="$BLUEPRINT_APP_ID"
        warn "Could not create separate agent identity — using blueprint app ID"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 9: Create / retrieve client secret for the blueprint
# ════════════════════════════════════════════════════════════════════════════
step 9 "Managing blueprint client secret"

CACHED_SECRET=""
CACHED_SECRET=$("$PYTHON" -c "
import keyring
s = keyring.get_password('openclaw', 'blueprint_secret')
print(s or '')
" 2>/dev/null) || true

if [ -n "$CACHED_SECRET" ]; then
    success "Using cached blueprint secret from credential store"
    BLUEPRINT_SECRET="$CACHED_SECRET"
else
    echo "  Creating new client secret on blueprint..."
    BLUEPRINT_SECRET=$(az ad app credential reset \
        --id "$BLUEPRINT_OBJECT_ID" \
        --display-name "Openclaw Device" \
        --query "password" -o tsv)

    # Cache in OS credential store
    if "$PYTHON" -c "
import keyring, sys
keyring.set_password('openclaw', 'blueprint_secret', sys.argv[1])
" "$BLUEPRINT_SECRET" 2>/dev/null; then
        success "Blueprint secret created and cached in credential store"
    else
        warn "Blueprint secret created but could not cache in credential store"
        success "Secret will be written to .env"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 10: Human device-code auth (one-time consent)
# ════════════════════════════════════════════════════════════════════════════
step 10 "Human device-code authentication (one-time consent)"

# Check if we already have a cached refresh token
EXISTING_RT=$("$PYTHON" -c "
import keyring
t = keyring.get_password('openclaw', 'human_refresh_token')
print('yes' if t else '')
" 2>/dev/null) || true

if [ -n "$EXISTING_RT" ]; then
    success "Human refresh token already cached in keychain"
else
    echo "  Starting device-code flow for human consent..."
    echo -e "  ${YELLOW}You will be shown a device code — sign in at https://microsoft.com/devicelogin${NC}"

    "$PYTHON" -c "
import sys, json
from msal import PublicClientApplication
import keyring

app = PublicClientApplication(
    client_id='$BLUEPRINT_APP_ID',
    authority='https://login.microsoftonline.com/$TENANT_ID',
)

flow = app.initiate_device_flow(
    scopes=['api://$BLUEPRINT_APP_ID/access_as_user']
)
if 'user_code' not in flow:
    print(f'ERROR: Could not initiate device flow: {flow}', file=sys.stderr)
    sys.exit(1)

print(f'\\n  📱 Device code: {flow[\"user_code\"]}')
print(f'  🌐 Go to: {flow[\"verification_uri\"]}')
print(f'  ⏳ Waiting for authentication...\\n')

result = app.acquire_token_by_device_flow(flow)
if 'error' in result:
    print(f'ERROR: {result[\"error\"]}: {result.get(\"error_description\", \"\")}', file=sys.stderr)
    sys.exit(1)

# Cache the refresh token in the OS keychain
if 'refresh_token' in result:
    keyring.set_password('openclaw', 'human_refresh_token', result['refresh_token'])
    print('  ✅ Human refresh token cached in OS keychain')
else:
    print('  ⚠️  No refresh token in response — OBO may not work', file=sys.stderr)
    sys.exit(1)
"
    if [ $? -ne 0 ]; then
        fail "Device-code authentication failed"
    fi
    success "Human authenticated and refresh token cached"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 11: Create Python venv and install dependencies
# ════════════════════════════════════════════════════════════════════════════
step 11 "Setting up Python virtual environment"

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

# ════════════════════════════════════════════════════════════════════════════
# Step 12: Write .env file
# ════════════════════════════════════════════════════════════════════════════
step 12 "Writing .env configuration"

cat > .env << EOF
# Openclaw Identity Research — generated by scripts/setup.sh
# Uses Entra Agent Identity Blueprint + OBO flow (no fake users)
# DO NOT commit this file (it is in .gitignore)

OPENCLAW_TENANT_ID=$TENANT_ID
OPENCLAW_BLUEPRINT_APP_ID=$BLUEPRINT_APP_ID
OPENCLAW_BLUEPRINT_OBJECT_ID=$BLUEPRINT_OBJECT_ID
OPENCLAW_BLUEPRINT_SECRET=$BLUEPRINT_SECRET
OPENCLAW_AGENT_ID=${AGENT_ID:-$BLUEPRINT_APP_ID}
OPENCLAW_AGENT_OBJECT_ID=${AGENT_OBJECT_ID:-}
OPENCLAW_HUMAN_USER_ID=$HUMAN_USER_ID
OPENCLAW_HUMAN_UPN=$HUMAN_UPN
OPENCLAW_LOG_LEVEL=INFO
EOF

chmod 600 .env
success ".env file created (chmod 600)"

# Verify .gitignore covers .env
if grep -qx '\.env' .gitignore 2>/dev/null || grep -q '^\.env$' .gitignore 2>/dev/null; then
    success ".env is listed in .gitignore"
else
    warn ".env may not be in .gitignore — verify before committing"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 13: Delete legacy fake agent user (if exists)
# ════════════════════════════════════════════════════════════════════════════
step 13 "Cleaning up legacy fake agent user (if any)"

# Discover the tenant's primary domain
DOMAIN=$(az rest --method GET \
    --uri "https://graph.microsoft.com/v1.0/domains" \
    --query "value[?isDefault].id" -o tsv 2>/dev/null || echo "")

if [ -n "$DOMAIN" ]; then
    LEGACY_UPN="openclaw-agent@$DOMAIN"
    LEGACY_ID=$(az ad user list --filter "userPrincipalName eq '$LEGACY_UPN'" \
        --query "[0].id" -o tsv 2>/dev/null || echo "")
    if [ -n "$LEGACY_ID" ]; then
        az ad user delete --id "$LEGACY_ID" 2>/dev/null && \
            success "Deleted legacy fake agent user: $LEGACY_UPN" || \
            warn "Could not delete legacy user $LEGACY_UPN — delete manually"
    else
        success "No legacy fake agent user found"
    fi
else
    warn "Could not discover domain — skip legacy user cleanup"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 14: Run tests
# ════════════════════════════════════════════════════════════════════════════
step 14 "Running tests"

if pytest -v --tb=short 2>&1; then
    success "All tests passed"
else
    warn "Some tests failed — review the output above"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 15: Print next steps
# ════════════════════════════════════════════════════════════════════════════
step 15 "Setup complete — next steps"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Setup complete! Here's how to start the MCP server:        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  1. Add Openclaw to your Copilot CLI config:"
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
echo -e "  2. Launch Copilot CLI:"
echo ""
echo -e "     ${BLUE}copilot${NC}"
echo ""
echo -e "  3. Available tools (pre-authenticated via OBO — no bootstrap needed):"
echo ""
echo -e "     ${GREEN}openclaw_whoami${NC}         — show agent identity and status"
echo -e "     ${GREEN}openclaw_teams_send${NC}    — send a message as the agent"
echo -e "     ${GREEN}openclaw_teams_read${NC}    — read messages from the human"
echo -e "     ${GREEN}openclaw_audit_log${NC}     — record an audit event"
echo ""
echo -e "  Blueprint:   ${BLUE}$BLUEPRINT_APP_ID${NC}"
echo -e "  Agent ID:    ${BLUE}${AGENT_ID:-$BLUEPRINT_APP_ID}${NC}"
echo -e "  Human User:  ${BLUE}$HUMAN_UPN${NC}"
echo -e "  Auth Flow:   ${BLUE}OBO (On-Behalf-Of) — agent-attributed tokens${NC}"
echo ""
