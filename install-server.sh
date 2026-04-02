#!/bin/sh
# Install quarry as a server daemon with TLS.
# Usage: curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/<SHA>/install-server.sh | sh
#
# This script installs quarry, generates TLS certificates, and starts the
# daemon.  It does NOT require the claude CLI and does NOT install the plugin.
# Use install-client.sh on the machines that need to connect to this server.
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

VERSION="1.11.0"
PACKAGE="punt-quarry"
BINARY="quarry"

# --- Step 1: Prerequisites ---

info "Checking prerequisites..."

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

if [ -z "${QUARRY_API_KEY:-}" ]; then
  fail "QUARRY_API_KEY is not set. Export it before running this script: export QUARRY_API_KEY=<your-key>"
fi

info "Downloading embedding model and generating TLS certificates..."
printf '\n'
QUARRY_SERVE_HOST=0.0.0.0 "$BINARY" install
printf '\n'

# --- Step 5: Health-check the service-managed daemon ---

# quarry install (above) registered quarry as a launchd/systemd service with
# KeepAlive/Restart=on-failure and --host 0.0.0.0 --tls baked in.  The service
# manager has already started the daemon.  Do NOT pkill it and re-spawn
# manually — that causes a port conflict because the service manager immediately
# restarts the killed daemon (KeepAlive) while the manual process is binding.

info "Waiting for daemon to be ready..."
printf '\n'

HEALTH_URL="https://localhost:8420/health"
CA_CERT="${HOME}/.punt-labs/quarry/tls/ca.crt"
MAX_TRIES=10
i=0
while [ "$i" -lt "$MAX_TRIES" ]; do
  i=$((i + 1))
  if curl -fsS --cacert "$CA_CERT" "$HEALTH_URL" >/dev/null 2>&1; then
    ok "Quarry daemon is healthy (attempt $i/$MAX_TRIES)"
    break
  fi
  if [ "$i" -eq "$MAX_TRIES" ]; then
    fail "Daemon did not become healthy after $MAX_TRIES attempts — check service logs"
  fi
  sleep 2
done
printf '\n'

# --- Step 6: Verify ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor || true
printf '\n'

# --- Done ---

printf '%b%b%s server is ready!%b\n\n' "$GREEN" "$BOLD" "$BINARY" "$NC"
printf 'The server daemon is running on port 8420 with TLS.\n\n'
printf 'To connect a client machine:\n'
printf '  1. Install quarry on the client: curl -fsSL <install-client.sh URL> | sh\n'
printf '  2. Connect: quarry login <this-host> --api-key <your-api-key>\n\n'
printf 'The CA fingerprint is shown above — clients will see it during login.\n'
