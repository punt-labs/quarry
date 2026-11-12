#!/bin/sh
# Install quarry — local semantic search for Claude Code.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/<SHA>/install.sh | sh
#   curl -fsSL ... | sh -s -- --no-plugin  # install the CLI only, skip the plugin
#   curl -fsSL ... | sh -s -- --network    # bind daemon to 0.0.0.0 (serve remote clients)
#
# Default (no flags): full install — daemon on localhost, TLS, plugin (if claude CLI found),
# local quarry login.  This is what most users want.
#
# --no-plugin: install the harness-neutral CLI (binary, PATH, model, TLS, per-repo
# enable, health check) but skip the Claude Code marketplace-register + plugin-install
# steps.  For non-Claude harnesses (Codex, Cursor, a plain terminal) and for
# enterprise-policy Claude users whose org blocks marketplace installs.  Honor
# QUARRY_NO_PLUGIN=1 identically, for argument-hostile contexts (a templated
# `curl … | sh`).  See punt-kit/standards/install-cli-only.md.
#
# --network: same as default, but binds daemon to 0.0.0.0 instead of localhost.
# Requires QUARRY_API_KEY.  Prints CA fingerprint and remote-login instructions.
#
# QUARRY_LOCAL_WHEEL=/path/to/wheel installs a working-tree wheel instead of the
# PyPI-pinned release -- for offline/air-gapped installs, pre-release testing,
# and the clean-machine harness (tests/harness/), which must exercise THIS
# branch's package, not the shipped version.  Its dependency pins come from
# install-constraints.txt in the working tree when present (else the v$VERSION tag).
set -eu

# --- Argument parsing ---

usage() {
  printf '%s\n' \
    'install.sh — install the quarry CLI and (by default) the Claude Code plugin' \
    '' \
    'Usage: curl -fsSL .../install.sh | sh                    # CLI + plugin (local daemon)' \
    '       curl -fsSL .../install.sh | sh -s -- --no-plugin  # CLI only, skip the plugin' \
    '       curl -fsSL .../install.sh | sh -s -- --network    # bind daemon to 0.0.0.0' \
    '' \
    'Options:' \
    '  --network     Bind the daemon to 0.0.0.0 (serves remote clients).' \
    '                Requires QUARRY_API_KEY to be set.' \
    '  --no-plugin   Install the CLI only; skip the Claude Code marketplace + plugin.' \
    '  -h, --help    Print this help and exit.' \
    '' \
    'Environment:' \
    '  QUARRY_NO_PLUGIN=1  Same as --no-plugin, for argument-hostile contexts:' \
    '                      curl -fsSL .../install.sh | QUARRY_NO_PLUGIN=1 sh' \
    '  QUARRY_LOCAL_WHEEL=/path/to/wheel' \
    '                      Install a working-tree wheel instead of the PyPI pin' \
    '                      (offline/air-gapped installs, pre-release + harness testing).'
}

# Parse before any work.  A misspelled flag (--no-plguin) must not be silently
# ignored — a piped `curl … | sh` would then install the plugin the user asked
# to skip — so an unknown option is a usage error (exit 2).
NETWORK=0
NO_PLUGIN_REQUESTED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --network)   NETWORK=1; shift ;;
    --no-plugin) NO_PLUGIN_REQUESTED=1; shift ;;
    --help|-h)   usage; exit 0 ;;
    *) printf 'install.sh: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
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

VERSION="3.6.2"
MARKETPLACE_REPO="punt-labs/claude-plugins"
MARKETPLACE_NAME="punt-labs"
PLUGIN_NAME="quarry"
PACKAGE="punt-quarry"
BINARY="quarry"

# --- Step 1: Prerequisites ---

info "Checking prerequisites..."

