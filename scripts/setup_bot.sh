#!/usr/bin/env bash
# setup_bot.sh — One-time Azure Bot provisioning for EntraClaw bot mode
#
# Pattern: matches setup.sh — idempotent, state-persisted, certificate auth (ADR-003).
#
# What it does:
#   1. Verifies prerequisites (az CLI, Python 3.12+, devtunnel CLI)
#   2. Verifies Azure login
#   3. Creates multi-tenant app registration for the bot (or reuses existing)
#   4. Generates a certificate and stores private key in OS keystore
#   5. Uploads public certificate to the app registration
#   6. Creates Azure Bot resource linked to the app
#   7. Writes bot config to .env and .entraclaw-state.json
#
# State is persisted in .entraclaw-state.json so re-runs are idempotent.
# Certificate private key is stored in OS keystore (Keychain/TPM/Keyring).
#
# Usage:
#   ./scripts/setup_bot.sh                  # provision everything
#   ./scripts/setup_bot.sh --teardown       # delete Azure resources
#
# After setup:
#   ./scripts/start_bot.sh                  # launch tunnel + bot server

set -euo pipefail

TOTAL_STEPS=8

# ── Argument parsing ───────────────────────────────────────────────────────

SHOW_HELP=false
TEARDOWN=false

for arg in "$@"; do
    case $arg in
        --teardown)
            TEARDOWN=true
            ;;
        --help|-h)
            SHOW_HELP=true
            ;;
        *)
            echo "ERROR: Unknown argument: $arg" >&2
            SHOW_HELP=true
            ;;
    esac
done

if [ "$SHOW_HELP" = true ]; then
    echo "Usage: ./scripts/setup_bot.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --teardown     Delete the Azure Bot resource and app registration."
    echo "  --help, -h     Show this help"
    exit 0
fi

# ── Colored output helpers ──────────────────────────────────────────────────

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

