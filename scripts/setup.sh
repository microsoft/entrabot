#!/usr/bin/env bash
# Openclaw Identity Research — one-command setup
# Creates a dedicated Provisioner app registration (client_credentials flow),
# uses it to call the Entra Agent Identity beta APIs via curl + Bearer token,
# creates an Agent Identity Blueprint + BlueprintPrincipal + Agent Identity,
# runs a one-time human device-code auth, caches the refresh token in the OS
# keychain, installs dependencies, and writes .env.
#
# KEY DESIGN DECISIONS:
#   - Azure CLI tokens include Directory.AccessAsUser.All which Agent Identity
#     APIs REJECT. We use a dedicated provisioner app with client_credentials.
#   - BlueprintPrincipal must be created explicitly (not auto-created).
#   - Permission propagation needs 30-120s; we retry with backoff.
#
# Idempotent: safe to re-run — detects existing resources and skips.
set -euo pipefail

TOTAL_STEPS=23
GRAPH_API_ID="00000003-0000-0000-c000-000000000000"
GRAPH_BETA="https://graph.microsoft.com/beta"
GRAPH_V1="https://graph.microsoft.com/v1.0"
APP_READWRITE_ALL_ID="1bfefb4e-e0b5-418b-a88f-73c46d2cc8e9"
PROVISIONER_APP_NAME="Openclaw Provisioner"
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

# ── Helper: Graph API call using provisioner token ──────────────────────────
# Uses curl (NOT az rest) with the provisioner's client_credentials token.
# Usage: graph_call METHOD URL [JSON_BODY]
graph_call() {
    local method="$1"
    local url="$2"
    local body="${3:-}"

    local curl_args=(
        -s -w "\n%{http_code}"
        -X "$method"
        -H "Authorization: Bearer $PROV_TOKEN"
        -H "Content-Type: application/json"
    )
    if [ -n "$body" ]; then
        curl_args+=(-d "$body")
    fi

    curl "${curl_args[@]}" "$url"
}

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

if ! command -v curl &>/dev/null; then
    MISSING+=("curl")
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
success "curl found"

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

success "Subscription: $ACCOUNT_NAME ($SUBSCRIPTION_ID)"
success "Tenant:       $TENANT_ID"

# ════════════════════════════════════════════════════════════════════════════
# Step 3: Get human user ID
# ════════════════════════════════════════════════════════════════════════════
step 3 "Getting signed-in user identity"

HUMAN_UPN=$(az account show --query "user.name" -o tsv 2>/dev/null || echo "")
HUMAN_USER_ID=$(az ad signed-in-user show --query "id" -o tsv 2>/dev/null || echo "")

if [ -z "$HUMAN_USER_ID" ]; then
    fail "Could not determine signed-in user ID. Ensure 'az login' is done with a user account."
fi

success "Human user: $HUMAN_UPN ($HUMAN_USER_ID)"

# ════════════════════════════════════════════════════════════════════════════
# Step 4: Create / find "Openclaw Provisioner" app registration
# ════════════════════════════════════════════════════════════════════════════
step 4 "Creating/finding Provisioner app registration"

