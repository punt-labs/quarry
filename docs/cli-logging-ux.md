# CLI Logging and Output UX Design

**Scope**: This design covers all code paths in `__main__.py` — local
and remote modes. Every command, flag, and output channel is in scope
regardless of whether the transport is a local LanceDB call or an HTTP
request to a remote quarry daemon.

Design document for quarry CLI stdout/stderr contracts, verbosity flags,
and progress reporting.

---

## 1. Current State Audit

### 1.1 Logging Infrastructure

`logging_config.py` provides `configure_logging(*, stderr_level: str = "WARNING")`.
Two handlers: a rotating file handler at INFO, and a stderr handler whose level
the caller controls.

Entry points call `configure_logging` at import time:

| Entry point        | stderr_level | Notes                              |
|--------------------|-------------|-------------------------------------|
| `__main__.py` (CLI) | `"WARNING"` | Correct for default CLI mode        |
| `mcp_server.py`    | `"INFO"`    | Correct per logging standard        |
| Hook entry points  | `"WARNING"` | Correct for background hooks        |

The `--verbose` flag is captured in `_verbose` but never passed to
`configure_logging`. The `--quiet` flag is captured in `_quiet` and used
only by `optimize_cmd` (2 occurrences). Neither flag adjusts the logging
stderr level. This means `--verbose` is a no-op today.

### 1.2 Output Channels

The CLI uses three output mechanisms:

1. **`_emit(data, text)`** -- writes JSON to stdout when `--json` is set,
   otherwise prints `text` to stdout. Used by every command that returns
   results.

2. **`err_console` (Rich Console, stderr)** -- used for errors
   (`style="red"`), warnings (`style="yellow"`), and the two `optimize`
   progress messages. This is correct: user-facing messages on stderr.

3. **`_progress(label)` context manager** -- Rich progress bar on the
   `console` object (stdout). Suppressed in `--json` mode. This is
   **wrong**: progress output goes to stdout, polluting pipeable results.

4. **`logger.*` calls** -- go to file always; go to stderr only at
   WARNING+ (default). With `--verbose`, these should appear on stderr.

### 1.3 Module-by-Module Audit

#### `__main__.py`

- **Path**: `src/quarry/__main__.py`
- **Log calls**: 2 (1 INFO in `sync_cmd`, 1 exception in `_cli_errors`)
- **Audience**: INFO is internal (worker count decision); exception is internal
- **Issues**:
  - `_progress` uses `console` (stdout), not `err_console` (stderr)
  - `_verbose` captured but never used to adjust `configure_logging`
  - `_quiet` used only in `optimize_cmd`, not system-wide
  - `uninstall_cmd` (line 1779) uses `console.print(msg)` — writes the
    uninstall confirmation to stdout via the Rich stdout console instead
    of `err_console` or `_emit`
  - `login_cmd` abort path (line 1335) uses bare `print("Aborted. Not
    logged in.")` to stdout — should use `err_console` for user messages
  - `login_cmd` fingerprint display (line 1329) uses `err_console.print`
    without a `--quiet` guard
  - `sync_cmd` remote 409 warning (line 1199) uses `err_console.print`
    without a `--quiet` guard

#### `sync.py`

- **Path**: `src/quarry/sync.py`
- **Log calls**: 18 (14 INFO, 2 WARNING, 2 exception)
- **Audience**: All internal/diagnostic. The INFO messages are the best
  source of progress data (plan computed, ingested X in Y seconds, batch
  inserted N chunks, completed in Zs). None reach the terminal today.
- **Issues**: Excellent logging that is invisible to the user. The
  `progress_callback` relays some messages to the Rich progress bar, but
  that progress bar is on stdout.

#### `pipeline.py`

- **Path**: `src/quarry/pipeline.py`
- **Log calls**: 12 (10 INFO, 2 WARNING via `_make_progress`)
- **Audience**: Mixed. `_make_progress` logs at INFO and calls the
  callback. Pipeline timing (chunk/embed/store) is diagnostic. Progress
  messages (Analyzing, Extracting, Chunking, Done) are user-facing.
- **Issues**: Same progress-on-stdout problem. The `_make_progress`
  pattern correctly logs + callbacks, but the callback feeds the
  stdout Rich bar.

#### `database.py`

- **Path**: `src/quarry/database.py`
- **Log calls**: 18 (10 INFO, 5 DEBUG, 3 WARNING)
- **Audience**: All internal. INSERT/DELETE/SEARCH counts, schema
  migration, FTS index status, RRF fusion stats.
- **Issues**: None. These are properly diagnostic. They write to file at
  INFO and only hit stderr at WARNING+ (migration failures, FTS failures).

#### `http_server.py`

