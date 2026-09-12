#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# PostToolUse on WebSearch: ingest a scrubbed digest of the results.
quarry-hook post-web-search 2>/dev/null || true
