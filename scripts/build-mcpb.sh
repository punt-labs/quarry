#!/usr/bin/env bash
# Build the .mcpb Desktop Extension bundle for Claude Desktop.
#
# The bundle manifest is GENERATED at build time into a staging directory and
# packed from there. It is never written into the tree: a manifest.json beside
# the plugin manifest is preferred by the plugin marketplace over
# plugin/.claude-plugin/plugin.json and strips the installed plugin's slash
# commands (removed in b2c9ffb for exactly that reason). Keeping the manifest out
# of the tree is load-bearing.
#
# The manifest's version is the single source of truth from pyproject.toml; all
# other fields come from scripts/mcpb-manifest.template.json. The mcp_config
# mirrors how `quarry install` wires Claude Desktop today: the installed `quarry`
# binary running `quarry mcp` (a stdio client of the daemon, DES-031 v2.2) — no
# uv-from-source, no mcp-proxy shim.
#
# Prerequisites: npm install -g @anthropic-ai/mcpb
#
# Usage: ./scripts/build-mcpb.sh
#
# Output: dist/punt-quarry-<version>.mcpb  (versioned)
#         dist/punt-quarry.mcpb            (stable name for latest/download)

set -euo pipefail

cd "$(dirname "$0")/.."

readonly TEMPLATE="scripts/mcpb-manifest.template.json"
readonly STAGING="dist/mcpb-staging"

if ! command -v mcpb >/dev/null 2>&1; then
    echo "ERROR: mcpb not found. Install with: npm install -g @anthropic-ai/mcpb" >&2
    exit 1
fi

# Version is sourced from pyproject.toml — the one source of truth (PL-DI-5).
version=$(python3 -c "
import re
text = open('pyproject.toml').read()
m = re.search(r'^version\s*=\s*\"(.+?)\"', text, re.MULTILINE)
print(m.group(1) if m else '')
")

if [ -z "$version" ]; then
    echo "ERROR: Could not find version in pyproject.toml" >&2
    exit 1
fi

# Validate the version's filename-safe shape, including Python prereleases
# such as 1.2.3rc1 as well as separated suffixes. This is filename
# hygiene, NOT injection defense: bash does not re-evaluate a $() inside a
# variable's stored value, so a parsed version like "1.0.0$(id)" is used
# literally, never executed. The guard keeps the derived artifact names
# (dist/punt-quarry-<version>.mcpb) predictable — no path separators, spaces, or
# shell metacharacters leaking a malformed pyproject version into a filename.
# Fail closed on anything that is not digits/dots with an optional
# pre-release/build suffix.
readonly VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+(([aAbB]|[rR][cC])[0-9]+)?([.-][A-Za-z0-9.]+)?$'
if ! [[ "$version" =~ $VERSION_RE ]]; then
    echo "ERROR: version '$version' from pyproject.toml is not a supported artifact version — refusing to build" >&2
    exit 1
fi

echo "Building punt-quarry $version .mcpb bundle..."

# Stage a clean directory holding only the generated manifest. Packing from this
# isolated dir (not the repo root) is what keeps manifest.json out of the tree
# and gives a deterministic, minimal bundle.
rm -rf "$STAGING"
mkdir -p "$STAGING"

# Render the template: substitute the version placeholder and re-serialize via
# json so a malformed template or a stray placeholder fails the build loudly.
VERSION="$version" MANIFEST_TEMPLATE="$TEMPLATE" python3 -c "
import json, os
template = json.load(open(os.environ['MANIFEST_TEMPLATE']))
if template.get('version') != '__VERSION__':
    raise SystemExit('ERROR: template version placeholder missing or changed')
template['version'] = os.environ['VERSION']
json.dump(template, open('$STAGING/manifest.json', 'w'), indent=2)
open('$STAGING/manifest.json', 'a').write('\n')
"

# Validate the generated manifest before packing — a schema failure here is far
# cheaper than a broken bundle discovered at double-click time.
mcpb validate "$STAGING/manifest.json"

mkdir -p dist
mcpb pack "$STAGING" "dist/punt-quarry-${version}.mcpb"

# Stable-named copy for the GitHub release (releases/latest/download/punt-quarry.mcpb).
cp "dist/punt-quarry-${version}.mcpb" "dist/punt-quarry.mcpb"

echo "Built: dist/punt-quarry-${version}.mcpb"
echo "       dist/punt-quarry.mcpb (stable name)"
echo "Size: $(du -h "dist/punt-quarry-${version}.mcpb" | cut -f1)"
