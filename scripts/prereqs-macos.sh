#!/usr/bin/env bash
# EntraClaw — macOS prerequisite installer.
#
# Checks for and installs (via Homebrew) everything needed to run setup.sh:
#   1. Homebrew (instructions only — installer needs sudo and EULA)
#   2. Xcode Command Line Tools (clang, git, headers for native Python pkgs)
#   3. Python 3.12+
#   4. Git (usually shipped with Xcode CLT; brew install as fallback)
#   5. Azure CLI
#   6. .NET SDK              [optional, --skip-a365 to opt out]
#   7. Microsoft Agent 365 DevTools CLI (a365)    [needs .NET SDK]
#   8. PowerShell 7+         [optional, --skip-pwsh to opt out]
#
# Safe to re-run — skips anything already installed. Run BEFORE setup.sh.
#
# Usage:
#   ./scripts/prereqs-macos.sh                # install everything
#   ./scripts/prereqs-macos.sh --skip-a365    # skip .NET SDK and a365 CLI
#   ./scripts/prereqs-macos.sh --skip-pwsh    # skip PowerShell 7
#   ./scripts/prereqs-macos.sh --core-only    # skip both a365 and pwsh
#
set -euo pipefail

SKIP_A365=false
SKIP_PWSH=false
SHOW_HELP=false

for arg in "$@"; do
    case $arg in
        --skip-a365) SKIP_A365=true ;;
        --skip-pwsh) SKIP_PWSH=true ;;
        --core-only) SKIP_A365=true; SKIP_PWSH=true ;;
        -h|--help) SHOW_HELP=true ;;
        *) echo "ERROR: Unknown argument: $arg" >&2; SHOW_HELP=true ;;
    esac
done

if [ "$SHOW_HELP" = true ]; then
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

# ── Colored output (matches setup.sh) ──────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'

step()    { echo -e "\n${CYAN}══ $1${NC}"; }
ok()      { echo -e "  ${GREEN}✓ $1${NC}"; }
skip()    { echo -e "  ${GRAY}○ $1${NC}"; }
do_install() { echo -e "  ${YELLOW}→ $1${NC}"; }
warn()    { echo -e "  ${YELLOW}⚠ $1${NC}"; }
err()     { echo -e "  ${RED}✗ $1${NC}"; }

INSTALLED=()
ALREADY=()
FAILED=()

is_macos() { [ "$(uname -s)" = "Darwin" ]; }

if ! is_macos; then
    err "This script only runs on macOS. For Linux, use your package manager directly; for Windows, see scripts/prereqs-windows.ps1."
    exit 1
fi

# ───────────────────────────────────────────────────────────────────────────
# 0. Homebrew
# ───────────────────────────────────────────────────────────────────────────
step "Homebrew (package manager)"

# Detect arch-appropriate brew prefix and add to PATH for this session.
BREW_PREFIX=""
if [ -x /opt/homebrew/bin/brew ]; then
    BREW_PREFIX="/opt/homebrew"
elif [ -x /usr/local/bin/brew ]; then
    BREW_PREFIX="/usr/local"
fi

if [ -n "$BREW_PREFIX" ] && [ -z "${HOMEBREW_PREFIX:-}" ]; then
    eval "$($BREW_PREFIX/bin/brew shellenv)"
fi

if ! command -v brew &>/dev/null; then
    err "Homebrew not found."
    echo ""
    echo "  Install it first (needs sudo + your password):"
    echo ""
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo ""
    echo "  Then re-run this script."
    exit 1
fi
ok "brew available ($(brew --version | head -n1))"

# ───────────────────────────────────────────────────────────────────────────
# 1. Xcode Command Line Tools (clang, git, headers for native Python deps)
# ───────────────────────────────────────────────────────────────────────────
step "Xcode Command Line Tools (clang + headers for cryptography/cffi)"

if xcode-select -p &>/dev/null; then
    ok "Xcode CLT already installed ($(xcode-select -p))"
    ALREADY+=("Xcode CLT")
else
    do_install "Triggering Xcode CLT install (a system dialog will appear)..."
    # `xcode-select --install` is interactive; it pops a GUI dialog.
    # The script can't complete the install for the user, so we instruct
    # and let them confirm before continuing.
    xcode-select --install || true
    echo ""
    warn "Accept the system dialog to install Xcode CLT, then press Enter to continue (or Ctrl-C to abort)."
    read -r _
    if xcode-select -p &>/dev/null; then
        ok "Xcode CLT installed"
        INSTALLED+=("Xcode CLT")
    else
        FAILED+=("Xcode CLT")
    fi
