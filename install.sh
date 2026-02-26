#!/bin/sh
# Install quarry — local semantic search for Claude Code and Claude Desktop.
# Usage: curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/main/install.sh | sh
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

# TODO: revert to "punt-quarry" once PyPI org prefix is approved
PACKAGE="punt-quarry@git+https://github.com/punt-labs/quarry.git"
PACKAGE_SHORT="punt-quarry"
BINARY="quarry"

# --- Step 1: Python ---

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

# --- Step 3: punt-quarry ---

info "Installing $PACKAGE_SHORT..."

# --force: overwrites existing binary (may exist from old package name or prior install)
uv tool install --force "$PACKAGE" || fail "Failed to install $PACKAGE_SHORT"
ok "$PACKAGE_SHORT installed"

if ! command -v "$BINARY" >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v "$BINARY" >/dev/null 2>&1; then
    fail "$PACKAGE installed but '$BINARY' not found on PATH"
  fi
fi

ok "$BINARY $(command -v "$BINARY")"

# --- Step 4: quarry install (model download + MCP registration) ---

info "Setting up quarry (downloading model, configuring MCP)..."
printf '\n'
"$BINARY" install
printf '\n'

# --- Step 5: quarry doctor ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor
printf '\n'

# --- Done ---

printf '%b%b%s is ready!%b\n\n' "$GREEN" "$BOLD" "$PACKAGE" "$NC"
printf 'Quick start:\n'
printf '  quarry ingest-file notes.md      # index a file\n'
printf '  quarry search "my topic"          # semantic search\n'
printf '  quarry ingest-url https://...     # index a webpage\n\n'
printf 'Claude Code and Claude Desktop are configured automatically.\n'
printf 'Restart Claude Desktop if it was running during install.\n\n'
