#!/usr/bin/env bash
# Test the built wheel in an isolated venv on port 8422.
#
# Verifies: import, onnxruntime providers, CLI entry point, doctor,
# serve + search round-trip.  All temp state lives in .tmp/ (gitignored).
#
# Usage: bash scripts/test-wheel.sh   (or: make test-wheel)

set -eu

cd "$(dirname "$0")/.."

# Isolate from production environment -- the test daemon must not inherit
# the production API key or host binding.
unset QUARRY_API_KEY 2>/dev/null || true
unset QUARRY_IP_ADDRESS 2>/dev/null || true

PORT=8422
VENV=.tmp/test-venv
DAEMON_PID=""
DAEMON_LOG=.tmp/test-wheel-daemon.log

cleanup() {
    if [ -n "$DAEMON_PID" ]; then
        kill "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    rm -rf "$VENV"
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
"$VENV/bin/quarry" --db test-wheel serve --port "$PORT" >"$DAEMON_LOG" 2>&1 &
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

# Search test -- query the search endpoint on the empty DB.
SEARCH_BODY=$(curl -s -w "\n%{http_code}" "http://127.0.0.1:$PORT/search?q=test&limit=1")
SEARCH_STATUS=$(echo "$SEARCH_BODY" | tail -1)
SEARCH_RESPONSE=$(echo "$SEARCH_BODY" | head -n -1)
if [ "$SEARCH_STATUS" != "200" ]; then
    fail "Search test" "HTTP $SEARCH_STATUS from /search (response: $SEARCH_RESPONSE)"
fi
echo "[test-wheel] Search test: PASS"

# Stop daemon.
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
DAEMON_PID=""

echo "[test-wheel] All checks passed."
