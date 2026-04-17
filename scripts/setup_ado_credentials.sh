#!/usr/bin/env bash
# setup_ado_credentials.sh — Store an ADO PAT in macOS Keychain for git.
#
# Usage:
#   ./scripts/setup_ado_credentials.sh
#
# After running, all git push/pull to dev.azure.com will authenticate automatically.

set -euo pipefail

echo "=== ADO Git Credential Setup ==="
echo ""
echo "This stores your Azure DevOps Personal Access Token in the macOS Keychain."
echo "Generate one at: https://dev.azure.com/YourOrg/_usersSettings/tokens"
echo "Required scope: Code (Read & Write)"
echo ""

# Prompt for PAT without echoing
read -rsp "Paste your ADO PAT: " PAT
echo ""

if [ -z "$PAT" ]; then
    echo "ERROR: No PAT provided." >&2
    exit 1
fi

# Store in git credential store
printf 'protocol=https\nhost=dev.azure.com\nusername=YourOrg\npassword=%s\n\n' "$PAT" \
    | git credential approve

echo "✅ PAT stored in credential store."
echo ""

# Verify by doing a ls-remote
echo "Verifying access..."
if git ls-remote ado HEAD >/dev/null 2>&1; then
    echo "✅ ADO access verified — git push will work."
else
    echo "⚠️  Could not verify access. The PAT may need 'Code (Read & Write)' scope."
    echo "   Try: git push ado feature/multi-tenant-lightweight-chat"
fi
