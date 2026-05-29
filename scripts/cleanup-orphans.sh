#!/usr/bin/env bash
# cleanup-orphans.sh — Delete orphaned Agent Identity resources
#
# When teardown.sh fails to delete Blueprint/Agent Identity (because
# az CLI tokens include Directory.AccessAsUser.All which Agent Identity
# APIs reject), those resources become orphaned.
#
# This script uses the EntraBot Provisioner app (cert-auth, re-created
# by ensure_app_registration if teardown wiped it) to get a clean Graph
# token, then deletes the orphans. No client secrets on disk or in the
# shell environment.
#
# Usage:
#   ./scripts/cleanup-orphans.sh <blueprint-object-id> [agent-identity-object-id]
#
# Example:
#   ./scripts/cleanup-orphans.sh xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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

# Verify az CLI is logged in (needed for the Python helper to bootstrap
# the Provisioner if it's missing)
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
read -r -p "Get a Provisioner cert-auth token and delete these? (y/N) " REPLY
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# Pick a Python that has the provisioning deps installed. Prefer the
# project venv if it exists; fall back to system python3.
if [ -x "$PROJECT_ROOT/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python3"
else
    PYTHON="python3"
fi

echo "Step 1: Acquiring Graph token via Provisioner cert-auth..."
TOKEN=$("$PYTHON" "$SCRIPT_DIR/provisioner-token.py")
if [ -z "$TOKEN" ]; then
    echo -e "${RED}Failed to acquire Provisioner token. See errors above.${NC}"
    exit 1
fi
echo -e "  ${GREEN}Token acquired${NC}"
echo ""

echo "Step 2: Deleting orphans..."

# Keep token out of argv; pass via env so it doesn't show up in `ps`.
PROVISIONER_TOKEN="$TOKEN" \
BLUEPRINT_OBJ_ID="$BLUEPRINT_OBJ_ID" \
AGENT_IDENTITY_OBJ_ID="$AGENT_IDENTITY_OBJ_ID" \
"$PYTHON" - <<'PY'
import os
import sys

import requests

token = os.environ["PROVISIONER_TOKEN"]
headers = {"Authorization": f"Bearer {token}"}

agent_id = os.environ.get("AGENT_IDENTITY_OBJ_ID") or ""
if agent_id:
    resp = requests.delete(
        f"https://graph.microsoft.com/beta/servicePrincipals/{agent_id}",
        headers=headers,
    )
    if resp.status_code in (200, 204):
        print(f"  ✅ Deleted Agent Identity SP ({agent_id})")
    elif resp.status_code == 404:
        print(f"  ⚠️  Agent Identity SP not found — already deleted")
    else:
        print(
            f"  ❌ Failed to delete Agent Identity SP: "
            f"{resp.status_code} {resp.text[:200]}"
        )

bp_id = os.environ.get("BLUEPRINT_OBJ_ID") or ""
if bp_id:
    resp = requests.delete(
        f"https://graph.microsoft.com/beta/applications/{bp_id}",
        headers=headers,
    )
    if resp.status_code in (200, 204):
        print(f"  ✅ Deleted Blueprint app ({bp_id})")
    elif resp.status_code == 404:
        print(f"  ⚠️  Blueprint not found — already deleted")
    else:
        print(
            f"  ❌ Failed to delete Blueprint: "
            f"{resp.status_code} {resp.text[:200]}"
        )
PY

echo ""
echo -e "${GREEN}Done.${NC} Orphan cleanup complete."
echo ""
echo -e "${YELLOW}Note:${NC} the Provisioner app (and its cert in Keychain) was left in place."
echo -e "  If you want a fully clean slate, run ./scripts/teardown.sh."