# The Claude Code plugin is a default-on feature of this installer.  A single
# boolean, SKIP_PLUGIN, gates the marketplace-register + plugin-install steps
# (7/8/9) and their success messaging.  Every other step -- binary download,
# PATH, tool dirs, embedding model, TLS, per-repo enable, health check -- runs
# regardless.  SKIP_PLUGIN is the OR of an explicit request (--no-plugin /
# QUARRY_NO_PLUGIN=1) and capability-absence (claude or git missing).  git is
# needed only to clone the plugin, so its absence skips the plugin rather than
# failing the CLI install.  There is no counter-flag to force the plugin on:
# request and capability-absence both drive the boolean the same direction, and
# you cannot install a plugin without claude.  SKIP_REASON records the cause so
# the final message names the right remedy -- an auto-skip user never passed
# --no-plugin, so "re-run without it" would be the wrong instruction.
SKIP_PLUGIN=0
SKIP_REASON=""
if [ "$NO_PLUGIN_REQUESTED" = "1" ] || [ "${QUARRY_NO_PLUGIN:-}" = "1" ]; then
  ok "plugin install skipped by request (--no-plugin / QUARRY_NO_PLUGIN=1)"
  SKIP_PLUGIN=1
  SKIP_REASON="requested"
fi

if [ "$SKIP_PLUGIN" = "0" ]; then
  if command -v claude >/dev/null 2>&1; then
    ok "claude CLI found"
  else
    warn "Claude Code not found -- skipping plugin install"
    SKIP_PLUGIN=1
    SKIP_REASON="no-claude"
  fi
fi

# git is required only to clone the plugin.  Absent + plugin still wanted =>
# auto-skip; absent + already skipping => no message needed.
if command -v git >/dev/null 2>&1; then
  ok "git found"
elif [ "$SKIP_PLUGIN" = "0" ]; then
  warn "git not found -- skipping plugin install (required to clone the plugin)"
  SKIP_PLUGIN=1
  SKIP_REASON="no-git"
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
    # Fail-fast gate only: this is whichever python3 is first on PATH, not
    # necessarily the interpreter uv builds the quarry tool venv with (uv
    # prefers its own managed interpreters; the venv's is reported after the
    # install below).
    ok "python3 on PATH: ${PY_MAJOR}.${PY_MINOR} (the quarry venv is resolved by uv)"
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

# The OCR dependency rapidocr declares the FULL `opencv-python`, whose GUI build
# dynamically links X11/GL system libraries (libGL.so.1, libxcb.so.1, libglib)
# that headless servers and minimal containers do not carry.  quarry already
# pins the server-safe `opencv-python-headless`, but BOTH wheels ship the same
# `cv2` module, so the GUI build shadows the headless one and `import cv2` then
# fails to load on a headless box.  That failure makes `quarry install` /
# `quarry doctor` report required-check failures, which aborts this installer
# under `set -e` -- i.e. a clean-machine install of the CLI is impossible on any
# box without a desktop.  A uv override drops the GUI build for the whole
# resolution (the marker never matches), leaving `opencv-python-headless` as the
# sole cv2 provider.  uv --overrides takes a requirements FILE, so write one.
#
# Use an EXPLICIT path template: bare `mktemp` (GNU default) fails on BSD/macOS,
# where `mktemp` requires a template argument -- under `set -eu` that aborts the
# installer before `uv tool install`, breaking every macOS install.  An absolute
# template with trailing X's is portable across BSD and GNU mktemp.
#
# Class-1 temp-file cleanup: remove the temp files on ANY exit from here on --
# normal completion, a `fail` (which exits), or a SIGINT/SIGTERM in the window
# before the install.  The cleanup trap is EXIT-only: INT and TERM turn into an
# `exit` (130 / 143, the shell convention) so the EXIT trap runs the cleanup and
# the installer STOPS -- a cleanup handler on INT/TERM alone would run and then
# carry on to `uv tool install` after Ctrl-C.  A per-branch `rm` would leak on an
# interrupt or on any command that fails between a mktemp and the install.  Both
# variables are initialised empty and the traps are registered BEFORE either
# mktemp, so a signal or failure between the two mktemps cannot leak the first
# file (`rm -f ""` is a silent no-op).  The trap is released right after `uv tool install` (below),
# before Step 8 registers its own EXIT trap for the HTTPS rewrite -- a shell
# holds only one handler per signal, so the two must not overlap.
OPENCV_OVERRIDE=""
CONSTRAINTS_FILE=""
trap 'rm -f "$OPENCV_OVERRIDE" "$CONSTRAINTS_FILE"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
OPENCV_OVERRIDE="$(mktemp "${TMPDIR:-/tmp}/quarry-opencv-override.XXXXXX")"
CONSTRAINTS_FILE="$(mktemp "${TMPDIR:-/tmp}/quarry-constraints.XXXXXX")"
printf '%s\n' 'opencv-python ; sys_platform == "never"' > "$OPENCV_OVERRIDE"