# A dedicated app for Agent ID provisioning — Azure CLI tokens include
# Directory.AccessAsUser.All which the Agent Identity APIs REJECT (403).
EXISTING_PROV=$(az ad app list --display-name "$PROVISIONER_APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || echo "")
if [ -n "$EXISTING_PROV" ]; then
    success "Found existing provisioner app: $EXISTING_PROV"
    PROV_CLIENT_ID="$EXISTING_PROV"
    PROV_OBJECT_ID=$(az ad app show --id "$PROV_CLIENT_ID" --query "id" -o tsv)
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

# ════════════════════════════════════════════════════════════════════════════
# Step 5: Create provisioner service principal
# ════════════════════════════════════════════════════════════════════════════
step 5 "Ensuring provisioner service principal"

EXISTING_PROV_SP=$(az ad sp list --filter "appId eq '$PROV_CLIENT_ID'" --query "[0].id" -o tsv 2>/dev/null || echo "")
if [ -n "$EXISTING_PROV_SP" ]; then
    success "Provisioner SP already exists ($EXISTING_PROV_SP)"
else
    az ad sp create --id "$PROV_CLIENT_ID" -o none 2>/dev/null || true
    success "Provisioner service principal created"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 6: Discover all Agent Identity permissions dynamically
# ════════════════════════════════════════════════════════════════════════════
step 6 "Discovering Agent Identity permissions from Microsoft Graph"

# Discover all 18 Agent Identity permissions from the Graph service principal
AGENT_PERM_IDS=$(az ad sp show --id "$GRAPH_API_ID" \
    --query "appRoles[?contains(value, 'AgentIdentity')].id" -o tsv 2>/dev/null || echo "")

AGENT_PERM_COUNT=0
if [ -n "$AGENT_PERM_IDS" ]; then
    AGENT_PERM_COUNT=$(echo "$AGENT_PERM_IDS" | wc -l | tr -d ' ')
fi

success "Found $AGENT_PERM_COUNT Agent Identity permissions"

# ════════════════════════════════════════════════════════════════════════════
# Step 7: Add all permissions to the provisioner app
# ════════════════════════════════════════════════════════════════════════════
step 7 "Adding application permissions to provisioner app"

# Build the list of permission specs: each Agent Identity permission + Application.ReadWrite.All
PERM_SPECS=()
while IFS= read -r perm_id; do
    [ -n "$perm_id" ] && PERM_SPECS+=("${perm_id}=Role")
done <<< "$AGENT_PERM_IDS"

# Always include Application.ReadWrite.All (needed for Blueprint CRUD)
PERM_SPECS+=("${APP_READWRITE_ALL_ID}=Role")

echo "  Adding ${#PERM_SPECS[@]} application permissions..."
az ad app permission add --id "$PROV_CLIENT_ID" \
    --api "$GRAPH_API_ID" \
    --api-permissions "${PERM_SPECS[@]}" 2>/dev/null || true

success "Application permissions configured (${#PERM_SPECS[@]} total)"

# ════════════════════════════════════════════════════════════════════════════
# Step 8: Grant admin consent (with 10-40s retry backoff)
# ════════════════════════════════════════════════════════════════════════════
step 8 "Granting admin consent for provisioner app"

CONSENT_GRANTED=false
for attempt in 1 2 3 4; do
    WAIT=$((10 * attempt))
    echo "  Waiting ${WAIT}s before consent attempt $attempt/4..."
    sleep "$WAIT"
    if az ad app permission admin-consent --id "$PROV_CLIENT_ID" 2>/dev/null; then
        CONSENT_GRANTED=true
        break
    else
        warn "Consent attempt $attempt failed (SP may still be propagating)"
    fi
done

if [ "$CONSENT_GRANTED" = true ]; then
    success "Admin consent granted"
else
    fail "Admin consent failed after 4 attempts. Grant manually:\n  az ad app permission admin-consent --id $PROV_CLIENT_ID"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 9: Wait for permission propagation
# ════════════════════════════════════════════════════════════════════════════
step 9 "Waiting for permission propagation (30s)"

echo "  Token endpoint may serve cached claims for 30-120s after consent..."
sleep 30
success "Propagation wait complete"

# ════════════════════════════════════════════════════════════════════════════
# Step 10: Create client secret for the provisioner app
# ════════════════════════════════════════════════════════════════════════════
step 10 "Creating provisioner client secret"

echo "  Creating new client secret on provisioner app..."
PROV_CRED_JSON=$(az ad app credential reset \
    --id "$PROV_CLIENT_ID" \
    --display-name "Openclaw Setup" \
    -o json 2>/dev/null)

PROV_SECRET=$("$PYTHON" -c "import sys,json; print(json.loads('''$PROV_CRED_JSON''')['password'])" 2>/dev/null)
if [ -z "$PROV_SECRET" ]; then
    # Fallback: try extracting with --query
    PROV_SECRET=$(az ad app credential reset \
        --id "$PROV_CLIENT_ID" \
        --display-name "Openclaw Setup Retry" \
        --query "password" -o tsv 2>/dev/null)
fi

if [ -z "$PROV_SECRET" ]; then
    fail "Could not create provisioner client secret"
fi
success "Provisioner client secret created"

# ════════════════════════════════════════════════════════════════════════════
# Step 11: Acquire token via ClientSecretCredential (NOT az rest)
# ════════════════════════════════════════════════════════════════════════════
step 11 "Acquiring provisioner token via ClientSecretCredential"

# Ensure azure-identity is available — install in project venv if it exists, else system
if [ -d "$PROJECT_ROOT/.venv" ]; then
    VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python3"
    "$VENV_PYTHON" -m pip install --quiet azure-identity 2>/dev/null || true
    TOKEN_PYTHON="$VENV_PYTHON"
else
    "$PYTHON" -m pip install --quiet azure-identity 2>/dev/null || true
    TOKEN_PYTHON="$PYTHON"
fi

PROV_TOKEN=$("$TOKEN_PYTHON" -c "
from azure.identity import ClientSecretCredential
cred = ClientSecretCredential('$TENANT_ID', '$PROV_CLIENT_ID', '$PROV_SECRET')
token = cred.get_token('https://graph.microsoft.com/.default')
print(token.token)
" 2>&1)
TOKEN_EXIT=$?

if [ $TOKEN_EXIT -ne 0 ] || [ -z "$PROV_TOKEN" ]; then
    echo "  Token acquisition error output:"
    echo "  $PROV_TOKEN" | head -5
    fail "Could not acquire provisioner token. Check errors above."
fi
success "Provisioner token acquired via client_credentials flow"

# ════════════════════════════════════════════════════════════════════════════
# Step 12: Create / find Agent Identity Blueprint
# ════════════════════════════════════════════════════════════════════════════
step 12 "Creating/finding Agent Identity Blueprint"

# Check for existing blueprint by displayName using the provisioner token
BP_RESPONSE=$(graph_call GET "$GRAPH_BETA/applications?\$filter=displayName%20eq%20'$BLUEPRINT_DISPLAY_NAME'")
BP_HTTP_CODE=$(echo "$BP_RESPONSE" | tail -1)
BP_BODY=$(echo "$BP_RESPONSE" | sed '$d')

BLUEPRINT_APP_ID=""
BLUEPRINT_OBJECT_ID=""

if [ "$BP_HTTP_CODE" = "200" ]; then
    BLUEPRINT_APP_ID=$(echo "$BP_BODY" | "$PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
values = data.get('value', [])
print(values[0]['appId'] if values else '')
" 2>/dev/null) || true
    BLUEPRINT_OBJECT_ID=$(echo "$BP_BODY" | "$PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
values = data.get('value', [])
print(values[0]['id'] if values else '')
" 2>/dev/null) || true
fi

if [ -n "$BLUEPRINT_APP_ID" ]; then
    success "Found existing blueprint: $BLUEPRINT_APP_ID (obj: $BLUEPRINT_OBJECT_ID)"
else
    echo "  Creating Agent Identity Blueprint via Graph beta API..."
    CREATE_BP_BODY=$(cat <<BPEOF
{
    "@odata.type": "Microsoft.Graph.AgentIdentityBlueprint",
    "displayName": "$BLUEPRINT_DISPLAY_NAME",
    "description": "Agent Identity Blueprint for Openclaw device agents",
    "sponsors@odata.bind": ["https://graph.microsoft.com/beta/users/$HUMAN_USER_ID"]
}
BPEOF
)
    BP_CREATE_RESPONSE=$(graph_call POST "$GRAPH_BETA/applications" "$CREATE_BP_BODY")
    BP_CREATE_CODE=$(echo "$BP_CREATE_RESPONSE" | tail -1)
    BP_CREATE_BODY=$(echo "$BP_CREATE_RESPONSE" | sed '$d')

    if [ "$BP_CREATE_CODE" = "201" ] || [ "$BP_CREATE_CODE" = "200" ]; then
        BLUEPRINT_APP_ID=$(echo "$BP_CREATE_BODY" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['appId'])")
        BLUEPRINT_OBJECT_ID=$(echo "$BP_CREATE_BODY" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['id'])")
        success "Created blueprint: $BLUEPRINT_APP_ID"
    else
        echo "  Response ($BP_CREATE_CODE): $BP_CREATE_BODY"
        fail "Failed to create Agent Identity Blueprint"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 13: Create BlueprintPrincipal (REQUIRED — not auto-created)
# ════════════════════════════════════════════════════════════════════════════
step 13 "Creating/finding BlueprintPrincipal (service principal for blueprint)"

# Check if BlueprintPrincipal SP already exists
BP_SP_RESPONSE=$(graph_call GET "$GRAPH_BETA/servicePrincipals?\$filter=appId%20eq%20'$BLUEPRINT_APP_ID'")
BP_SP_CODE=$(echo "$BP_SP_RESPONSE" | tail -1)
BP_SP_BODY=$(echo "$BP_SP_RESPONSE" | sed '$d')

EXISTING_BP_SP=""
if [ "$BP_SP_CODE" = "200" ]; then
    EXISTING_BP_SP=$(echo "$BP_SP_BODY" | "$PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
values = data.get('value', [])
print(values[0]['id'] if values else '')
" 2>/dev/null) || true
fi

if [ -n "$EXISTING_BP_SP" ]; then
    success "BlueprintPrincipal already exists ($EXISTING_BP_SP)"
else
    echo "  Creating BlueprintPrincipal..."
    BP_SP_CREATE_BODY=$(cat <<SPEOF
{
    "@odata.type": "Microsoft.Graph.AgentIdentityBlueprintPrincipal",
    "appId": "$BLUEPRINT_APP_ID"
}
SPEOF
)
    # Retry — blueprint app may not have propagated
    BP_SP_CREATED=false
    for attempt in 1 2 3 4; do
        BP_SP_CREATE_RESPONSE=$(graph_call POST "$GRAPH_BETA/servicePrincipals" "$BP_SP_CREATE_BODY")
        BP_SP_CREATE_CODE=$(echo "$BP_SP_CREATE_RESPONSE" | tail -1)
        BP_SP_CREATE_RESULT=$(echo "$BP_SP_CREATE_RESPONSE" | sed '$d')

        if [ "$BP_SP_CREATE_CODE" = "201" ] || [ "$BP_SP_CREATE_CODE" = "200" ]; then
            EXISTING_BP_SP=$(echo "$BP_SP_CREATE_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('id',''))")
            success "BlueprintPrincipal created ($EXISTING_BP_SP)"
            BP_SP_CREATED=true
            break
        else
            WAIT=$((10 * attempt))
            warn "BlueprintPrincipal creation returned $BP_SP_CREATE_CODE, retrying in ${WAIT}s..."
            sleep "$WAIT"
        fi
    done

    if [ "$BP_SP_CREATED" = false ]; then
        echo "  Last response: $BP_SP_CREATE_RESULT"
        fail "Failed to create BlueprintPrincipal after retries"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 14: Set identifierUris on Blueprint
# ════════════════════════════════════════════════════════════════════════════
step 14 "Setting Application ID URI on Blueprint"

# Check current identifierUris
URI_CHECK_RESPONSE=$(graph_call GET "$GRAPH_BETA/applications/$BLUEPRINT_OBJECT_ID?\$select=identifierUris")
URI_CHECK_CODE=$(echo "$URI_CHECK_RESPONSE" | tail -1)
URI_CHECK_BODY=$(echo "$URI_CHECK_RESPONSE" | sed '$d')

CURRENT_URI=""
if [ "$URI_CHECK_CODE" = "200" ]; then
    CURRENT_URI=$(echo "$URI_CHECK_BODY" | "$PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
uris = data.get('identifierUris', [])
print(uris[0] if uris else '')
" 2>/dev/null) || true
fi

if [ -n "$CURRENT_URI" ]; then
    success "Application ID URI already set: $CURRENT_URI"
else
    echo "  Setting identifierUris to api://$BLUEPRINT_APP_ID..."
    URI_PATCH_BODY="{\"identifierUris\":[\"api://$BLUEPRINT_APP_ID\"]}"
    URI_PATCH_RESPONSE=$(graph_call PATCH "$GRAPH_BETA/applications/$BLUEPRINT_OBJECT_ID" "$URI_PATCH_BODY")
    URI_PATCH_CODE=$(echo "$URI_PATCH_RESPONSE" | tail -1)

    if [ "$URI_PATCH_CODE" = "204" ] || [ "$URI_PATCH_CODE" = "200" ]; then
        success "Set Application ID URI: api://$BLUEPRINT_APP_ID"
    else
        warn "Could not set identifierUris ($URI_PATCH_CODE) — may need manual configuration"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 15: Add custom API scope (access_as_user) for OBO
# ════════════════════════════════════════════════════════════════════════════
step 15 "Adding custom API scope (access_as_user) for OBO"

# Check if scope already exists
SCOPE_CHECK_RESPONSE=$(graph_call GET "$GRAPH_BETA/applications/$BLUEPRINT_OBJECT_ID?\$select=api")
SCOPE_CHECK_CODE=$(echo "$SCOPE_CHECK_RESPONSE" | tail -1)
SCOPE_CHECK_BODY=$(echo "$SCOPE_CHECK_RESPONSE" | sed '$d')

EXISTING_SCOPE=""
if [ "$SCOPE_CHECK_CODE" = "200" ]; then
    EXISTING_SCOPE=$(echo "$SCOPE_CHECK_BODY" | "$PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
scopes = data.get('api', {}).get('oauth2PermissionScopes', [])
for s in scopes:
    if s.get('value') == 'access_as_user':
        print(s['id'])
        break
" 2>/dev/null) || true
fi

if [ -n "$EXISTING_SCOPE" ]; then
    success "Scope access_as_user already exists ($EXISTING_SCOPE)"
else
    SCOPE_ID=$("$PYTHON" -c "import uuid; print(uuid.uuid4())")
    SCOPE_PATCH_BODY=$(cat <<SCOPEEOF
{
    "api": {
        "oauth2PermissionScopes": [{
            "adminConsentDescription": "Allow Openclaw agent to act on behalf of the user",
            "adminConsentDisplayName": "Access as user",
            "id": "$SCOPE_ID",
            "isEnabled": true,
            "type": "User",
            "userConsentDescription": "Allow Openclaw agent to act on your behalf",
            "userConsentDisplayName": "Access as user",
            "value": "access_as_user"
        }]
    }
}
SCOPEEOF
)
    SCOPE_PATCH_RESPONSE=$(graph_call PATCH "$GRAPH_BETA/applications/$BLUEPRINT_OBJECT_ID" "$SCOPE_PATCH_BODY")
    SCOPE_PATCH_CODE=$(echo "$SCOPE_PATCH_RESPONSE" | tail -1)

    if [ "$SCOPE_PATCH_CODE" = "204" ] || [ "$SCOPE_PATCH_CODE" = "200" ]; then
        success "Created scope: access_as_user ($SCOPE_ID)"
    else
        warn "Could not create access_as_user scope ($SCOPE_PATCH_CODE) — may need manual configuration"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 16: Add Graph delegated permissions to Blueprint
# ════════════════════════════════════════════════════════════════════════════
step 16 "Adding Graph delegated permissions to Blueprint"

# User.Read, Chat.Create, ChatMessage.Send, Chat.ReadWrite (Delegated/Scope)
az ad app permission add --id "$BLUEPRINT_APP_ID" \
    --api "$GRAPH_API_ID" \
    --api-permissions \
        e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope \
        9ff7295e-131b-4d94-90e1-69fde507ac11=Scope \
        116b7235-7cc6-461e-b163-8e55691d839e=Scope \
        7427e0e9-2fba-42fe-b0c0-848c9e6a8182=Scope 2>/dev/null || true

success "Delegated permissions: User.Read, Chat.Create, ChatMessage.Send, Chat.ReadWrite"

# ════════════════════════════════════════════════════════════════════════════
# Step 17: Grant admin consent for Blueprint permissions
# ════════════════════════════════════════════════════════════════════════════
step 17 "Granting admin consent for Blueprint"

BP_CONSENT_GRANTED=false
for attempt in 1 2 3 4; do
    WAIT=$((10 * attempt))
    echo "  Waiting ${WAIT}s before consent attempt $attempt/4..."
    sleep "$WAIT"
    if az ad app permission admin-consent --id "$BLUEPRINT_APP_ID" 2>/dev/null; then
        BP_CONSENT_GRANTED=true
        break
    else
        warn "Consent attempt $attempt failed"
    fi
done

if [ "$BP_CONSENT_GRANTED" = true ]; then
    success "Admin consent granted for Blueprint"
else
    warn "Admin consent for Blueprint failed. Grant manually:"
    warn "  az ad app permission admin-consent --id $BLUEPRINT_APP_ID"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 18: Create client secret on Blueprint (for OBO exchange)
# ════════════════════════════════════════════════════════════════════════════
step 18 "Managing Blueprint client secret"

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
    echo "  Creating new client secret on Blueprint..."
    BP_CRED_JSON=$(az ad app credential reset \
        --id "$BLUEPRINT_OBJECT_ID" \
        --display-name "Openclaw Device" \
        -o json 2>/dev/null)
    BLUEPRINT_SECRET=$("$PYTHON" -c "import sys,json; print(json.loads('''$BP_CRED_JSON''')['password'])" 2>/dev/null)
    if [ -z "$BLUEPRINT_SECRET" ]; then
        BLUEPRINT_SECRET=$(az ad app credential reset \
            --id "$BLUEPRINT_OBJECT_ID" \
            --display-name "Openclaw Device Retry" \
            --query "password" -o tsv 2>/dev/null)
    fi

    if [ -z "$BLUEPRINT_SECRET" ]; then
        fail "Could not create Blueprint client secret"
    fi

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
# Step 19: Create Agent Identity (service principal linked to Blueprint)
# ════════════════════════════════════════════════════════════════════════════
step 19 "Creating/finding Agent Identity"

HOSTNAME_SHORT=$(hostname -s 2>/dev/null || hostname)
AGENT_DISPLAY_NAME="Openclaw Agent - $HOSTNAME_SHORT"

# Check for existing agent identity using provisioner token
AGENT_SEARCH_RESPONSE=$(graph_call GET "$GRAPH_BETA/servicePrincipals?\$filter=displayName%20eq%20'$(echo "$AGENT_DISPLAY_NAME" | sed "s/ /%20/g; s/'/''/g")'")
AGENT_SEARCH_CODE=$(echo "$AGENT_SEARCH_RESPONSE" | tail -1)
AGENT_SEARCH_BODY=$(echo "$AGENT_SEARCH_RESPONSE" | sed '$d')

AGENT_ID=""
AGENT_OBJECT_ID=""

if [ "$AGENT_SEARCH_CODE" = "200" ]; then
    AGENT_ID=$(echo "$AGENT_SEARCH_BODY" | "$PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
values = data.get('value', [])
print(values[0].get('appId', '') if values else '')
" 2>/dev/null) || true
    AGENT_OBJECT_ID=$(echo "$AGENT_SEARCH_BODY" | "$PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
values = data.get('value', [])
print(values[0].get('id', '') if values else '')
" 2>/dev/null) || true
fi

if [ -n "$AGENT_ID" ]; then
    success "Found existing agent identity: $AGENT_DISPLAY_NAME ($AGENT_ID)"
else
    echo "  Creating Agent Identity via Graph beta API..."
    AGENT_CREATE_BODY=$(cat <<AGENTEOF
{
    "@odata.type": "Microsoft.Graph.AgentIdentity",
    "displayName": "$AGENT_DISPLAY_NAME",
    "agentIdentityBlueprintId": "$BLUEPRINT_APP_ID",
    "sponsors@odata.bind": ["https://graph.microsoft.com/beta/users/$HUMAN_USER_ID"]
}
AGENTEOF
)

    AGENT_CREATED=false
    for attempt in 1 2 3; do
        AGENT_CREATE_RESPONSE=$(graph_call POST "$GRAPH_BETA/servicePrincipals" "$AGENT_CREATE_BODY")
        AGENT_CREATE_CODE=$(echo "$AGENT_CREATE_RESPONSE" | tail -1)
        AGENT_CREATE_RESULT=$(echo "$AGENT_CREATE_RESPONSE" | sed '$d')

        if [ "$AGENT_CREATE_CODE" = "201" ] || [ "$AGENT_CREATE_CODE" = "200" ]; then
            AGENT_ID=$(echo "$AGENT_CREATE_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('appId',''))")
            AGENT_OBJECT_ID=$(echo "$AGENT_CREATE_RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('id',''))")
            success "Created agent identity: $AGENT_DISPLAY_NAME ($AGENT_ID)"
            AGENT_CREATED=true
            break
        elif [ "$AGENT_CREATE_CODE" = "403" ]; then
            fail "Permission denied creating Agent Identity — check provisioner permissions"
        else
            WAIT=$((10 * attempt))
            warn "Agent Identity creation returned $AGENT_CREATE_CODE, retrying in ${WAIT}s..."
            sleep "$WAIT"
        fi
    done

    if [ "$AGENT_CREATED" = false ]; then
        echo "  Last response: $AGENT_CREATE_RESULT"
        fail "Failed to create Agent Identity after retries"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 20: Human device-code flow (one-time consent)
# ════════════════════════════════════════════════════════════════════════════
step 20 "Human device-code authentication (one-time consent)"

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
import sys
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
print(f'  ⏳ Waiting for authentication (timeout: 120s)...\\n')

result = app.acquire_token_by_device_flow(flow, timeout=120)
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
# Step 21: Create Python venv and install dependencies
# ════════════════════════════════════════════════════════════════════════════
step 21 "Setting up Python virtual environment and installing dependencies"

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

# Write .env file
cat > .env << EOF
# Openclaw Identity Research — generated by scripts/setup.sh
# Uses Entra Agent Identity Blueprint + OBO flow (no fake users)
# DO NOT commit this file (it is in .gitignore)

OPENCLAW_TENANT_ID=$TENANT_ID
OPENCLAW_BLUEPRINT_APP_ID=$BLUEPRINT_APP_ID
OPENCLAW_BLUEPRINT_OBJECT_ID=$BLUEPRINT_OBJECT_ID
OPENCLAW_BLUEPRINT_SECRET=$BLUEPRINT_SECRET
OPENCLAW_AGENT_ID=$AGENT_ID
OPENCLAW_AGENT_OBJECT_ID=$AGENT_OBJECT_ID
OPENCLAW_HUMAN_USER_ID=$HUMAN_USER_ID
OPENCLAW_HUMAN_UPN=$HUMAN_UPN
OPENCLAW_PROVISIONER_APP_ID=$PROV_CLIENT_ID
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
# Step 22: Run tests
# ════════════════════════════════════════════════════════════════════════════
step 22 "Running tests"

if pytest -v --tb=short 2>&1; then
    success "All tests passed"
else
    warn "Some tests failed — review the output above"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 23: Print summary
# ════════════════════════════════════════════════════════════════════════════
step 23 "Setup complete — summary"

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
echo -e "  Provisioner: ${BLUE}$PROV_CLIENT_ID${NC}"
echo -e "  Blueprint:   ${BLUE}$BLUEPRINT_APP_ID${NC}"
echo -e "  Agent ID:    ${BLUE}$AGENT_ID${NC}"
echo -e "  Human User:  ${BLUE}$HUMAN_UPN${NC}"
echo -e "  Auth Flow:   ${BLUE}OBO (On-Behalf-Of) — agent-attributed tokens${NC}"
echo ""
