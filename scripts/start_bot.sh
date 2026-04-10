#!/usr/bin/env bash
# start_bot.sh — Launch the EntraClaw bot gateway (tunnel + bot server + MCP server)
#
# One command to start everything needed for bot mode:
#   1. Validates prerequisites (devtunnel CLI, .env config, venv)
#   2. Starts a Dev Tunnel on the configured port
#   3. Starts the bot server (aiohttp on localhost:PORT)
#   4. Prints the tunnel URL to register in Azure Bot Service
#
# The MCP server is launched separately by Claude Code via:
#   claude --dangerously-load-development-channels server:entraclaw
#
# Usage:
#   ./scripts/start_bot.sh          # start tunnel + bot server
#   ./scripts/start_bot.sh --stop   # kill running tunnel + bot server
#
# Prerequisites (one-time):
#   1. Run ./scripts/setup_bot.sh first (creates Azure Bot + app registration + cert)
#   2. devtunnel CLI installed (https://learn.microsoft.com/azure/developer/dev-tunnels/get-started)
#   3. pip install -e ".[dev]"

set -euo pipefail
cd "$(dirname "$0")/.."

# ── Colors ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}▸${NC} $*"; }
ok()    { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "${RED}✗${NC} $*"; exit 1; }

# ── Pidfile management ─────────────────────────────────────────────────────
PIDDIR="${HOME}/.entraclaw/bot"
TUNNEL_PIDFILE="${PIDDIR}/tunnel.pid"
BOT_PIDFILE="${PIDDIR}/bot.pid"

mkdir -p "${PIDDIR}"

stop_processes() {
    local stopped=0
    for pidfile in "${TUNNEL_PIDFILE}" "${BOT_PIDFILE}"; do
        if [ -f "${pidfile}" ]; then
            pid=$(cat "${pidfile}")
            if kill -0 "${pid}" 2>/dev/null; then
                kill "${pid}" 2>/dev/null || true
                info "Stopped PID ${pid} ($(basename "${pidfile}" .pid))"
                stopped=1
            fi
            rm -f "${pidfile}"
        fi
    done
    if [ "${stopped}" -eq 0 ]; then
        info "No running bot processes found."
    fi
}

if [ "${1:-}" = "--stop" ]; then
    stop_processes
    exit 0
fi

# Stop any existing processes before starting fresh
stop_processes 2>/dev/null || true

# ── Load .env ──────────────────────────────────────────────────────────────
if [ -f .env ]; then
    set -a
    source .env
    set +a
    ok "Loaded .env"
else
    warn "No .env file found — using environment variables"
fi

# ── Validate config ───────────────────────────────────────────────────────
PORT="${ENTRACLAW_BOT_TUNNEL_PORT:-3978}"

if [ -z "${ENTRACLAW_BOT_APP_ID:-}" ]; then
    fail "ENTRACLAW_BOT_APP_ID not set. Run ./scripts/setup_bot.sh first."
fi
ok "Bot app ID: ${ENTRACLAW_BOT_APP_ID}"

# ── Check prerequisites ──────────────────────────────────────────────────
if ! command -v devtunnel &>/dev/null; then
    fail "devtunnel CLI not found.\n   Install: https://learn.microsoft.com/azure/developer/dev-tunnels/get-started"
fi
ok "devtunnel CLI found"

# Activate venv if available
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
    ok "Activated .venv"
elif [ -f venv/bin/activate ]; then
    source venv/bin/activate
    ok "Activated venv"
fi

python -c "import entraclaw.bot.server" 2>/dev/null \
    || fail "entraclaw.bot.server not importable. Run: pip install -e '.[dev]'"
ok "entraclaw package importable"

python -c "import aiohttp" 2>/dev/null \
    || fail "aiohttp not installed. Run: pip install -e '.[dev]'"
ok "aiohttp installed"

# ── Start Dev Tunnel ─────────────────────────────────────────────────────
info "Starting Dev Tunnel on port ${PORT}..."
devtunnel host -p "${PORT}" --allow-anonymous > "${PIDDIR}/tunnel.log" 2>&1 &
TUNNEL_PID=$!
echo "${TUNNEL_PID}" > "${TUNNEL_PIDFILE}"

# Wait for tunnel URL to appear in log
TUNNEL_URL=""
for i in $(seq 1 15); do
    sleep 1
    if TUNNEL_URL=$(grep -oE 'https://[a-z0-9-]+\.devtunnels\.ms' "${PIDDIR}/tunnel.log" 2>/dev/null | head -1) && [ -n "${TUNNEL_URL}" ]; then
        break
    fi
done

if [ -n "${TUNNEL_URL}" ]; then
    ok "Dev Tunnel ready: ${BOLD}${TUNNEL_URL}${NC}"
    echo ""
    echo -e "   ${YELLOW}→ Set this as the Messaging Endpoint in Azure Bot Service:${NC}"
    echo -e "   ${BOLD}${TUNNEL_URL}/api/messages${NC}"
    echo ""
else
    warn "Tunnel started (PID ${TUNNEL_PID}) but URL not detected yet."
    warn "Check ${PIDDIR}/tunnel.log for the URL."
fi

# ── Start Bot Server ─────────────────────────────────────────────────────
info "Starting bot server on localhost:${PORT}..."
export ENTRACLAW_MODE=bot
python -m entraclaw.bot.server > "${PIDDIR}/bot.log" 2>&1 &
BOT_PID=$!
echo "${BOT_PID}" > "${BOT_PIDFILE}"

sleep 1
if kill -0 "${BOT_PID}" 2>/dev/null; then
    ok "Bot server running (PID ${BOT_PID})"
else
    fail "Bot server exited immediately. Check ${PIDDIR}/bot.log"
fi

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Bot gateway is running!${NC}"
echo ""
echo -e "   Tunnel PID:  ${TUNNEL_PID} (log: ${PIDDIR}/tunnel.log)"
echo -e "   Bot PID:     ${BOT_PID} (log: ${PIDDIR}/bot.log)"
echo -e "   Inbound:     ${PIDDIR}/inbound.jsonl"
echo -e "   Outbound:    ${PIDDIR}/outbound.jsonl"
echo ""
echo -e "   ${CYAN}Now launch Claude Code:${NC}"
echo -e "   claude --dangerously-load-development-channels server:entraclaw"
echo ""
echo -e "   ${CYAN}To stop:${NC}"
echo -e "   ./scripts/start_bot.sh --stop"
