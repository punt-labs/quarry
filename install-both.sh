#!/bin/sh
# Install quarry — local semantic search for Claude Code (server + client).
# Usage: curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/<SHA>/install-both.sh | sh
#
# This is the full installation for a single machine that acts as both the
# quarry server and a Claude Code client.  It installs the daemon, generates
# TLS certificates, and configures the plugin — all in one step.
#
# For separate server and client machines, use install-server.sh and
# install-client.sh instead.
set -eu

# --- Colors (disabled when not a terminal) ---
if [ -t 1 ]; then
  BOLD='\033[1m' GREEN='\033[32m' YELLOW='\033[33m' NC='\033[0m'
else
  BOLD='' GREEN='' YELLOW='' NC=''
fi

info() { printf '%b▶%b %s\n' "$BOLD" "$NC" "$1"; }
ok()   { printf '  %b✓%b %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '  %b!%b %s\n' "$YELLOW" "$NC" "$1"; }
fail() { printf '  %b✗%b %s\n' "$YELLOW" "$NC" "$1"; exit 1; }

VERSION="1.10.1"
MARKETPLACE_REPO="punt-labs/claude-plugins"
MARKETPLACE_NAME="punt-labs"
PLUGIN_NAME="quarry"
PACKAGE="punt-quarry"
BINARY="quarry"

# --- Step 1: Prerequisites ---

info "Checking prerequisites..."

if command -v claude >/dev/null 2>&1; then
  ok "claude CLI found"
else
  fail "'claude' CLI not found. Install Claude Code first: https://docs.anthropic.com/en/docs/claude-code"
fi

if command -v git >/dev/null 2>&1; then
  ok "git found"
else
  fail "'git' not found. Install git first: https://git-scm.com/downloads"
fi

# --- Step 2: uv ---

info "Checking uv..."

if command -v uv >/dev/null 2>&1; then
  ok "uv already installed"
else
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if [ -f "$HOME/.local/bin/env" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.local/bin/env"
  elif [ -f "$HOME/.cargo/env" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.cargo/env"
  fi
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    fail "uv install succeeded but 'uv' not found on PATH. Restart your shell and re-run."
  fi
  ok "uv installed"
fi

# --- Step 3: Python 3.13+ ---

info "Checking Python..."

PYTHON_FLAG=""
HAVE_PYTHON=0
if command -v python3 >/dev/null 2>&1; then
  PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
  PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
  if [ "$PY_MAJOR" -gt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 13 ]; }; then
    ok "Python ${PY_MAJOR}.${PY_MINOR}"
    HAVE_PYTHON=1
  fi
fi

if [ "$HAVE_PYTHON" = "0" ]; then
  info "Installing Python 3.13 via uv..."
  uv python install 3.13 || fail "Failed to install Python 3.13"
  ok "Python 3.13 (uv-managed)"
  PYTHON_FLAG="--python 3.13"
fi

# --- Step 4: Install quarry CLI ---

info "Installing $PACKAGE..."

# shellcheck disable=SC2086
uv tool install --force $PYTHON_FLAG "$PACKAGE==$VERSION" || fail "Failed to install $PACKAGE==$VERSION"
ok "$PACKAGE==$VERSION installed"

if ! command -v "$BINARY" >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v "$BINARY" >/dev/null 2>&1; then
    fail "$PACKAGE installed but '$BINARY' not found on PATH"
  fi
fi

# GPU runtime handled by 'quarry install' below

ok "$BINARY $(command -v "$BINARY")"

# --- Step 5: Download embedding model and generate TLS certificates ---

info "Downloading embedding model and generating TLS certificates..."
printf '\n'
"$BINARY" install
printf '\n'

# --- Step 5: Register marketplace ---

info "Registering Punt Labs marketplace..."

if claude plugin marketplace list < /dev/null 2>/dev/null | grep -q "$MARKETPLACE_NAME"; then
  ok "marketplace already registered"
  claude plugin marketplace update "$MARKETPLACE_NAME" < /dev/null 2>/dev/null || true
else
  claude plugin marketplace add "$MARKETPLACE_REPO" < /dev/null || fail "Failed to register marketplace"
  ok "marketplace registered"
fi

# --- Step 6: SSH fallback for plugin install ---

NEED_HTTPS_REWRITE=0
cleanup_https_rewrite() {
  if [ "$NEED_HTTPS_REWRITE" = "1" ]; then
    git config --global --unset url."https://github.com/".insteadOf 2>/dev/null || true
    NEED_HTTPS_REWRITE=0
  fi
}
trap cleanup_https_rewrite EXIT INT TERM

if ! ssh -n -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
  warn "SSH auth to GitHub unavailable, using HTTPS fallback"
  git config --global url."https://github.com/".insteadOf "git@github.com:"
  NEED_HTTPS_REWRITE=1
fi

# --- Step 7: Install plugin ---

info "Installing $PLUGIN_NAME plugin..."

claude plugin uninstall "${PLUGIN_NAME}@${MARKETPLACE_NAME}" < /dev/null 2>/dev/null || true
if ! claude plugin install "${PLUGIN_NAME}@${MARKETPLACE_NAME}" < /dev/null; then
  cleanup_https_rewrite
  fail "Failed to install $PLUGIN_NAME"
fi
if ! claude plugin list < /dev/null 2>/dev/null | grep -q "$PLUGIN_NAME@$MARKETPLACE_NAME"; then
  cleanup_https_rewrite
  fail "$PLUGIN_NAME install reported success but plugin not found"
fi
ok "$PLUGIN_NAME plugin installed"

cleanup_https_rewrite

# --- Step 8.5: Configure local TLS access ---

# Wait for daemon to be ready (up to 30 seconds)
info "Waiting for quarry daemon to be ready..."
_i=0
while [ $_i -lt 15 ]; do
  if curl -fsk "https://localhost:8420/health" >/dev/null 2>&1; then
    ok "Daemon is ready"
    break
  fi
  sleep 2
  _i=$((_i + 1))
done
if [ $_i -eq 15 ]; then
  warn "Daemon did not respond after 30s — login may fail (will retry automatically)"
fi

info "Configuring local TLS connection..."
printf '\n'
if QUARRY_API_KEY="${QUARRY_API_KEY:-}" "$BINARY" login localhost --yes 2>/dev/null; then
  ok "Local TLS connection configured — plugin will use wss://localhost:8420/mcp"
else
  warn "quarry login localhost failed — plugin will use local stdio fallback (quarry mcp)"
  warn "To configure TLS later: quarry login localhost --yes"
fi
printf '\n'

# --- Step 8: Verify ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor || true
printf '\n'

# --- Done ---

printf '%b%b%s is ready!%b\n\n' "$GREEN" "$BOLD" "$PLUGIN_NAME" "$NC"
printf 'Restart Claude Code to activate the plugin.\n\n'
printf 'Quick start:\n'
printf '  /find <query>                     # semantic search\n'
printf '  /ingest <url>                     # index a webpage\n'
printf '  quarry ingest notes.md            # index a file from CLI\n\n'
printf 'To connect another machine to this server:\n'
printf '  1. Install the client: curl -fsSL <install-client.sh URL> | sh\n'
printf '  2. Connect: quarry login <this-host> --api-key <your-api-key>\n\n'
