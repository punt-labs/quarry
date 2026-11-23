#!/usr/bin/env bash
set -euo pipefail
python=$1
cli=$2
name="wheel-canary.md"
body="The wheel canary lives in the blue observatory."
query=$3

printf '%s\n' "$body" | "$cli" --json remember --name "$name" --collection wheel-smoke >remember.json
"$python" -I - remember.json <<'PY'
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
        sys.exit(f"Wheel smoke failed: remember task failed: {status.error}")
    time.sleep(0.5)
sys.exit("Wheel smoke failed: remember task did not finish")
PY

"$cli" --json find --collection wheel-smoke --limit 1 "$query" >hits.json
if "$python" -I - "$name" "$body" hits.json <<'PY'
import json
import sys
from pathlib import Path

name, body, path = sys.argv[1:]
hits = json.loads(Path(path).read_bytes())
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
printf 'Wheel smoke failed: Search did not return the indexed document and its contents\n' >&2
exit 1
