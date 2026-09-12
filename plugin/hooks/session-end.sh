#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# SessionEnd: capture the session transcript on close.
quarry-hook session-end 2>/dev/null || true