# QUARRY_LOCAL_WHEEL installs a working-tree wheel instead of the PyPI pin --
# for offline / air-gapped installs, pre-release testing, and the clean-machine
# harness (tests/harness/), which must exercise THIS branch's package, not the
# already-released $VERSION.  Same spirit as ETHOS_LOCAL_BINARY in ethos's
# installer.  Unset (the default) installs "$PACKAGE==$VERSION" from PyPI.
if [ -n "${QUARRY_LOCAL_WHEEL:-}" ]; then
  if [ ! -f "$QUARRY_LOCAL_WHEEL" ] || [ ! -s "$QUARRY_LOCAL_WHEEL" ]; then
    fail "QUARRY_LOCAL_WHEEL set but not a readable, non-empty file: $QUARRY_LOCAL_WHEEL"
  fi
  INSTALL_TARGET="$QUARRY_LOCAL_WHEEL"
  info "Installing from local wheel: $QUARRY_LOCAL_WHEEL"
else
  INSTALL_TARGET="$PACKAGE==$VERSION"
fi

# Pin every dependency to the versions uv.lock names.  `uv tool install` does not
# read uv.lock, so without constraints each dependency resolves to the newest
# release PyPI offers that day (v3.5.4 shipped lancedb 0.41.0 / pyarrow 26.0.1
# while the tested lock said 0.40.0 / 26.0.0).  install-constraints.txt is
# exported from uv.lock at release time (`make install-constraints`) and fetched
# here from the immutable v$VERSION tag, so an old installer always pairs with
# the pins it shipped with.  VERSION is 3.5.5 or later and every such tag ships
# the file, so there is no unpinned path: any failure to obtain valid pins (HTTP
# 404 for a missing tag or file, other HTTP errors, network error, empty body, a
# captive-portal page) is a hard failure naming the cause.  Installing unpinned
# is the bug this step exists to prevent.
# QUARRY_LOCAL_WHEEL installs use the working-tree file next to this script when
# present (the tag may not exist for an unreleased branch); with none they warn
# and fetch like any other install.  The lookup runs only when $0 is a real file:
# under `curl | sh` $0 is `sh`, its dirname is the caller's cwd, and a planted
# install-constraints.txt there must never be trusted.
#
# A constraints file is not inert: uv honours directives inside it
# (--index-url, --extra-index-url, --find-links, --no-index, -r/-c, URLs) that
# redirect package resolution.  So EVERY line of EITHER source must be a plain
# pin -- `name==version`, optionally ` ; <environment marker>`, the exact shape
# `uv export --no-annotate --no-header` emits -- or a comment/blank line.
PIN_RE='^[A-Za-z0-9._-]+==[A-Za-z0-9._+!*-]+( ; [A-Za-z0-9._ "()<>=!~,*'"'"'-]+)?$'

check_pins_file() {
  # grep's own status is read directly (no pipe): 0 = a non-pin line exists,
  # 1 = every line is a pin, >=2 = grep failed and nothing was validated.
  _grep_rc=0
  _bad="$(grep -Ev -e '^[[:space:]]*$' -e '^[[:space:]]*#' -e "$PIN_RE" "$1")" || _grep_rc=$?
  case "$_grep_rc" in
    1) ;;
    0) fail "pins file contains a non-pin line: $(printf '%s\n' "$_bad" | head -1)" ;;
    *) fail "Could not validate pins file $1" ;;
  esac
}

CONSTRAINTS_URL="https://raw.githubusercontent.com/punt-labs/quarry/v$VERSION/install-constraints.txt"
USE_CONSTRAINTS=0
if [ -n "${QUARRY_LOCAL_WHEEL:-}" ] && [ ! -f "$0" ]; then
  warn "QUARRY_LOCAL_WHEEL is set but the installer is not running from a file (piped) -- fetching the pins for v$VERSION instead of trusting the current directory"
