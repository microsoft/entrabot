#!/usr/bin/env bash
# Setup MXC sandbox for entrabot
#
# Detects or builds the Microsoft Execution Containers (MXC) binary,
# self-signs it, records the SHA256 hash, and configures .env.
#
# Usage:
#   ./scripts/setup_sandbox.sh [--force-build] [--skip-sign]
#
# This script is:
# - Idempotent: safe to run multiple times
# - Non-fatal: failures degrade to unavailable sandbox, not setup failure
# - Platform-aware: macOS (Seatbelt), Windows (processcontainer), Linux (future)
#
# Exit codes:
#   0 - Success (binary ready)
#   1 - Failed (sandbox will be unavailable at runtime)
#   2 - Skipped (--help or platform unsupported)

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────

# MXC source repository
MXC_REPO="https://github.com/microsoft/mxc.git"
MXC_VERSION_TAG="v0.6.1"
MXC_PINNED_COMMIT="161598fd08a4fdd030f461de19af23ce4a310b41"
MXC_SCHEMA_VERSION="0.6.0-alpha"

# Binary names per platform
case "$(uname -s)" in
    Darwin)
        PLATFORM="macos"
        BINARY_NAME="mxc-exec-mac"
        ;;
    Linux)
        PLATFORM="linux"
        BINARY_NAME="lxc-exec"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        PLATFORM="windows"
        BINARY_NAME="wxc-exec.exe"
        ;;
    *)
        echo "❌ Unsupported platform: $(uname -s)"
        echo "MXC sandbox requires macOS, Linux, or Windows"
        exit 2
        ;;
esac

# Directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/.mxc-build"
MXC_SOURCE_DIR="$BUILD_DIR/mxc-src"
MXC_PATCH_FILE="$PROJECT_ROOT/scripts/mxc-mac-stdin-compat.patch"
BINARY_HASHES_FILE="$PROJECT_ROOT/src/entrabot/sandbox/binary.py"
ENV_FILE="$PROJECT_ROOT/.env"

# Flags
FORCE_BUILD=false
SKIP_SIGN=false
SHOW_HELP=false

# ── Argument parsing ───────────────────────────────────────────────────────

for arg in "$@"; do
    case $arg in
        --force-build)
            FORCE_BUILD=true
            ;;
        --skip-sign)
            SKIP_SIGN=true
            ;;
        --help|-h)
            SHOW_HELP=true
            ;;
        *)
            echo "❌ Unknown argument: $arg"
            echo "Usage: $0 [--force-build] [--skip-sign] [--help]"
            exit 2
            ;;
    esac
done

if [ "$SHOW_HELP" = true ]; then
    cat <<EOF
Setup MXC sandbox for entrabot

Usage: $0 [OPTIONS]

Options:
  --force-build    Force rebuild even if binary exists
  --skip-sign      Skip code signing (for CI/testing)
  --help, -h       Show this help

The script will:
1. Check for existing MXC binary in \$MXC_BIN_DIR or npm global
2. If not found, build from source (requires Rust 1.93+)
3. Self-sign the binary (codesign on macOS)
4. Record SHA256 hash in src/entrabot/sandbox/binary.py
5. Add ENTRABOT_ENABLE_RUN_CODE=1 to .env

Platform support:
  ✅ macOS (Seatbelt) - ready
  🚧 Linux (bubblewrap/lxc) - future
  🚧 Windows (processcontainer) - future

Exit codes:
  0 - Success (binary ready)
  1 - Failed (sandbox unavailable at runtime)
  2 - Skipped (unsupported platform or --help)

EOF
    exit 0
fi

# ── Helper functions ───────────────────────────────────────────────────────

info() {
    echo "ℹ️  $*"
}

success() {
    echo "✅ $*"
}

warn() {
    echo "⚠️  $*"
}

error() {
    echo "❌ $*" >&2
}

# ── Step 1: Check for existing binary ──────────────────────────────────────

info "Step 1/5: Checking for existing MXC binary..."

BINARY_PATH=""

# Check MXC_BIN_DIR first
if [ -n "${MXC_BIN_DIR:-}" ] && [ -f "$MXC_BIN_DIR/$BINARY_NAME" ]; then
    BINARY_PATH="$MXC_BIN_DIR/$BINARY_NAME"
    info "Found binary in MXC_BIN_DIR: $BINARY_PATH"
# Check npm global next
elif command -v "$BINARY_NAME" &> /dev/null; then
    BINARY_PATH="$(command -v "$BINARY_NAME")"
    info "Found binary in PATH: $BINARY_PATH"
