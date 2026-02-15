#!/usr/bin/env bash
# quarry-mcp installer
# Usage: curl -fsSL https://raw.githubusercontent.com/jmf-pobox/quarry-mcp/main/install.sh | bash
#
# What this does:
#   1. Checks Python 3.10+ is available
#   2. Installs uv if not present (official installer)
#   3. Installs quarry-mcp via uv
#   4. Runs quarry install (downloads model, configures MCP)
#   5. Runs quarry doctor (verifies everything)

set -euo pipefail

# --- Helpers ----------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

info()  { printf "${BOLD}==>${NC} %s\n" "$1"; }
ok()    { printf "  ${GREEN}✓${NC} %s\n" "$1"; }
warn()  { printf "  ${YELLOW}○${NC} %s\n" "$1"; }
fail()  { printf "  ${RED}✗${NC} %s\n" "$1"; exit 1; }

# --- Step 1: Python ---------------------------------------------------------

info "Checking Python..."

if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    fail "Python not found. Install Python 3.10+ from https://python.org"
fi

PY_VERSION=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$($PYTHON -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PYTHON -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    fail "Python $PY_VERSION found, but 3.10+ is required"
fi

ok "Python $PY_VERSION ($($PYTHON --version 2>&1))"

# --- Step 2: uv -------------------------------------------------------------

info "Checking uv..."

if command -v uv &>/dev/null; then
    ok "uv $(uv --version 2>&1 | head -1) already installed"
else
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the env so uv is on PATH for this session
    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck source=/dev/null
        . "$HOME/.local/bin/env"
    elif [ -f "$HOME/.cargo/env" ]; then
        # shellcheck source=/dev/null
        . "$HOME/.cargo/env"
    fi
    if command -v uv &>/dev/null; then
        ok "uv installed"
    else
        export PATH="$HOME/.local/bin:$PATH"
        if command -v uv &>/dev/null; then
            ok "uv installed (added ~/.local/bin to PATH)"
        else
            fail "uv install succeeded but 'uv' not found on PATH. Restart your shell and re-run."
        fi
    fi
fi

# --- Step 3: quarry-mcp -----------------------------------------------------

info "Installing quarry-mcp..."

INSTALL_OUTPUT=$(uv tool install quarry-mcp 2>&1) || true
if echo "$INSTALL_OUTPUT" | grep -q "already installed"; then
    uv tool upgrade quarry-mcp || fail "Failed to upgrade quarry-mcp"
    ok "quarry-mcp upgraded"
elif echo "$INSTALL_OUTPUT" | grep -q "Installed"; then
    ok "quarry-mcp installed"
else
    echo "$INSTALL_OUTPUT"
    fail "Failed to install quarry-mcp"
fi

# Verify quarry is on PATH
if ! command -v quarry &>/dev/null; then
    # uv tool installs to ~/.local/bin by default
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v quarry &>/dev/null; then
        fail "quarry-mcp installed but 'quarry' not found on PATH. Run: export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
fi

ok "quarry $(quarry --version 2>&1 || echo 'installed')"

# --- Step 4: quarry install --------------------------------------------------

info "Setting up quarry (downloading model, configuring MCP)..."
echo ""
quarry install
echo ""

# --- Step 5: quarry doctor ---------------------------------------------------

info "Final verification..."
echo ""
quarry doctor
echo ""

# --- Done --------------------------------------------------------------------

printf "\n${GREEN}${BOLD}quarry-mcp is ready!${NC}\n"
echo ""
echo "Quick start:"
echo "  quarry ingest-file notes.md      # index a file"
echo "  quarry search \"my topic\"          # semantic search"
echo "  quarry ingest-url https://...     # index a webpage"
echo ""
echo "Claude Code and Claude Desktop are configured automatically."
echo "Restart Claude Desktop if it was running during install."