elif [ -n "${QUARRY_LOCAL_WHEEL:-}" ]; then
  SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
  if [ -s "$SCRIPT_DIR/install-constraints.txt" ]; then
    cp "$SCRIPT_DIR/install-constraints.txt" "$CONSTRAINTS_FILE" ||
      fail "Could not read $SCRIPT_DIR/install-constraints.txt"
    check_pins_file "$CONSTRAINTS_FILE"
    USE_CONSTRAINTS=1
    ok "dependency pins: $SCRIPT_DIR/install-constraints.txt (working tree)"
  else
    warn "QUARRY_LOCAL_WHEEL is set but $SCRIPT_DIR/install-constraints.txt was not found -- fetching the pins for v$VERSION instead"
  fi
fi

if [ "$USE_CONSTRAINTS" = "0" ]; then
  info "Fetching dependency pins for v$VERSION..."
  # No -f: the status code must be readable to tell a 404 from an outage.
  PINS_STATUS="$(curl -sSL -o "$CONSTRAINTS_FILE" -w '%{http_code}' "$CONSTRAINTS_URL")" ||
    fail "Could not fetch dependency pins from $CONSTRAINTS_URL (network error) -- refusing to install unpinned dependencies. Check your network and re-run."
  case "$PINS_STATUS" in
    200)
      if [ ! -s "$CONSTRAINTS_FILE" ]; then
        fail "Dependency pins fetched from $CONSTRAINTS_URL are empty -- refusing to install unpinned dependencies."
      fi
      # Every line must be a plain pin: a captive-portal page, or a file with
      # resolver-redirecting directives, must not reach uv.
      check_pins_file "$CONSTRAINTS_FILE"
      USE_CONSTRAINTS=1
      ok "dependency pins: v$VERSION install-constraints.txt"
      ;;
    404)
      fail "Dependency pins not found at $CONSTRAINTS_URL (HTTP 404) -- v$VERSION should ship install-constraints.txt; refusing to install unpinned dependencies. Check the release tag exists."
      ;;
    *)
      fail "Could not fetch dependency pins from $CONSTRAINTS_URL (HTTP $PINS_STATUS) -- refusing to install unpinned dependencies. Re-run later."
      ;;
  esac
fi

# Every path above either obtained valid pins or exited, so the install is
# always constrained.
# shellcheck disable=SC2086
uv tool install --force --overrides "$OPENCV_OVERRIDE" --constraints "$CONSTRAINTS_FILE" $PYTHON_FLAG "$INSTALL_TARGET" ||
  fail "Failed to install $INSTALL_TARGET"

# Overrides and constraints files are no longer needed: remove them and release
# the trap so Step 8's EXIT trap (registered only on the plugin path) is
# uncontested.
rm -f "$OPENCV_OVERRIDE" "$CONSTRAINTS_FILE"
trap - EXIT INT TERM
ok "$INSTALL_TARGET installed"

if ! command -v "$BINARY" >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v "$BINARY" >/dev/null 2>&1; then
    fail "$PACKAGE installed but '$BINARY' not found on PATH"
  fi
fi

# The interpreter the tool venv actually runs on: the shebang of the installed
# entry point.  This is what version-sensitive failures depend on, and it can
# differ from the ambient python3 checked in Step 3.
TOOL_PYTHON="$(head -1 "$(command -v "$BINARY")" | sed 's/^#!//')"
TOOL_PY_VERSION=""
TOOL_PY_ERR=""
if [ ! -f "$TOOL_PYTHON" ]; then
  TOOL_PY_ERR="quarry entry-point shebang is not an interpreter path: '$TOOL_PYTHON'"
elif ! TOOL_PY_VERSION="$("$TOOL_PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>&1)"; then
  TOOL_PY_ERR="quarry tool venv Python '$TOOL_PYTHON' failed to report its version: $TOOL_PY_VERSION"
  TOOL_PY_VERSION=""
fi
if [ -n "$TOOL_PY_VERSION" ]; then
  ok "quarry tool venv Python: $TOOL_PY_VERSION"
else
  warn "$TOOL_PY_ERR"
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

# --network mode requires QUARRY_API_KEY before downloading (fail early).
if [ "$NETWORK" = "1" ]; then
  if [ -z "${QUARRY_API_KEY:-}" ]; then
    fail "QUARRY_API_KEY is not set. Export it before running this script: export QUARRY_API_KEY=<your-key>"
  fi
fi

