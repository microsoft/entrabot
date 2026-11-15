#!/usr/bin/env bash
# setup_delegated.sh — Sign in with your browser, cache the token, then launch the MCP server.
#
# Usage:
#   ./scripts/setup_delegated.sh
#
# What it does:
#   1. Reads ENTRABOT_CLIENT_ID from .env
#   2. Opens your browser for Entra sign-in (MSAL localhost redirect on port 8400)
#   3. Caches the token in OS keystore (Keychain on macOS)
#   4. The MCP server picks it up silently via try_silent() — no blocking
#
# After running this, launch Claude Code normally:
#   claude --dangerously-load-development-channels server:entrabot

set -euo pipefail
cd "$(dirname "$0")/.."

# One value from .env, read as data the way entrabot.config reads the file: a UTF-8 byte-order
# mark and CRLF line ends are tolerated, each line is trimmed, blank lines, comments and lines
# with no "=" are skipped, the line is split at the FIRST "=", key and value are trimmed (a value
# is not unquoted), and the FIRST value of a repeated key wins. Prints the value and returns 0;
# returns 1 when the file or the key is absent. Nothing in the file is ever run: other tools write
# .env too, and running it as shell code would run a value holding $(...), backticks or ";".
env_file_value() {
    local file="$1" wanted="$2" line key first=true
    [ -f "$file" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$first" = true ]; then
            line="${line#$'\xef\xbb\xbf'}"
            first=false
        fi
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        case "$line" in
            '' | '#'*) continue ;;
            *=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        key="${key%"${key##*[![:space:]]}"}"
        if [ "$key" = "$wanted" ]; then
            line="${line#*=}"
            printf '%s' "${line#"${line%%[![:space:]]*}"}"
            return 0
        fi
    done < "$file"
    return 1
}

# Only the two values this script needs, read as data (never run: a value holding $(...),
# backticks or ";" stays text). A value already in the environment wins, as in entrabot.config.
for key in ENTRABOT_CLIENT_ID ENTRABOT_TENANT_ID; do
    if [ -z "${!key:-}" ] && value="$(env_file_value .env "$key")"; then
        export "$key=$value"
    fi
done

if [ -z "${ENTRABOT_CLIENT_ID:-}" ]; then
    echo "ERROR: ENTRABOT_CLIENT_ID not set in .env"
    echo "Set it to your multi-tenant app registration's Application (client) ID."
    exit 1
fi

# Activate venv
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

echo "=== EntraBot Delegated Auth Setup ==="
echo ""
echo "Client ID: ${ENTRABOT_CLIENT_ID}"
echo "Tenant:    ${ENTRABOT_TENANT_ID:-common}"
echo ""
echo "Opening browser for sign-in..."
echo ""

python3 << 'PY'
import os
import sys
import json

from entrabot.auth.delegated import MsalDelegatedAuth

client_id = os.environ["ENTRABOT_CLIENT_ID"]
tenant_id = os.environ.get("ENTRABOT_TENANT_ID", "common")

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
    print("  claude --dangerously-load-development-channels server:entrabot")
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
print("  claude --dangerously-load-development-channels server:entrabot")
PY