# Check build directory
elif [ -f "$BUILD_DIR/target/release/$BINARY_NAME" ]; then
    BINARY_PATH="$BUILD_DIR/target/release/$BINARY_NAME"
    info "Found binary in build directory: $BINARY_PATH"
fi

# If found and not forcing rebuild, skip to signing
if [ -n "$BINARY_PATH" ] && [ "$FORCE_BUILD" = false ]; then
    success "Binary exists: $BINARY_PATH"
else
    BINARY_PATH=""
fi

# ── Step 2: Build from source if needed ────────────────────────────────────

if [ -z "$BINARY_PATH" ]; then
    info "Step 2/5: Building MXC from source..."

    mkdir -p "$BUILD_DIR/target/release"

    if [ "$PLATFORM" != "macos" ]; then
        error "Source build is only implemented for macOS right now"
        exit 1
    fi

    if ! command -v cargo &> /dev/null; then
        error "cargo not found. Install Rust via https://rustup.rs/ (toolchain 1.93+)"
        exit 1
    fi

    if ! xcode-select -p &> /dev/null; then
        error "Xcode Command Line Tools not installed. Run: xcode-select --install"
        exit 1
    fi

    if [ ! -d "$MXC_SOURCE_DIR/.git" ]; then
        info "Cloning $MXC_REPO into $MXC_SOURCE_DIR"
        git clone --depth 1 --branch "$MXC_VERSION_TAG" "$MXC_REPO" "$MXC_SOURCE_DIR"
    fi

    info "Checking out pinned MXC commit $MXC_PINNED_COMMIT"
    git -C "$MXC_SOURCE_DIR" fetch --depth 1 origin "$MXC_PINNED_COMMIT"
    git -C "$MXC_SOURCE_DIR" checkout --force "$MXC_PINNED_COMMIT"

    if [ -f "$MXC_PATCH_FILE" ]; then
        if grep -q "pipe JSON via stdin" "$MXC_SOURCE_DIR/src/core/mxc_darwin/src/main.rs"; then
            info "MXC stdin compatibility patch already applied"
        else
            info "Applying MXC stdin compatibility patch"
            git -C "$MXC_SOURCE_DIR" apply "$MXC_PATCH_FILE"
        fi
    fi

    case "$(uname -m)" in
        arm64)
            MXC_TARGET_TRIPLE="aarch64-apple-darwin"
            ;;
        x86_64)
            MXC_TARGET_TRIPLE="x86_64-apple-darwin"
            ;;
        *)
            error "Unsupported macOS architecture: $(uname -m)"
            exit 1
            ;;
    esac

    info "Building mxc-exec-mac from source"
    (
        cd "$MXC_SOURCE_DIR"
        ./build-mac.sh --rust-only
    )

    cp \
        "$MXC_SOURCE_DIR/src/target/$MXC_TARGET_TRIPLE/release/$BINARY_NAME" \
        "$BUILD_DIR/target/release/$BINARY_NAME"
    chmod +x "$BUILD_DIR/target/release/$BINARY_NAME"

    BINARY_PATH="$BUILD_DIR/target/release/$BINARY_NAME"
    success "Built MXC binary: $BINARY_PATH"
else
    info "Step 2/5: Skipped (binary exists)"
fi

# ── Step 3: Self-sign binary (macOS) ───────────────────────────────────────

if [ "$SKIP_SIGN" = false ]; then
    info "Step 3/5: Code signing binary..."
    
    case "$PLATFORM" in
        macos)
            if command -v codesign &> /dev/null; then
                # Self-sign with ad-hoc signature (codesign -s -)
                # This is sufficient for local development
                # Production distribution would need Apple Developer ID
                if codesign -s - -f "$BINARY_PATH" 2>/dev/null; then
                    success "Signed binary with ad-hoc signature"
                else
                    warn "Code signing failed (non-fatal)"
                    warn "Binary may require explicit security approval on first run"
                fi
            else
                warn "codesign not found, skipping signature"
            fi
            ;;
        linux)
            info "Linux: No code signing required"
            ;;
        windows)
            warn "Windows: Code signing not yet implemented"
            warn "Binary may require SmartScreen approval on first run"
            ;;
    esac
else
    info "Step 3/5: Skipped (--skip-sign)"
fi

# ── Step 4: Record SHA256 hash ─────────────────────────────────────────────

info "Step 4/5: Recording SHA256 hash..."