info "Downloading embedding model and generating TLS certificates..."
printf '\n'
if [ "$NETWORK" = "1" ]; then
  QUARRY_SERVE_HOST=0.0.0.0 "$BINARY" install
else
  "$BINARY" install
fi
printf '\n'

# Restart the service-managed daemon and verify the process was replaced.
#
# `quarry install` (above) restarts a registered service, but this step is the
# one that guarantees a daemon from before the upgrade is not left holding the
# old engine (and the pre-swap onnxruntime) in memory.  The restart used to be
# `systemctl --user restart quarry 2>/dev/null || true`: any failure -- no user
# bus in a non-login shell, a unit that failed to start, launchd refusing the
# kickstart -- vanished, and the installer reported success over a stale daemon.
# Now the service's main PID is read before and after.  An unchanged PID, a dead
# daemon, or a failing restart command fails the install with the reason.  A new
# PID proves only that the process was REPLACED, not that it is healthy: with
# Restart=always a daemon that dies at import still shows a new PID.  Health is
# proven by the Step 6 readiness check, which fails (not warns) once a service
# is registered here.
#
# "Registered" means the service MANAGER has the unit loaded (systemd LoadState
# == loaded; `launchctl print` succeeds), not merely that a unit file or plist
# is on disk: `quarry install` writes the file and then degrades best-effort
# when systemctl/launchctl is unreachable (SSH, containers).  File present but
# manager unreachable => warn and skip, matching that degrade.  Nothing on disk
# and nothing loaded => silent no-op (a CLI-only install has no service).
SYSTEMD_UNIT="$HOME/.config/systemd/user/quarry.service"
LAUNCHD_LABEL="com.punt-labs.quarry"  # must match _LABEL in src/quarry/service.py
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
SERVICE_REGISTERED=0

# Print the service's main PID, or 0 when it is not running.  Exits non-zero
# when the service manager cannot be queried.  launchd is read from the same
# gui/<uid> domain the restart targets (`launchctl list` reads the caller's own
# domain, which differs under ssh/sudo).
service_pid() {
  if [ "$1" = "systemd" ]; then
    _pid="$(systemctl --user show quarry --property=MainPID --value)" || return 1
  else
    _out="$(launchctl print "gui/$(id -u)/$LAUNCHD_LABEL")" || return 1
    _pid="$(printf '%s\n' "$_out" | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)[[:space:]]*$/\1/p' | head -1)"
  fi
  printf '%s\n' "${_pid:-0}"
}

query_error() {
  if [ "$1" = "systemd" ]; then
    fail "Cannot query the quarry systemd service (is the user session bus available?)"
  fi
  fail "Cannot query the quarry launchd service (launchctl print gui/$(id -u)/$LAUNCHD_LABEL failed)"
}

restart_service() {
  _mgr="$1"
  _before="$(service_pid "$_mgr")" || query_error "$_mgr"
  if [ "$_mgr" = "systemd" ]; then
    systemctl --user restart quarry ||
      fail "Failed to restart the quarry service: systemctl --user restart quarry"
  else
    launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" ||
      fail "Failed to restart the quarry service: launchctl kickstart -k gui/$(id -u)/$LAUNCHD_LABEL"
  fi
  # kickstart can return before launchd publishes the new pid: poll up to ~10 s
  # for a running pid that differs from the old one.
  _after=0
  _n=0
  while [ "$_n" -lt 10 ]; do
    _after="$(service_pid "$_mgr")" || query_error "$_mgr"
    if [ "$_after" != "0" ] && [ "$_after" != "$_before" ]; then
      break
    fi
    _n=$((_n + 1))
    sleep 1
  done
  if [ "$_after" = "0" ]; then
    fail "The quarry daemon is not running after restart -- check the service logs"
  fi
  if [ "$_after" = "$_before" ]; then
    fail "The quarry daemon kept PID $_before across the restart -- it is still the pre-upgrade process. Restart it by hand: see README 'Managing the Daemon'."
  fi
  SERVICE_REGISTERED=1
  ok "daemon restarted (PID $_before -> $_after)"
}

