#!/usr/bin/env bash
# Openclaw Identity Research — one-command setup
# Creates an Entra app registration AND a dedicated Agent User,
# assigns an M365 E3 license, installs dependencies, writes .env.
# Idempotent: safe to re-run — detects existing resources and skips.
set -euo pipefail

TOTAL_STEPS=14
APP_DISPLAY_NAME="Openclaw Agent"
GRAPH_API_ID="00000003-0000-0000-c000-000000000000"

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

# Optional: Copilot CLI
if command -v copilot &>/dev/null || command -v github-copilot-cli &>/dev/null; then
    success "Copilot CLI found"
else
    warn "Copilot CLI not found — you can install it later"
fi

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

# Discover the tenant's primary domain for UPN creation
DOMAIN=$(az rest --method GET \
    --uri "https://graph.microsoft.com/v1.0/domains" \
    --query "value[?isDefault].id" -o tsv 2>/dev/null || echo "")
if [ -z "$DOMAIN" ]; then
    DOMAIN=$(az account show --query "tenantDefaultDomain" -o tsv 2>/dev/null || echo "")
fi
if [ -z "$DOMAIN" ]; then
    fail "Could not discover tenant domain. Ensure you have directory read permissions."
fi

# Discover the signed-in human user's info
HUMAN_UPN=$(az account show --query "user.name" -o tsv 2>/dev/null || echo "")
HUMAN_USER_ID=$(az ad signed-in-user show --query "id" -o tsv 2>/dev/null || echo "")

success "Subscription: $ACCOUNT_NAME ($SUBSCRIPTION_ID)"
success "Tenant:       $TENANT_ID"
success "Domain:       $DOMAIN"
success "Human user:   $HUMAN_UPN ($HUMAN_USER_ID)"

# ════════════════════════════════════════════════════════════════════════════
# Step 3: Create / find Entra app registration
# ════════════════════════════════════════════════════════════════════════════
step 3 "Creating/finding Entra app registration \"$APP_DISPLAY_NAME\""

EXISTING_APP=$(az ad app list --display-name "$APP_DISPLAY_NAME" --query "[0].appId" -o tsv 2>/dev/null)
if [ -n "$EXISTING_APP" ]; then
    success "Found existing app registration: $EXISTING_APP"
    CLIENT_ID="$EXISTING_APP"
    OBJECT_ID=$(az ad app list --display-name "$APP_DISPLAY_NAME" --query "[0].id" -o tsv)
else
    echo "  Creating new app registration..."
    APP_JSON=$(az ad app create \
        --display-name "$APP_DISPLAY_NAME" \
        --sign-in-audience AzureADMyOrg \
        --is-fallback-public-client true \
        --enable-id-token-issuance true \
        --enable-access-token-issuance true \
        --query "{appId: appId, id: id}" -o json)
    CLIENT_ID=$(echo "$APP_JSON" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['appId'])")
    OBJECT_ID=$(echo "$APP_JSON" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['id'])")
    success "Created app registration: $CLIENT_ID"
fi

# Enable ROPC (public client) for agent user token acquisition
az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
    --headers "Content-Type=application/json" \
    --body '{"isFallbackPublicClient": true}' 2>/dev/null || true
success "ROPC (public client) enabled"

# ════════════════════════════════════════════════════════════════════════════
# Step 4: Expose custom API scope (access_as_user)
# ════════════════════════════════════════════════════════════════════════════
step 4 "Exposing custom API scope (api://$CLIENT_ID/access_as_user)"

# Set the Application ID URI if not already set
APP_ID_URI=$(az ad app show --id "$OBJECT_ID" --query "identifierUris[0]" -o tsv 2>/dev/null)
if [ -z "$APP_ID_URI" ] || [ "$APP_ID_URI" = "None" ]; then
    az rest --method PATCH \
        --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
        --headers "Content-Type=application/json" \
        --body "{\"identifierUris\":[\"api://$CLIENT_ID\"]}" 2>/dev/null
    success "Set Application ID URI: api://$CLIENT_ID"
else
    success "Application ID URI already set: $APP_ID_URI"
fi

# Add oauth2PermissionScope
EXISTING_SCOPE=$(az ad app show --id "$OBJECT_ID" \
    --query "api.oauth2PermissionScopes[?value=='access_as_user'].id" -o tsv 2>/dev/null)
