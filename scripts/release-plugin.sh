#!/usr/bin/env bash
set -euo pipefail

# Prepare plugin for release: swap name to prod, remove -dev commands.
# The tagged commit has only prod artifacts; the marketplace cache clones from it.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The shippable plugin surface lives under plugin/ so a git-subdir marketplace
# install fetches only that subtree; the plugin root is ${REPO_ROOT}/plugin.
PLUGIN_JSON="${REPO_ROOT}/plugin/.claude-plugin/plugin.json"
COMMANDS_DIR="${REPO_ROOT}/plugin/commands"

# Swap plugin name from *-dev to prod
current_name="$(python3 -c "import json; print(json.load(open('${PLUGIN_JSON}'))['name'])")"
prod_name="${current_name%-dev}"

if [[ "$current_name" == "$prod_name" ]]; then
  echo "Plugin name is already '${prod_name}' (no -dev suffix)" >&2
  exit 1
fi

echo "Swapping plugin name: ${current_name} → ${prod_name}"
python3 -c "
import json, pathlib
p = pathlib.Path('${PLUGIN_JSON}')
d = json.loads(p.read_text())
d['name'] = '${prod_name}'
p.write_text(json.dumps(d, indent=2) + '\n')
"

git -C "$REPO_ROOT" add "$PLUGIN_JSON"

# Remove -dev commands (if any exist).
#
# The find runs in a process substitution, whose exit status `set -e` does not
# see. A wrong or missing COMMANDS_DIR would therefore leave dev_files empty and
# fall through to the "name swap only" branch — shipping a prod release with the
# *-dev commands still in it, silently. Assert the directory up front, and let
# find's stderr through rather than discarding it.
if [[ ! -d "$COMMANDS_DIR" ]]; then
  echo "error: $COMMANDS_DIR not found" >&2
  exit 1
fi

dev_files=()
while IFS= read -r -d '' f; do
  dev_files+=("$f")
done < <(find "$COMMANDS_DIR" -name '*-dev.md' -print0)

if [[ ${#dev_files[@]} -gt 0 ]]; then
  for f in "${dev_files[@]}"; do
    echo "Removing: $(basename "$f")"
  done
  git -C "$REPO_ROOT" rm "${dev_files[@]}"
fi

# The org bans --no-verify; hooks run against the release-prep commit like
# any other.
git -C "$REPO_ROOT" commit -m "chore: prepare plugin for release"
