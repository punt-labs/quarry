#!/usr/bin/env bash
# Build once, then drive the installed wheel from a disposable local daemon.
set -euo pipefail
cd "$(dirname "$0")/.."
work=$(mktemp -d)
daemon_pid=""

stop_daemon() {
  local second
  [[ -n "$daemon_pid" ]] || return 0
  kill "$daemon_pid" 2>/dev/null || true
  for ((second = 0; second < 10; second++)); do
    kill -0 "$daemon_pid" 2>/dev/null || break
    sleep 1
  done
  if ((second == 10)); then
    kill -KILL "$daemon_pid" 2>/dev/null || true
  fi
  wait "$daemon_pid" 2>/dev/null || true
  daemon_pid=""
}

cleanup() {
  local result=$1
  stop_daemon
  if [[ "$result" != 0 && -f "$work/daemon.log" ]]; then
    tail -60 "$work/daemon.log" >&2
  fi
  rm -rf -- "$work"
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  printf 'Wheel smoke failed: %s\n' "$*" >&2
  exit 1
}

uv_bin=$(command -v uv)
clean=(env -i "PATH=$PATH" "HOME=$HOME")
"${clean[@]}" "$uv_bin" --no-config build --python 3.13 --wheel --out-dir "$work/dist"
wheel=("$work"/dist/punt_quarry-*.whl)
[[ ${#wheel[@]} == 1 && -f "${wheel[0]}" ]] || fail "Expected exactly one wheel"
version=$("$uv_bin" --no-config run --no-project --python 3.13 python -c \
  'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')

cd "$work"
"${clean[@]}" "$uv_bin" --no-config venv --python 3.13 venv
python="$work/venv/bin/python"
"${clean[@]}" "$uv_bin" --no-config pip install --python "$python" "${wheel[0]}" packaging

mkdir home
model_cache=${QUARRY_SMOKE_MODEL_CACHE:-$work/hub}
[[ "$model_cache" == /* ]] || fail "QUARRY_SMOKE_MODEL_CACHE must be an absolute path"
runtime=(env -i "PATH=$work/venv/bin:/usr/bin:/bin" "HOME=$work/home"
  "QUARRY_ROOT=$work/data" "QUARRY_PROVIDER=cpu" "PUNT_TELEMETRY_EXPORTER=sqlite"
  "HF_HUB_CACHE=$model_cache" "HF_HUB_DISABLE_IMPLICIT_TOKEN=1" "HF_HUB_DISABLE_TELEMETRY=1")

"${runtime[@]}" "$python" -I - "$version" <<'PY'
import sys

import onnxruntime
import quarry
from packaging.version import Version

assert Version(quarry.__version__) == Version(sys.argv[1])
assert "CPUExecutionProvider" in onnxruntime.get_available_providers()
PY
cli="$work/venv/bin/quarry"
cli_version=$("${runtime[@]}" "$cli" version) || fail "Version command failed"
"$python" -I -c 'import sys; from packaging.version import Version; assert Version(sys.argv[1]) == Version(sys.argv[2])' \
  "$cli_version" "$version" || fail "Expected CLI version $version, got $cli_version"

offline=0
if [[ -n "${QUARRY_SMOKE_MODEL_CACHE:-}" ]]; then
  [[ -d "$model_cache" ]] || fail "Model cache is not a directory"
  offline=1
fi
"${runtime[@]}" "HF_HUB_OFFLINE=$offline" "$python" -I -c \
  'from quarry.embeddings import OnnxEmbeddingBackend; OnnxEmbeddingBackend.download_model_files()' ||
  fail "Could not provision the pinned ONNX model"
runtime+=("HF_HUB_OFFLINE=1")
"${runtime[@]}" "$work/venv/bin/quarryd" --host 127.0.0.1 --port 0 >daemon.log 2>&1 &
daemon_pid=$!
port_file="$work/data/default/serve.port"
ready=0
for ((second = 0; second < 40; second++)); do
  kill -0 "$daemon_pid" 2>/dev/null || fail "Daemon exited during startup"
  if [[ -s "$port_file" ]]; then
    port=$(cat "$port_file")
    if [[ "$port" =~ ^[0-9]+$ ]] &&
      health=$(curl --noproxy '*' --fail --silent --max-time 1 "http://127.0.0.1:$port/health") &&
      [[ "$health" == *'"state":"ready"'* ]]; then
      ready=1
      break
    fi
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || fail "Daemon did not become ready"

name="wheel-canary.md"
body="The wheel canary lives in the blue observatory."
query="blue observatory"
printf '%s\n' "$body" | "${runtime[@]}" "$cli" remember --name "$name" --collection wheel-smoke
found=0
for ((attempt = 1; attempt <= 30; attempt++)); do
  "${runtime[@]}" "$cli" --json find --collection wheel-smoke --limit 1 "$query" >hits.json
  if "$python" -I - "$name" "$body" <<'PY'
import json
import sys
from pathlib import Path

name, body = sys.argv[1:]
hits = json.loads(Path("hits.json").read_text())
if not isinstance(hits, list):
    raise ValueError("Expected a JSON result list")
sys.exit(0 if any(hit["document_name"] == name and body in hit["text"] for hit in hits) else 3)
PY
  then
    found=1
    break
  elif [[ "$?" != 3 ]]; then
    fail "Invalid search results"
  fi
  sleep 1
done
[[ "$found" == 1 ]] || fail "Search did not return the indexed document and its contents"
stop_daemon

observe="$work/home/.punt-labs/quarry"
grep -q 'Quarry server listening' "$observe/logs/quarry.log" || fail "Missing startup diagnostic"
grep -q 'Server stopped' "$observe/logs/quarry.log" || fail "Missing shutdown diagnostic"
"$python" -I - "$observe/telemetry.db" "$query" <<'PY'
import json
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as conn:
    rows = conn.execute(
        "SELECT attributes FROM spans WHERE name = 'quarry.daemon.search'"
    ).fetchall()
    assert rows, "No search spans"
    for (raw,) in rows:
        attrs = json.loads(raw)
        assert attrs["query_length"] == len(sys.argv[2])
        assert sys.argv[2] not in raw
    assert any(json.loads(raw)["result_count"] == 1 for (raw,) in rows)
PY
printf 'PASS: punt-quarry %s wheel (remember → find, diagnostics, telemetry)\n' "$version"
