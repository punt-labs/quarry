#!/usr/bin/env bash
# Tail the most recent lines of the quarry diagnostic log.
#
# The convenience companion to logs-errors.sh: a raw, unfiltered window on the
# freshest diagnostics. Graceful in the ways logs-errors.sh is, so the two
# behave symmetrically:
#   - the log absent is the normal pre-daemon state (report it, exit 0);
#   - a present-but-unreadable log is a READ FAILURE, not absence: it is
#     surfaced distinctly on stderr and never mis-reported as absent (the
#     misleading-fallback class, bug class 2);
#   - a non-integer LOG_LINES falls back to 40 with a warning. Without this
#     guard an unguarded `tail -n <junk>` fails, and the old `|| echo` fallback
#     then LIED "no diagnostic log" even when the log was present — the exact
#     misleading-fallback failure this guard closes (bug class 2).
#
# Usage:
#   scripts/logs-tail.sh
#   LOG_DIR=/path/to/logs LOG_LINES=80 scripts/logs-tail.sh
#
# Env overrides:
#   LOG_DIR    daemon log directory (default $HOME/.punt-labs/quarry/logs)
#   LOG_LINES  number of trailing lines to print (default 40)

set -euo pipefail

log_dir="${LOG_DIR:-$HOME/.punt-labs/quarry/logs}"
log_lines="${LOG_LINES:-40}"

# Same LOG_LINES sanitization as logs-errors.sh (second occurrence — below the
# rule-of-three extraction bar; each sibling stays self-contained rather than
# depend on a sourced lib whose path resolution is fragile under `set -u`).
case "$log_lines" in
    '' | *[!0-9]*)
        echo "warning: LOG_LINES='$log_lines' is not a non-negative integer; using 40" >&2
        log_lines=40
        ;;
esac

diagnostic_log="$log_dir/quarry.log"

# Three states, never collapsed: `[ -r ]` alone is false for BOTH an absent log
# and a present-but-unreadable one, so testing only readability mis-reports a
# present log as missing (the misleading-fallback class, bug class 2).
#   (a) absent               → the normal pre-daemon state; report it, exit 0.
#   (b) present, unreadable  → a READ FAILURE; say so on stderr, never claim
#                              absence; exit 0 (a diagnostic never gates).
#   (c) present, readable    → tail it.
if [ ! -e "$diagnostic_log" ]; then
    echo "no diagnostic log at $diagnostic_log"
    exit 0
fi
if [ ! -r "$diagnostic_log" ]; then
    echo "diagnostic log present but unreadable: $diagnostic_log" >&2
    exit 0
fi

tail -n "$log_lines" "$diagnostic_log"
