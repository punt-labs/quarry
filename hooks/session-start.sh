#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$HOME/.claude/settings.json"
COMMANDS_DIR="$HOME/.claude/commands"
# Derive tool pattern from plugin name (supports quarry-dev and quarry)
PLUGIN_NAME="$(python3 -c "import json, sys, pathlib; print(json.load(open(pathlib.Path(sys.argv[1]) / '.claude-plugin' / 'plugin.json'))['name'])" "$PLUGIN_ROOT")"
TOOL_PATTERN="mcp__plugin_${PLUGIN_NAME}_quarry__"

# Dev plugins skip prod commands and vice versa.
if [[ "$PLUGIN_NAME" == *"-dev"* ]]; then
  SUFFIX="-dev"
else
  SUFFIX=""
fi

ACTIONS=()

# ── Retire old commands ───────────────────────────────────────────────
# Commands removed or renamed in the CLI/MCP rework.
RETIRED=()
for old_name in "${RETIRED[@]}"; do
  dest="$COMMANDS_DIR/${old_name}.md"
  if [[ -f "$dest" ]]; then
    rm "$dest"
    ACTIONS+=("Retired /${old_name}")
  fi
done

# ── Deploy or update commands ─────────────────────────────────────────
DEPLOYED=()
UPDATED=()
for cmd_file in "$PLUGIN_ROOT/commands/"*.md; do
  name="$(basename "$cmd_file")"

  # Skip dev commands for prod plugin and vice versa.
  if [[ -n "$SUFFIX" ]]; then
    [[ "$name" != *"-dev.md" ]] && continue
  else
    [[ "$name" == *"-dev.md" ]] && continue
  fi

  dest="$COMMANDS_DIR/$name"
  mkdir -p "$COMMANDS_DIR"
  if [[ ! -f "$dest" ]]; then
    cp "$cmd_file" "$dest"
    DEPLOYED+=("/${name%.md}")
  elif ! diff -q "$cmd_file" "$dest" >/dev/null 2>&1; then
    cp "$cmd_file" "$dest"
    UPDATED+=("/${name%.md}")
  fi
done
if [[ ${#DEPLOYED[@]} -gt 0 ]]; then
  ACTIONS+=("Deployed commands: ${DEPLOYED[*]}")
fi
if [[ ${#UPDATED[@]} -gt 0 ]]; then
  ACTIONS+=("Updated commands: ${UPDATED[*]}")
fi

# ── Allow MCP tools in user settings if not already allowed ──────────
if command -v jq &>/dev/null && [[ -f "$SETTINGS" ]]; then
  if ! jq -e --arg pat "$TOOL_PATTERN" '.permissions.allow // [] | map(select(contains($pat))) | length > 0' "$SETTINGS" >/dev/null 2>&1; then
    TMPFILE="$(mktemp)"
    jq --arg pat "${TOOL_PATTERN}*" '.permissions.allow = (.permissions.allow // []) + [$pat]' "$SETTINGS" > "$TMPFILE"
    mv "$TMPFILE" "$SETTINGS"
    ACTIONS+=("Auto-allowed ${PLUGIN_NAME} MCP tools in permissions")
  fi
fi

# ── Notify Claude if anything was set up ─────────────────────────────
if [[ ${#ACTIONS[@]} -gt 0 ]]; then
  MSG="Quarry plugin first-run setup complete."
  for action in "${ACTIONS[@]}"; do
    MSG="$MSG $action."
  done
  cat <<ENDJSON
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "$MSG"
  }
}
ENDJSON
fi

exit 0