step()    { echo -e "\n${BLUE}[$1/$TOTAL_STEPS]${NC} $2"; }
success() { echo -e "  ${GREEN}✅ $1${NC}"; }
warn()    { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
fail()    { echo -e "  ${RED}❌ $1${NC}"; exit 1; }

# ── Resolve project root ────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

BOT_DISPLAY_NAME="EntraClaw Bot"
BOT_RG_NAME="entraclaw-bot-rg"
BOT_RESOURCE_NAME="entraclaw-bot"
BOT_RG_LOCATION="westus2"

# ── State helpers (shared pattern with setup.sh) ─────────────────────────

read_state() {
    local key="$1"
    python3 -c "
import json, pathlib, sys
state_file = pathlib.Path('$PROJECT_ROOT/.entraclaw-state.json')
if not state_file.is_file():
    sys.exit(0)
data = json.loads(state_file.read_text())
val = data.get('$key', '')
if val:
    print(val)
" || echo ""
}

write_state() {
    local key="$1"
    local value="$2"
    python3 -c "
import json, pathlib
state_file = pathlib.Path('$PROJECT_ROOT/.entraclaw-state.json')
data = json.loads(state_file.read_text()) if state_file.is_file() else {}
data['$key'] = '$value'
state_file.write_text(json.dumps(data, indent=2) + '\n')
"
}

clear_state() {
    local key="$1"
    python3 -c "
import json, pathlib
state_file = pathlib.Path('$PROJECT_ROOT/.entraclaw-state.json')
if not state_file.is_file():
    return
data = json.loads(state_file.read_text())
data.pop('$key', None)
state_file.write_text(json.dumps(data, indent=2) + '\n')
" 2>/dev/null || true
}

# ── Teardown ─────────────────────────────────────────────────────────────

if [ "$TEARDOWN" = true ]; then
    echo -e "${YELLOW}Tearing down Azure Bot resources...${NC}"

    BOT_APP_ID=$(read_state "BOT_APP_ID")
    if [ -n "$BOT_APP_ID" ]; then
        echo "  Deleting app registration: $BOT_APP_ID"
        az ad app delete --id "$BOT_APP_ID" 2>/dev/null || true
        clear_state "BOT_APP_ID"
        clear_state "BOT_CERT_THUMBPRINT"
    fi

    if az group show --name "$BOT_RG_NAME" &>/dev/null; then
        echo "  Deleting resource group: $BOT_RG_NAME"
        az group delete --name "$BOT_RG_NAME" --yes --no-wait 2>/dev/null || true
    fi

    # Clean up keychain
    if [ -f .venv/bin/activate ]; then
        source .venv/bin/activate
    fi
    python3 -c "
import keyring
try:
    keyring.delete_password('entraclaw-bot', 'private-key')
    keyring.delete_password('entraclaw-bot', 'certificate')
except Exception:
    pass
" 2>/dev/null || true

    echo -e "${GREEN}✅ Teardown complete${NC}"
    exit 0
fi

# ════════════════════════════════════════════════════════════════════════════

echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   EntraClaw Bot Gateway — One-Time Setup                    ║${NC}"
echo -e "${GREEN}║   (Azure Bot + single-tenant app + certificate auth)       ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"

# ════════════════════════════════════════════════════════════════════════════
# Step 1: Verify prerequisites
# ════════════════════════════════════════════════════════════════════════════
step 1 "Verifying prerequisites"

MISSING=()

if ! command -v az &>/dev/null; then
    MISSING+=("az (Azure CLI — https://aka.ms/install-az)")
fi

PYTHON=""
for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" &>/dev/null; then
        PY_VER=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        if [ "$(echo "$PY_VER >= 3.12" | bc || python3 -c "print(int($PY_VER >= 3.12))")" = "1" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    MISSING+=("python3.12+ (https://www.python.org/downloads/)")
fi

if ! command -v devtunnel &>/dev/null; then
    MISSING+=("devtunnel CLI (https://learn.microsoft.com/azure/developer/dev-tunnels/get-started)")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    for m in "${MISSING[@]}"; do
        echo -e "  ${RED}✗ $m${NC}"
    done
    fail "Install the missing prerequisites above and re-run."
fi

success "az CLI found ($(az version --query '"azure-cli"' -o tsv 2>/dev/null || echo '?'))"
success "$PYTHON found ($PY_VER)"
success "devtunnel CLI found"

# ════════════════════════════════════════════════════════════════════════════
# Step 2: Verify Azure login
# ════════════════════════════════════════════════════════════════════════════
step 2 "Verifying Azure login"

if ! az account show &>/dev/null; then
    fail "Not logged in to Azure CLI. Run 'az login' first."
fi

TENANT_ID=$(az account show --query "tenantId" -o tsv)
ACCOUNT_NAME=$(az account show --query "name" -o tsv)
SUBSCRIPTION_ID=$(az account show --query "id" -o tsv)
USER_NAME=$(az account show --query "user.name" -o tsv)

success "Signed in as: $USER_NAME"
success "Tenant:       $TENANT_ID"
success "Subscription: $ACCOUNT_NAME ($SUBSCRIPTION_ID)"

# ════════════════════════════════════════════════════════════════════════════
# Step 3: Create or find multi-tenant app registration
# ════════════════════════════════════════════════════════════════════════════
step 3 "Ensuring app registration"

BOT_APP_ID=$(read_state "BOT_APP_ID")

# Validate cached app still exists
if [ -n "$BOT_APP_ID" ]; then
    if az ad app show --id "$BOT_APP_ID" &>/dev/null; then
        success "Using cached bot app: $BOT_APP_ID"
    else
        warn "Cached bot app is stale: $BOT_APP_ID"
        BOT_APP_ID=""
        clear_state "BOT_APP_ID"
        clear_state "BOT_CERT_THUMBPRINT"
    fi
fi

# Search for existing app by display name
if [ -z "$BOT_APP_ID" ]; then
    EXISTING=$(az ad app list --display-name "$BOT_DISPLAY_NAME" --query "[0].appId" -o tsv 2>/dev/null || echo "")
    if [ -n "$EXISTING" ]; then
        BOT_APP_ID="$EXISTING"
        success "Found existing bot app: $BOT_APP_ID"
        write_state "BOT_APP_ID" "$BOT_APP_ID"
    fi
fi

# Create new app registration
if [ -z "$BOT_APP_ID" ]; then
    echo "  Creating single-tenant app registration: $BOT_DISPLAY_NAME"
    BOT_APP_ID=$(az ad app create \
        --display-name "$BOT_DISPLAY_NAME" \
        --sign-in-audience "AzureADMyOrg" \
        --query "appId" -o tsv 2>&1) || fail "App registration creation failed: $BOT_APP_ID"

    write_state "BOT_APP_ID" "$BOT_APP_ID"
    success "Created bot app: $BOT_APP_ID"

    # Create service principal
    echo "  Creating service principal..."
    az ad sp create --id "$BOT_APP_ID" --query "id" -o tsv &>/dev/null \
        || warn "Service principal may already exist"
    success "Service principal created"

    # Wait for Entra propagation
    echo "  Waiting for Entra propagation (10s)..."
    sleep 10
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 4: Generate certificate and store in OS keystore
# ════════════════════════════════════════════════════════════════════════════
step 4 "Managing bot certificate (ADR-003: no client secrets)"

# Activate venv for cryptography + keyring
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

BOT_CERT_THUMBPRINT=$(read_state "BOT_CERT_THUMBPRINT")

if [ -n "$BOT_CERT_THUMBPRINT" ]; then
    # Verify the private key is still in the keystore
    KEY_EXISTS=$("$PYTHON" -c "
import keyring
key = keyring.get_password('entraclaw-bot', 'private-key')
print('yes' if key else 'no')
" 2>/dev/null || echo "no")

    if [ "$KEY_EXISTS" = "yes" ]; then
        success "Using cached certificate (thumbprint: ${BOT_CERT_THUMBPRINT:0:20}...)"
    else
        warn "Certificate thumbprint cached but private key missing from keystore — regenerating"
        BOT_CERT_THUMBPRINT=""
        clear_state "BOT_CERT_THUMBPRINT"
    fi
fi

if [ -z "$BOT_CERT_THUMBPRINT" ]; then
    echo "  Generating self-signed certificate for bot..."

    BOT_CERT_THUMBPRINT=$("$PYTHON" -c "
import sys, json, hashlib, base64
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from datetime import datetime, timedelta, timezone
import keyring

# Generate RSA 2048 key + self-signed cert
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, 'entraclaw-bot-$BOT_APP_ID'),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'EntraClaw Bot Gateway'),
])
cert = (x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.now(timezone.utc))
    .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
    .sign(key, hashes.SHA256()))

