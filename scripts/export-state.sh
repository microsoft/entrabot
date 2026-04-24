#!/usr/bin/env bash
# EntraClaw — export state for transfer to another machine
#
# Exports everything needed to run the MCP server on a new machine
# WITHOUT re-provisioning. Creates an encrypted archive that can be
# safely committed to a private repo branch for transfer.
#
# Usage:
#   ./scripts/export-state.sh [--password PASS]
#
# The archive includes:
#   - .env (MCP server config)
#   - .entraclaw-state.json or .openclaw-state.json (provisioning state)
#   - chat_id (persisted Teams chat ID)
#   - blueprint private key (from OS keychain)
#   - Claude Code memory files
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

EXPORT_DIR="$PROJECT_ROOT/.export-tmp"
ARCHIVE="$PROJECT_ROOT/entraclaw-state-export.tar.gz.enc"

# Parse args
PASSWORD=""
for arg in "$@"; do
    case $arg in
        --password=*) PASSWORD="${arg#--password=}" ;;
    esac
done

if [ -z "$PASSWORD" ]; then
    echo -n "Enter encryption password for export: "
    read -s PASSWORD
    echo
fi

echo -e "${BLUE}Exporting EntraClaw state...${NC}"

# Clean up any previous export
rm -rf "$EXPORT_DIR"
mkdir -p "$EXPORT_DIR"

# 1. Copy .env
if [ -f "$PROJECT_ROOT/.env" ]; then
    cp "$PROJECT_ROOT/.env" "$EXPORT_DIR/.env"
    echo -e "  ${GREEN}✅ .env${NC}"
else
    echo -e "  ${RED}❌ .env not found${NC}"
fi

# 2. Copy state file (check both names)
if [ -f "$PROJECT_ROOT/.entraclaw-state.json" ]; then
    cp "$PROJECT_ROOT/.entraclaw-state.json" "$EXPORT_DIR/.entraclaw-state.json"
    echo -e "  ${GREEN}✅ .entraclaw-state.json${NC}"
elif [ -f "$PROJECT_ROOT/.openclaw-state.json" ]; then
    cp "$PROJECT_ROOT/.openclaw-state.json" "$EXPORT_DIR/.openclaw-state.json"
    echo -e "  ${GREEN}✅ .openclaw-state.json${NC}"
else
    echo -e "  ${YELLOW}⚠️  No state file found${NC}"
fi

# 3. Copy chat_id
CHAT_ID_FILE="$HOME/.entraclaw/data/chat_id"
if [ -f "$CHAT_ID_FILE" ]; then
    cp "$CHAT_ID_FILE" "$EXPORT_DIR/chat_id"
    echo -e "  ${GREEN}✅ chat_id: $(cat "$CHAT_ID_FILE")${NC}"
else
    echo -e "  ${YELLOW}⚠️  No chat_id file found${NC}"
fi

# 4. Export private key from keychain
PYTHON=""
# Prefer venv python (has keyring installed)
if [ -f "$PROJECT_ROOT/.venv/bin/python3" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python3"
else
    for candidate in python3.12 python3.13 python3; do
        if command -v "$candidate" &>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    done
fi

if [ -n "$PYTHON" ]; then
    KEY=$("$PYTHON" -c "
try:
    import keyring
    key = keyring.get_password('entraclaw', 'blueprint-private-key')
    if key:
        print(key, end='')
    else:
        # Try legacy name
        key = keyring.get_password('openclaw', 'blueprint-private-key')
        if key:
            print(key, end='')
except Exception as e:
    import sys
    print(f'ERROR: {e}', file=sys.stderr)
" 2>/dev/null) || true

    if [ -n "$KEY" ] && [ "$KEY" != "" ]; then
        echo "$KEY" > "$EXPORT_DIR/blueprint-private-key.pem"
        echo -e "  ${GREEN}✅ Blueprint private key from keychain${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Could not extract private key from keychain${NC}"
    fi
fi

# 5. Copy Claude Code memory files
# Glob matches both the current entraclaw project slug and any legacy openclaw
# slug, so exports still round-trip for installs that renamed at different
# times. Order checks entraclaw first; falls back to openclaw.
MEMORY_DIR=""
for candidate in "$HOME/.claude/projects/"*entraclaw*/memory "$HOME/.claude/projects/"*openclaw*/memory; do
    if [ -d "$candidate" ]; then
        MEMORY_DIR="$candidate"
        break
    fi
done

if [ -n "$MEMORY_DIR" ] && [ -d "$MEMORY_DIR" ]; then
    mkdir -p "$EXPORT_DIR/claude-memory"
    cp "$MEMORY_DIR"/*.md "$EXPORT_DIR/claude-memory/" 2>/dev/null || true
    echo -e "  ${GREEN}✅ Claude Code memory ($(ls "$EXPORT_DIR/claude-memory/" | wc -l | xargs) files)${NC}"
else
    echo -e "  ${YELLOW}⚠️  No Claude Code memory directory found${NC}"
fi

# 6. Copy .mcp.json (for reference — will need path updates on new machine)
if [ -f "$PROJECT_ROOT/.mcp.json" ]; then
    cp "$PROJECT_ROOT/.mcp.json" "$EXPORT_DIR/.mcp.json"
    echo -e "  ${GREEN}✅ .mcp.json (paths will need updating on import)${NC}"
fi

# Create encrypted archive
echo ""
echo -e "${BLUE}Creating encrypted archive...${NC}"
tar -czf - -C "$EXPORT_DIR" . | openssl enc -aes-256-cbc -salt -pbkdf2 -pass "pass:$PASSWORD" -out "$ARCHIVE"

# Clean up
rm -rf "$EXPORT_DIR"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Export complete!                              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Archive: ${BLUE}$ARCHIVE${NC}"
echo -e "  Size:    ${BLUE}$(du -h "$ARCHIVE" | cut -f1)${NC}"
echo ""
echo -e "  ${YELLOW}Transfer this file to the new machine, then run:${NC}"
echo -e "  ${BLUE}./scripts/import-state.sh --password=YOUR_PASSWORD${NC}"
echo ""
echo -e "  ${RED}⚠️  Delete the archive after import — it contains secrets!${NC}"
