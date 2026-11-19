#!/usr/bin/env bash
# Scan the quarry daemon log directory for error/failure signals.
#
# Quarry processes share ~/.punt-labs/quarry/logs/quarry.log.
# The scan can also read historical logs left by older installations. Nothing surfaces the errors accumulating there — `quarry
# doctor` does not read the log and no one greps it — so a broken watch/sync
# feature logged 100+ "Watch index failed" lines that sat unnoticed for weeks
# (alongside historical "LanceError(IO): Too many open files"). This target
# closes that blind spot: a first-class diagnostic that greps the live logs for
# known error signals, prints a per-signal summary count, then the most recent
# matching lines.
#
# It is a diagnostic, not a gate: it ALWAYS exits 0 (even when matches are
# found, and even when the log dir or files are absent). Wiring it into a
# quality gate would make routine runtime noise fail the build; keep it a
# standalone probe like `make report`.
#
# Usage:
#   scripts/logs-errors.sh
#   LOG_DIR=/path/to/logs LOG_LINES=80 scripts/logs-errors.sh
#
# Env overrides:
#   LOG_DIR    daemon log directory (default $HOME/.punt-labs/quarry/logs)
#   LOG_LINES  most-recent matching lines to print (default 40)

set -euo pipefail

log_dir="${LOG_DIR:-$HOME/.punt-labs/quarry/logs}"
log_lines="${LOG_LINES:-40}"

# LOG_LINES feeds `tail -n`; a non-integer would crash it. Fall back with a
# warning rather than propagate the error (exception-boundary discipline: this
# diagnostic must not abort on a bad knob).
case "$log_lines" in
    '' | *[!0-9]*)
        echo "warning: LOG_LINES='$log_lines' is not a non-negative integer; using 40" >&2
        log_lines=40
        ;;
esac

# A missing log dir is the normal state before the daemon has ever run. Report
# it plainly and exit 0 — absence is not an error for a diagnostic.
if [ ! -d "$log_dir" ]; then
    echo "no daemon logs at $log_dir"
    exit 0
fi

# The dir exists but cannot be read/searched (mode 000): the `*.log` glob below
# expands to nothing and falls through to "no daemon logs" — a FALSE absent, the
# same misleading-fallback class as an unreadable file (bug class 2). Surface it
# distinctly on stderr and exit 0 (a diagnostic never gates), never claiming the
# logs absent when the truth is we could not read the directory.
if [ ! -r "$log_dir" ] || [ ! -x "$log_dir" ]; then
    echo "daemon log dir present but unreadable: $log_dir" >&2
    exit 0
fi

