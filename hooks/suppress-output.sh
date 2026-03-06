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

if [[ "$TOOL_NAME" == "find" ]]; then
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

if [[ "$TOOL_NAME" == "list" ]]; then
  FIRST_LINE=$(printf '%s' "$RESULT" | head -1)
  # Empty results for each kind
  if [[ "$RESULT" == "No documents" ]] || \
     [[ "$RESULT" == "No collections" ]] || \
     [[ "$RESULT" == "No databases" ]] || \
     [[ "$RESULT" == "No registered directories" ]] || \
     [[ "$FIRST_LINE" == "Error:"* ]]; then
    jq -n --arg r "$RESULT" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    COUNT=$(printf '%s\n' "$RESULT" | tail -n +2 | wc -l | tr -d ' ')
    # Detect kind from first column in header (after ▶ prefix)
    LABEL="items"
    HEADER="${FIRST_LINE#*▶  }"
    if [[ "$HEADER" == DIRECTORY* ]]; then LABEL="registrations"
    elif [[ "$HEADER" == DATABASE* ]]; then LABEL="databases"
    elif [[ "$HEADER" == DOCUMENT\ * ]]; then LABEL="documents"
    elif [[ "$HEADER" == COLLECTION* ]]; then LABEL="collections"
    fi
    jq -n --arg summary "${COUNT} ${LABEL}" --arg ctx "$RESULT" '{
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

if [[ "$TOOL_NAME" == "show" ]]; then
  FIRST_LINE=$(printf '%s' "$RESULT" | head -1)
  if [[ "$FIRST_LINE" == "No data found"* ]] || [[ "$FIRST_LINE" == "Document"*"not found"* ]]; then
    jq -n --arg r "$FIRST_LINE" '{
      hookSpecificOutput: {
        hookEventName: "PostToolUse",
        updatedMCPToolOutput: $r
      }
    }'
  else
    DOC=$(printf '%s' "$RESULT" | head -1 | sed 's/Document: //')
    SECOND_LINE=$(printf '%s' "$RESULT" | sed -n '2p')
    if [[ "$SECOND_LINE" == "Page:"* ]]; then
      PAGE="${SECOND_LINE#Page: }"
      SUMMARY="${DOC} page ${PAGE}"
    else
      SUMMARY="${DOC}"
    fi
    jq -n --arg summary "$SUMMARY" --arg ctx "$RESULT" '{
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