- **Path**: `src/quarry/http_server.py`
- **Log calls**: 18 (10 INFO, 3 WARNING, 2 ERROR, 2 exception, 1 DEBUG)
- **Audience**: All internal/server diagnostic.
- **Issues**: None for CLI local mode. Server logging is separate.

#### `mcp_server.py`

- **Path**: `src/quarry/mcp_server.py`
- **Log calls**: 1 (exception in `_handle_errors`)
- **Audience**: Internal.
- **Issues**: None.

#### `sitemap.py`

- **Path**: `src/quarry/sitemap.py`
- **Log calls**: 4 (all INFO)
- **Audience**: Internal (discovery counts, parse counts).
- **Issues**: None.

#### `embeddings.py`

- **Path**: `src/quarry/embeddings.py`
- **Log calls**: 9 (5 INFO, 3 DEBUG, 1 WARNING)
- **Audience**: INFO (model load, embedding throughput) is diagnostic.
  WARNING (large batch, CUDA fallback) is operational.
- **Issues**: None.

#### `provider.py`

- **Path**: `src/quarry/provider.py`
- **Log calls**: 4 (3 INFO, 1 WARNING)
- **Audience**: All internal (provider detection decisions).
- **Issues**: None.

#### `tls.py`

- **Path**: `src/quarry/tls.py`
- **Log calls**: 5 (4 INFO, 1 DEBUG)
- **Audience**: Internal (cert generation, file writes).
- **Issues**: None.

#### `hooks.py`

- **Path**: `src/quarry/hooks.py`
- **Log calls**: 16 (5 INFO, 6 DEBUG, 3 WARNING, 2 ERROR)
- **Audience**: Internal (hook lifecycle, background sync decisions).
- **Issues**: None for CLI -- hooks are not user-facing.

#### `_stdlib.py`

- **Path**: `src/quarry/_stdlib.py`
- **Log calls**: 2 (1 WARNING, 1 exception)
- **Audience**: Internal.
- **Issues**: None.

#### `doctor.py`

- **Path**: `src/quarry/doctor.py`
- **Log calls**: 0 direct logger calls. Uses `print()` throughout.
- **Audience**: User-facing (install/doctor output).
- **Issues**: Uses `print()` to stdout, which is appropriate for its
  structured pass/fail output. Not affected by verbosity flags.

#### `service.py`

- **Path**: `src/quarry/service.py`
- **Log calls**: 14 (10 INFO, 3 WARNING, 1 ERROR)
- **Audience**: Internal (service registration, GPU runtime swap).
- **Issues**: None.

#### `proxy.py`

- **Path**: `src/quarry/proxy.py`
- **Log calls**: 3 (all INFO)
- **Audience**: Internal (download progress, install path).
- **Issues**: None.

#### `ocr_local.py`

- **Path**: `src/quarry/ocr_local.py`
- **Log calls**: 3 (all INFO)
- **Audience**: Internal (engine init, per-page OCR char counts).
- **Issues**: None. Per-page logging is acceptable since OCR is slow
  and the count aids diagnosis.

#### Other modules with logging

- `html_processor.py`, `pdf_analyzer.py`, `presentation_processor.py`,
  `spreadsheet_processor.py`, `text_extractor.py`, `text_processor.py`,
  `code_processor.py`, `image_analyzer.py`: All declare
  `logger = logging.getLogger(__name__)`. Log calls are sparse (0-3 per
  module), all at DEBUG or INFO level, all internal/diagnostic.
  No issues.

---

## 2. Stdout Contract

Stdout carries **only machine-parseable results**. A downstream pipe
(`quarry find "query" | jq .`) must never see progress bars, warnings,
or decorative output.

### What goes to stdout

| Command | Default (human) | `--json` |
|---------|-----------------|----------|
| `find`  | Formatted search results | JSON array of result objects |
| `show`  | Document metadata or page text | JSON object |
| `status` | Formatted status summary | JSON object |
| `list documents` | Formatted document table | JSON array |
| `list collections` | Formatted collection table | JSON array |
| `list databases` | Formatted database table | JSON array |
| `list registrations` | Formatted registration list | JSON array |
| `ingest` | JSON summary (current behavior) | JSON object |
| `remember` | JSON summary | JSON object |
| `sync` | Per-collection summary | JSON object (collection -> stats) |
| `register` | Confirmation message | JSON object |
| `deregister` | Confirmation message | JSON object |
| `delete` | Confirmation message | JSON object |
| `use` | Confirmation message | JSON object |
| `login` | Confirmation message | JSON object |
| `logout` | Confirmation message | JSON object |
| `optimize` | Confirmation message | JSON object |
| `uninstall` | Confirmation message | JSON object |
| `version` | Version string | JSON object (*) |
| `remote list` | Remote config display | JSON object |
| `doctor` | Pass/fail checklist (print) | Not yet supported |
| `install` | Step-by-step output (print) | Not yet supported |

