#!/usr/bin/env bash
# Openclaw Identity Research — teardown
# Removes the Entra app registration, cached credentials, and .env file.
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

echo -e "${YELLOW}⚠️  This will delete the Openclaw Agent app registration and all cached credentials.${NC}"
read -p "Are you sure? (y/N) " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# ── Delete app registration ─────────────────────────────────────────────────

if [ -n "${OPENCLAW_CLIENT_ID:-}" ]; then
    OBJECT_ID=$(az ad app list \
        --filter "appId eq '${OPENCLAW_CLIENT_ID}'" \
        --query "[0].id" -o tsv 2>/dev/null)
    if [ -n "$OBJECT_ID" ]; then
        az ad app delete --id "$OBJECT_ID"
        echo -e "  ${GREEN}✅ Deleted app registration ($OPENCLAW_CLIENT_ID)${NC}"
    else
        echo -e "  ${YELLOW}⚠️  App registration not found — may already be deleted${NC}"
    fi
else
    echo -e "  ${YELLOW}⚠️  No OPENCLAW_CLIENT_ID in .env — skipping app deletion${NC}"
fi

# ── Clear cached credentials ────────────────────────────────────────────────

if [ -n "${OPENCLAW_CLIENT_ID:-}" ]; then
    python3 -c "
import keyring
try:
    keyring.delete_password('openclaw', '${OPENCLAW_CLIENT_ID}/client_secret')
    print('  ✅ Cleared cached credentials')
except Exception:
    print('  ⚠️  No cached credentials found (or keyring unavailable)')
" 2>/dev/null || echo -e "  ${YELLOW}⚠️  Could not clear credential store${NC}"
fi

# ── Remove .env ─────────────────────────────────────────────────────────────

if [ -f .env ]; then
    rm -f .env
    echo -e "  ${GREEN}✅ Removed .env file${NC}"
else
    echo -e "  ${YELLOW}⚠️  No .env file to remove${NC}"
fi

echo ""
echo -e "${GREEN}Done.${NC} Run ${YELLOW}./scripts/setup.sh${NC} to set up again."
