#!/usr/bin/env bash
# cleanup-orphans.sh — Delete orphaned Agent Identity resources
#
# When teardown.sh fails to delete Blueprint/Agent Identity (because
# az CLI tokens include Directory.AccessAsUser.All which Agent Identity
# APIs reject), those resources become orphaned.
#
# This script creates a temporary Provisioner app, uses its clean
# client_credentials token to delete the orphans, then deletes itself.
#
# Usage:
#   ./scripts/cleanup-orphans.sh <blueprint-object-id> [agent-identity-object-id]
#
# Example:
#   ./scripts/cleanup-orphans.sh 11111111-1111-1111-1111-111111111111 22222222-2222-2222-2222-222222222222

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ $# -lt 1 ]; then
    echo "Usage: $0 <blueprint-object-id> [agent-identity-object-id]"
    echo ""
    echo "Find orphaned IDs in Azure Portal → Entra ID → App registrations"
    echo "or Enterprise applications."
    exit 1
fi

BLUEPRINT_OBJ_ID="${1:-}"
AGENT_IDENTITY_OBJ_ID="${2:-}"

# Verify az CLI is logged in
if ! az account show &>/dev/null; then
    echo -e "${RED}Not logged in. Run: az login${NC}"
    exit 1
fi

TENANT_ID=$(az account show --query tenantId -o tsv)
echo -e "Tenant: ${GREEN}$TENANT_ID${NC}"
echo ""

if [ -n "$BLUEPRINT_OBJ_ID" ]; then
    echo "  Blueprint to delete:       $BLUEPRINT_OBJ_ID"
fi
if [ -n "$AGENT_IDENTITY_OBJ_ID" ]; then
    echo "  Agent Identity to delete:  $AGENT_IDENTITY_OBJ_ID"
fi
echo ""
read -p "Create temporary Provisioner and delete these? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Step 1: Creating temporary Provisioner app..."

TEMP_APP_JSON=$(az ad app create \
    --display-name "EntraClaw Orphan Cleanup (temporary)" \
    --sign-in-audience AzureADMyOrg \
    -o json)

TEMP_APP_ID=$(echo "$TEMP_APP_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['appId'])")
TEMP_OBJ_ID=$(echo "$TEMP_APP_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['id'])")
echo -e "  ${GREEN}Created: $TEMP_APP_ID${NC}"

# Create service principal
az ad sp create --id "$TEMP_APP_ID" -o json >/dev/null 2>&1 || true

# Create client secret
TEMP_SECRET_JSON=$(az ad app credential reset \
    --id "$TEMP_OBJ_ID" \
    --display-name "cleanup-temp" \
    --append \
    -o json)
TEMP_SECRET=$(echo "$TEMP_SECRET_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['password'])")

echo "Step 2: Granting Application.ReadWrite.All permission..."

# Get Microsoft Graph SP
GRAPH_SP_ID=$(az ad sp list --filter "appId eq '00000003-0000-0000-c000-000000000000'" \
    --query "[0].id" -o tsv)

# Find Application.ReadWrite.All role ID
APP_RW_ROLE=$(az ad sp show --id "$GRAPH_SP_ID" \
    --query "appRoles[?value=='Application.ReadWrite.All'].id | [0]" -o tsv)

# Assign the permission
TEMP_SP_ID=$(az ad sp list --filter "appId eq '$TEMP_APP_ID'" --query "[0].id" -o tsv)

az rest --method POST \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$TEMP_SP_ID/appRoleAssignments" \
    --body "{\"principalId\":\"$TEMP_SP_ID\",\"resourceId\":\"$GRAPH_SP_ID\",\"appRoleId\":\"$APP_RW_ROLE\"}" \
    -o none 2>/dev/null || true

echo "  Granting admin consent..."
az ad app permission admin-consent --id "$TEMP_APP_ID" 2>/dev/null || true

echo "  Waiting 15s for permission propagation..."
sleep 15

echo "Step 3: Getting clean token and deleting orphans..."

# Get token via client_credentials (no Directory.AccessAsUser.All)
python3 -c "
import requests, sys

token_resp = requests.post(
    'https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token',
    data={
        'client_id': '$TEMP_APP_ID',
        'client_secret': '$TEMP_SECRET',
        'scope': 'https://graph.microsoft.com/.default',
        'grant_type': 'client_credentials',
    },
)
if 'error' in token_resp.json():
    print(f'  ERROR getting token: {token_resp.json()}', file=sys.stderr)
    sys.exit(1)

token = token_resp.json()['access_token']
headers = {'Authorization': f'Bearer {token}'}

# Delete Agent Identity SP
agent_id = '$AGENT_IDENTITY_OBJ_ID'
if agent_id:
    resp = requests.delete(f'https://graph.microsoft.com/beta/servicePrincipals/{agent_id}', headers=headers)
    if resp.status_code in (204, 200):
        print(f'  ✅ Deleted Agent Identity SP ({agent_id})')
    elif resp.status_code == 404:
        print(f'  ⚠️  Agent Identity SP not found — already deleted')
    else:
        print(f'  ❌ Failed to delete Agent Identity SP: {resp.status_code} {resp.text[:200]}')

# Delete Blueprint app
bp_id = '$BLUEPRINT_OBJ_ID'
if bp_id:
    resp = requests.delete(f'https://graph.microsoft.com/beta/applications/{bp_id}', headers=headers)
    if resp.status_code in (204, 200):
        print(f'  ✅ Deleted Blueprint app ({bp_id})')
    elif resp.status_code == 404:
        print(f'  ⚠️  Blueprint not found — already deleted')
    else:
        print(f'  ❌ Failed to delete Blueprint: {resp.status_code} {resp.text[:200]}')
"

echo ""
echo "Step 4: Cleaning up temporary Provisioner..."

az ad app delete --id "$TEMP_OBJ_ID" 2>/dev/null && \
    echo -e "  ${GREEN}✅ Deleted temporary Provisioner${NC}" || \
    echo -e "  ${YELLOW}⚠️  Could not delete temp Provisioner — delete manually: $TEMP_OBJ_ID${NC}"

echo ""
echo -e "${GREEN}Done.${NC} Orphaned resources cleaned up."