(*) `quarry --version --json` currently produces non-JSON output.
The `_version_callback` uses bare `print()` and exits before the
`--json` flag is processed (it is an `is_eager` Typer callback).
Fix: check `--json` in the callback and emit
`{"name": "quarry", "version": "<ver>"}` when set.

### Local vs Remote JSON Divergence

Fire-and-forget commands (`ingest`, `remember`, `delete`, `register`,
`deregister`, `sync`) return different JSON shapes depending on mode:

- **Local mode**: Returns the operation result (e.g., `IngestResult`
  with `chunks_created`, `document_name`, timing data).
- **Remote mode**: Returns `{"task_id": "<id>", "status": "accepted"}`
  (HTTP 202 response). The server processes the operation asynchronously.

Scripts that parse `--json` output must handle both shapes. This is the
Class 3 (remote/local divergence) pattern documented in the project's
testing rules. Any new fire-and-forget command must document which JSON
shape each mode returns and must have an equivalence test asserting the
field-name contract for both paths.

### Rule

Nothing appears on stdout except the result payload. The `_emit` helper
enforces this for most commands. The `_progress` context manager and
`doctor`/`install` print calls are the exceptions that need fixing.

---

## 3. Stderr Contract

Stderr carries **everything else**: progress indicators, warnings, errors,
verbose diagnostic logs, and Rich-formatted user messages.

### What goes to stderr

| Category | Source | Example |
|----------|--------|---------|
| Errors | `err_console.print(..., style="red")` | `Error: file not found` |
| Warnings | `err_console.print(..., style="yellow")` | `Warning: --workers ignored` |
| Progress | Rich Progress bar (after fix) | `Syncing [3/10 ingested]` |
| Verbose logs | `logger.info()` via stderr handler | `sync: plan computed in 0.12s` |
| Debug logs | `logger.debug()` (file only) | `Search: 5 results returned` |

### Rule

The `err_console` is already `Console(stderr=True)`. The `_progress`
context manager must switch from `console` (stdout) to `err_console`
(stderr). This is the single most important fix in this design.

---

## 4. Flag Behavior

### 4.1 Default (no flags)

- **stdout**: Results only (via `_emit`)
- **stderr**: Warnings and errors only (logger at WARNING, Rich errors)
- **Progress**: Rich progress bar on stderr for `ingest`, `sync`
- **Logger stderr level**: `WARNING`

### 4.2 `--verbose` / `-v`

- **stdout**: Unchanged (results only)
- **stderr**: Adds INFO-level logger output (progress, timing, file
  counts, plan details, embedding throughput)
- **Progress**: Rich progress bar on stderr (unchanged)
- **Logger stderr level**: `INFO`

Implementation: `main_callback` calls `configure_logging(stderr_level="INFO")`
when `--verbose` is set. This makes all the existing INFO-level logging
in sync.py, pipeline.py, database.py, and embeddings.py visible on the
terminal without any changes to those modules.

### 4.3 `--quiet` / `-q`

- **stdout**: Results only (via `_emit`)
- **stderr**: Suppressed for progress, warnings, and INFO logs. No
  progress bar, no warnings, no INFO logs. Fatal errors that precede
  a non-zero exit are always shown regardless of `--quiet`.
- **Progress**: Suppressed (`_progress` yields `None`)
- **Logger stderr level**: `CRITICAL`

Implementation: `main_callback` calls
`configure_logging(stderr_level="CRITICAL")` when `--quiet` is set.
The `_progress` context manager already suppresses when `_json_output`
is True; extend the condition to also suppress when `_quiet` is True.
The `optimize_cmd` already checks `_quiet` for its two `err_console`
messages; generalize this to all commands.

### 4.4 `--json`

- **stdout**: JSON output (via `_emit`)
- **stderr**: Unchanged from default (warnings + errors). The user may
  combine `--json --verbose` to get JSON on stdout and diagnostic logs
  on stderr.
- **Progress**: Suppressed (no Rich bar). This is the current behavior
  and is correct.

### 4.5 Flag Combinations

| Flags | stdout | stderr |
|-------|--------|--------|
| (none) | human text | warnings + errors + progress |
| `--verbose` | human text | INFO logs + warnings + errors + progress |
| `--quiet` | human text | fatal errors only |
| `--json` | JSON | warnings + errors |
| `--json --verbose` | JSON | INFO logs + warnings + errors |
| `--json --quiet` | JSON | fatal errors only |
| `--verbose --quiet` | **error, exit 1** | mutually exclusive |

---

## 5. Progress Reporting Design

### 5.1 Principles

