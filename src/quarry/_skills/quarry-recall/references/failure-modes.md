# Failure Modes

Each row: recognize it, take the one next action, then stop. Do not loop.

## Daemon down or unreachable

**Recognize**: a connection error, timeout, or a message naming `quarryd`
unreachable.

**Next action**: run `quarry doctor` to confirm, then restart the daemon —
`systemctl --user restart quarry` (Linux) or `launchctl kickstart -k
gui/$(id -u)/com.punt-labs.quarry` (macOS). Retry the search once the daemon
reports healthy.

**Do not**: retry the same `find` in a loop against a daemon that is still
down. One restart attempt, one retry — if it still fails, report the outage
and move on without quarry for this turn.

## Empty results

**Recognize**: `total_results: 0` or an empty hit list for a query you
expected to match.

**Next action**: narrow or rephrase once — try a shorter, more literal
phrase, drop a scope flag (`--collection`, `--agent-handle`) that may be too
narrow, or check `quarry status` to confirm the expected collection is
indexed at all.

**Do not**: report "not found" as "does not exist" or "never happened."
Report it as "not found in the indexed scope" — the fact may simply not be
captured yet.

## Remote vs. local mode

**Recognize**: results differ from what you expect, or a collection you
know is registered locally doesn't appear.

**Next action**: run `quarry status` — it reports which database and mode
(local vs. a remote `QUARRY_URL`/`quarry login` target) is active. A remote
target only searches its own database; local collections are invisible to
it and vice versa.

**Do not**: assume a missing collection means it was never indexed — check
which target you're actually searching first.

## Stale index

**Recognize**: a file you know changed doesn't show up, or shows outdated
content.

**Next action**: run `quarry sync` to catch up incremental changes for
registered directories, then retry the search.

**Do not**: re-ingest or re-register a directory that's already tracked —
`sync` is the correct catch-up path, not a fresh `register`.

## Collection not found

**Recognize**: an error naming an unknown collection for `--collection`.

**Next action**: run `quarry list collections` (or the `list` tool with
`kind="collections"`) to see the real names, then retry with the correct
one.

**Do not**: guess a plausible collection name and retry blind — list first.

## Unauthorized

**Recognize**: a 401/403-shaped error, or a message naming the client as
not authorized against a remote target.

**Next action**: this is a configuration problem, not a transient one —
report it plainly (which target, what failed) rather than retrying. Local
mode has no auth; this only fires against a remote `QUARRY_URL`/`quarry
login` target with a bad or missing credential.

**Do not**: retry the same request hoping the credential appears. Surface
the failure and let the operator fix the credential.
