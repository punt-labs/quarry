#!/usr/bin/env bash
# Format quarry MCP tool output for the UI panel.
#
# updatedMCPToolOutput sets the text displayed in the tool-result panel.
# additionalContext passes the full tool data to the model separately,
# so the model can emit tables while the panel stays compact.
#
# tool_response arrives as a JSON-encoded STRING, not an object.
# We must parse it twice: once to extract the string, once to
# read the .result field inside it.
#
# Supports both prod (mcp__plugin_quarry_quarry__*) and dev
# (mcp__plugin_quarry-dev_quarry__*) tool prefixes by extracting the
# bare tool name via ${TOOL##*__}.
#
# Note: no `set -euo pipefail` — hooks must degrade gracefully on
# malformed input rather than failing the tool call. Matches biff's
# approach. Panel line counts for data tools are approximate when
# table cells wrap across multiple lines.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name')
TOOL_NAME="${TOOL##*__}"
RESULT=$(echo "$INPUT" | jq -r '.tool_response' | jq -r '.result // .')

# Data tools: summary in panel, full output via additionalContext.

if [[ "$TOOL_NAME" == "search_documents" ]]; then
  FIRST_LINE=$(printf '%s' "$RESULT" | head -1)
  if [[ "$FIRST_LINE" == "No results"* ]]; then
    jq -n --arg r "$FIRST_LINE" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    jq -n --arg summary "$FIRST_LINE" --arg ctx "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $summary,
        additionalContext: $ctx
      }
    }'
  fi
  exit 0
fi

if [[ "$TOOL_NAME" == "get_documents" ]]; then
  if [[ "$RESULT" == "No documents" ]]; then
    jq -n --arg r "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    COUNT=$(printf '%s\n' "$RESULT" | tail -n +2 | wc -l | tr -d ' ')
    jq -n --arg summary "${COUNT} documents" --arg ctx "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $summary,
        additionalContext: $ctx
      }
    }'
  fi
  exit 0
fi

if [[ "$TOOL_NAME" == "list_collections" ]]; then
  if [[ "$RESULT" == "No collections" ]]; then
    jq -n --arg r "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    COUNT=$(printf '%s\n' "$RESULT" | tail -n +2 | wc -l | tr -d ' ')
    jq -n --arg summary "${COUNT} collections" --arg ctx "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $summary,
        additionalContext: $ctx
      }
    }'
  fi
  exit 0
fi

if [[ "$TOOL_NAME" == "list_databases" ]]; then
  if [[ "$RESULT" == "No databases" ]]; then
    jq -n --arg r "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    COUNT=$(printf '%s\n' "$RESULT" | tail -n +2 | wc -l | tr -d ' ')
    jq -n --arg summary "${COUNT} databases" --arg ctx "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $summary,
        additionalContext: $ctx
      }
    }'
  fi
  exit 0
fi

if [[ "$TOOL_NAME" == "list_registrations" ]]; then
  if [[ "$RESULT" == "No registered directories" ]]; then
    jq -n --arg r "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    COUNT=$(printf '%s\n' "$RESULT" | tail -n +2 | wc -l | tr -d ' ')
    jq -n --arg summary "${COUNT} registrations" --arg ctx "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $summary,
        additionalContext: $ctx
      }
    }'
  fi
  exit 0
fi

if [[ "$TOOL_NAME" == "status" ]]; then
  jq -n --arg summary "quarry status" --arg ctx "$RESULT" '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      updatedMCPToolOutput: $summary,
      additionalContext: $ctx
    }
  }'
  exit 0
fi

if [[ "$TOOL_NAME" == "get_page" ]]; then
  FIRST_LINE=$(printf '%s' "$RESULT" | head -1)
  if [[ "$FIRST_LINE" == "No data found"* ]]; then
    jq -n --arg r "$FIRST_LINE" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    DOC=$(printf '%s' "$RESULT" | head -1 | sed 's/Document: //')
    PAGE=$(printf '%s' "$RESULT" | sed -n '2p' | sed 's/Page: //')
    jq -n --arg summary "${DOC} page ${PAGE}" --arg ctx "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $summary,
        additionalContext: $ctx
      }
    }'
  fi
  exit 0
fi

# Action tools and fallback: full output in panel only.
jq -n --arg r "$RESULT" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    updatedMCPToolOutput: $r
  }
}'