1. Progress goes to stderr (Rich console with `stderr=True`)
2. Default mode shows a spinner with a status message
3. Verbose mode adds logger INFO output alongside the spinner
4. Quiet mode shows nothing
5. JSON mode shows nothing (no Rich bar)
6. Rich detects TTY automatically on the console passed to Progress.
   No manual `sys.stderr.isatty()` check is needed. When stderr is
   piped, the spinner suppresses automatically.

### 5.2 `quarry sync` (local mode)

**Default (no flags):**

```text
⠋ [quarry] 3 to ingest, 0 to refresh, 1 to delete, 42 unchanged
```

Rich Progress renders a single line on stderr with a spinner animation
and the task description. Each `progress_callback` call replaces the
description in-place (the entire line redraws). The user sees a live
status that cycles through:

```text
⠙ [quarry] Ingested src/main.py in 0.45s
⠹ [quarry] Ingested src/utils.py in 0.31s
⠸ [quarry] Ingested src/config.py in 0.28s
⠼ [quarry] Deleted old_file.py
⠴ Done
```

When the spinner stops, the final summary is printed via `_emit` to
stdout.

**Verbose (`-v`):**

Same spinner, plus logger INFO lines interleaved on stderr:

```text
2026-04-18 10:37:41 [INFO] quarry.sync: sync: [quarry] plan computed in 0.12s
2026-04-18 10:37:41 [INFO] quarry.sync: [quarry] 3 to ingest, 0 to refresh, 1 to delete, 42 unchanged
2026-04-18 10:37:42 [INFO] quarry.pipeline: pipeline: chunked 5 pages -> 12 chunks in 0.08s
2026-04-18 10:37:42 [INFO] quarry.pipeline: pipeline: embedded 12 chunks in 0.34s (35.3 chunks/s)
2026-04-18 10:37:42 [INFO] quarry.sync: [quarry] Ingested src/main.py in 0.45s
...
2026-04-18 10:37:44 [INFO] quarry.sync: sync: [quarry] batch-inserted 31 chunks in 0.15s
2026-04-18 10:37:44 [INFO] quarry.sync: sync: [quarry] completed in 3.21s (3 ingested, 0 refreshed, 1 deleted, 42 skipped, 0 failed)
```

**Quiet (`-q`):**

No output on stderr. Exit code 0 on success, 1 on failure. Stdout has
the result via `_emit`.

**JSON (`--json`):**

No progress on stderr. Stdout has the JSON result.

### 5.3 `quarry ingest <file>` (local mode)

**Default:**

```text
⠋ Analyzing: report.pdf
⠙ Pages: 12 total, 10 text, 2 image
⠹ Extracting text from 10 pages
⠸ Running OCR on 2 pages
⠼ Created 45 chunks
⠴ Generating embeddings (Snowflake/snowflake-arctic-embed-m-v1.5)
⠦ Done: 45 chunks indexed from report.pdf
```

Single-line spinner on stderr with status updates via the progress
callback. Each line replaces the previous (in-place redraw).

**Verbose (`-v`):**

Spinner plus logger INFO with timing details (chunking time, embedding
throughput, storage time).

### 5.3.1 `quarry ingest <url>` -- sitemap discovery gap

When ingesting a URL, the CLI wraps `ingest_auto` in
`_progress(f"Fetching {source}")`. The `ingest_auto` function calls
`sitemap.discover()` to find all pages before ingesting them. During
sitemap discovery (which may fetch and parse `robots.txt`, multiple
`sitemap.xml` files, and follow sitemap index chains), the spinner
shows `Fetching <url>` frozen with no status updates.