# Collect the primary *.log files. The glob deliberately excludes rotated
# quarry.log.1..N (they match *.log.N, not *.log); the live files carry the
# current state. `[ -f ]` guards the literal-glob case when nothing matches,
# so an empty dir falls through to the no-logs branch below. Count the ones we
# cannot read as we go: an unreadable log means every count below undercounts,
# and a diagnostic that silently omits a file it cannot open would report the
# logs "clean" when it is really just blind to them (bug class 2).
files=()
unreadable=0
for f in "$log_dir"/*.log; do
    if [ -f "$f" ]; then
        files+=("$f")
        [ -r "$f" ] || unreadable=$((unreadable + 1))
    fi
done

if [ "${#files[@]}" -eq 0 ]; then
    echo "no daemon logs at $log_dir"
    exit 0
fi

# Record read failures on the FILESYSTEM, not in a shell variable. The greps
# below run inside `$()` command substitutions (and one inside a pipe), each a
# SUBSHELL — a variable assigned there never reaches this parent scope, so an
# earlier `scan_failed=1` set in count_signal was silently lost and a mid-scan
# grep read failure still printed "no errors found" (the false clean this tool
# must never emit). A grep that fails to read a log instead appends a line to
# this marker; the parent reads it after all scans (`-s`) to gate the verdict.
# mktemp keeps it private and race-free; the EXIT trap removes it (the script
# always exits 0, so the trap always fires). The glob-time `unreadable` count
# above is kept only to phrase the message — the marker is the sole gate, so a
# file that is readable at grep time is never falsely flagged, and a file that
# fails only mid-scan still is.
scan_status=$(mktemp "${TMPDIR:-/tmp}/quarry-logscan.XXXXXX")
trap 'rm -f "$scan_status"' EXIT

# Error signals to summarize, as parallel label/pattern arrays (Bash 3.2, the
# macOS default, has no associative arrays). Patterns are matched
# case-insensitively (grep -i), which is what "case-insensitive where sensible"
# resolves to for a runtime-log probe: `ERROR`/`error`/`Error:` are all the same
# signal to an operator scanning for trouble.
labels=(
    "ERROR:"
    "Traceback:"
    "'Error:' lines:"
    "failed/Failed:"
    "not found:"
    "Too many open files / EMFILE:"
    "Watch index failed:"
    "Delete failed:"
)
patterns=(
    'ERROR'
    'Traceback'
    'Error:'
    'failed'
    'not found'
    'Too many open files|EMFILE'
    'Watch index failed'
    'Delete failed'
)

# One combined alternation drives both the distinct-line total and the
# recent-lines tail, built from the same patterns so the two never drift.
combined=""
for p in "${patterns[@]}"; do
    if [ -z "$combined" ]; then
        combined="$p"
    else
        combined="$combined|$p"
    fi
done

# Classify a grep (or grep|tail pipeline) exit code. Exit 1 is benign (no line
# matched). Exit 141 is SIGPIPE — a downstream `tail` closed the pipe early, not
# a read error. Anything else >1 is a REAL read failure (an unreadable file or
# an IO error), which `|| true` used to flatten into the no-match path so a run
# over logs it could not read printed "no errors found" and exited 0 (a false
# clean, the exact failure this tool exists to catch). On a real failure, warn
# (naming the pattern) and append to the parent-visible marker so the clean
# verdict is withheld; a partial count from the readable files is still emitted
# (better an undercount, clearly flagged, than a lie). Runs inside subshells, so
# the marker file — not a shell variable — is how the signal reaches the parent.
check_grep_rc() {
    if [ "$1" -gt 1 ] && [ "$1" -ne 141 ]; then
        echo "warning: log scan failed (grep exit $1) for pattern '$2'" >&2
        echo failed >>"$scan_status"
    fi
}

# Sum per-file line counts for a pattern across all log files. `grep -c` prints
# one `count` per file (filenames suppressed with -h); awk sums them. Its output
# is tiny (one integer per file), so capturing it is cheap. grep's exit code is
# captured (`|| rc=$?`) and classified rather than discarded, so a read failure
# is surfaced instead of masked.
count_signal() {
    local out rc=0
    out=$(grep -hicE "$1" -- "${files[@]}") || rc=$?
    check_grep_rc "$rc" "$1"
    printf '%s\n' "$out" | awk '{ s += $1 } END { print s + 0 }'
}

# The most recent matching lines across all logs. grep is STREAMED straight into
# `tail` rather than captured into a variable first — on a real daemon log the
# match set runs to thousands of lines, and buffering all of them only to keep
# the last `log_lines` wastes memory. Under `set -o pipefail` the pipeline's exit
# code is the rightmost non-zero, so a grep read failure (exit 2) still surfaces
# through the pipe as the captured `rc`; check_grep_rc treats 141 (tail closing
# the pipe → grep SIGPIPE) as benign so it is never misread as a read failure.
recent_lines() {
    local rc=0
    grep -hiE "$combined" -- "${files[@]}" | tail -n "$log_lines" || rc=$?
    check_grep_rc "$rc" "$combined"
}

names=""
for f in "${files[@]}"; do
    names="$names $(basename "$f")"
done

echo "quarry daemon log scan: $log_dir"
echo "files:$names"
echo
echo "error signal summary:"
i=0
while [ "$i" -lt "${#labels[@]}" ]; do
    n=$(count_signal "${patterns[$i]}")
    printf '  %-32s %s\n' "${labels[$i]}" "$n"
    i=$((i + 1))
done
echo

total=$(count_signal "$combined")

# Fold every read failure the subshells recorded into a parent-visible verdict.
# A non-empty marker means at least one grep could not read a log during the
# summary loop or the total, so the counts are a floor, not the truth — the
# clean verdict is withheld regardless of the total. Report the incomplete scan;
# name the count of files we could not open when we know it (unreadable at glob
# time), else report the generic mid-scan read failure surfaced by grep's rc.
scan_failed=0
[ -s "$scan_status" ] && scan_failed=1

if [ "$scan_failed" -eq 1 ]; then
    if [ "$unreadable" -gt 0 ]; then
        echo "log scan incomplete: $unreadable file(s) unreadable — results may undercount"
    else
        echo "log scan incomplete: a log read failed — results may undercount"
    fi
fi

if [ "$total" -gt 0 ]; then
    echo "most recent $log_lines matching lines:"
    recent_lines
    echo
    echo "$total error lines found"
elif [ "$scan_failed" -eq 0 ]; then
    echo "no errors found"
fi

# Always exit 0: this is a diagnostic, not a gate. Only the clean-verdict TEXT
# is withheld on a read failure — the process still succeeds.
exit 0