if command -v systemctl >/dev/null 2>&1; then
  if _load="$(systemctl --user show quarry --property=LoadState --value 2>/dev/null)"; then
    if [ "$_load" = "loaded" ]; then
      restart_service systemd
    fi
  elif [ -f "$SYSTEMD_UNIT" ]; then
    warn "$SYSTEMD_UNIT exists but systemctl --user is unreachable -- skipping the daemon restart (restart it by hand after login)"
  fi
elif command -v launchctl >/dev/null 2>&1; then
  if launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    restart_service launchd
  elif [ -f "$LAUNCHD_PLIST" ]; then
    warn "$LAUNCHD_PLIST exists but launchd does not have the service loaded -- skipping the daemon restart"
  fi
fi

# --- Step 6: Health-check the service-managed daemon ---

if [ "$NETWORK" = "1" ]; then
  # Network mode: strict health check with TLS CA verification.
  info "Waiting for daemon to be ready..."
  printf '\n'

  # Probe the literal 127.0.0.1, not "localhost": the daemon binds IPv4 loopback
  # and login pins 127.0.0.1, so on an IPv6-preferring host "localhost" resolves
  # ::1 first and the gate would miss the ready IPv4 daemon (false timeout).  The
  # server cert carries 127.0.0.1 as an IP SAN, so strict --cacert still verifies.
  HEALTH_URL="https://127.0.0.1:8420/health"
  CA_CERT="${HOME}/.punt-labs/quarry/tls/ca.crt"
  MAX_TRIES=10
  _i=0
  while [ "$_i" -lt "$MAX_TRIES" ]; do
    _i=$((_i + 1))
    # Gate on state=="ready": a warming daemon returns HTTP 200 with
    # state=="starting", so a bare 200 is not readiness.
    if curl -fsS --cacert "$CA_CERT" "$HEALTH_URL" 2>/dev/null | grep -q '"state"[[:space:]]*:[[:space:]]*"ready"'; then
      ok "Quarry daemon is ready (attempt $_i/$MAX_TRIES)"
      break
    fi
    if [ "$_i" -eq "$MAX_TRIES" ]; then
      fail "Daemon did not become healthy after $MAX_TRIES attempts -- check service logs"
    fi
    sleep 2
  done
  printf '\n'
else
  # Default mode: lenient health check (used before login attempt).
  info "Waiting for quarry daemon to be ready..."
  _i=0
  while [ $_i -lt 15 ]; do
    # Probe 127.0.0.1 (the IPv4 bind / login target), not the dual-stack-ambiguous
    # "localhost", so an IPv6-preferring resolver can't miss the ready daemon.
    if curl -fsk "https://127.0.0.1:8420/health" 2>/dev/null | grep -q '"state"[[:space:]]*:[[:space:]]*"ready"'; then
      ok "Daemon is ready"
      break
    fi
    sleep 2
    _i=$((_i + 1))
  done
  if [ $_i -eq 15 ]; then
    if [ "$SERVICE_REGISTERED" = "1" ]; then
      # A registered, just-restarted daemon that never reports ready crashed
      # (or hung) after the restart: a new PID does not prove health.
      fail "Daemon did not become ready within 30s after the restart -- check service logs (journalctl --user -u quarry on Linux; the quarry-stderr.log under the launchd log dir on macOS)"
    fi
    warn "Daemon did not respond after 30s -- login may fail (will retry automatically)"
  fi
fi

# --- Step 7: Marketplace registration ---
# Runs only when the plugin is not skipped (claude present, git present, and no
# --no-plugin / QUARRY_NO_PLUGIN=1 request).

if [ "$SKIP_PLUGIN" = "0" ]; then
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
# Runs only when the plugin is not skipped.

NEED_HTTPS_REWRITE=0
cleanup_https_rewrite() {
  if [ "$NEED_HTTPS_REWRITE" = "1" ]; then
    git config --global --unset url."https://github.com/".insteadOf 2>/dev/null || true
    NEED_HTTPS_REWRITE=0
  fi
}

if [ "$SKIP_PLUGIN" = "0" ]; then
  trap cleanup_https_rewrite EXIT INT TERM

  if ! ssh -n -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    warn "SSH auth to GitHub unavailable, using HTTPS fallback"
    git config --global url."https://github.com/".insteadOf "git@github.com:"
    NEED_HTTPS_REWRITE=1
  fi
fi

