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
# The payload read is time-bounded, never EOF-bounded: the harness can leave
# its end of the pipe open at teardown, and a plain `cat` would then block
# inside the cancellation window. read -t returns whatever arrived by the
# deadline (and at once on EOF).
#
# The job leads its own session and process group, so it survives both SIGHUP
# and a teardown kill of the hook's process group. setsid does that where
# present; the macOS base install lacks it but ships perl, whose POSIX::setsid
# is the same call. With neither, nohup survives SIGHUP only.
INPUT=""
IFS= read -r -d "" -t 1 INPUT || true
JOB="printf '%s' \"\$1\" | quarry-hook session-end"
if command -v setsid >/dev/null 2>&1; then
  setsid bash -c "$JOB" _ "$INPUT" >/dev/null 2>&1 &
elif command -v perl >/dev/null 2>&1; then
  perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV' bash -c "$JOB" _ "$INPUT" >/dev/null 2>&1 &
else
  nohup bash -c "$JOB" _ "$INPUT" >/dev/null 2>&1 &
fi
disown 2>/dev/null || true
exit 0
