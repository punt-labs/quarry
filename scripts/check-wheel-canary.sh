#!/usr/bin/env bash
set -euo pipefail
python=$1
cli=$2
name="wheel-canary.md"
body="The wheel canary lives in the blue observatory."
query=$3

printf '%s\n' "$body" | "$cli" remember --name "$name" --collection wheel-smoke
for ((attempt = 1; attempt <= 30; attempt++)); do
  hits=$("$cli" --json find --collection wheel-smoke --limit 1 "$query")
  if "$python" -I - "$name" "$body" "$hits" <<'PY'
import json
import sys

name, body, raw = sys.argv[1:]
hits = json.loads(raw)
if not isinstance(hits, list):
    raise ValueError("Expected a JSON result list")
for hit in hits:
    if not isinstance(hit, dict) or not all(
        isinstance(hit.get(key), str) for key in ("document_name", "text")
    ):
        raise ValueError("Expected a named text result")
sys.exit(0 if any(hit["document_name"] == name and body in hit["text"] for hit in hits) else 3)
PY
  then
    exit 0
  elif [[ "$?" != 3 ]]; then
    printf 'Wheel smoke failed: Invalid search results\n' >&2
    exit 1
  fi
  sleep 1
done
printf 'Wheel smoke failed: Search did not return the indexed document and its contents\n' >&2
exit 1
