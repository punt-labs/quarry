#!/usr/bin/env bash
[[ -f "$HOME/.punt-hooks-kill" ]] && exit 0
command -v quarry-hook >/dev/null 2>&1 || exit 0
# SessionEnd: capture the session transcript on close.
#
# Claude Code cancels any SessionEnd hook still running when the session tears
# down ("Hook cancelled"), and a large transcript takes ~12s to ingest. The
# harness kills the process, so `|| true` cannot save a slow synchronous run.
# Read stdin here, pass it to a detached job via argv (the harness may close
# fd 0 on exit), and return at once so the ingest outlives the teardown window.
#
# The job runs under setsid so it leads its own session and process group: it
# survives both SIGHUP and a teardown kill of the hook's process group. Where
# setsid is absent (macOS base install) it falls back to nohup, which survives
# SIGHUP only; a process-group kill would still reach it there.
INPUT=$(cat)
JOB="printf '%s' \"\$1\" | quarry-hook session-end"
if command -v setsid >/dev/null 2>&1; then
  setsid bash -c "$JOB" _ "$INPUT" >/dev/null 2>&1 &
else
  nohup bash -c "$JOB" _ "$INPUT" >/dev/null 2>&1 &
fi
disown 2>/dev/null || true
exit 0