if [ -z "$EXISTING_SCOPE" ]; then
    SCOPE_ID=$("$PYTHON" -c "import uuid; print(uuid.uuid4())")
    az rest --method PATCH \
        --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
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
# Step 5: Add Graph API delegated permissions
# ════════════════════════════════════════════════════════════════════════════
step 5 "Adding Graph API delegated permissions"

# Permission GUIDs (Microsoft Graph delegated):
#   User.Read            = e1fe6dd8-ba31-4d61-89e7-88639da4683d
#   Chat.Create          = 9ff7295e-131b-4d94-90e1-69fde507ac11
#   ChatMessage.Send     = 116b7235-7cc6-461e-b163-8e55691d839e
#   Chat.ReadWrite       = 7427e0e9-2fba-42fe-b0c0-848c9e6a8182
az ad app permission add --id "$CLIENT_ID" \
    --api "$GRAPH_API_ID" \
    --api-permissions \
        e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope \
        9ff7295e-131b-4d94-90e1-69fde507ac11=Scope \
        116b7235-7cc6-461e-b163-8e55691d839e=Scope \
        7427e0e9-2fba-42fe-b0c0-848c9e6a8182=Scope 2>/dev/null || true

success "Delegated permissions: User.Read, Chat.Create, ChatMessage.Send, Chat.ReadWrite"

# ════════════════════════════════════════════════════════════════════════════
# Step 6: Create service principal (if not exists)
# ════════════════════════════════════════════════════════════════════════════
step 6 "Creating service principal"

EXISTING_SP=$(az ad sp list --filter "appId eq '$CLIENT_ID'" --query "[0].id" -o tsv 2>/dev/null)
if [ -n "$EXISTING_SP" ]; then
    success "Service principal already exists ($EXISTING_SP)"
else
    az ad sp create --id "$CLIENT_ID" -o none 2>/dev/null
    success "Service principal created"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 7: Grant admin consent for permissions
# ════════════════════════════════════════════════════════════════════════════
step 7 "Granting admin consent for permissions"

if az ad app permission admin-consent --id "$CLIENT_ID" 2>/dev/null; then
    success "Admin consent granted"
else
    warn "Admin consent may need to be granted manually in the Entra portal"
    warn "Visit: https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationMenuBlade/~/CallAnAPI/appId/$CLIENT_ID"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 8: Create / retrieve client secret
# ════════════════════════════════════════════════════════════════════════════
step 8 "Managing client secret"

