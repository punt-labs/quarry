#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_PLUGIN_ROOT="$PLUGIN_ROOT" quarry-hook session-setup 2>/dev/null || true
