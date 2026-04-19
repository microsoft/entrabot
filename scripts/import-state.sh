#!/usr/bin/env bash
# EntraClaw — import state from another machine
#
# Restores everything exported by export-state.sh so the MCP server
# can run on this machine without re-provisioning.
#
# Usage:
#   ./scripts/import-state.sh [--password PASS] [--archive PATH]
#
# After import:
#   1. Create venv: python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
#   2. Start Claude Code: claude --dangerously-load-development-channels server:entraclaw
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

ARCHIVE="$PROJECT_ROOT/entraclaw-state-export.tar.gz.enc"
PASSWORD=""

for arg in "$@"; do
    case $arg in
        --password=*) PASSWORD="${arg#--password=}" ;;
        --archive=*) ARCHIVE="${arg#--archive=}" ;;
    esac
done

if [ ! -f "$ARCHIVE" ]; then
    echo -e "${RED}Archive not found: $ARCHIVE${NC}"
    echo "  Specify with: --archive=PATH"
    exit 1
fi

if [ -z "$PASSWORD" ]; then
    echo -n "Enter decryption password: "
    read -s PASSWORD
    echo
fi

IMPORT_DIR="$PROJECT_ROOT/.import-tmp"
rm -rf "$IMPORT_DIR"
mkdir -p "$IMPORT_DIR"

echo -e "${BLUE}Decrypting and extracting archive...${NC}"
openssl enc -aes-256-cbc -d -salt -pbkdf2 -pass "pass:$PASSWORD" -in "$ARCHIVE" | tar -xzf - -C "$IMPORT_DIR"

if [ $? -ne 0 ]; then
    echo -e "${RED}Decryption failed — wrong password?${NC}"
    rm -rf "$IMPORT_DIR"
    exit 1
fi

echo -e "${BLUE}Importing state...${NC}"

# 1. Restore .env
if [ -f "$IMPORT_DIR/.env" ]; then
    cp "$IMPORT_DIR/.env" "$PROJECT_ROOT/.env"
    chmod 600 "$PROJECT_ROOT/.env"
    echo -e "  ${GREEN}✅ .env restored${NC}"
fi

# 2. Restore state file
for state_file in .entraclaw-state.json .openclaw-state.json; do
    if [ -f "$IMPORT_DIR/$state_file" ]; then
        cp "$IMPORT_DIR/$state_file" "$PROJECT_ROOT/$state_file"
        echo -e "  ${GREEN}✅ $state_file restored${NC}"
    fi
done

# 3. Restore chat_id
if [ -f "$IMPORT_DIR/chat_id" ]; then
    mkdir -p "$HOME/.entraclaw/data"
    cp "$IMPORT_DIR/chat_id" "$HOME/.entraclaw/data/chat_id"
    echo -e "  ${GREEN}✅ chat_id restored: $(cat "$HOME/.entraclaw/data/chat_id")${NC}"
fi

# 4. Import private key to keychain
PYTHON=""
for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -f "$IMPORT_DIR/blueprint-private-key.pem" ] && [ -n "$PYTHON" ]; then
    "$PYTHON" -c "
import keyring
key = open('$IMPORT_DIR/blueprint-private-key.pem').read()
keyring.set_password('entraclaw', 'blueprint-private-key', key)
print('Key stored in keychain')
" 2>/dev/null && echo -e "  ${GREEN}✅ Blueprint private key stored in keychain${NC}" \
    || echo -e "  ${YELLOW}⚠️  Could not store key in keychain — install keyring: pip install keyring${NC}"
fi

# 5. Restore Claude Code memory
if [ -d "$IMPORT_DIR/claude-memory" ]; then
    # Find or create the Claude memory directory for this project
    # The path is based on the project root, with slashes replaced by dashes
    PROJECT_PATH=$(echo "$PROJECT_ROOT" | sed 's|/|-|g')
    MEMORY_DIR="$HOME/.claude/projects/$PROJECT_PATH/memory"
    mkdir -p "$MEMORY_DIR"
    cp "$IMPORT_DIR/claude-memory"/*.md "$MEMORY_DIR/" 2>/dev/null || true
    echo -e "  ${GREEN}✅ Claude Code memory restored to $MEMORY_DIR${NC}"
fi

# 6. Regenerate .mcp.json with correct paths for this machine
# NOTE: To add persona-sati (mind server), see .mcp.json.example
ENTRACLAW_MCP_BIN="$PROJECT_ROOT/.venv/bin/entraclaw-mcp"
cat > "$PROJECT_ROOT/.mcp.json" << MCPEOF
{
  "mcpServers": {
    "entraclaw": {
      "type": "stdio",
      "command": "$ENTRACLAW_MCP_BIN",
      "args": [],
      "description": "EntraClaw Agent Identity — Teams tools + background DM/email poll"
    }
  }
}
MCPEOF
echo -e "  ${GREEN}✅ .mcp.json regenerated with local paths (see .mcp.json.example for persona-sati)${NC}"

# Clean up
rm -rf "$IMPORT_DIR"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Import complete!                              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${YELLOW}Next steps:${NC}"
echo -e "  1. Create venv:  ${BLUE}python3.12 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'${NC}"
echo -e "  2. Run tests:    ${BLUE}pytest -v${NC}"
echo -e "  3. Start Claude: ${BLUE}claude --dangerously-load-development-channels server:entraclaw${NC}"
echo ""
echo -e "  ${RED}⚠️  Delete the archive now: rm entraclaw-state-export.tar.gz.enc${NC}"