fi

# ───────────────────────────────────────────────────────────────────────────
# 2. Python 3.12+
# ───────────────────────────────────────────────────────────────────────────
step "Python 3.12+"

py_ver_ok() {
    local cmd="$1"
    command -v "$cmd" &>/dev/null || return 1
    "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" &>/dev/null
}

PY_FOUND=""
for candidate in python3.13 python3.12 python3; do
    if py_ver_ok "$candidate"; then
        PY_FOUND="$candidate"
        break
    fi
done

if [ -n "$PY_FOUND" ]; then
    PY_VER=$("$PY_FOUND" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
    ok "Python $PY_VER already installed ($PY_FOUND → $(command -v "$PY_FOUND"))"
    ALREADY+=("Python $PY_VER")
else
    do_install "Installing Python 3.12 via Homebrew..."
    if brew install python@3.12; then
        # brew links python@3.12 as `python3.12` only; setup.sh probes for that.
        if py_ver_ok python3.12; then
            ok "Python 3.12 installed"
            INSTALLED+=("Python 3.12")
        else
            warn "python@3.12 installed but python3.12 not on PATH. Open a new shell and re-run."
            FAILED+=("Python 3.12 (PATH)")
        fi
    else
        FAILED+=("Python 3.12")
    fi
fi

# ───────────────────────────────────────────────────────────────────────────
# 3. Git (almost always present via Xcode CLT; brew install as a safety net)
# ───────────────────────────────────────────────────────────────────────────
step "Git"

if command -v git &>/dev/null; then
    ok "Git already installed ($(git --version))"
    ALREADY+=("Git")
else
    do_install "Installing Git via Homebrew..."
    if brew install git; then
        INSTALLED+=("Git")
        ok "Git installed"
    else
        FAILED+=("Git")
    fi
fi

# ───────────────────────────────────────────────────────────────────────────
# 4. Azure CLI
# ───────────────────────────────────────────────────────────────────────────
step "Azure CLI"

if command -v az &>/dev/null; then
    AZ_VER=$(az version --query '"azure-cli"' -o tsv 2>/dev/null || echo "?")
    ok "Azure CLI $AZ_VER already installed"
    ALREADY+=("Azure CLI")
else
    do_install "Installing Azure CLI via Homebrew..."
    if brew install azure-cli; then
        INSTALLED+=("Azure CLI")
        ok "Azure CLI installed"
    else
        FAILED+=("Azure CLI")
    fi
fi

# ───────────────────────────────────────────────────────────────────────────
# 5. .NET SDK (optional — only needed for the Microsoft Agent 365 DevTools CLI)
# ───────────────────────────────────────────────────────────────────────────
if [ "$SKIP_A365" = true ]; then
    step ".NET SDK"
    skip "Skipped (--skip-a365 / --core-only). Pass --with-a365-work-iq to setup.sh and re-run prereqs to add it later."
else
    step ".NET SDK (for Microsoft Agent 365 DevTools CLI)"

    if command -v dotnet &>/dev/null; then
        ok ".NET SDK already installed ($(dotnet --version))"
        ALREADY+=(".NET SDK")
    else
        do_install "Installing .NET SDK via Homebrew formula..."
        # Use the `dotnet` formula (not the `dotnet-sdk` cask). The
        # `powershell` formula installed below depends on this same
        # formula; mixing the cask and formula causes a symlink conflict
        # on /opt/homebrew/bin/dotnet.
        if brew install dotnet; then
            INSTALLED+=(".NET SDK")
            ok ".NET SDK installed"
        else
            FAILED+=(".NET SDK")
        fi
    fi

    # ── 6. a365 CLI (depends on dotnet) ────────────────────────────────────
    step "Microsoft Agent 365 DevTools CLI (a365)"

    # The dotnet global tools dir is not on PATH by default on macOS.
    export PATH="$PATH:$HOME/.dotnet/tools"

    if ! command -v dotnet &>/dev/null; then
        err "dotnet not found; cannot install a365"
        FAILED+=("Agent 365 DevTools CLI")
    elif command -v a365 &>/dev/null; then
        ok "a365 already installed"
        ALREADY+=("Agent 365 DevTools CLI")
        do_install "Checking for a365 update..."
        if dotnet tool update --global Microsoft.Agents.A365.DevTools.Cli 2>/dev/null; then
            ok "a365 updated"
        else
            skip "a365 update skipped (already current or not managed by dotnet global tools)"
        fi
    else
        do_install "Installing a365 via 'dotnet tool install --global'..."
        if dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli; then
            export PATH="$PATH:$HOME/.dotnet/tools"
            if command -v a365 &>/dev/null; then
                INSTALLED+=("Agent 365 DevTools CLI")
                ok "a365 installed"
            else
                warn "a365 installed but ~/.dotnet/tools is not on PATH for this shell."
                warn "Add this to ~/.zshrc (or ~/.bash_profile) and open a new terminal:"
                warn "  export PATH=\"\$PATH:\$HOME/.dotnet/tools\""
                INSTALLED+=("Agent 365 DevTools CLI (needs PATH update)")
            fi
        else
            FAILED+=("Agent 365 DevTools CLI")
        fi
    fi
fi

# ───────────────────────────────────────────────────────────────────────────
# 7. PowerShell 7+ (optional — only needed for --configure-a365-work-iq)
# ───────────────────────────────────────────────────────────────────────────
if [ "$SKIP_PWSH" = true ]; then
    step "PowerShell 7+"
    skip "Skipped (--skip-pwsh / --core-only). Only needed for setup.sh --configure-a365-work-iq."
else
    step "PowerShell 7+ (for setup.sh --configure-a365-work-iq)"

    if command -v pwsh &>/dev/null; then
        PWSH_VER=$(pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' 2>/dev/null || echo "?")
        ok "PowerShell $PWSH_VER already installed"
        ALREADY+=("PowerShell 7")
    else
        do_install "Installing PowerShell via Homebrew formula..."
        # `powershell` is a formula in homebrew-core (depends on dotnet,
        # which we've already installed by this point). The older cask
        # of the same name was retired — `brew install --cask powershell`
        # now fails with "No Cask with this name exists".
        if brew install powershell; then
            INSTALLED+=("PowerShell 7")
            ok "PowerShell installed"
        else
            FAILED+=("PowerShell 7")
        fi
    fi
fi

# ───────────────────────────────────────────────────────────────────────────
# Final validation — re-probe everything setup.sh needs
# ───────────────────────────────────────────────────────────────────────────
step "Final validation"

export PATH="$PATH:$HOME/.dotnet/tools"

all_good=true
check() {
    local name="$1"; local cmd="$2"; local required="${3:-true}"
    if command -v "$cmd" &>/dev/null; then
        ok "$name found ($(command -v "$cmd"))"
    else
        if [ "$required" = "true" ]; then
            err "$name NOT FOUND — open a new terminal and re-check"
            all_good=false
        else
            skip "$name not installed (optional)"
        fi
    fi
}

check "Homebrew"   brew   true
check "git"        git    true
check "Python 3.12+" python3.12 true
check "Azure CLI" az     true
if [ "$SKIP_A365" = false ]; then
    check ".NET SDK" dotnet true
    check "a365 CLI" a365   true
fi
if [ "$SKIP_PWSH" = false ]; then
    check "pwsh"     pwsh   false
fi

# ───────────────────────────────────────────────────────────────────────────
# Summary
# ───────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  PREREQUISITE CHECK COMPLETE${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"

if [ ${#ALREADY[@]} -gt 0 ]; then
    echo -e "\n  ${GREEN}Already installed:${NC}"
    for i in "${ALREADY[@]}"; do echo -e "    ${GREEN}• $i${NC}"; done
fi
if [ ${#INSTALLED[@]} -gt 0 ]; then
    echo -e "\n  ${YELLOW}Newly installed:${NC}"
    for i in "${INSTALLED[@]}"; do echo -e "    ${YELLOW}• $i${NC}"; done
fi
if [ ${#FAILED[@]} -gt 0 ]; then
    echo -e "\n  ${RED}FAILED to install:${NC}"
    for i in "${FAILED[@]}"; do echo -e "    ${RED}• $i${NC}"; done
fi

echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
    err "Some prerequisites failed to install. Fix them manually and re-run."
    exit 1
elif [ "$all_good" = false ]; then
    warn "Installs succeeded but some tools aren't on PATH yet."
    warn "Open a NEW terminal, then run:"
    warn "  ./scripts/setup.sh --new --with-upn-suffix=<yourname>"
    exit 0
else
    ok "All prerequisites ready!"
    echo ""
    echo "  Next step:"
    echo "    ./scripts/setup.sh --new --with-upn-suffix=<yourname>"
    echo ""
    exit 0
fi