# Compute thumbprint (SHA-256 of DER, base64url no padding) — matches setup.sh pattern
der_bytes = cert.public_bytes(serialization.Encoding.DER)
thumbprint = base64.urlsafe_b64encode(hashlib.sha256(der_bytes).digest()).rstrip(b'=').decode()

# Store private key in OS credential store (Keychain on macOS)
pem_key = key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
keyring.set_password('entraclaw-bot', 'private-key', pem_key)

# Store certificate for reference
pem_cert = cert.public_bytes(serialization.Encoding.PEM).decode()
keyring.set_password('entraclaw-bot', 'certificate', pem_cert)

# Write cert to known path for az CLI upload
with open('/tmp/entraclaw-bot-cert.pem', 'w') as f:
    f.write(pem_cert)

print(thumbprint)
" 2>&1) || fail "Certificate generation failed: $BOT_CERT_THUMBPRINT"

    if [ -z "$BOT_CERT_THUMBPRINT" ]; then
        fail "Certificate generation returned empty thumbprint"
    fi

    # Upload certificate to app registration
    echo "  Uploading public certificate to Entra app registration..."
    CERT_FILE="/tmp/entraclaw-bot-cert.pem"
    if [ ! -f "$CERT_FILE" ]; then
        fail "Certificate file not found at $CERT_FILE"
    fi
    # Read PEM content (strip BEGIN/END lines) for az CLI --cert argument
    CERT_CONTENT=$(grep -v '^-----' "$CERT_FILE" | tr -d '\n')
    az ad app credential reset \
        --id "$BOT_APP_ID" \
        --cert "$CERT_CONTENT" \
        --append \
        -o none 2>&1 || fail "Certificate upload failed"

    # Clean up temp cert file
    rm -f "$CERT_FILE"

    write_state "BOT_CERT_THUMBPRINT" "$BOT_CERT_THUMBPRINT"
    success "Certificate generated, uploaded to Entra, private key in OS keystore"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 5: Create Azure Bot resource
# ════════════════════════════════════════════════════════════════════════════
step 5 "Ensuring Azure Bot resource"

# Register the Bot Service resource provider if needed
az provider register --namespace Microsoft.BotService --wait &>/dev/null || true

# Create resource group if needed
if ! az group show --name "$BOT_RG_NAME" &>/dev/null; then
    echo "  Creating resource group: $BOT_RG_NAME ($BOT_RG_LOCATION)"
    az group create \
        --name "$BOT_RG_NAME" \
        --location "$BOT_RG_LOCATION" \
        -o none 2>&1 || fail "Resource group creation failed"
    success "Resource group created"
else
    success "Resource group already exists: $BOT_RG_NAME"
fi

# Create Azure Bot if needed
BOT_EXISTS=$(az bot show --resource-group "$BOT_RG_NAME" --name "$BOT_RESOURCE_NAME" --query "name" -o tsv 2>/dev/null || echo "")
if [ -z "$BOT_EXISTS" ]; then
    echo "  Creating Azure Bot resource: $BOT_RESOURCE_NAME"
    az bot create \
        --resource-group "$BOT_RG_NAME" \
        --name "$BOT_RESOURCE_NAME" \
        --app-type "SingleTenant" \
        --appid "$BOT_APP_ID" \
        --tenant-id "$TENANT_ID" \
        -o none 2>&1 || fail "Azure Bot creation failed"
    success "Azure Bot resource created"
else
    success "Azure Bot resource already exists: $BOT_RESOURCE_NAME"
fi

