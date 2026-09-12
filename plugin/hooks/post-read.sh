#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# PostToolUse on Read: ingest prose files read from outside the tree (opt-in).
quarry-hook post-read 2>/dev/null || true