CACHED_SECRET=""
CACHED_SECRET=$("$PYTHON" -c "
import keyring
s = keyring.get_password('openclaw', '$CLIENT_ID/client_secret')
print(s or '')
" 2>/dev/null) || true

if [ -n "$CACHED_SECRET" ]; then
    success "Using cached client secret from credential store"
    CLIENT_SECRET="$CACHED_SECRET"
else
    echo "  Creating new client secret..."
    CLIENT_SECRET=$(az ad app credential reset \
        --id "$CLIENT_ID" \
        --display-name "Openclaw MCP Server" \
        --query "password" -o tsv)

    # Cache in OS credential store
    if "$PYTHON" -c "
import keyring, sys
keyring.set_password('openclaw', '$CLIENT_ID/client_secret', sys.argv[1])
" "$CLIENT_SECRET" 2>/dev/null; then
        success "Client secret created and cached in credential store"
    else
        warn "Client secret created but could not cache in credential store"
        success "Secret will be written to .env"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 9: Create Agent User in Entra ID
# ════════════════════════════════════════════════════════════════════════════
step 9 "Creating/finding Agent User"

AGENT_UPN="openclaw-agent@$DOMAIN"
AGENT_PASSWORD=""

# Check if agent user already exists
AGENT_USER_ID=$(az ad user list --filter "userPrincipalName eq '$AGENT_UPN'" \
    --query "[0].id" -o tsv 2>/dev/null || echo "")

if [ -n "$AGENT_USER_ID" ]; then
    success "Agent user already exists: $AGENT_UPN ($AGENT_USER_ID)"
    # Try to retrieve cached password
    AGENT_PASSWORD=$("$PYTHON" -c "
import keyring
p = keyring.get_password('openclaw', 'agent_password')
print(p or '')
" 2>/dev/null) || true
    if [ -z "$AGENT_PASSWORD" ]; then
        # Reset the password so we have a known value
        AGENT_PASSWORD="$(openssl rand -base64 16)!"
        az ad user update --id "$AGENT_USER_ID" \
            --password "$AGENT_PASSWORD" \
            --force-change-password-next-sign-in false 2>/dev/null
        success "Agent user password reset"
    fi
else
    AGENT_PASSWORD="$(openssl rand -base64 16)!"
    AGENT_USER_ID=$(az ad user create \
        --display-name "Openclaw Agent" \
        --user-principal-name "$AGENT_UPN" \
        --password "$AGENT_PASSWORD" \
        --force-change-password-next-sign-in false \
        --query id -o tsv 2>/dev/null)
    success "Created agent user: $AGENT_UPN ($AGENT_USER_ID)"
fi

# Cache agent password in OS credential store
"$PYTHON" -c "
import keyring, sys
keyring.set_password('openclaw', 'agent_password', sys.argv[1])
" "$AGENT_PASSWORD" 2>/dev/null || true

# ════════════════════════════════════════════════════════════════════════════
# Step 10: Assign M365 E3 license to Agent User
# ════════════════════════════════════════════════════════════════════════════
step 10 "Assigning M365 E3 license to Agent User"

# Get the E3 SKU ID (ENTERPRISEPACK)
E3_SKU=$(az rest --method GET \
    --uri "https://graph.microsoft.com/v1.0/subscribedSkus" \
    --query "value[?contains(skuPartNumber,'ENTERPRISEPACK')].skuId" -o tsv 2>/dev/null || echo "")

if [ -z "$E3_SKU" ]; then
    warn "No M365 E3 license (ENTERPRISEPACK) found in tenant"
    warn "Agent user needs a Teams license to send messages"
else
    # Check if already assigned
    HAS_LICENSE=$(az rest --method GET \
        --uri "https://graph.microsoft.com/v1.0/users/$AGENT_USER_ID/licenseDetails" \
        --query "value[?skuId=='$E3_SKU'].skuId" -o tsv 2>/dev/null || echo "")
    if [ -n "$HAS_LICENSE" ]; then
        success "E3 license already assigned"
    else
        if az rest --method POST \
            --uri "https://graph.microsoft.com/v1.0/users/$AGENT_USER_ID/assignLicense" \
            --body "{\"addLicenses\":[{\"skuId\":\"$E3_SKU\"}],\"removeLicenses\":[]}" 2>/dev/null; then
            success "E3 license assigned to $AGENT_UPN"
        else
            warn "Could not assign E3 license — may need manual assignment"
        fi
    fi
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
# DO NOT commit this file (it is in .gitignore)

OPENCLAW_TENANT_ID=$TENANT_ID
OPENCLAW_CLIENT_ID=$CLIENT_ID
OPENCLAW_CLIENT_SECRET=$CLIENT_SECRET
OPENCLAW_SUBSCRIPTION_ID=$SUBSCRIPTION_ID
OPENCLAW_AGENT_USER_ID=$AGENT_USER_ID
OPENCLAW_AGENT_UPN=$AGENT_UPN
OPENCLAW_AGENT_PASSWORD=$AGENT_PASSWORD
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
# Step 13: Run tests
# ════════════════════════════════════════════════════════════════════════════
step 13 "Running tests"

if pytest -v --tb=short 2>&1; then
    success "All tests passed"
else
    warn "Some tests failed — review the output above"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 14: Print next steps
# ════════════════════════════════════════════════════════════════════════════
step 14 "Setup complete — next steps"

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
echo -e "  3. Available tools (pre-authenticated — no bootstrap needed):"
echo ""
echo -e "     ${GREEN}openclaw_whoami${NC}         — show agent identity and status"
echo -e "     ${GREEN}openclaw_teams_send${NC}    — send a message as the agent"
echo -e "     ${GREEN}openclaw_teams_read${NC}    — read messages from the human"
echo -e "     ${GREEN}openclaw_audit_log${NC}     — record an audit event"
echo ""
echo -e "  Agent User: ${BLUE}$AGENT_UPN${NC}"
echo -e "  Human User: ${BLUE}$HUMAN_UPN${NC}"
echo ""
