#!/bin/sh
# Install quarry — local semantic search for Claude Code.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/<SHA>/install.sh | sh
#   curl -fsSL ... | sh -s -- --server    # server-only (daemon + TLS, no plugin)
#   curl -fsSL ... | sh -s -- --client    # client-only (CLI + plugin, no daemon)
#
# Default (no flags): full install — daemon, TLS, plugin, local client login.
# --server: installs quarry daemon with TLS.  No Claude Code required.
# --client: installs quarry CLI and plugin.  No model download, no daemon.
set -eu

# --- Argument parsing ---

usage() {
  printf 'Usage: install.sh [--server | --client | --help]\n\n'
  printf '  (default)   Full install: daemon + plugin + local TLS login\n'
  printf '  --server    Server-only: daemon + TLS, no Claude Code plugin\n'
  printf '  --client    Client-only: CLI + plugin, no model or daemon\n'
  printf '  --help, -h  Show this help\n'
}

MODE="full"
MODE_SET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --server)
      if [ "$MODE_SET" = "1" ]; then
        printf 'Error: --server and --client are mutually exclusive.\n' >&2
        usage >&2; exit 1
      fi
      MODE="server"; MODE_SET=1; shift ;;
    --client)
      if [ "$MODE_SET" = "1" ]; then
        printf 'Error: --server and --client are mutually exclusive.\n' >&2
        usage >&2; exit 1
      fi
      MODE="client"; MODE_SET=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
done

# --- Colors (disabled when not a terminal) ---
if [ -t 1 ]; then
  BOLD='\033[1m' GREEN='\033[32m' YELLOW='\033[33m' NC='\033[0m'
else
  BOLD='' GREEN='' YELLOW='' NC=''
fi