The sitemap discovery phase has no progress callback today.
`sitemap.discover()` does not accept a callback parameter, so the
spinner cannot report discovery progress (e.g., "Found 12 pages in
sitemap" or "Fetching sitemap index").

**Deferred enhancement**: Add a `progress_callback` parameter to
`sitemap.discover()` and wire it through `ingest_auto`. Until then,
the frozen spinner during discovery is a known UX gap for large sites
with complex sitemap structures.

### 5.4 `quarry optimize` (local mode)

**Default:**

```text
Fragment count: 247          ← stderr (err_console.print)
Running optimization...      ← stderr (err_console.print)
Optimization complete.       ← stdout (_emit)
```

Uses `err_console.print` for the first two lines (already on stderr).
The "Optimization complete." is the result via `_emit` (stdout).

**Quiet (`-q`):**

No stderr output. Only the `_emit` result on stdout.

### 5.5 SIGINT / Ctrl-C Behavior

Ctrl-C raises `KeyboardInterrupt`, which unwinds the `_progress`
context manager's `finally` block and stops the Rich progress bar.
The process exits with code 130 (standard SIGINT convention).

Background threads (e.g., sync's ThreadPoolExecutor workers) complete
any in-flight LanceDB writes before the process exits. No additional
error message is printed to stderr. This is intentional: a partial
sync with committed writes is safer than a torn write from forcefully
killing worker threads mid-INSERT.

No special handling is needed in the CLI for local mode. Python's
default SIGINT behavior plus the `finally` block in `_progress` produce
the correct outcome.

**Remote mode**: Ctrl-C during a remote HTTP request (`_remote_https_request`)
raises `KeyboardInterrupt` in the `urllib.request.urlopen` call, which
closes the socket and unwinds the stack normally. The server-side
operation (ingest, sync) continues — the 202 fire-and-forget pattern
means the server is already processing independently. No additional
handling is needed; the CLI exits with code 130 and the server finishes
its work.

### 5.6 `quarry remember` Progress

**Default:** Spinner on stderr wrapping the `ingest_content` call. The
spinner description updates with the progress callback, same as
`ingest`.

**Verbose (`-v`):** Same spinner, plus logger INFO lines for chunking,
embedding, and storage timing (identical to `ingest` verbose output).

**Quiet (`-q`):** No spinner. Only the `_emit` result on stdout.

**JSON (`--json`):** No spinner. JSON result on stdout.

`remember` uses the same `_progress` context manager as `ingest` and
`sync`. No separate progress implementation is needed.

---

## 6. Remediation List

**Note on line numbers**: Line numbers in this section reference
`__main__.py` as of commit `324841d` (v1.14.0 post-release). They will
drift as code changes. Use the function/variable names (not line numbers)
to locate each site.

### 6.1 Critical -- Progress bar on wrong fd

**File**: `src/quarry/__main__.py`, `_progress` function (line 206-224)

**Problem**: `Progress(console=console)` uses `console` which writes to
stdout. This pollutes pipeable output.

**Fix**: Change to `Progress(console=err_console)`. This is a one-line
change that fixes the single worst UX bug.

**Dependency**: Must be applied simultaneously with §6.3 (`--quiet`
suppression). Once progress moves to stderr, `--quiet` must suppress it
or the quiet contract is violated. See §6.12 for implementation order.

### 6.2 Critical -- `--verbose` is a no-op

**File**: `src/quarry/__main__.py`, `main_callback` function

**Problem**: `_verbose` is stored but never used. The comment says
"reserved: commands will use for extra output" but no command does.

**Fix**: After setting `_verbose = verbose`, call
`configure_logging(stderr_level="INFO")`. This immediately makes all
existing INFO-level logging in sync.py, pipeline.py, database.py, and
embeddings.py visible on the terminal.

### 6.3 High -- `--quiet` only used by `optimize`

**File**: `src/quarry/__main__.py`

**Problem**: `_quiet` is checked only in `optimize_cmd`. Other commands
with stderr output (warnings in `sync_cmd`, errors throughout) ignore it.

**Fix**:

1. Call `configure_logging(stderr_level="CRITICAL")` when `_quiet` is set.
2. Extend `_progress` to yield `None` when `_quiet` is True (same
   pattern as `_json_output`).
3. Guard `err_console.print` warning calls with `if not _quiet`.

**Dependency**: Must be applied simultaneously with §6.1 (progress to
stderr). See §6.12 for implementation order.

### 6.4 Medium -- `configure_logging` called at import time

**File**: `src/quarry/__main__.py`, line 78

**Problem**: `configure_logging(stderr_level="WARNING")` runs at module
import, before `main_callback` parses flags. Re-calling it in
`main_callback` with a different level works because `dictConfig` is
re-entrant, but the initial call creates handlers that are then replaced.

**Fix**: Move `configure_logging` into `main_callback` after flag
parsing. Determine `stderr_level` from the flags:

- `--verbose`: `"INFO"`
- `--quiet`: `"CRITICAL"`
- default: `"WARNING"`

The `logger = logging.getLogger(__name__)` at module level is fine --
it does not trigger handler creation.

Early import-time log messages (before `main_callback` runs) are
captured only by the file handler. INFO and above are written to the
file handler; DEBUG is never written to the file handler by design
(the file handler level is INFO per the org logging standard). The
only effect is that any INFO messages emitted during import do not
appear on stderr even with `--verbose`, because the stderr handler
has not yet been reconfigured. In practice, no module emits
user-visible messages at import time.

### 6.5 Medium -- `doctor`/`install` use `print()` to stdout

**File**: `src/quarry/doctor.py`

**Problem**: The structured pass/fail output goes to stdout via `print()`.
This is acceptable for interactive use but means `quarry doctor | grep`
works only accidentally.

**Fix**: Under `--quiet`, `doctor` and `install` should suppress
line-by-line output and exit with the appropriate code (0 for all
checks passed, non-zero otherwise). This brings them into alignment
with the `--quiet` contract in §4.3. Defer implementation but mark as
explicit technical debt. A future `--json` mode for `doctor` would use
`_emit`. The print-to-stdout behavior without `--quiet` is consistent
with how `doctor` commands work in other tools (e.g., `brew doctor`).

### 6.6 Low -- Inconsistent progress callback types

**File**: `src/quarry/sync.py` and `src/quarry/pipeline.py`

**Problem**: sync.py's internal `_progress` calls `logger.info(msg)`
then `progress_callback(msg)`. pipeline.py's `_make_progress` calls
`logger.info(fmt, *args)` then `callback(fmt % args)`. These are
functionally equivalent but structurally inconsistent.

**Fix**: No change required. Both patterns work correctly. The sync
pattern is clearer (message is pre-formatted); the pipeline pattern
avoids string formatting when the callback is None. Unifying them is
a low-value refactor.

### 6.7 Critical -- `uninstall_cmd` uses stdout console

**File**: `src/quarry/__main__.py`, `uninstall` function (line 1779)

**Problem**: `console.print(msg)` writes the uninstall confirmation to
stdout via the Rich stdout console. This pollutes pipeable output and
is inconsistent with other commands that use `_emit` or `err_console`.

**Fix**: Change `console.print(msg)` to `_emit({"message": msg}, msg)`.
This routes the confirmation through the standard output mechanism,
respecting `--json` mode and keeping stdout clean.

After this fix, delete the `console = Console()` declaration at line 127.
The only remaining console should be `err_console = Console(stderr=True)`.
§6.1 (progress bar fix) and this fix are the last two consumers of the
stdout console; once both land, the declaration is dead code.

### 6.8 Critical -- `login_cmd` abort uses bare `print()`

**File**: `src/quarry/__main__.py`, `login_cmd` function (line 1335)

**Problem**: `print("Aborted. Not logged in.")` writes to stdout via
bare `print()`. User-facing messages must go to stderr.

**Fix**: Change to `err_console.print("Aborted. Not logged in.")`.

### 6.9 High -- Remote-path `--quiet` violations

**File**: `src/quarry/__main__.py`

Three remote code paths emit stderr output without checking `_quiet`:

1. **Sync 409 warning** (line 1199): `err_console.print("Sync already
   in progress: ...")` is not `--quiet` guarded. A scripted `quarry
   sync -q` should get exit code 0 and silence, not a warning line.

2. **Login fingerprint display** (line 1329): `err_console.print(
   f"Server CA fingerprint: {fp}")` is informational output that
   should be suppressed under `--quiet`.

3. **Login abort** (line 1335): See §6.8. After fixing the `print()`
   to `err_console.print`, this line also needs a `--quiet` guard.

**Fix**: Guard all three with `if not _quiet`. The sync 409 is a
warning (non-fatal, exit 0); the fingerprint is informational; the
abort message is a confirmation. None are fatal errors that precede
a non-zero exit.

### 6.10 Medium -- `quarry remember` progress wrapper

**File**: `src/quarry/__main__.py`, `remember_cmd` function

**Problem**: `remember` calls `ingest_content` but does not wrap the
call in `_progress`. There is no spinner for the user to see during
embedding.

**Fix**: Wrap the `ingest_content` call in `with _progress("Remembering")
as callback` and pass the callback to `ingest_content`. The signature
at `pipeline.py:809` already accepts the parameter:

```python
def ingest_content(
    content: str,
    document_name: str,
    db: LanceDB,
    settings: Settings,
    *,
    ...
    progress_callback: Callable[[str], None] | None = None,
    ...
) -> IngestResult:
```

No library change needed — the CLI just needs to pass the callback.

**Remote mode**: The remote `remember` path returns 202 with a `task_id`
(fire-and-forget). No progress wrapper is needed for the remote path
because the server handles processing asynchronously.

### 6.11 Low -- `--verbose` help text is vague

**File**: `src/quarry/__main__.py`, `main` Typer app definition

**Problem**: The `--verbose` flag help text says "Verbose output." which
does not tell the user what additional output to expect.

**Fix**: Change to "Show INFO-level diagnostic logs on stderr (timing,
plans, counts)."

### 6.12 Implementation Order

The remediations have dependency relationships. Apply them in this order
to avoid intermediate broken states:

```text
Phase 1 (foundation — atomic, apply together):
  §6.4  Move configure_logging into main_callback
  §6.1  Progress bar to stderr     ──┐
  §6.3  --quiet suppression          ├── must be simultaneous
  §6.2  --verbose wires to INFO    ──┘

Phase 2 (cleanup — independent of each other):
  §6.7  uninstall_cmd stdout → _emit
  §6.7+ Delete console = Console() at line 127
  §6.8  login_cmd abort → err_console

Phase 3 (guards — requires Phase 1):
  §6.9  Remote-path --quiet guards (depends on §6.3)

Phase 4 (enhancements — independent):
  §6.10 remember progress wrapper
  §6.11 --verbose help text
  §6.5  doctor/install --quiet (deferred tech debt)
  §6.6  Progress callback consistency (no change required)
```

§6.4 must land first because §6.1–§6.3 rely on `configure_logging`
being called from `main_callback` where the flags are available.
§6.1 and §6.3 are coupled: moving progress to stderr without quiet
suppression violates the quiet contract. §6.9 depends on §6.3
because the quiet guards reference the `_quiet` flag behavior
established there.

---

## 7. Consistency Rules

### 7.1 When to use each output mechanism

| Mechanism | When to use | Example |
|-----------|-------------|---------|
| `_emit(data, text)` | Command results that the user asked for | Search results, status data, confirmation |
| `err_console.print(..., style="red")` | Errors that terminate the command | `Error: file not found` |
| `err_console.print(..., style="yellow")` | Warnings that the user should see | `Warning: --workers ignored` |
| `err_console.print(...)` (no style) | Non-error user-facing info on stderr | `Fragment count: 247` |
| `logger.info()` | Diagnostic info visible with `--verbose` | Timing, counts, decisions |
| `logger.warning()` | Degraded-but-functional conditions | FTS index missing, CUDA fallback |
| `logger.debug()` | Developer-only trace info (file only) | Cache hits, intermediate values |
| `logger.exception()` | Unexpected errors (always file, stderr at WARNING+) | Unhandled exceptions |
| `print()` | Only in `doctor`/`install` step output | `[1/8] Creating directories...` |

### 7.2 Rules

1. **Never use `print()` in product commands.** Use `_emit` for results,
   `err_console` for user messages, `logger` for diagnostics.

2. **Never use `console` (stdout).** All Rich console output must use
   `err_console` (stderr). Delete or replace the `console = Console()`
   instance after moving progress to `err_console`.

3. **Progress callbacks write to stderr via Rich.** The `_progress`
   context manager yields a callback or None. The callback updates a
   Rich progress bar on `err_console`.

4. **`_emit` is the only function that writes to stdout.** Plus the
   `_version_callback` which uses `print()` for the version string.

5. **Logger INFO is the "verbose" level.** If a message is useful for
   diagnosing "why was this slow?" or "what did quarry decide to do?",
   log it at INFO. It goes to the file always and to stderr with `-v`.

6. **Logger WARNING is the "default" stderr level.** If a message
   indicates a degraded condition the user should know about even
   without `-v`, log it at WARNING. It goes to stderr by default.

7. **Guards for `--quiet`.** Any `err_console.print` call that is not
   an error exit (i.e., not followed by `raise typer.Exit(code=1)`)
   should be guarded with `if not _quiet`. Error messages that precede
   a non-zero exit are always shown.

---

## 8. Rejected Alternatives

### 8.1 Structured logging (key=value format)

**Rejected.** The org logging standard (punt-kit/standards/logging.md)
explicitly says: "Do not add structured key-value formatting. The log
is human-read during diagnosis, not machine-parsed for aggregation."
Quarry is a local-first tool, not a cloud service.

### 8.2 Separate `--debug` flag

**Rejected.** The org CLI standard (punt-kit/standards/cli.md) defines
three verbosity levels: default, `--verbose`, and `--quiet`. Adding
`--debug` for DEBUG-level output adds complexity for a level that is
only useful to quarry developers. Developers can set
`QUARRY_LOG_LEVEL=DEBUG` or read the log file directly.

### 8.3 Per-command verbosity

**Rejected.** Some CLIs (e.g., `terraform plan -verbose`) have
per-command verbosity flags. Quarry's global `--verbose` is simpler
and consistent with the org standard. Every module already logs at
INFO; making that visible is a global decision, not per-command.

### 8.4 Progress bars with counts (e.g., `[3/10]`)

**Rejected for default mode.** The sync plan knows the total file count,
but the progress callback receives free-form strings, not structured
progress. Adding a `(current, total)` protocol to the callback would
require changing sync.py, pipeline.py, and every caller. The spinner +
status message pattern is sufficient for default mode. A future
enhancement could add structured progress reporting, but it is not
needed for this design.

### 8.5 Logging to stdout

**Rejected.** The org logging standard requires logging to stderr.
Stdout is reserved for machine-parseable results per the CLI standard.
This is non-negotiable.

### 8.6 Removing the file handler in quiet mode

**Rejected.** `--quiet` suppresses stderr output only. The file handler
always runs at INFO so post-mortem diagnosis is always possible. "I ran
`quarry sync -q` and it exited 1" must be diagnosable from the log file.

### 8.7 Mapping `--verbose` to DEBUG

**Rejected.** The org CLI standard (`punt-kit/standards/cli.md` §Global
Flags) says `--verbose` enables "debug logging." The org logging standard
(`punt-kit/standards/logging.md` §Levels) defines DEBUG as "implementation
details" with examples like "intermediate computation" and "cache
hits/misses." Quarry intentionally maps `--verbose` to INFO instead.

**Evidence — DEBUG volume is unsuitable for user-facing output:**

There are 40 `logger.debug()` calls across 12 files in `src/quarry/`.
Many are in hot loops that fire per-query or per-batch:

| Module | DEBUG calls | Hot-loop behavior |
|--------|-------------|-------------------|
| `database.py` | 11 | Per-query: RRF fusion stats, table creation races, search result counts |
| `hooks.py` | 12 | Per-hook-event: lifecycle decisions, lock contention, config checks |
| `embeddings.py` | 2 | Per-batch: batch count and per-batch timing |
| `text_extractor.py` | 2 | Per-page: character counts for each extracted page |
| `_stdlib.py` | 3 | Plugin root checks |
| `text_processor.py` | 3 | Per-file: format detection, section splitting |
| `pdf_analyzer.py` | 2 | Per-PDF: page count, per-page analysis |
| Others (5 files) | 5 | One-shot: provider detection, HTML processing, TLS, HTTP server |

For a sync of 100 files with the default batch size: `embeddings.py`
produces ~200 DEBUG lines (2 per batch x ~100 batches), `database.py`
produces ~11 per search invocation (RRF stats, table checks, result
counts), and `text_extractor.py` produces 2 per page of every PDF.
A 100-file sync touching 500 pages would emit 1000+ DEBUG lines from
these three modules alone.

By contrast, there are 80 `logger.info()` calls across 16 files. These
are the operational messages users want: plan summaries, per-file
timing, batch counts, embedding throughput.

**Design decision:** `--verbose` = INFO on stderr. `QUARRY_LOG_LEVEL`
environment variable = the developer escape hatch for DEBUG and finer
levels. The `configure_logging` function reads `QUARRY_LOG_LEVEL` when
present and applies it to the stderr handler, overriding the flag-derived
level. Third-party loggers (`lancedb`, `onnxruntime`, `httpx`) are
suppressed at WARNING in the `dictConfig` `loggers` block to prevent
their DEBUG output from flooding stderr even when `QUARRY_LOG_LEVEL=DEBUG`.

This conflicts with the literal reading of the CLI standard. The conflict
is deliberate: quarry's DEBUG output is unusable as a verbose user
experience, and promoting it would degrade the flag's utility. The logging
standard's own definition of DEBUG — "implementation details" for
"developers tracing through code" — supports treating it differently
from user-facing verbosity.

---

## 9. Compliance and PII Audit

### 9.1 Logging Configuration

`configure_logging` uses `logging.config.dictConfig` with:

- **Rotating file handler**: `~/.punt-labs/quarry/logs/` directory,
  created with `0o700` permissions (owner-only access).
- **Format**: timestamp, level, logger name, message. No structured
  key-value format (per org standard, see §8.1).
- **Levels**: File handler at INFO, stderr handler at WARNING (default)
  or as overridden by `--verbose`/`--quiet`.

All three properties are compliant with the org logging standard.

### 9.2 PII Audit

**Methodology**: Searched all logger calls for PII-adjacent terms:

```text
rg -i 'logger\.\w+.*(password|token|secret|api.?key|credential|ssn|email|phone)' src/quarry/
→ 0 matches

rg -i 'logger\.\w+.*(content|text|body|chunk)' src/quarry/
→ 6 matches (all metadata references, not content):
  - hooks.py:425     "no text in tool_response, falling back"
  - hooks.py:720     "no conversation text found"
  - database.py:110  "Created FTS index on text column"
  - ocr_local.py:152 "OCR image %s: %d chars"  (char count, not text)
  - text_extractor.py:48  "Page %d: %d chars"   (char count, not text)
  - _hook_entry.py:113    "could not read text file %s"  (path, not content)
```

Log messages contain:

- **File paths and document names**: Logged at INFO during ingest and
  sync. These are safe per the org standard -- they describe what quarry
  operated on, not user content.
- **Collection names**: Logged at INFO. User-chosen identifiers, not
  personal data.
- **Timing and counts**: Logged at INFO. No PII.

Log messages do **not** contain:

- **Document content text**: Never logged. Chunk text is stored in
  LanceDB only, never written to log files. The 6 matches above
  reference the word "text" in metadata context (column names, char
  counts, absence checks), not actual document content.
- **Embedding vectors**: Never logged. Stored in LanceDB only.
- **Query text**: Not logged at INFO. Search queries appear only at
  DEBUG level (developer trace), which is file-only and never reaches
  stderr in normal operation.
- **Credentials**: Zero matches for password, token, secret, api_key,
  credential, SSN, email, or phone in any logger call.

**Status**: Complete. No PII concerns identified.
