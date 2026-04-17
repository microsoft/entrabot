#!/usr/bin/env bash
# setup_delegated.sh — Sign in with your browser, cache the token, then launch the MCP server.
#
# Usage:
#   ./scripts/setup_delegated.sh
#
# What it does:
#   1. Reads ENTRACLAW_CLIENT_ID from .env
#   2. Opens your browser for Entra sign-in (MSAL localhost redirect on port 8400)
#   3. Caches the token in OS keystore (Keychain on macOS)
#   4. The MCP server picks it up silently via try_silent() — no blocking
#
# After running this, launch Claude Code normally:
#   claude --dangerously-load-development-channels server:entraclaw

set -euo pipefail
cd "$(dirname "$0")/.."

# Load .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

if [ -z "${ENTRACLAW_CLIENT_ID:-}" ]; then
    echo "ERROR: ENTRACLAW_CLIENT_ID not set in .env"
    echo "Set it to your multi-tenant app registration's Application (client) ID."
    exit 1
fi

# Activate venv
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

echo "=== EntraClaw Delegated Auth Setup ==="
echo ""
echo "Client ID: ${ENTRACLAW_CLIENT_ID}"
echo "Tenant:    ${ENTRACLAW_TENANT_ID:-common}"
echo ""
echo "Opening browser for sign-in..."
echo ""

python3 << 'PY'
import os
import sys
import json

from entraclaw.auth.delegated import MsalDelegatedAuth

client_id = os.environ["ENTRACLAW_CLIENT_ID"]
tenant_id = os.environ.get("ENTRACLAW_TENANT_ID", "common")

auth = MsalDelegatedAuth(client_id=client_id, tenant_id=tenant_id)

# Try silent first (already cached?)
silent = auth.try_silent()
if silent:
    claims = silent.get("id_token_claims", {})
    print(f"✓ Already signed in as: {claims.get('preferred_username', 'unknown')}")
    print(f"  Name:      {claims.get('name', 'unknown')}")
    print(f"  Tenant:    {claims.get('tid', 'unknown')}")
    print(f"  Token:     cached ({len(silent.get('access_token', ''))} bytes)")
    print()
    print("Token is cached. Launch Claude Code:")
    print("  claude --dangerously-load-development-channels server:entraclaw")
    sys.exit(0)

# Interactive — opens browser
try:
    result = auth.authenticate()
except Exception as exc:
    print(f"✗ Authentication failed: {exc}", file=sys.stderr)
    sys.exit(1)

if "error" in result:
    print(f"✗ Entra returned error: {result.get('error_description', result['error'])}", file=sys.stderr)
    sys.exit(1)

claims = result.get("id_token_claims", {})
print()
print(f"✓ Signed in as: {claims.get('preferred_username', 'unknown')}")
print(f"  Name:      {claims.get('name', 'unknown')}")
print(f"  Tenant:    {claims.get('tid', 'unknown')}")
print(f"  Token:     cached in OS keystore ({len(result.get('access_token', ''))} bytes)")
print()
print("Token is cached. Now launch Claude Code:")
print("  claude --dangerously-load-development-channels server:entraclaw")
PY
