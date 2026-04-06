#!/usr/bin/env bash
# Openclaw Identity Research — teardown
# Removes the Agent Identity (SP), Blueprint (app), cached credentials, and .env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Load existing config (non-fatal if missing)
# shellcheck disable=SC1091
source .env 2>/dev/null || true

echo -e "${YELLOW}⚠️  This will delete the Openclaw Agent Identity, Blueprint, and all cached credentials.${NC}"
read -p "Are you sure? (y/N) " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# ── Delete Agent Identity (service principal) ───────────────────────────────

if [ -n "${OPENCLAW_AGENT_OBJECT_ID:-}" ]; then
    if az ad sp delete --id "$OPENCLAW_AGENT_OBJECT_ID" 2>/dev/null; then
        echo -e "  ${GREEN}✅ Deleted Agent Identity SP ($OPENCLAW_AGENT_OBJECT_ID)${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Could not delete Agent Identity SP — may already be deleted${NC}"
    fi
else
    echo -e "  ${YELLOW}⚠️  No OPENCLAW_AGENT_OBJECT_ID in .env — skipping SP deletion${NC}"
fi

# ── Delete Blueprint (app registration) ─────────────────────────────────────

if [ -n "${OPENCLAW_BLUEPRINT_OBJECT_ID:-}" ]; then
    if az ad app delete --id "$OPENCLAW_BLUEPRINT_OBJECT_ID" 2>/dev/null; then
        echo -e "  ${GREEN}✅ Deleted Blueprint app registration ($OPENCLAW_BLUEPRINT_APP_ID)${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Blueprint not found — may already be deleted${NC}"
    fi
elif [ -n "${OPENCLAW_BLUEPRINT_APP_ID:-}" ]; then
    OBJECT_ID=$(az ad app list \
        --filter "appId eq '${OPENCLAW_BLUEPRINT_APP_ID}'" \
        --query "[0].id" -o tsv 2>/dev/null)
    if [ -n "$OBJECT_ID" ]; then
        az ad app delete --id "$OBJECT_ID"
        echo -e "  ${GREEN}✅ Deleted Blueprint app registration ($OPENCLAW_BLUEPRINT_APP_ID)${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Blueprint not found — may already be deleted${NC}"
    fi
else
    echo -e "  ${YELLOW}⚠️  No OPENCLAW_BLUEPRINT_APP_ID in .env — skipping app deletion${NC}"
fi

# ── Delete Provisioner app (if exists) ──────────────────────────────────────

PROV_OBJ=$(az ad app list --display-name "Openclaw Agent ID Provisioner" \
    --query "[0].id" -o tsv 2>/dev/null || echo "")
if [ -n "$PROV_OBJ" ]; then
    az ad app delete --id "$PROV_OBJ" 2>/dev/null && \
        echo -e "  ${GREEN}✅ Deleted Provisioner app registration${NC}" || \
        echo -e "  ${YELLOW}⚠️  Could not delete Provisioner app${NC}"
fi

# ── Clear cached credentials ────────────────────────────────────────────────

python3 -c "
import keyring
cleared = []
for key in ['blueprint_secret', 'human_refresh_token', 'agent_password']:
    try:
        keyring.delete_password('openclaw', key)
        cleared.append(key)
    except Exception:
        pass
if cleared:
    print(f'  ✅ Cleared cached credentials: {\", \".join(cleared)}')
else:
    print('  ⚠️  No cached credentials found (or keyring unavailable)')
" 2>/dev/null || echo -e "  ${YELLOW}⚠️  Could not clear credential store${NC}"

# ── Remove .env ─────────────────────────────────────────────────────────────

if [ -f .env ]; then
    rm -f .env
    echo -e "  ${GREEN}✅ Removed .env file${NC}"
else
    echo -e "  ${YELLOW}⚠️  No .env file to remove${NC}"
fi

echo ""
echo -e "${GREEN}Done.${NC} Run ${YELLOW}./scripts/setup.sh${NC} to set up again."
