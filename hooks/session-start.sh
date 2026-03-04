#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$HOME/.claude/settings.json"
COMMANDS_DIR="$HOME/.claude/commands"
# Derive tool pattern from plugin name (supports quarry-dev and quarry)
PLUGIN_NAME="$(python3 -c "import json; print(json.load(open('${PLUGIN_ROOT}/.claude-plugin/plugin.json'))['name'])")"
TOOL_PATTERN="mcp__plugin_${PLUGIN_NAME}_quarry__"

ACTIONS=()

# ── Deploy top-level commands if missing ──────────────────────────────
DEPLOYED=()
for cmd_file in "$PLUGIN_ROOT/commands/"*.md; do
  name="$(basename "$cmd_file")"
  dest="$COMMANDS_DIR/$name"
  if [[ ! -f "$dest" ]]; then
    mkdir -p "$COMMANDS_DIR"
    cp "$cmd_file" "$dest"
    DEPLOYED+=("/${name%.md}")
  fi
done
if [[ ${#DEPLOYED[@]} -gt 0 ]]; then
  ACTIONS+=("Deployed commands: ${DEPLOYED[*]}")
fi

# ── Allow MCP tools in user settings if not already allowed ──────────
if command -v jq &>/dev/null && [[ -f "$SETTINGS" ]]; then
  if ! jq -e ".permissions.allow // [] | map(select(contains(\"$TOOL_PATTERN\"))) | length > 0" "$SETTINGS" >/dev/null 2>&1; then
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