# --- Step 9: Install plugin ---
# Runs only when the plugin is not skipped.

if [ "$SKIP_PLUGIN" = "0" ]; then
  info "Installing $PLUGIN_NAME plugin..."

  UNINSTALL_ERR=$(claude plugin uninstall "${PLUGIN_NAME}@${MARKETPLACE_NAME}" < /dev/null 2>&1) || {
    case "$UNINSTALL_ERR" in
      *"not installed"*|*"not found"*|*"No plugin"*|*"does not exist"*|*"no plugin"*)
        # Expected on fresh machines — suppress silently.
        ;;
      *)
        warn "Plugin uninstall failed: $UNINSTALL_ERR (continuing with install)"
        ;;
    esac
  }
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

info "Configuring local TLS connection..."
printf '\n'
if QUARRY_API_KEY="${QUARRY_API_KEY:-}" "$BINARY" login 127.0.0.1 --yes 2>/dev/null; then
  ok "Local TLS connection configured -- plugin will use wss://127.0.0.1:8420/mcp"
else
  warn "quarry login 127.0.0.1 failed -- plugin will use local stdio fallback (quarry mcp)"
  warn "To configure TLS later: quarry login 127.0.0.1 --yes"
fi
printf '\n'

# --- Step 11: Verify ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor || true
printf '\n'

# --- Done ---

if [ "$NETWORK" = "1" ]; then
  printf '%b%b%s server is ready!%b\n\n' "$GREEN" "$BOLD" "$BINARY" "$NC"
  printf 'The server daemon is running on port 8420 with TLS.\n\n'
  printf 'To connect a client machine:\n'
  printf '  1. Install quarry on the client:\n'
  printf '     curl -fsSL <install.sh URL> | sh\n'
  printf '  2. Connect: quarry login <this-host> --api-key <your-api-key>\n\n'
  printf 'The CA fingerprint is shown above -- clients will see it during login.\n'
else
  printf '%b%b%s is ready!%b\n\n' "$GREEN" "$BOLD" "$PLUGIN_NAME" "$NC"
  # The messaging is gated on SKIP_PLUGIN, not on why we skipped: when no plugin
  # was installed, the "Restart Claude Code to activate the plugin" line is never
  # printed (it would be a lie).  The capability-absent auto-skip and the explicit
  # --no-plugin skip print the same CLI-only block; only the remedy branches on
  # SKIP_REASON so an auto-skip user is told to install the missing tool, not to
  # "re-run without --no-plugin" (which they never passed).
  if [ "$SKIP_PLUGIN" = "0" ]; then
    printf 'Restart Claude Code to activate the plugin.\n\n'
  else
    printf 'The quarry CLI is installed and works via the command line\n'
    printf '(quarry find / ingest / remember / doctor) and the stdio MCP server\n'
    printf '(quarry mcp).  Both talk to the resident quarryd daemon that quarry\n'
    printf 'install started above.  The Claude Code plugin was skipped.\n\n'
    case "$SKIP_REASON" in
      no-claude)
        printf 'The plugin was skipped because Claude Code was not found.  Install it\n'
        printf '(https://docs.anthropic.com/en/docs/claude-code), then re-run install.sh\n'
        printf 'to add the plugin.\n\n'
        ;;
      no-git)
        printf 'The plugin was skipped because git was not found (required to clone the\n'
        printf 'plugin).  Install git, then re-run install.sh to add the plugin.\n\n'
        ;;
      *)
        printf 'To add the plugin later, re-run install.sh without --no-plugin and with\n'
        printf 'QUARRY_NO_PLUGIN unset.\n\n'
        ;;
    esac
  fi
  printf 'Quick start:\n'
  if [ "$SKIP_PLUGIN" = "0" ]; then
    printf '  /find <query>                     # semantic search (in Claude Code)\n'
    printf '  /ingest <url>                     # index a webpage (in Claude Code)\n'
  fi
  printf '  quarry find "<query>"             # semantic search from the CLI\n'
  printf '  quarry ingest notes.md            # index a file from the CLI\n\n'
  printf 'To serve remote clients from this machine:\n'
  # shellcheck disable=SC2016
  printf '  export QUARRY_API_KEY=$(openssl rand -hex 32)\n'
  printf '  Re-run with --network flag.\n\n'
fi
