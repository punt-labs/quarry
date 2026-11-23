#!/usr/bin/env bash
# Install and exercise a release wheel without using the checkout or a local daemon.
# Usage: bash scripts/smoke-release.sh candidate VERSION WHEEL [MCPB]
#        bash scripts/smoke-release.sh published VERSION [MCPB]
set -euo pipefail

usage() {
  printf '%s\n' 'Usage: smoke-release.sh candidate VERSION WHEEL [MCPB] | published VERSION [MCPB]'
}

fail() {
  printf 'Quarry %s smoke failed (%s): %s\n' "$channel" "$version" "$*" >&2
  exit 1
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
version=${2:-}
bundle_arg=""
case "${1:-}:$#" in
  candidate:3|candidate:4)
    channel="candidate wheel"
    wheel_dir=$(cd -- "$(dirname -- "$3")" && pwd -P) || fail "Cannot resolve the candidate wheel path"
    package="$wheel_dir/$(basename -- "$3")"
    [[ -f "$package" && "$package" == *.whl ]] || fail "Expected an existing wheel"
    bundle_arg=${4:-}
    ;;
  candidate:*)
    channel="candidate wheel"
    fail "Pass exactly one candidate wheel"
    ;;
  published:2|published:3)
    channel="production PyPI"
    package="punt-quarry==$version"
    bundle_arg=${3:-}
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
bundle=""
if [[ -n "$bundle_arg" ]]; then
  bundle_dir=$(cd -- "$(dirname -- "$bundle_arg")" && pwd -P) || fail "Cannot resolve the bundle path"
  bundle="$bundle_dir/$(basename -- "$bundle_arg")"
  [[ -f "$bundle" && "$bundle" == *.mcpb ]] || fail "Expected an existing MCPB"
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
fi
[[ "$version" =~ ^[0-9][0-9A-Za-z.!+_-]*$ ]] || fail "Expected a PEP 440 version without its v prefix"
printf 'Testing punt-quarry %s from %s\n' "$version" "$channel"
model_cache=${QUARRY_SMOKE_MODEL_CACHE:-}
if [[ -n "$model_cache" && ( "$model_cache" != /* || ! -d "$model_cache" ) ]]; then
  fail "QUARRY_SMOKE_MODEL_CACHE must be an absolute path to an existing directory"
fi

work=$(mktemp -d)
daemon_pid=""
cleanup() {
  local result=$1
  local second
  if [[ -n "$daemon_pid" ]]; then
    kill "$daemon_pid" 2>/dev/null || true
    for ((second = 0; second < 10; second++)); do
      kill -0 "$daemon_pid" 2>/dev/null || break
      sleep 1
    done
    if ((second == 10)); then
      kill -KILL "$daemon_pid" 2>/dev/null || true
    fi
    wait "$daemon_pid" 2>/dev/null || true
  fi
  if [[ "$result" != 0 && -f "$work/daemon.log" ]]; then
    tail -30 "$work/daemon.log" >&2
  fi
  rm -rf -- "$work"
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$work"
uv_bin=$(command -v uv)
clean=(env -i "PATH=$PATH" "HOME=$HOME")
"${clean[@]}" "$uv_bin" --no-config venv --python 3.13 venv
python="$work/venv/bin/python"
install=("${clean[@]}" "$uv_bin" --no-config pip install --python "$python" --default-index https://pypi.org/simple/)
if [[ "$channel" == "production PyPI" ]]; then
  for attempt in {1..5}; do
    if "${install[@]}" --refresh-package punt-quarry --only-binary punt-quarry "$package" packaging; then
      break
    fi
    [[ "$attempt" != 5 ]] || fail "Could not install the tagged version from PyPI"
    sleep 15
  done
else
  "${install[@]}" "$package" packaging
fi

mkdir "$work/home"
isolated=(env -i "PATH=$work/venv/bin:/usr/bin:/bin" "HOME=$work/home"
  "QUARRY_ROOT=$work/data" "QUARRY_PROVIDER=cpu" "PUNT_TELEMETRY=off"
  "HF_HUB_CACHE=${model_cache:-$work/hub}"
  "HF_HUB_DISABLE_IMPLICIT_TOKEN=1" "HF_HUB_DISABLE_TELEMETRY=1")
actual=$("${isolated[@]}" "$work/venv/bin/quarry" version) || fail "Installed quarry version command failed"
"$python" -I -c 'import sys; from packaging.version import Version; sys.exit(Version(sys.argv[1]) != Version(sys.argv[2]))' \
  "$actual" "$version" || fail "Expected installed CLI version $version, got $actual"
if [[ -z "$model_cache" ]]; then
  # Provision the model pinned by the installed version before going offline.
  "${isolated[@]}" "$python" -I -c \
    'from quarry.embeddings import OnnxEmbeddingBackend; OnnxEmbeddingBackend.download_model_files()' ||
    fail "Could not provision the installed version's embedding model"
fi
isolated+=("HF_HUB_OFFLINE=1")
cli=("${isolated[@]}" "$work/venv/bin/quarry")

"${isolated[@]}" "$work/venv/bin/quarryd" --host 127.0.0.1 --port 0 >"$work/daemon.log" 2>&1 &
daemon_pid=$!
port_file="$work/data/default/serve.port"
ready=0
for ((second = 0; second < 40; second++)); do
  kill -0 "$daemon_pid" 2>/dev/null || fail "Installed quarryd exited during startup"
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
[[ "$ready" == 1 ]] || fail "Installed quarryd did not become ready"

name="release-canary.md"
body="The release canary lives in the blue observatory."
printf '%s\n' "$body" | "${cli[@]}" --json remember --name "$name" --collection release-smoke >"$work/remember.json" ||
  fail "Installed CLI could not submit a document for indexing"
"${isolated[@]}" "$python" -I - "$work/remember.json" <<'PY'
import json
import sys
import time
from pathlib import Path

from quarry import TargetResolver

accepted = json.loads(Path(sys.argv[1]).read_bytes())
client = TargetResolver.connect()
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    status = client.task_status(accepted["task_id"])
    if status.status == "completed":
        sys.exit(0)
    if status.status == "failed":
        sys.exit(f"Quarry remember task failed: {status.error}")
    time.sleep(0.5)
sys.exit("Quarry remember task did not finish")
PY
"${cli[@]}" --json find --collection release-smoke --limit 1 "blue observatory" >"$work/hits.json" ||
  fail "Installed CLI search failed"
if "$python" -I - "$work/hits.json" "$name" "$body" <<'PY'
import json
import sys
from pathlib import Path

path, name, body = sys.argv[1:]
hits = json.loads(Path(path).read_text())
if not isinstance(hits, list):
    raise ValueError("Expected a JSON result list")
sys.exit(0 if any(hit["document_name"] == name and body in hit["text"] for hit in hits) else 3)
PY
then
  printf 'PASS: punt-quarry %s from %s (installed CLI remember → find)\n' "$version" "$channel"
else
  if [[ "$?" != 3 ]]; then
    fail "Installed CLI returned invalid search results"
  fi
  fail "Search did not return the indexed document and its contents"
fi
if [[ -n "$bundle" ]]; then
  "${isolated[@]}" "$python" -I "$script_dir/smoke-mcpb.py" "$bundle" "$version" "$body"
  printf 'PASS: Quarry MCPB %s (manifest-launched MCP find)\n' "$version"
fi
if [[ "${QUARRY_SMOKE_LIVE:-0}" == 1 ]]; then
  "${isolated[@]}" "$python" -I - <<'PY'
from quarry.http_url import HttpUrl
from quarry.ingestion.web_fetch import WebFetcher

page = WebFetcher(timeout=10).fetch_body(HttpUrl("https://example.com/"))
if page.media_type != "text/html" or "Example Domain" not in page.text:
    raise SystemExit("Public HTTPS canary did not return the expected page")
PY
  printf 'PASS: punt-quarry %s (public HTTPS fetch)\n' "$version"
fi