# Compute SHA256
if command -v shasum &> /dev/null; then
    HASH=$(shasum -a 256 "$BINARY_PATH" | awk '{print $1}')
elif command -v sha256sum &> /dev/null; then
    HASH=$(sha256sum "$BINARY_PATH" | awk '{print $1}')
else
    error "Neither shasum nor sha256sum found"
    exit 1
fi

info "SHA256: $HASH"

# Update PINNED_HASHES in binary.py
if [ -f "$BINARY_HASHES_FILE" ]; then
    # Map platform to dict key
    case "$PLATFORM" in
        macos)
            DICT_KEY="darwin-arm64"  # or darwin-x86_64 based on arch
            if [ "$(uname -m)" = "arm64" ]; then
                DICT_KEY="darwin-arm64"
            else
                DICT_KEY="darwin-x86_64"
            fi
            ;;
        linux)
            DICT_KEY="linux-x86_64"
            ;;
        windows)
            DICT_KEY="win32-x86_64"
            ;;
    esac
    
    # Use Python to update the hash dictionary
    python3 <<PYTHON_UPDATE
import re

with open("$BINARY_HASHES_FILE", "r") as f:
    content = f.read()

# Update the hash for this platform
pattern = r'("$DICT_KEY":\s*)"[0-9a-f]{64}"'
replacement = r'\1"$HASH"'
content = re.sub(pattern, replacement, content)

with open("$BINARY_HASHES_FILE", "w") as f:
    f.write(content)

print(f"Updated hash for $DICT_KEY: $HASH")
PYTHON_UPDATE
    success "Updated SHA256 hash in $BINARY_HASHES_FILE"
else
    error "File not found: $BINARY_HASHES_FILE"
    exit 1
fi

# ── Step 5: Configure .env ─────────────────────────────────────────────────

info "Step 5/5: Configuring .env..."

# Create .env if it doesn't exist
if [ ! -f "$ENV_FILE" ]; then
    touch "$ENV_FILE"
fi

# Add or update sandbox config
update_env_var() {
    local key=$1
    local value=$2
    
    if grep -q "^${key}=" "$ENV_FILE"; then
        # Update existing
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
        else
            sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
        fi
    else
        # Append new
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

# Enable run_code tool
update_env_var "ENTRABOT_ENABLE_RUN_CODE" "1"

# Set binary directory if in build dir
if [[ "$BINARY_PATH" == "$BUILD_DIR"* ]]; then
    update_env_var "MXC_BIN_DIR" "$BUILD_DIR/target/release"
fi

# Set default operator ceiling (restrictive by default)
if ! grep -q "^ENTRABOT_SANDBOX_READONLY_PATHS=" "$ENV_FILE"; then
    update_env_var "ENTRABOT_SANDBOX_READONLY_PATHS" "/tmp"
fi
if ! grep -q "^ENTRABOT_SANDBOX_READWRITE_PATHS=" "$ENV_FILE"; then
    update_env_var "ENTRABOT_SANDBOX_READWRITE_PATHS" "/tmp"
fi
if ! grep -q "^ENTRABOT_SANDBOX_TIMEOUT_MS=" "$ENV_FILE"; then
    update_env_var "ENTRABOT_SANDBOX_TIMEOUT_MS" "30000"
fi
if ! grep -q "^ENTRABOT_SANDBOX_NETWORK=" "$ENV_FILE"; then
    update_env_var "ENTRABOT_SANDBOX_NETWORK" "block"
fi

success "Updated .env configuration"

# ── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ MXC Sandbox Setup Complete"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Binary:     $BINARY_PATH"
echo "SHA256:     $HASH"
echo "Platform:   $PLATFORM"
echo "Status:     $([ -x "$BINARY_PATH" ] && echo "✅ Executable" || echo "❌ Not executable")"
echo ""
echo "Environment configuration (.env):"
echo "  ENTRABOT_ENABLE_RUN_CODE=1"
echo "  ENTRABOT_SANDBOX_READONLY_PATHS=/tmp"
echo "  ENTRABOT_SANDBOX_READWRITE_PATHS=/tmp"
echo "  ENTRABOT_SANDBOX_TIMEOUT_MS=30000"
echo "  ENTRABOT_SANDBOX_NETWORK=block"
echo ""
echo "To test:"
echo "  1. Start EntraBot MCP: claude server:entrabot"
echo "  2. From Claude Code: run_code with argv=[\"echo\", \"hello\"]"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

exit 0
