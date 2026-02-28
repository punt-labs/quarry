#!/bin/sh
# Install quarry — local semantic search for Claude Code.
# Usage: curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/<SHA>/install.sh | sh
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

# --- Step 2: Python + uv ---

info "Checking Python..."

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  fail "Python not found. Install Python 3.13+ from https://python.org"
fi

PY_MAJOR=$("$PYTHON" -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 13 ]; }; then
  fail "Python ${PY_MAJOR}.${PY_MINOR} found, but 3.13+ is required"
fi

ok "Python ${PY_MAJOR}.${PY_MINOR}"

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

# --- Step 3: Install quarry CLI ---

info "Installing $PACKAGE..."

uv tool install --force "$PACKAGE" || fail "Failed to install $PACKAGE"
ok "$PACKAGE installed"

if ! command -v "$BINARY" >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v "$BINARY" >/dev/null 2>&1; then
    fail "$PACKAGE installed but '$BINARY' not found on PATH"
  fi
fi

ok "$BINARY $(command -v "$BINARY")"

# --- Step 4: Download embedding model ---

info "Downloading embedding model..."
printf '\n'
"$BINARY" install
printf '\n'

# --- Step 5: Register marketplace ---

info "Registering Punt Labs marketplace..."

if claude plugin marketplace list 2>/dev/null | grep -q "$MARKETPLACE_NAME"; then
  ok "marketplace already registered"
  claude plugin marketplace update "$MARKETPLACE_NAME" 2>/dev/null || true
else
  claude plugin marketplace add "$MARKETPLACE_REPO" || fail "Failed to register marketplace"
  ok "marketplace registered"
fi

# --- Step 6: SSH fallback for plugin install ---

# claude plugin install clones via SSH (git@github.com:...).
# Users without SSH keys need an HTTPS fallback.
NEED_HTTPS_REWRITE=0
cleanup_https_rewrite() {
  if [ "$NEED_HTTPS_REWRITE" = "1" ]; then
    git config --global --unset url."https://github.com/".insteadOf 2>/dev/null || true
    NEED_HTTPS_REWRITE=0
  fi
}
trap cleanup_https_rewrite EXIT INT TERM

if ! ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
  warn "SSH auth to GitHub unavailable, using HTTPS fallback"
  git config --global url."https://github.com/".insteadOf "git@github.com:"
  NEED_HTTPS_REWRITE=1
fi

# --- Step 7: Install plugin ---

info "Installing $PLUGIN_NAME plugin..."

claude plugin uninstall "${PLUGIN_NAME}@${MARKETPLACE_NAME}" 2>/dev/null || true
if ! claude plugin install "${PLUGIN_NAME}@${MARKETPLACE_NAME}"; then
  cleanup_https_rewrite
  fail "Failed to install $PLUGIN_NAME"
fi
if ! claude plugin list 2>/dev/null | grep -q "$PLUGIN_NAME@$MARKETPLACE_NAME"; then
  cleanup_https_rewrite
  fail "$PLUGIN_NAME install reported success but plugin not found"
fi
ok "$PLUGIN_NAME plugin installed"

cleanup_https_rewrite

# --- Step 8: Verify ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor
printf '\n'

# --- Done ---

printf '%b%b%s is ready!%b\n\n' "$GREEN" "$BOLD" "$PLUGIN_NAME" "$NC"
printf 'Restart Claude Code to activate the plugin.\n\n'
printf 'Quick start:\n'
printf '  /find <query>                     # semantic search\n'
printf '  /ingest <url>                     # index a webpage\n'
printf '  quarry ingest-file notes.md       # index a file from CLI\n\n'
