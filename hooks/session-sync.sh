#!/usr/bin/env bash
# SessionStart: auto-register and sync the current repo with quarry.
# The sync runs in the background so it never blocks session start.
# Static context is returned immediately; sync results are not surfaced.
quarry hooks session-start >/dev/null 2>/dev/null &
disown 2>/dev/null || true
cat <<'ENDJSON'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Quarry semantic search is active for this project. Use quarry MCP tools to search this codebase semantically."
  }
}
ENDJSON