info() { printf '%b\342\226\266%b %s\n' "$BOLD" "$NC" "$1"; }
ok()   { printf '  %b\342\234\223%b %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '  %b!%b %s\n' "$YELLOW" "$NC" "$1"; }
fail() { printf '  %b\342\234\227%b %s\n' "$YELLOW" "$NC" "$1"; exit 1; }

VERSION="1.12.2"
MARKETPLACE_REPO="punt-labs/claude-plugins"
MARKETPLACE_NAME="punt-labs"
PLUGIN_NAME="quarry"
PACKAGE="punt-quarry"
BINARY="quarry"

# --- Step 1: Prerequisites ---

info "Checking prerequisites..."

# claude CLI required for full and client modes (plugin install).
if [ "$MODE" != "server" ]; then
  if command -v claude >/dev/null 2>&1; then
    ok "claude CLI found"
  else
    fail "'claude' CLI not found. Install Claude Code first: https://docs.anthropic.com/en/docs/claude-code"
  fi
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

# --- Step 3b: Detect NVIDIA GPU ---

HAS_NVIDIA=0
if command -v nvidia-smi >/dev/null 2>&1; then
  if nvidia-smi >/dev/null 2>&1; then
    ok "NVIDIA GPU detected"
    HAS_NVIDIA=1
  fi
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

# --- Step 4b: Swap onnxruntime for onnxruntime-gpu when an NVIDIA GPU is present ---
#
# MUST run AFTER `uv tool install --force` (which re-pins the CPU wheel from
# pyproject.toml) and BEFORE `quarry install` (so the service-managed daemon
# starts with CUDA providers available).  The Python-side swap in
# `ensure_gpu_runtime()` reports success but does not stick inside the tool
# venv under real conditions -- see bead quarry-mxi9.
#
# The two packages conflict (same `onnxruntime` Python module, different PyPI
# names), so we uninstall CPU before installing GPU.  `uv pip --python` targets
# the venv that owns that interpreter.
if [ "$HAS_NVIDIA" = "1" ]; then
  info "Installing CUDA support (onnxruntime-gpu)..."
  TOOL_PYTHON="$(head -1 "$(command -v "$BINARY")" | sed 's/^#!//')"
  if [ -f "$TOOL_PYTHON" ]; then
    uv pip uninstall --python "$TOOL_PYTHON" onnxruntime < /dev/null 2>/dev/null || true
    if uv pip install --python "$TOOL_PYTHON" "onnxruntime-gpu>=1.18.0" < /dev/null; then
      ok "onnxruntime-gpu installed"
    else
      warn "Failed to install onnxruntime-gpu -- restoring CPU onnxruntime"
      uv pip install --python "$TOOL_PYTHON" "onnxruntime>=1.18.0" < /dev/null || fail "Could not restore onnxruntime -- re-run install.sh"
      ok "onnxruntime (CPU) restored"
    fi
  else
    warn "Could not locate tool Python -- CUDA support skipped"
  fi
fi

ok "$BINARY $(command -v "$BINARY")"

# --- Step 5: Download embedding model and generate TLS certificates ---
# Skipped in --client mode: clients connect to a remote server and do not
# need the 120MB embedding model or local TLS certificates.

if [ "$MODE" != "client" ]; then
  # --server mode requires QUARRY_API_KEY before downloading (fail early).
  if [ "$MODE" = "server" ]; then
    if [ -z "${QUARRY_API_KEY:-}" ]; then
      fail "QUARRY_API_KEY is not set. Export it before running this script: export QUARRY_API_KEY=<your-key>"
    fi
  fi

  info "Downloading embedding model and generating TLS certificates..."
  printf '\n'
  if [ "$MODE" = "server" ]; then
    QUARRY_SERVE_HOST=0.0.0.0 "$BINARY" install
  else
    "$BINARY" install
  fi
  printf '\n'

  # Belt-and-suspenders restart of the service-managed daemon.
  #
  # `quarry install` (above) calls `_launchd_install` / `_systemd_install`,
  # which DO restart the service when one was already registered.  If a stale
  # daemon from a previous install is running with the old in-memory
  # onnxruntime imports, force a restart here so it picks up the tool-venv
  # swap from Step 4b before the health check.  Both commands are idempotent
  # no-ops when the service is not registered.
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user restart quarry 2>/dev/null || true
  elif command -v launchctl >/dev/null 2>&1; then
    # Label must match _LABEL in src/quarry/service.py.
    launchctl kickstart -k "gui/$(id -u)/com.punt-labs.quarry" 2>/dev/null || true
  fi
fi

# --- Step 6: Health-check the service-managed daemon ---
# Runs for server and full modes (wherever quarry install ran).

if [ "$MODE" != "client" ]; then
  if [ "$MODE" = "server" ]; then
    # Server mode: strict health check with TLS CA verification.
    info "Waiting for daemon to be ready..."
    printf '\n'

    HEALTH_URL="https://localhost:8420/health"
    CA_CERT="${HOME}/.punt-labs/quarry/tls/ca.crt"
    MAX_TRIES=10
    _i=0
    while [ "$_i" -lt "$MAX_TRIES" ]; do
      _i=$((_i + 1))
      if curl -fsS --cacert "$CA_CERT" "$HEALTH_URL" >/dev/null 2>&1; then
        ok "Quarry daemon is healthy (attempt $_i/$MAX_TRIES)"
        break
      fi
      if [ "$_i" -eq "$MAX_TRIES" ]; then
        fail "Daemon did not become healthy after $MAX_TRIES attempts -- check service logs"
      fi
      sleep 2
    done
    printf '\n'
  else
    # Full mode: lenient health check (used before login attempt).
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
      warn "Daemon did not respond after 30s -- login may fail (will retry automatically)"
    fi
  fi
fi

# --- Step 7: Marketplace registration ---
# Runs for full and client modes (wherever the plugin is installed).

if [ "$MODE" != "server" ]; then
  info "Registering Punt Labs marketplace..."

  if claude plugin marketplace list < /dev/null 2>/dev/null | grep -q "$MARKETPLACE_NAME"; then
    ok "marketplace already registered"
    claude plugin marketplace update "$MARKETPLACE_NAME" < /dev/null 2>/dev/null || true
  else
    claude plugin marketplace add "$MARKETPLACE_REPO" < /dev/null || fail "Failed to register marketplace"
    ok "marketplace registered"
  fi
fi

# --- Step 8: SSH fallback for plugin install ---
# Runs for full and client modes.

NEED_HTTPS_REWRITE=0
cleanup_https_rewrite() {
  if [ "$NEED_HTTPS_REWRITE" = "1" ]; then
    git config --global --unset url."https://github.com/".insteadOf 2>/dev/null || true
    NEED_HTTPS_REWRITE=0
  fi
}

if [ "$MODE" != "server" ]; then
  trap cleanup_https_rewrite EXIT INT TERM

  if ! ssh -n -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    warn "SSH auth to GitHub unavailable, using HTTPS fallback"
    git config --global url."https://github.com/".insteadOf "git@github.com:"
    NEED_HTTPS_REWRITE=1
  fi
fi

# --- Step 9: Install plugin ---
# Runs for full and client modes.

if [ "$MODE" != "server" ]; then
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
fi

# --- Step 10: Configure local TLS access ---
# Runs only in full mode (local server + local client).

if [ "$MODE" = "full" ]; then
  info "Configuring local TLS connection..."
  printf '\n'
  if QUARRY_API_KEY="${QUARRY_API_KEY:-}" "$BINARY" login localhost --yes 2>/dev/null; then
    ok "Local TLS connection configured -- plugin will use wss://localhost:8420/mcp"
  else
    warn "quarry login localhost failed -- plugin will use local stdio fallback (quarry mcp)"
    warn "To configure TLS later: quarry login localhost --yes"
  fi
  printf '\n'
fi

# --- Step 11: Verify ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor || true
printf '\n'

# --- Done ---

if [ "$MODE" = "server" ]; then
  printf '%b%b%s server is ready!%b\n\n' "$GREEN" "$BOLD" "$BINARY" "$NC"
  printf 'The server daemon is running on port 8420 with TLS.\n\n'
  printf 'To connect a client machine:\n'
  printf '  1. Install quarry on the client:\n'
  printf '     curl -fsSL <install.sh URL> | sh -s -- --client\n'
  printf '  2. Connect: quarry login <this-host> --api-key <your-api-key>\n\n'
  printf 'The CA fingerprint is shown above -- clients will see it during login.\n'
elif [ "$MODE" = "client" ]; then
  printf '%b%b%s client is ready!%b\n\n' "$GREEN" "$BOLD" "$PLUGIN_NAME" "$NC"
  printf 'Restart Claude Code, then connect to your server:\n\n'
  printf '  quarry login <server-host> --api-key <your-api-key>\n\n'
  printf 'The login command will:\n'
  printf '  1. Fetch and display the server CA fingerprint\n'
  printf '  2. Ask you to confirm trust (TOFU)\n'
  printf '  3. Configure Claude Code to use the remote server\n\n'
  printf 'After login, restart Claude Code to activate the remote connection.\n'
else
  printf '%b%b%s is ready!%b\n\n' "$GREEN" "$BOLD" "$PLUGIN_NAME" "$NC"
  printf 'Restart Claude Code to activate the plugin.\n\n'
  printf 'Quick start:\n'
  printf '  /find <query>                     # semantic search\n'
  printf '  /ingest <url>                     # index a webpage\n'
  printf '  quarry ingest notes.md            # index a file from CLI\n\n'
  printf 'To connect another machine to this server:\n'
  printf '  1. Install the client:\n'
  printf '     curl -fsSL <install.sh URL> | sh -s -- --client\n'
  printf '  2. Connect: quarry login <this-host> --api-key <your-api-key>\n\n'
fi
