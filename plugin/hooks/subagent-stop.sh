#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
# SubagentStop: capture the subagent's own transcript when it completes.
# BLOCKING hook — the Python handler must never emit a decision field or exit 2.
quarry-hook subagent-stop 2>/dev/null || true
