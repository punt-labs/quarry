#!/usr/bin/env bash
# Test the built wheel in an isolated venv on port 8422.
#
# Verifies import, ONNX Runtime, the CLI, a daemon search, and the shared
# diagnostics and telemetry output. The sandbox uses its own home and data
# root under .tmp/ so it does not touch the operator's real Quarry state.
#
# Usage: bash scripts/test-wheel.sh   (or: make test-wheel)

set -eu

cd "$(dirname "$0")/.."

# Isolate from production environment -- the test daemon must not inherit
# the production API key or host binding.
unset QUARRY_API_KEY 2>/dev/null || true
unset QUARRY_IP_ADDRESS 2>/dev/null || true
unset PUNT_TELEMETRY QUARRY_TELEMETRY TELEMETRY_ENABLED 2>/dev/null || true

PORT=8422
VENV=.tmp/test-venv
DAEMON_PID=""
DAEMON_LOG=.tmp/test-wheel-daemon.log
# Keep the embedding cache while isolating diagnostics and databases.
export HF_HOME="${HF_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/huggingface}"
TEST_HOME=$PWD/.tmp/test-wheel-home
CACHE_DIR="${UV_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/uv}"
mkdir -p "$TEST_HOME"
export UV_CACHE_DIR="$CACHE_DIR"
export HOME="$TEST_HOME"
DATA_ROOT=$PWD/.tmp/test-wheel-data
export QUARRY_ROOT="$DATA_ROOT"

cleanup() {
    if [ -n "$DAEMON_PID" ]; then
        kill "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    rm -rf "$VENV" "$DATA_ROOT" "$TEST_HOME"
    echo "[test-wheel] Cleanup: done"
}
trap cleanup EXIT

fail() {
    echo "[test-wheel] $1: FAIL -- $2" >&2
    if [ -f "$DAEMON_LOG" ]; then
        echo "[test-wheel] Daemon log (last 20 lines):" >&2
        tail -20 "$DAEMON_LOG" >&2
    fi
    exit 1
}

# Extract expected version from pyproject.toml.
EXPECTED_VERSION=$(python3 -c "
import re, pathlib
text = pathlib.Path('pyproject.toml').read_text()
m = re.search(r'^version\s*=\s*\"(.+?)\"', text, re.MULTILINE)
print(m.group(1))
")

# Step 1: Build wheel.
echo "[test-wheel] Building wheel..."
rm -rf dist/ "$VENV"
uv build --quiet

# Step 2: Create isolated venv and install wheel.
echo "[test-wheel] Installing in isolated venv..."
uv venv "$VENV" --quiet
uv pip install --quiet --python "$VENV/bin/python" dist/punt_quarry-*.whl

# Step 3a: Import check.
IMPORT_VERSION=$("$VENV/bin/python" -c "import quarry; print(quarry.__version__)")
if [ "$IMPORT_VERSION" != "$EXPECTED_VERSION" ]; then
    fail "Import check" "got $IMPORT_VERSION, expected $EXPECTED_VERSION"
fi
echo "[test-wheel] Import check: PASS ($IMPORT_VERSION)"

# Step 3b: ORT providers.
ORT_PROVIDERS=$("$VENV/bin/python" -c "import onnxruntime as ort; print(','.join(ort.get_available_providers()))")
if [ -z "$ORT_PROVIDERS" ]; then
    fail "ORT providers" "no providers returned"
fi
echo "[test-wheel] ORT providers: PASS ($ORT_PROVIDERS)"

# Step 3c: CLI version.
CLI_VERSION=$("$VENV/bin/quarry" version 2>&1) || fail "CLI version" "quarry version exited non-zero: $CLI_VERSION"
if [ "$CLI_VERSION" != "$EXPECTED_VERSION" ]; then
    fail "CLI version" "got '$CLI_VERSION', expected '$EXPECTED_VERSION'"
fi
echo "[test-wheel] CLI version: PASS ($CLI_VERSION)"

# Step 3d: Doctor (best-effort -- some checks will fail in isolation).
if "$VENV/bin/quarry" doctor >/dev/null 2>&1; then
    echo "[test-wheel] Doctor: PASS"
else
    echo "[test-wheel] Doctor: WARN (non-zero exit in isolated venv, expected)"
fi

# Step 3e: Serve on :8422 + API round-trip.
echo "[test-wheel] Starting daemon on :$PORT..."
"$VENV/bin/quarryd" --db test-wheel --port "$PORT" >"$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!

# Wait for health endpoint (up to 30 seconds).
HEALTH_OK=0
for _i in $(seq 1 30); do
    if curl -s "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 1
done

if [ "$HEALTH_OK" -ne 1 ]; then
    fail "Serve on :$PORT" "health endpoint not reachable after 30s"
fi
echo "[test-wheel] Serve on :$PORT: PASS"

# The daemon requires serve.token on EVERY request, loopback included, and mints
# a fresh one per start (DES-031 v2.2 R4).  /health is the only unauthenticated
# route, so the search round-trip has to present the live bearer -- read from the
# run dir the daemon just wrote it to, never from a stored config.
TOKEN_FILE="$DATA_ROOT/test-wheel/serve.token"
[ -f "$TOKEN_FILE" ] || fail "Serve token" "daemon minted no token at $TOKEN_FILE"
TOKEN=$(cat "$TOKEN_FILE")

# Search test -- query the search endpoint on the empty DB.  The bearer goes in
# on stdin via --config, never on curl's argv: an argument is readable through
# ps by any local user for the lifetime of the request.
SEARCH_BODY=$(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
    | curl -s -w "\n%{http_code}" --config - \
        "http://127.0.0.1:$PORT/v1/search?q=quarry-wheel-smoke-query&limit=1")
SEARCH_STATUS=$(echo "$SEARCH_BODY" | tail -1)
SEARCH_RESPONSE=$(echo "$SEARCH_BODY" | sed '$d')  # portable 'all but last'
if [ "$SEARCH_STATUS" != "200" ]; then
    fail "Search test" "HTTP $SEARCH_STATUS from /v1/search (response: $SEARCH_RESPONSE)"
fi
echo "[test-wheel] Search test: PASS"

# Stop daemon.
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
DAEMON_PID=""

OBSERVE_ROOT="$TEST_HOME/.punt-labs/quarry"
grep -q 'Quarry server listening' "$OBSERVE_ROOT/logs/quarry.log" \
    || fail "Diagnostic log" "daemon startup record did not reach the shared log"
grep -q 'Server stopped' "$OBSERVE_ROOT/logs/quarry.log" \
    || fail "Diagnostic log" "daemon stop record did not reach the shared log"
"$VENV/bin/python" - "$OBSERVE_ROOT/telemetry.db" <<'CHECK'
import json
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as conn:
    (raw,) = conn.execute(
        "SELECT attributes FROM spans WHERE name = 'quarry.daemon.search'"
    ).fetchone()
    attrs = json.loads(raw)
    assert attrs["query_length"] == len("quarry-wheel-smoke-query")
    assert attrs["result_count"] == 0
    assert "quarry-wheel-smoke-query" not in raw
CHECK
echo "[test-wheel] Durable diagnostics and content-free telemetry: PASS"
echo "[test-wheel] All checks passed."