# Enable Teams channel
echo "  Ensuring Teams channel is enabled..."
az bot msteams create \
    --resource-group "$BOT_RG_NAME" \
    --name "$BOT_RESOURCE_NAME" \
    -o none 2>&1 || warn "Teams channel may already be enabled (or requires manual setup)"
success "Teams channel configured"

# ════════════════════════════════════════════════════════════════════════════
# Step 6: Update .env with bot configuration
# ════════════════════════════════════════════════════════════════════════════
step 6 "Updating .env with bot configuration"

# Append bot config to .env (preserve existing entries)
BOT_ENV_KEYS="ENTRACLAW_BOT_APP_ID ENTRACLAW_BOT_CERT_THUMBPRINT ENTRACLAW_BOT_TUNNEL_PORT ENTRACLAW_MODE"

if [ -f .env ]; then
    # Remove existing bot entries to avoid duplicates
    for key in $BOT_ENV_KEYS; do
        sed -i '' "/^${key}=/d" .env 2>/dev/null || true
    done
    # Remove old bot comment block if present
    sed -i '' '/^# Bot mode/d' .env 2>/dev/null || true
else
    echo "# EntraClaw Identity Research — generated by scripts/setup.sh" > .env
    echo "# DO NOT commit this file (it is in .gitignore)" >> .env
    echo "" >> .env
fi

cat >> .env << EOF

# Bot mode (certificate auth per ADR-003 — no client secrets)
ENTRACLAW_BOT_APP_ID=$BOT_APP_ID
ENTRACLAW_BOT_CERT_THUMBPRINT=$BOT_CERT_THUMBPRINT
ENTRACLAW_BOT_TUNNEL_PORT=3978
EOF

chmod 600 .env
success ".env updated with bot configuration"

# ════════════════════════════════════════════════════════════════════════════
# Step 7: Build Teams app package for sideloading
# ════════════════════════════════════════════════════════════════════════════
step 7 "Building Teams app package"

MANIFEST_DIR="$PROJECT_ROOT/manifests/teams-app"
PACKAGE_PATH="$PROJECT_ROOT/manifests/entraclaw-bot.zip"

if [ ! -f "$MANIFEST_DIR/manifest.json" ]; then
    warn "No manifest.json found at $MANIFEST_DIR — skipping package build"
else
    # Update the bot ID in the manifest to match the actual app ID
    "$PYTHON" -c "
import json, pathlib
mf = pathlib.Path('$MANIFEST_DIR/manifest.json')
data = json.loads(mf.read_text())
data['id'] = '$BOT_APP_ID'
data['bots'][0]['botId'] = '$BOT_APP_ID'
mf.write_text(json.dumps(data, indent=2) + '\n')
print('  Updated manifest bot ID to $BOT_APP_ID')
"

    # Zip the manifest package
    (cd "$MANIFEST_DIR" && zip -q -j "$PACKAGE_PATH" manifest.json color.png outline.png)
    success "Teams app package: $PACKAGE_PATH"

    echo ""
    echo -e "  ${YELLOW}To sideload the bot in Teams:${NC}"
    echo -e "  1. Open Teams → Apps → Manage your apps → Upload a custom app"
    echo -e "  2. Select: ${BLUE}$PACKAGE_PATH${NC}"
    echo -e "  3. Click 'Add' to install in personal scope"
    echo -e "  4. Guests in your tenant will see the bot after switching to your org"
fi

# ════════════════════════════════════════════════════════════════════════════
# Step 8: Summary
# ════════════════════════════════════════════════════════════════════════════
step 8 "Setup complete — summary"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Bot Gateway setup complete!                                ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Bot App ID:    ${BLUE}$BOT_APP_ID${NC}"
echo -e "  Cert Thumbprint: ${BLUE}${BOT_CERT_THUMBPRINT:0:20}...${NC}"
echo -e "  Auth:          ${BLUE}Certificate (private key in OS keyring, no secrets on disk)${NC}"
echo -e "  Bot Resource:  ${BLUE}$BOT_RESOURCE_NAME${NC} (in ${BOT_RG_NAME})"
echo -e "  Teams Channel: ${BLUE}Enabled${NC}"
echo ""
echo -e "  ${YELLOW}NEXT STEPS:${NC}"
echo -e "  1. Run the bot:  ${BLUE}./scripts/start_bot.sh${NC}"
echo -e "     (This starts the Dev Tunnel + bot server. The tunnel URL will be"
echo -e "      auto-configured as the messaging endpoint.)"
echo -e "  2. Sideload the Teams app manifest (if not already done)"
echo -e "  3. Launch Claude Code:  ${BLUE}claude --dangerously-load-development-channels server:entraclaw${NC}"
echo ""
echo -e "  ${YELLOW}TEARDOWN:${NC}"
echo -e "  ./scripts/setup_bot.sh --teardown"
echo ""
