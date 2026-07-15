# Changelog

All notable changes to punt-quarry will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Categories: `format` (document types), `transform` (content conversions: OCR, parsing,
embedding), `connector` (data sources: local FS, cloud), `index` (storage, chunking, sync),
`query` (search and filtering), `tool` (MCP/CLI surface), `infra` (schema, build, config).

Legacy categories in older entries: `provider` (now `transform`), `pipeline` (now split
across `transform`, `index`, and `connector`).

## [Unreleased]

### Added

- **tool (daemon REST)**: two maintenance endpoints — `POST /v1/optimize`
  (compact the LanceDB table and rebuild indexes; `force` bypasses the
  fragment-count safety guard) and `POST /v1/backfill-sessions` (ingest
  historical session transcripts; `dry_run`/`collection`/`project`/`limit`) —
  each accepted as a `202` background task pollable at `/v1/tasks/{id}`. They
  are the daemon counterparts of the `quarry optimize` and `quarry
  backfill-sessions` CLI commands, returning the same result fields.
- **infra (build)**: `make openapi` renders the daemon's OpenAPI contract to
  `docs/openapi.json` from the live FastAPI app; `make check-openapi` (wired
  into `make check`) fails if the committed schema drifts from the app, keeping
  the published wire contract honest.

### Changed

- **tool (daemon REST)**: the `quarry serve` daemon's REST API is now a FastAPI
  app, and every engine route moved under a `/v1` version prefix (`/v1/search`,
  `/v1/status`, `/v1/tasks/{id}`, …). `/health` and `/ca.crt` stay unversioned
  so a client can probe liveness and bootstrap trust before it knows the wire
  version. The handlers still parse the wire by hand, so every clamp, coercion,
  and error shape is byte-identical to the prior Starlette handlers (search
  `limit` clamped to `[1,50]`, `page>=1`, the body-size guards, the always-`400`
  `/use`, the sync `409` conflict body); FastAPI supplies only the published
  OpenAPI schema, the typed `response_model` docs, and the uniform
  `{"error": …}` envelope for `422`/`HTTPException`/`500`. The CLI's remote path
  (`RemoteClient` and the `quarry login` connectivity probe) version-prefixes
  every engine route from the single `API_VERSION` source, so remote CLI parity
  is preserved. `/health` now also reports `state` (`starting`|`ready`),
  `api_version`, and `quarry_version`.

- **infra (dependencies)**: bumped runtime and tooling dependencies to their
  current releases — runtime: `mcp` 1.26.0→1.28.1, `uvicorn` 0.40.0→0.51.0,
  `pymupdf` 1.27.2.3→1.28.0, `soupsieve` 2.8.3→2.8.4; dev tooling: `ruff`
  0.15.0→0.15.21, `pyright` 1.1.408→1.1.411; CI actions: `astral-sh/setup-uv`,
  `actions/setup-python`, `actions/upload-artifact`, `codecov/codecov-action`,
  and `DavidAnson/markdownlint-cli2-action`. No behavioral changes; every bump
  passed the full `make check` gate before merge.
- **infra (CI)**: added `pyright` to the CI lint workflow alongside `mypy`, so a
  type regression that passes one checker but breaks the other can no longer
  merge green. This closes the gap that let the `mcp` 1.28.1 bump land while
  `make check` was red locally on a `reportDeprecated` finding.

### Removed

- **tool (daemon REST)**: removed the `/sync/{task_id}` and `/ingest/{task_id}`
  task-status alias routes; poll every background task through the canonical
  `/v1/tasks/{task_id}` instead (the CLI already did).
- **tool (MCP transport)**: removed the daemon-side MCP WebSocket route
  (`/mcp`) and its `run_mcp_session` handler. The daemon now serves the REST
  API only. This is the first step of the DES-031 v2.2 MCP-as-client direction
  (`docs/des-client-architecture.md`), and it clears the `reportDeprecated`
  failure from the deprecated `mcp.server.websocket.websocket_server`, restoring
  a green `make check`. The local `quarry mcp` stdio server is unchanged, so
  Claude Code MCP over stdio continues to work; remote MCP-over-daemon returns
  later in the refactor as a `QuarryClient` path. **Mitigation:** if your Claude
  Code plugin routes MCP through mcp-proxy to the daemon `/mcp` endpoint (the
  config `quarry login` writes as `wss://…/mcp`, which `.claude-plugin/plugin.json`
  prefers when present), that endpoint is gone in this interim — switch to the
  local stdio `quarry mcp` server, or stay on the prior release, until the
  remote `QuarryClient` MCP path lands.

## [1.19.0] - 2026-07-14

### Added

- **infra (daemon fd telemetry)**: the `quarry serve` daemon now logs its open
  file-descriptor usage on a fixed cadence (every 5 minutes) so a climbing count
  — the proven LanceDB deleted-index-handle leak — is visible in logs before it
  reaches `RLIMIT_NOFILE`, returns EMFILE, and requests start failing with HTTP
  500. Each sample logs `open_fds`, the soft `RLIMIT_NOFILE`, and `pct_used`
  (counted from `/proc/self/fd`, falling back to `/dev/fd`), at INFO normally and
  WARNING past 80% of the limit; an unlimited soft limit never warns. The monitor
  task starts with the server lifespan and is cancelled on shutdown; a sample
  that raises — an EMFILE mid-scan at real exhaustion, or a container with no fd
  directory — logs a single line (with the traceback) and keeps ticking rather
  than silently killing telemetry for the daemon's remaining life. Observability
  only — the leak fix itself lands separately.

- **captures (shadow repo)**: opt-in private capture shadow sync moves redacted
  session captures off the public repo into a per-project private
  `<repo>-quarry`. Enable via a `shadow:` block in `.punt-labs/quarry/config.md`
  (default `enabled: false`; remote derived as `<origin>-quarry` when unset). The
  gitignored captures dir becomes a standalone nested git repo with a fail-closed
  allowlist `.gitignore` (only `session-*.md` can be staged). New CLI: `quarry
  captures push` (re-scrub + push each enabled project's captures) and `quarry
  captures init [--create]` (bootstrap the shadow; `--create` makes the private
  remote via `gh` and verifies it is private). The push also runs automatically
  at the end of `quarry sync` (fail-open — a push failure never blocks a
  session), and via `POST /captures/push` on the daemon. Security: before every
  commit the staged `.md` bytes are re-scrubbed with the DES-036 scrubber and an
  I/O-race guard aborts the commit on any residual; a verifiably public remote is
  refused and unverifiable visibility (no `gh`) requires an explicit
  `acknowledge_unverified`. `quarry doctor` reports the shadow state (including a
  required failure when the public repo already tracks captures, with the
  `git rm --cached` + history-purge remediation). Auth reuses the user's existing
  git credentials — no new secret storage (quarry-ow3k, DES-039).

### Fixed

- **index (daemon)**: the `quarry serve` daemon no longer leaks a file
  descriptor per index rebuild. The daemon holds a LanceDB connection for its
  whole lifetime and rebuilds the FTS/scalar index on every sync;
  `create_fts_index(replace=True)` supersedes an index generation and deletes the
  old files, but LanceDB's Rust core keeps the deleted-file readers open. Over
  many syncs the descriptors accumulated until the process hit `RLIMIT_NOFILE`
  and `quarry find` began returning HTTP 500 (while short-lived CLI processes,
  which connect once and exit, never noticed — so `quarry doctor` passed). A new
  `Database.connect` now returns a self-recycling connection that reopens itself
  after a bounded number of index rebuilds, dropping the Rust reader cache and
  releasing the descriptors; recycling happens only at a table-open boundary so
  the release is clean. Confirmed a bump to the latest lancedb (0.34.0) does not
  fix the leak — it is a Rust-core reader-cache behavior present in every tested
  version — so the fix is quarry-side. A resource-invariant test tier
  (`tests/test_resource_invariants.py`) guards against regressions in CI, and
  `quarry doctor` gained an "FD headroom" check that warns before descriptor
  usage crosses 80% of the soft limit — and reports descriptor exhaustion
  (`EMFILE`/`ENFILE` raised while sampling) as a failure rather than a reassuring
  "unavailable", so the one check meant to catch exhaustion no longer passes at
  the moment it occurs.

- **index (capture)**: session capture files and WebFetch DB ingest now redact
  personally identifying information at write time, in addition to the existing
  secret and profanity scrubbing. Three write-time passes run for every capture:
  filesystem home directories (`/Users/<user>/` and `/home/<user>/` for any
  username) collapse to `~/`, email addresses become `[REDACTED:email]`, and the
  local machine hostname (resolved via `socket.gethostname()`, plus its `.local`
  and short-leaf forms) becomes `[REDACTED:hostname]`. Email redaction runs
  before hostname redaction so a hostname inside an email domain is subsumed by
  whole-email redaction rather than leaking the local part. Redaction is
  idempotent, so re-running backfill over prior captures is a no-op. Both capture
  producers (PreCompact and backfill) now write through a single `CaptureWriter`
  choke point that scrubs before an atomic write, so a scrub or write failure
  never leaves a partial or half-redacted file. WebFetch content is scrubbed
  before it reaches the pushable `web-captures` collection (quarry-fpc5).

## [1.18.2] - 2026-07-04

### Fixed

- **query (search)**: hybrid-search results matched only by the keyword (BM25)
  channel no longer report a bogus `similarity: 1.00`. They previously got a
  placeholder distance of `0`, so an off-topic keyword hit could show a perfect
  score above a genuinely-relevant semantic match. Such rows now report their
  true cosine similarity (query vs. stored vector), and a row with no usable
  vector sinks to the bottom (`-1`) instead of floating to the top.
  `SearchResult` is now a value type that owns the distance→similarity
  conversion in one place, so the CLI, HTTP, and MCP surfaces report identical,
  bounded scores (quarry-gcnf).

## [1.18.1] - 2026-07-04

### Fixed

- **query (search)**: search similarity is now a true cosine score in `[-1, 1]`.
  Embeddings were never L2-normalized and LanceDB used its default L2 metric, so
  `similarity = 1 - _distance` was unbounded and non-comparable — a passage that
  literally contained the query text could score near zero. Vectors are now
  L2-normalized to unit length in `embed_texts` (ingest and query alike, one
  choke point) and vector search uses the cosine metric, so a matching passage
  scores near `1.0` and every score is bounded. Verified end-to-end on the built
  wheel: a relevant match scored `0.0185` before and `0.5093` after. Re-ingest
  content to store the new unit-length vectors, though existing vectors still
  rank correctly under the cosine metric (quarry-3a7f).

## [1.18.0] - 2026-07-03

### Added

- **infra (oo-ratchet)**: three hardening features for `tools/oo_score.py`, the
  OO quality gate. `--verify` recomputes scores for the committed code and fails
  if any `.oo-baseline.json` entry diverges from the file's true score, catching
  a phantom baseline (one committed out of sync with its code) at PR time; it
  fails closed on a missing baseline unless `--allow-missing` is passed. It runs
  as a CI-only step (`make check-oo-integrity`, wired into
  `.github/workflows/lint.yml`), not in the local `make check` chain, because the
  ratchet requires each commit to improve a metric — which diverges from the
  not-yet-updated baseline until `make update-oo` runs. `--correct <file>
  --reason <text>` (`make correct-oo FILE=... REASON=...`) re-records ONE
  baseline entry to its true score with a mandatory, audited reason — a scoped
  fix for a proven phantom without the nuclear full `--rebaseline`. Ratio metrics
  (`avg_params`, `avg_complexity`, `method_ratio`) now tolerate a sub-0.02
  micro-regression when the file still comfortably clears its absolute threshold
  and a companion size/complexity metric improved, absorbing the denominator
  artifact from extracting a 0-param function without loosening any absolute
  threshold (quarry-0bdi).

### Changed

- **sync**: ingestion now commits progressively instead of accumulating every
  document's vectors and writing once at the end (DES-034, supersedes DES-026
  change #3). A streaming embed producer chunks each document once and embeds it
  in bounded windows, and a new `ProgressiveIndexer` flushes to LanceDB whenever
  the buffered vector bytes reach `sync_flush_mb` (default 32) — a flush can fire
  mid-document, so a single very large file no longer materializes all its
  vectors. Three user-visible consequences: **bounded memory** (peak resident
  vectors are `sync_flush_mb + one window`, independent of file or collection
  size), **progressive visibility** (each flush commits a new LanceDB version, so
  concurrent search returns partial results as a sync fills, with no read block —
  the FTS channel catches up at the post-sync rebuild), and **crash-resume** that
  is now *within-file*: the registry stores a `chunks_committed` watermark and
  `partial_hash` per file, so a resumed sync re-embeds only the incomplete tail
  `[watermark, end)` rather than the whole file or the whole collection. Resume
  deletes any post-watermark chunks before re-embedding (no duplicates) and falls
  back to a full re-embed when the file changed or the loader is non-deterministic
  (OCR). Single-document `quarry ingest` shares the same bounded, progressive
  path. New settings `sync_flush_mb` and `embed_window_chunks`. The
  `prepare_document`/`batch_insert` whole-file path is removed (quarry-4qk2).

### Fixed

- **transform (pdf)**: PDF reflow no longer garbles table-of-contents pages. The
  `quarry-qa2d` reflow joins lines that reach the block right margin, but a
  dot-leader entry (`10.1 Bearer Token Authentication . . . . . 11`) reaches the
  margin like a wrapped prose line, so consecutive TOC entries concatenated into
  runs — worse than the old hard-wrapped output. (fitz fragments each entry into
  separate title / dot-leader / page-number lines sharing a baseline; the
  page-number fragment is what reaches the margin.) Reflow now detects dot-leader
  runs (≥ 4 leader dots — a bare ellipsis or a decimal like `3.14` is excluded),
  treats a block with ≥ 2 such lines as a table of contents, and reassembles its
  fragments into one line per visual row by clustering on `y0` adjacency (so a
  mixed-font title and its smaller page number stay on the same row). Ordinary
  prose is untouched — it takes the byte-identical soft-wrap-plus-de-hyphenation
  path (quarry-e8ma).

- **tool (install)**: `quarry install` no longer reports a hard failure when the
  onnxruntime GPU wheel swap fails but the CPU runtime is successfully restored.
  The GPU-swap outcome is now classified on the `GpuStatus` enum member instead
  of substring-matching `"failed"` — `GpuStatus.RESTORED`'s message
  (`"onnxruntime-gpu install failed, CPU restored"`) contains `"failed"`, so a
  recovered swap was wrongly reported as a hard install failure (exit 1). It now
  warns (⚠) and exits 0, since the daemon still starts on CPU. Additionally, an
  *unexpected* exception during the GPU step now fails the install (✗, non-zero)
  rather than being silently skipped, so a half-completed swap that leaves the
  runtime broken can no longer be reported as success (quarry-773e).

- **transform (pdf)**: PDF text pages are now reflowed at extraction instead of
  stored hard-wrapped. Previously `pdf_text_extractor` used PyMuPDF's flat
  `page.get_text()`, which emits one newline per *visual* line, so a paragraph
  that wrapped across several screen lines was stored with spurious mid-sentence
  newlines — every consumer (`/show`, the menu-bar app, agents) had to re-guess
  paragraph structure. Extraction now reconstructs paragraphs from
  `page.get_text("dict")` block/line geometry (new `ingestion/pdf_reflow.py`,
  a `PdfReflow` value tree): soft-wrapped lines that reach the block's right
  margin are joined and de-hyphenated; a short line that closes a sentence
  before a capitalised line is kept as a paragraph break (trailing quotes and
  brackets are stripped first, so a line ending `."` or `.')` still reads as
  terminal); block boundaries become blank-line paragraph breaks; short
  schema/heading lines stay on their own line. A standalone page-number line
  (1–3 digit runs and 4-digit non-years, exempting plausible years 1000–2999)
  is stripped only when it sits in the top or bottom page margin — a numeric
  table cell or statistic in the body is kept as content — and each strip is
  logged at debug. De-hyphenation (in `ingestion/hyphenation.py`) strips the
  line-break hyphen by default so `informa-` + `tion` becomes `information`, a
  token BM25 and vector search can match; the hyphen is kept only for compound
  prefixes (`self-`, `well-`, `co-`, …) or known full compounds. If reflow
  yields empty text for a page that has extractable text (an all-numeric page,
  a missing `blocks` key), extraction falls back to the flat `get_text()` and
  logs a warning, so a whole page is never silently dropped; a line with a
  malformed bounding box is skipped rather than aborting the document.
  `page_raw_text` and the `/show` output shape are unchanged (still a plain
  string) — only the content is cleaner, so there is no schema or API migration.
  The OCR path (`ingestion/ocr_local.py`) has no per-line bounding boxes and is
  a separate follow-on.

  **Migration**: content-hash sync will not auto-re-extract already-indexed
  documents, because the source files are unchanged. Existing PDF content stays
  hard-wrapped until re-ingested — re-ingest affected documents to reflow them.

## [1.17.0] - 2026-07-03

### Fixed

- **doctor**: the "Orphaned captures" check no longer false-positives on the
  `web-captures` fallback bucket. The check flagged any `<x>-captures`
  collection whose base `<x>` wasn't a registration; `web-captures` is the
  intentional base-less fallback for web fetches with no covering registration,
  so it was reported orphaned on every run once it held any captured content.
  The fallback sentinel is now excluded (derived from
  `hooks.WEB_CAPTURES_FALLBACK`, not a duplicated literal), while a genuine
  `<project>-captures` orphaned by deregistration is still flagged. The check's
  DB/registry I/O is also now guarded, so a corrupt LanceDB table or locked
  registry returns a failed check instead of crashing the whole `quarry doctor`
  run (quarry-ty14).
- **deregister**: the remote/daemon path now matches the local path across all
  three surfaces (CLI, HTTP, MCP). `quarry deregister <nonexistent>` returns
  exit 1 with `No registration found for '<collection>'` instead of the old
  fire-and-forget exit 0 "Deregister accepted" (quarry-noiw): the daemon
  validates the registration synchronously and returns 404. The CLI now polls
  the async chunk-purge task and surfaces a failed or timed-out purge as a
  non-zero exit with the server's error, instead of printing success and dying
  silently (quarry-xsz3). `SyncRegistry` connections set
  `PRAGMA busy_timeout=5000`, so a deregister contending with a concurrent sync
  waits for the write lock rather than failing instantly with "database is
  locked". The MCP `deregister_directory` tool is likewise synchronous with the
  same not-found and failure surfacing. Remote HTTP client helpers were
  extracted from `__main__.py` into a new `remote_client.py` module. See the
  DES-026 amendment (2026-07-01).
- **embedding**: GPU→CPU ONNX fallback now runs at the CPU thread budget. The
  CPU fallback session reused the CUDA `SessionOptions` (which pinned
  `intra_op_num_threads=1` because the GPU does the GEMMs), so a degraded daemon
  ran single-threaded instead of the designed `min(2, ncpu)` CPU parallelism.
  `OnnxSessionBuilder._build_cpu_fallback` now builds a fresh
  `ThreadConfig(is_gpu=False)` and fresh options (DES-032).
- **embedding**: `ThreadConfig.apply_env_limits` now logs the EFFECTIVE
  `OMP_NUM_THREADS` read back from the environment, not the intended cap. When a
  preset value (systemd/Docker) diverges from the computed cap it emits a
  `logger.warning` that the DES-032 oversubscription mitigation may be defeated —
  previously the logs falsely claimed the fix was active. `ThreadConfig` also
  warns when `os.cpu_count()` returns `None` and the 4-CPU fallback triggers,
  rather than silently guessing the budget.
- **serve**: Daemon warm-up now logs each resource phase distinctly (write db,
  isolated query db, query ONNX session, ready). Previously the serve path
  logged only "Loading embedding model...", so a `query_database` failure was
  mis-attributed to the embedding model. The misleading "Loading embedding
  model" / "Embedding model ready" pair in `http_server.serve` is removed.

### Changed

- **infra**: Add `.github/dependabot.yml` (uv + github-actions, weekly) that
  ignores Starlette major versions (`>=1.0.0`). Starlette v1 breaks HTTP route
  handling in `src/quarry/http_server.py` (`build_app`); the project pins
  `starlette<1.0.0`. Closed PR #297 and Cursor Bugbot's HIGH "Starlette 1.x
  route regression" finding prompted this guard so Dependabot stops reopening
  the unsafe bump. 0.x patch/minor updates remain allowed.

## [1.16.0] - 2026-05-11

### Added

- **cli**: `quarry enable` and `quarry disable` commands. Single command
  to set up all three knowledge capture types for a project: file sync
  (directory registration), passive captures (web fetches and session
  transcripts routed to `<name>-captures` collection), and agent memory
  (ethos identity extensions bootstrapped automatically).
- **cli**: `quarry disable --keep-data` flag to remove registration
  without deleting indexed data.
- **hooks**: Session-start captures and web-fetch captures now route to
  `<name>-captures` instead of mixing into the file-sync collection.
  Falls back to `web-captures` / `session-notes` when no registration
  covers the cwd.
- **hooks**: Session-start walk-up matching — opening a session in a
  subdirectory of a registered parent uses the parent's collection
  instead of crashing with ValueError.
- **hooks**: Descendant guard — auto-registration skips when the cwd
  is a parent of existing child registrations, preventing subsumption.
- **doctor**: `Enable status` check reports whether the cwd has quarry
  enabled and whether config.md exists.
- **doctor**: `Orphaned captures` check reports captures collections
  whose base registration has been removed.
- **test**: `make test-wheel` target builds the wheel, installs in an
  isolated venv, and runs smoke checks on port 8422 alongside the
  production daemon. Caught two dependency bugs on first run
  (tree-sitter-language-pack 1.x, starlette 1.0).
- **test**: `make check-full` = `make check` + `make test-wheel`.

### Fixed

- **deps**: Pin `tree-sitter-language-pack<1.0.0` — v1.x removed
  `SupportedLanguage`, breaking quarry on fresh wheel installs.
- **deps**: Pin `starlette<1.0.0` — v1.0 breaks route handling.

## [1.15.0] - 2026-04-18

### Fixed

- **tool**: Progress bar wrote to stdout, polluting pipes. Moved to
  stderr via `err_console`.
- **tool**: `uninstall` command wrote result to stdout via `console`
  instead of `_emit`.
- **tool**: `login` abort message used bare `print()` to stdout.
- **tool**: `status` command missing `embedding_dimension` in local
  JSON output (present in remote).
- **infra**: Aggressive jemalloc tuning for daemon memory. MALLOC_CONF
  now sets `narenas:1,tcache:false,dirty_decay_ms:1000,muzzy_decay_ms:0`.
  LanceDB's Rust core retains freed Arrow buffer arenas indefinitely;
  this config reduces post-sync RSS from 5.4 GB to 1.1 GB (80%
  reduction). Empirically tested across 4 variants — single arena +
  no thread-local cache eliminates fragmentation from batch writes.
- **index**: `delete_document` called `count_rows()` twice per file
  during sync, scanning all fragment metadata on every deletion.
  On a 62K-row table this added 4-7 seconds per file. Added
  `count=False` fast path; sync and pipeline callers skip counting.
- **index**: `optimize_table` cleanup window reduced from 7 days to
  1 hour. Daily syncs that re-embed files produced tombstoned
  fragments that accumulated for a week, causing 416 MB disk growth
  per sync cycle.
- **index**: Explicit `del chunk_batch` + `gc.collect(0)` after
  batch insert in `sync_collection` to release numpy arrays
  promptly. Full `gc.collect(2)` + RSS logging at end of `sync_all`.

### Changed

- **api**: All mutating HTTP endpoints now return 202 + task_id.
  Unified `TaskState` with `kind` field replaces per-operation
  `SyncTaskState` and `IngestTaskState`. Single polling endpoint
  `GET /tasks/{task_id}` (with `/sync/{id}` and `/ingest/{id}` as
  aliases). Endpoints converted: `/remember`, `/documents` DELETE,
  `/collections` DELETE, `/registrations` POST/DELETE. `/sync` keeps
  409 for concurrent requests; all others allow concurrency.
- **tool**: CLI remote paths for remember, delete, register, and
  deregister switched to fire-and-forget (print task_id, exit 0).

### Added

- **tool**: `--verbose` / `-v` now streams INFO-level diagnostic logs
  to stderr (sync plans, embedding throughput, batch timing). Was a
  no-op previously.
- **tool**: `--quiet` / `-q` suppresses all stderr output (progress,
  warnings, INFO logs). Fatal errors still shown.
- **tool**: `quarry remember` now shows a progress spinner in local
  mode.
- **infra**: `QUARRY_LOG_LEVEL` env var overrides the flag-derived
  stderr level. Third-party loggers (lancedb, onnxruntime, httpx)
  pinned at WARNING.
- **api**: Task garbage collection — completed/failed tasks evicted
  after 1-hour TTL on next task creation.
- **test**: 14 JSON equivalence tests covering local/remote shape
  divergence for all fire-and-forget commands (Class 3 pattern).
- **test**: 57 edge-case tests for CLI flag combinations, pipe safety,
  progress on stderr, fatal errors under --quiet.
- **docs**: Operation concurrency model appendix in architecture.tex.
- **infra**: `make docs` now builds Z-spec PDFs using local Oxford Z
  fonts in `docs/tex/` (was broken due to missing `oxsz10.mf`).

## [1.14.0] - 2026-04-17

### Fixed

- **index**: compaction death spiral from unguarded concurrent sync.
  The serve process accumulated 133K LanceDB fragments (83 GB) and
  burned 13 CPU cores for 5 days. Five fixes: server-side sync lock
  (409 on concurrent POST /sync), registration subsumption (parent
  deregisters children), batched LanceDB writes (single table.add per
  collection sync), optimize_table guard (skip above 10K fragments),
  async sync endpoint (202 + task_id, fire-and-forget CLI).

### Added

- **tool**: `quarry optimize` CLI command with `--force` flag for
  manual compaction of degraded databases.
- **tool**: `GET /sync/{task_id}` HTTP endpoint for polling sync
  status.

## [1.13.0] - 2026-04-12

### Added

- **tool**: `quarry doctor` now checks FTS index health, sync recency across
  registered collections, and existence of registered sync directories.
- **tool**: `/use <database>` slash command for switching databases. Also
  available as `/quarry use <name>`.

### Changed

- **tool**: `_sync_in_background` now returns `"launched"`, `"running"`, or
  `"failed"` instead of a boolean. Session-start context message distinguishes
  "sync already running" from "sync failed to launch".
- **infra**: Replace `rglob("*")` size calculations with `du`-based
  `dir_size_bytes()` helper across 6 call sites. Reduces `quarry list databases`
  from ~30s to <1s on large (59K file) lance directories.
- **infra**: `_configure_claude_code()` now generates `mcp-proxy --config quarry`
  (reads TLS + bearer from TOML) instead of bare `mcp-proxy ws://localhost:8420/mcp`.
  Falls back to `quarry mcp` when mcp-proxy or the TOML profile is absent.

### Fixed

- **infra**: `_quarry_exec_args()` no longer falls back to `sys.executable`
  or `shutil.which("quarry")` when the uv tool binary is absent. Raises
  `RuntimeError` if `~/.local/bin/quarry` does not exist. Prevents baking
  a dev venv Python path into systemd/launchd units, which caused
  crash-loops from CPU-only onnxruntime.
- **infra**: Updated stale "As of v1.11.0" remote routing references in
  DESIGN.md and architecture.tex to reflect v1.12.4 state (12 commands now
  route remotely).
- **infra**: Mock `_systemd_install` and `_launchd_install` in
  `TestRunInstall` to prevent flakes on CI/dev machines without user systemd.
- **infra**: `install.sh` plugin uninstall now only suppresses "not installed"
  errors. Other failures (permissions, network) emit a warning instead of
  being silently swallowed.

## [1.12.4] - 2026-04-11

## [1.12.3] - 2026-04-11

### Changed

- **infra**: Simplified `install.sh` from three modes (`--server`/`--client`/default)
  to two: default and `--network`.  Default installs everything (CLI, model, daemon
  on localhost, GPU swap, plugin if claude CLI found, local quarry login).
  `--network` is the same but binds daemon to 0.0.0.0 and requires
  `QUARRY_API_KEY`.  Claude Code plugin install is now optional -- skipped with a
  note when `claude` CLI is not on PATH, instead of failing.  Clients no longer
  need a `--client` flag; just install normally and `quarry login <server>`.
  Removed `--server` and `--client` flags.

## [1.12.2] - 2026-04-11

### Fixed

- **infra**: Install scripts (`install-server.sh`, `install-client.sh`,
  `install-both.sh`) regressed the shell-level onnxruntime → onnxruntime-gpu
  swap when they were split out of `install.sh`, so NVIDIA users ran the
  one-liner and silently ended up on `CPUExecutionProvider` with a CPU-only
  `onnxruntime` wheel in the tool venv. The split installers deferred GPU
  detection to `ensure_gpu_runtime()` in `src/quarry/service.py`, which under
  real conditions returned `"onnxruntime-gpu installed"` (rc=0) while the GPU
  wheel was absent from `site-packages` afterward (quarry-mxi9, needs rmh
  investigation). Ported the 40-line shell-level GPU swap block from
  `install.sh` into all three split installers. The swap runs after
  `uv tool install --force` (which re-pins the CPU wheel from `pyproject.toml`)
  and before `quarry install` (so the service-managed daemon starts with CUDA
  providers available). Added `tests/test_install_scripts.py`, a shell
  integration test that invokes each script against a mock `quarry` + mock
  `uv` + mock `nvidia-smi` under a restricted `PATH` and asserts the required
  call ordering (`uv tool install --force` → `uv pip uninstall onnxruntime` →
  `uv pip install onnxruntime-gpu` → `quarry install`). `install-server.sh`
  and `install-both.sh` also force a `systemctl --user restart quarry` /
  `launchctl kickstart -k` between `quarry install` and the health check, as
  belt-and-suspenders against a stale daemon that started before the tool-venv
  swap. See bead quarry-e4c2 and follow-up bead quarry-0z84 (factor into a
  shared sourced fragment so the drift can't recur).
- **infra**: Install scripts (`install-server.sh`, `install-client.sh`,
  `install-both.sh`) pinned `VERSION=1.11.0` after the 1.12.1 release, so the
  one-liner silently installed a version-behind release. Bumped to `1.12.1`.
  README.md install URLs repinned from a stale commit SHA (`fa18b25`, predates
  1.12.1) to the commit that contains the bumped scripts — keeping the
  install-time source immutable while fetching the up-to-date `VERSION`.

## [1.12.1] - 2026-04-09

### Fixed

- **tool**: All six remote-calling CLI commands (`find`, `status`, `list documents`,
  `list collections`, `list registrations`, `list databases`) now print a one-line
  error and exit 1 when the daemon is unreachable, instead of dumping a raw
  `ConnectionRefusedError` traceback. `_remote_https_request` wraps `OSError` as
  `RemoteError` at the transport layer so all callers see a consistent exception
  type.
- **transform**: `_auto_workers` selects 4 workers when the active ONNX execution
  provider is `CUDAExecutionProvider`, up from a hardcoded 1. Parsing is the
  bottleneck on GPU hosts and is parallelizable; CPU-only hosts remain at 1 worker.
  Respects `QUARRY_PROVIDER` env var.
- **infra**: Fixed 17 pre-existing test failures caused by `onnxruntime` namespace
  corruption in dev venvs. `_patch_onnx_backend` is now a context manager and all
  patches use `create=True` for attributes missing from the broken namespace.

## [1.12.0] - 2026-04-09

### Added

- **tool**: `POST /sync` endpoint — trigger background sync of registered
  directories remotely.
- **tool**: `GET /databases` endpoint — list server-visible databases.
- **tool**: `POST /use` endpoint — returns 400; database selection is
  client-side only.
- **tool**: `GET /registrations`, `POST /registrations`, `DELETE /registrations`
  endpoints — manage registered directories remotely.
- **tool**: `quarry sync`, `quarry register`, `quarry deregister`, and
  `quarry list registrations` route to remote when configured.
- **security**: `POST /registrations` rejects directories outside the server
  process's `$HOME` to prevent exfiltration of sensitive paths via subsequent
  sync.
- **tool**: `POST /remember` endpoint — accept inline text content for remote
  ingestion via JSON body.
- **tool**: `POST /ingest` endpoint — accept URL for remote ingestion via JSON
  body. File upload is deferred.
- **tool**: `quarry remember` and `quarry ingest <url>` route to remote when
  configured.
- **tool**: `GET /show` endpoint — retrieve document metadata or page text remotely.
- **tool**: `DELETE /documents` and `DELETE /collections` endpoints — delete indexed
  data remotely. Returns 404 if the resource does not exist.
- **tool**: `quarry show` and `quarry delete` route to remote when configured.
- **infra**: Generalized `_remote_https_request(method, path, config, body)` helper
  supporting GET, POST, and DELETE. Thin `_remote_https_get` wrapper preserved for
  backward compatibility. Handles JSON body encoding, 204 No Content, and non-2xx
  error reporting.
- **tool**: `quarry list documents` and `quarry list collections` route to the
  remote HTTPS API when a remote server is configured.
- **infra**: CORS middleware now allows POST and DELETE methods (previously GET only).
- **infra**: Shared `_format_documents_text` and `_format_collections_text` formatters
  ensure remote and local output paths produce identical output.

### Fixed

- **connector**: Fall back to single-page ingestion when sitemap discovery finds pages but path filtering yields zero matches — previously silently ingested nothing for sites with partially parseable sitemaps (e.g. namespace-prefixed XML)
- Removed stale `noqa: S603` suppression in `hooks.py`.

## [1.11.0] - 2026-04-01

### Added

- **tool**: `quarry login <host> [--port N] [--api-key KEY] [--yes]` — TOFU login
  flow: fetches server CA cert over HTTPS (verify-off bootstrap), displays SHA256
  fingerprint, prompts for confirmation, stores pinned CA cert, validates connection,
  writes `~/.punt-labs/mcp-proxy/quarry.toml` with `wss://` URL and `ca_cert` path.
- **tool**: `quarry logout` — removes quarry section from mcp-proxy config.
- **tool**: `quarry remote list [--ping]` — shows configured remote server;
  `--ping` validates connectivity with the pinned CA cert.
- **tool**: `quarry find` and `quarry status` route to the remote HTTPS API when
  a remote server is configured in `quarry.toml`.
- **infra**: TLS certificate generation — self-signed EC P-256 CA and server cert
  with full x509 extension set. Certs written atomically to `~/.punt-labs/quarry/tls/`
  with 0600/0644 permissions.
- **infra**: `quarry serve --tls` — enables HTTPS/WSS; TLS certs auto-generated
  via `quarry install` before serving.
- **infra**: `/ca.crt` HTTP endpoint (auth-exempt) — serves CA cert PEM for TOFU
  bootstrap.
- **infra**: `install-server.sh` — server-only installer (no claude CLI required).
- **infra**: `install-client.sh` — client-only installer (no model or daemon).
- **infra**: `install-both.sh` — single-machine installer with loopback TLS.
- **plugin**: mcp-proxy invocation updated to `mcp-proxy --config quarry`.

### Fixed

- **infra**: `quarry install` now detects NVIDIA GPUs and swaps `onnxruntime`
  for `onnxruntime-gpu` automatically. Previously this logic lived only in the
  install shell scripts, so upgrading via `uv tool install --force` would lose
  CUDA support. Now works regardless of installation method.
- **infra**: `quarry install` now restarts the quarry systemd service after cert
  regeneration. Previously `systemctl enable --now` did not restart an
  already-running service, causing it to serve stale TLS certs.
- **infra**: CA cert CN is now `"Quarry CA"` instead of hostname-scoped
  `"Quarry CA (hostname)"`. The CA is identified by its SHA256 fingerprint
  (TOFU), not its CN.

## [1.10.1] - 2026-03-29

### Fixed

- **infra**: `install.sh` detects NVIDIA GPUs via `nvidia-smi` and swaps
  `onnxruntime` for `onnxruntime-gpu` in the tool venv, enabling
  CUDAExecutionProvider on machines with NVIDIA hardware. Rolls back to
  CPU onnxruntime if GPU install fails.

## [1.10.0] - 2026-03-29

### Added

- **tool**: `quarry doctor` reports active ONNX provider and model file as
  informational check.
- **tool**: `quarry status` shows Provider line (e.g. "CPUExecutionProvider
  (int8)" or "CUDAExecutionProvider (fp16)").

## [1.9.1] - 2026-03-29

### Fixed

- **tool**: `quarry --version` now works (was "No such option"). Added eager
  `--version` callback to the typer app.
- **tool**: CLI help output uses plain text instead of rich markup panels,
  per CLI standard.
- **tool**: Help command ordering: product commands first, admin commands after.
- **tool**: `hooks` subcommand hidden from `--help` (internal, not user-facing).

## [1.9.0] - 2026-03-29

### Added

- **transform**: Auto-detect ONNX execution provider at startup. Selects
  CUDA+FP16 when available, falls back to CPU+int8. `QUARRY_PROVIDER` env var
  overrides: `cpu` (force CPU), `cuda` (force CUDA, fail loudly), unset
  (auto-detect). Session options use `ORT_ENABLE_ALL` for graph optimizations.
- **infra**: `quarry install` downloads FP16 model on CUDA-capable machines.

### Changed

- **infra**: Removed `ONNX_MODEL_FILE` constant from config.py. Model file
  is now derived from provider selection via `provider.py`.

## [1.8.1] - 2026-03-29

### Added

- **infra**: `quarry install` step 7/7 writes `session_context` into ethos
  identity extension files (`~/.punt-labs/ethos/identities/<handle>.ext/quarry.yaml`).
  Migrates existing agents that have `memory_collection` but no `session_context`.
  Uses raw file append to preserve YAML comments and formatting. Per-identity
  exception handling ensures one malformed file doesn't abort the rest. Missing
  `memory_collection` is surfaced in the output.

## [1.8.0] - 2026-03-28

### Changed

- **index**: PreCompact hook spawns ingestion as a background process instead of
  blocking compaction. Reduces hook latency from ~30s to <1s.
- **tool**: PreCompact systemMessage now includes collection name and document
  handle for actionable retrieval via `/find`, replacing the uninformative chunk
  count.
- **query**: `find` CLI and MCP tool now use hybrid search (vector + BM25 FTS
  with Reciprocal Rank Fusion) instead of vector-only search.

### Added

- **infra**: Schema migration adds `agent_handle`, `memory_type`, and `summary`
  columns to LanceDB chunks table. Existing databases are migrated automatically.
- **infra**: Tantivy full-text search (BM25) index on the `text` column, created
  or replaced on every table open.
- **query**: Hybrid search with RRF fusion across vector and FTS channels.
  Optional temporal decay via `decay_rate` parameter (default 0.0 = disabled).
- **tool**: `--agent-handle`, `--memory-type`, and `--summary` options on
  `quarry ingest`, `quarry remember`, and `quarry find` CLI commands.
- **tool**: `agent_handle` and `memory_type` filter parameters on MCP `find` tool.
- **tool**: `agent_handle`, `memory_type`, and `summary` parameters on MCP
  `remember` tool.
- **index**: PreCompact hook reads ethos sidecar config to tag ingested content
  with the current agent's handle.
- **infra**: Per-phase timing instrumentation across sync, embedding, and
  pipeline. Logs wall-clock time for: plan computation, per-file ingestion,
  per-batch embedding (including tokenization), LanceDB writes, deletes,
  index creation, table optimization, and total sync duration.

### Fixed

- **infra**: PreCompact background process redirects stdin to DEVNULL (prevents
  fd leak holding Claude Code's stdin pipe open). Background process calls
  `configure_logging()` to write diagnostics to `~/.punt-labs/quarry/logs/quarry.log`.
- **infra**: PreCompact Popen guarded with try/except OSError — cleans up temp
  file and fails gracefully instead of crashing the hook.
- **infra**: Adopted logging standard (`logging_config.py` with `dictConfig`,
  5MB rotating file, `0o700` directory permissions).

## [1.7.1] - 2026-03-26

### Fixed

- **infra**: PreCompact hook returned invalid `hookSpecificOutput` schema (hookEventName "PreCompact" not recognized by Claude Code); use top-level `systemMessage` instead

## [1.7.0] - 2026-03-26

### Fixed

- **Pre-compact deduplication** — each compaction now deletes prior captures for the same session before ingesting the new transcript. Previously, repeated compactions accumulated redundant documents (session 64b2aacf had 14 copies). Dedup is fault-tolerant: failures log and proceed with ingestion.
- **Enhanced transcript extraction** — short tool results (<= 500 chars) are now included in pre-compact captures, prefixed with `[tool_result]`. Long tool results and tool_use blocks remain excluded. Truncation now drops oldest content first (front-truncation), keeping the most recent conversation.

### Added

- **Raw JSONL archival** — pre-compact hook now copies the raw transcript to `~/.punt-labs/quarry/sessions/` before extraction. Archives are deduplicated per session and pruned after 90 days. Archival is fault-tolerant: failures log and proceed with ingestion.
- **Knowledge recall hints** — SessionStart context now leads with a behavioral nudge ("check quarry before researching"). PreCompact returns confirmation that the transcript was captured and prior conversations are searchable.

### Changed

- **Project-scoped captures** — web fetch auto-ingestion and pre-compact transcript capture now scope to the project's registered collection instead of global `web-captures` / `session-notes` buckets. Falls back to global collections when cwd has no registration.

## [1.6.0] - 2026-03-26

### Added

- **`docs/architecture.tex`** — comprehensive LaTeX architecture document covering system design, daemon model, module responsibilities, wire protocol, configuration, search tuning, logging standards, security, deployment, and test architecture. Consolidates content from four separate markdown files into one authoritative document
- **`researcher` agent** — plugin subagent that combines quarry local search with web research. Searches quarry first, web for gaps, auto-ingests valuable findings so research compounds across sessions
- **CLAUDE.md injection** — `quarry install` appends a quarry capabilities section to `~/.claude/CLAUDE.md` so agents discover quarry's tools and commands in every project
- **AGENTS.md** — rewritten as an agent-first guide to quarry integration: MCP tools, slash commands, hooks, subagents, architecture, and integration patterns

### Changed

- **README.md** — rewritten to lead with Claude Code (primary use case), condensed MCP tools to a table, removed quarry-menubar section, reduced from 344 to 178 lines
- **`DESIGN.md`** — slimmed to ADRs only; architecture and module tables moved to `docs/architecture.tex`
- **`docs/claude-code-quarry.tex`** — refreshed implementation validation section to reflect current hook wiring status (all three knowledge capture hooks are now wired)
- **SessionStart context** — fixed stale MCP tool names (`search_documents`/`get_page` → `find`/`show`), added slash command list and researcher agent mention
- **`session-start.sh`** — refactored from 88-line shell script with business logic to 3-line thin gate per punt-kit hook standard; command deployment and permissions logic moved to Python in `_stdlib.py`
- **`prfaq.tex`** — merged `prfaq-ambient.tex` into single document reflecting current project state; removed references to deleted features (AWS, convention hints, quarry-menubar)
- **`TESTING.md`** — moved to `docs/TESTING.md`
- **Directory standard** — user data moved from `~/.quarry/` to `~/.punt-labs/quarry/` per org filesystem standard. Per-project config moved from `.claude/quarry.local.md` to `.punt-labs/quarry/config.md`. Logs moved to `~/.punt-labs/quarry/logs/`. No automatic migration — run `mv ~/.quarry/data ~/.punt-labs/quarry/data` to preserve existing databases.

### Removed

- **Convention hint hooks** — removed the entire PreToolUse/Bash hook system (instant rules, sequence rules, Bash-command accumulator). Dev workflow conventions belong in CLAUDE.md, not in a knowledge management product
- **AWS backends** — removed Textract OCR and SageMaker embedding backends, all AWS infrastructure (CloudFormation templates, deployment scripts, IAM policies), and boto3/botocore dependencies. Local backends (RapidOCR, ONNX) always outperformed AWS in testing
- **`docs/ADVANCED-CONFIG.md`**, **`docs/SEARCH-TUNING.md`**, **`docs/NON-FUNCTIONAL-DESIGN.md`** — absorbed into `docs/architecture.tex`
- **`docs/TOOL-PyPI.md`** — obsolete manual publishing checklist; releases use `.github/workflows/release.yml`
- **`docs/build-plan-ppv/`** — completed design work, no longer needed
- **`docs/prd/quarry-menubar.md`**, **`docs/sparc/quarry-menubar-implementation.md`** — quarry-menubar is a separate repo
- **`prfaq-ambient.tex`** — merged into `prfaq.tex`
- **`data/`** — stale development-era LanceDB with old table schema

## [1.5.2] - 2026-03-15

### Fixed

- **launchd service upgrade fails silently** — `launchctl load` does nothing when
  a service with the same label is already registered with a different binary path.
  The old binary kept respawning via KeepAlive, ignoring the new plist. Fix: check
  if the service is loaded and `unload -w` first, then write the new plist and
  `load`. (#106)

## [1.5.1] - 2026-03-15

### Fixed

- **Concurrent background syncs** — SessionStart hook spawned a new `quarry sync`
  on every session open/resume with no guard against concurrent instances. 7
  simultaneous sessions produced 7 sync processes (580% CPU, 6.8 GB RAM). Fix:
  atomic `O_CREAT|O_EXCL` lock file in `~/.quarry/sync.pid`, with proper EPERM
  handling and separated error paths for Popen vs pidfile write failures. (#103)
- **suppress-output hook missed quarry-proxy tools** — PostToolUse matcher for
  suppressing verbose MCP output only matched `quarry` tools, not `quarry-proxy`
  tools. (#103)

## [1.5.0] - 2026-03-13

### Fixed

- **Stale README install.sh SHA** — install command referenced SHA `b10f69c` but
  the script had changed to `fcf0d67`, causing checksum verification failures for
  new users.

## [1.4.0] - 2026-03-13

### Added

- **MCP-over-WebSocket endpoint** (`/mcp`) — Multiple Claude Code sessions can
  share a single `quarry serve` daemon over WebSocket instead of spawning
  separate MCP server processes. Uses `mcp-proxy` compatible JSON-RPC framing.
- **Per-session database isolation** — Each MCP session gets its own ContextVar
  for `_db_name`, so `use_database("work")` in one session doesn't affect others.
- **WebSocket auth** — Bearer token authentication checked before WebSocket
  accept (close code 1008 on failure). Auth-exempt when no API key configured.
- **Daemon lifecycle management** — `quarry install` now registers quarry as a
  system daemon (launchd on macOS, systemd on Linux). The daemon runs
  `quarry serve --port 8420`, starts at login, and restarts on crash.
  New `quarry uninstall` command removes the service.
- **mcp-proxy auto-install** — `quarry install` downloads the `mcp-proxy`
  binary from GitHub Releases (platform-specific, SHA256-verified) to
  `~/.local/bin/`. The quarry plugin uses mcp-proxy as its MCP transport,
  eliminating Python startup cost for every Claude Code session.

### Changed

- **Default port for `quarry serve`** — Changed from `0` (OS-assigned random
  port) to `8420` (fixed well-known port). Enables static `mcp-proxy` configs
  pointing at `ws://localhost:8420/mcp`. Override with `--port`.
- **HTTP server migrated to Starlette + uvicorn** — Replaced stdlib
  `ThreadingHTTPServer` with async ASGI for native WebSocket support and
  concurrent request handling. All existing REST endpoints preserved.
- **Port file written after confirmed bind** — Port file now written only after
  uvicorn has bound the socket, eliminating the race where readers could see a
  port that isn't yet listening.

### Fixed

- **`mcp` dependency pinned to `<2.0.0`** — Protects against private API
  (`_mcp_server`) breakage on major version bumps.

## [1.3.9] - 2026-03-11

### Changed

- **Hook cold start 6x faster** — New `quarry-hook` console script dispatches
  hook events via dict lookup without importing the full CLI stack (typer,
  pydantic, lancedb, onnxruntime). Extracted stdlib-only helpers into
  `_stdlib.py` and added PEP 562 lazy loading to `__init__.py`. Shell scripts
  now invoke `quarry-hook` instead of `quarry hooks`. Cold start dropped from
  1.48s to 0.24s. (`infra`)

### Fixed

- **Config parser handles blank lines and comments** — The stdlib YAML parser
  for `.claude/quarry.local.md` now correctly skips blank lines and indented
  comment lines within the `auto_capture` block instead of terminating parsing
  early. Also supports YAML boolean aliases (`yes`/`no`/`on`/`off`) and fails
  closed on unrecognized values. (`infra`)

## [1.3.8] - 2026-03-11

## [1.3.7] - 2026-03-10

## [1.3.6] - 2026-03-10

## [1.3.5] - 2026-03-10

### Fixed

- **Session start hook blocks on sync** — `handle_session_start` called
  `sync_collection` synchronously inside the SessionStart hook, blocking
  session startup for 10+ seconds on projects with changed files. The sync
  (file discovery, text extraction, ONNX embedding) is a pure side effect
  that the hook's return value doesn't depend on. Moved sync to a detached
  `quarry sync` subprocess via `_sync_in_background()`, which syncs all
  registered directories (not just the current project). Registration and
  context injection remain synchronous; sync runs fire-and-forget. Present
  since v0.10.0 (2026-02-24), 12 releases affected.

## [1.3.4] - 2026-03-10

## [1.3.3] - 2026-03-10

## [1.3.2] - 2026-03-09

### Fixed

- **Session start hook hang** — `sys.stdin.read()` blocks until EOF.
  When Claude Code does not close the stdin pipe for SessionStart hooks,
  `quarry hooks session-start` hung forever, freezing session resume.
  Added `_read_hook_stdin()` using non-blocking `os.read()` in a
  `select` loop with 50ms inter-chunk timeout. See biff DES-027.

## [1.3.1] - 2026-03-09

## [1.3.0] - 2026-03-09

## [1.2.0] - 2026-03-09

### Added

- **Convention hints via PreToolUse hook** — Passive, non-blocking hints that surface project conventions when agent commands drift: `git add -A` → stage specific files, `pip install` → use uv, `git commit` without full quality gate → reminder. Two-class rule system: instant rules (single command regex) and sequence rules (temporal context from a rolling event accumulator). All hints use `permissionDecision: "allow"` — advisory only, never blocking. Configurable via `convention_hints: false` in `.claude/quarry.local.md`.

## [1.1.0] - 2026-03-09

### Fixed

- **Hook wiring gap** — Three Python hook handlers (`handle_session_start`, `handle_post_web_fetch`, `handle_pre_compact`) were fully implemented but never invoked. Shell scripts in `hooks/` didn't call them, and hooks.json was missing PostToolUse/WebFetch and PreCompact entries. Added `session-sync.sh`, `web-fetch.sh`, and `pre-compact.sh` thin dispatchers and registered all three in hooks.json. Sessions now auto-register and sync the codebase, auto-capture fetched URLs, and preserve transcripts before compaction.

## [1.0.2] - 2026-03-08

### Tool

- **`quarry serve` Fly.io deployment** — HTTP server supports `--host 0.0.0.0` for container environments. Threaded request handling for concurrent clients. Configurable CORS origins via `--cors-origin`. (#86, #87, #88)
- **Bearer token auth** — `--api-key` flag enables `Authorization: Bearer` authentication on all HTTP endpoints (#85)

### Infra

- **Chat database expansion** — `sync-chat-db.sh` now ingests the full punt-labs.com content surface: reading list, press releases, demos, research files (md/pdf/docx), projects.json and radar.json (via JSON→markdown conversion), and rendered HTML pages. Fixes macOS→Linux tar xattr issues (`--no-xattrs`). (#89)
- **Fly.io auto-stop disabled** — machine runs continuously for zero cold-start latency on chat widget requests

### Fixed

- Redact query strings from HTTP access logs (CWE-532) (#84)
- HTTP request logging at INFO level with search query details (#83)
- Include README.md in Docker build for uv build backend (#88)

## [1.0.1] - 2026-03-07

### Tool

- Fire-and-forget for side-effect MCP tools (#81)

### Fixed

- `--json` flag produces valid JSON for every CLI command (#80)
- 38 new tests for CLI error paths, flag passthrough, and edge cases
- Remove `[skip ci]` from release-plugin.sh (suppressed tag-triggered releases)

### Docs

- Add DESIGN.md and update stale documentation (#82)

## [1.0.0] - 2026-03-06

### Tool

- **CLI/MCP surface rework** — unified verbs across CLI, MCP tools, and slash commands:
  - `search` → `find` (CLI and MCP)
  - `ingest-file`, `ingest-url`, `ingest-sitemap` → unified `ingest` with auto-detection
  - New `remember` command for inline text content (CLI + MCP + `/remember` slash command)
  - New `show` command for document metadata and page text (replaces `get_page`)
  - New `status` CLI command (database dashboard)
  - New `use` CLI command with persistent default database (`~/.quarry/config.toml`)
  - `list` requires a noun: `list documents|collections|databases|registrations`
  - `delete` and `delete-collection` → unified `delete` with `--type` flag
  - Global flags: `--json`, `--verbose`, `--quiet`, `--db`
  - `version` command
- **Dev/prod plugin isolation** — plugin installs from `main` now use a `-dev` suffix (`quarry-dev`) so development and marketplace installs don't collide. Session-start hook derives MCP namespace from `plugin.json` name instead of hardcoding. Restore script auto-detects release commits and guards against no-op runs. (#74, #75)

### Infra

- **Pyright strict mode** — zero errors under strict type checking. Cross-module helpers renamed to drop `_` prefix (reserved for module-private). Test-only suppressions scoped via execution environments. (#79)
- **Doctor subprocess timeout** — `quarry doctor` Claude Code MCP check now has a 10s timeout instead of blocking indefinitely (#79)
- **Installer stdin fix** — `install.sh` no longer consumes stdin when piped via `curl | sh`, preventing silent hangs during interactive prompts
- **Doctor exit code** — `quarry doctor` no longer aborts the installer when it reports warnings (#71)
- Development status classifier updated from Alpha to Beta

## [0.10.1] - 2026-02-28

### Infra

- Installer rewritten to use marketplace plugin install pattern
- Installer auto-installs Python 3.13 via `uv python install` when system Python is too old (Ubuntu 24.04 ships 3.12)
- Installer checks for git before marketplace operations, failing fast with a clear message instead of opaque errors
- Installer uses uninstall-before-install for idempotency (`claude plugin update` is unreliable)
- Installer adds read-after-write verification after plugin install
- Installer output helpers normalized to standard `▶ ✓ ! ✗` format

## [0.10.0] - 2026-02-25

### Tool

- **Automagic knowledge capture** — Claude Code plugin hooks now automatically capture knowledge without manual indexing:
  - **Session start** — auto-registers the project directory and runs incremental sync on every session. Returns context to Claude about what's indexed.
  - **Post web fetch** — every URL Claude fetches is auto-ingested into a `web-captures` collection for later semantic search.
  - **Pre-compact** — conversation transcript is captured into `session-notes` before context compaction, so decisions and discoveries survive across sessions.
- **Per-project hook configuration** — `.claude/quarry.local.md` YAML frontmatter lets users selectively disable individual hooks (`session_sync`, `web_fetch`, `compaction`). All hooks default to enabled.
- **Hooks CLI dispatcher** — `quarry hooks {session-start,post-web-fetch,pre-compact}` subcommands read JSON from stdin, call the handler, and write JSON to stdout. Fail-open: always exits 0 and emits `{}` on error.

### Index

- **Collection name disambiguation** — when auto-registering a project whose leaf directory name collides with an existing collection, quarry appends the parent directory name (e.g. `myproject-mine`) or a hash suffix as fallback.

### Infra

- **pyyaml** added as runtime dependency (hook configuration parsing)
- **types-PyYAML** added as dev dependency

### Fixed

- **document_name mismatch in format processors** — `document_name` is now threaded through all format processors so ingested documents use the caller-provided name instead of deriving it from the file path (#60)
- **get_page scan limit** — non-vector LanceDB queries now use an explicit scan limit to avoid silently truncating results (#61)

## [0.9.2] - 2026-02-24

### Connector

- **Smart URL ingestion** — `ingest_auto` auto-discovers sitemaps via [ultimate-sitemap-parser](https://github.com/mediacloud/ultimate-sitemap-parser) (robots.txt, well-known locations, recursive indexes, XML/RSS/Atom/plain text formats). Falls back to single-page ingestion when no sitemap found. Discovery errors gracefully degrade to single-page mode.
- **Sitemap parsing via USP** — Replaced hand-rolled XML parser with USP for robust handling of malformed content, gzipped sitemaps, and sitemap indexes. Net -286 lines.

### Tool

- **`/ingest` handles directories** — `/ingest ~/path/to/dir` now routes to `register_directory` + sync instead of failing with "unsupported file format".
- **`ingest_auto` MCP tool** — New tool that subsumes `ingest_url` and `ingest_sitemap` for URL inputs. All `/ingest <url>` commands route here.

## [0.9.1] - 2026-02-24

### Infra

- **Dual command path** — SessionStart hook deploys commands to `~/.claude/commands/` for top-level access (`/find`, `/ingest`, etc.) alongside namespaced `quarry:*` versions. Auto-allows MCP tool permissions on first run. Follows punt-kit dual-command-path pattern.
- **Plugin rename** — Fixed plugin name from `quarry-dev` to `quarry` so marketplace shows the correct name.
- **Removed stale manifest.json** — Old marketplace manifest was blocking plugin commands from loading.

## [0.9.0] - 2026-02-24

### Tool

- **Claude Code plugin** — quarry is now a full Claude Code plugin (`quarry@punt-labs`), with slash commands, MCP server, hooks, and formatted output all bundled together. Install with `claude plugin install quarry@punt-labs`.
- **Slash commands** — `/find`, `/ingest`, `/explain`, `/source`, `/quarry` provide natural-language access to search, ingestion, and knowledge base management directly from Claude Code.
- **Formatted MCP output** — All 17 MCP tools return pre-formatted plain text with constrained-width tables instead of raw JSON. PostToolUse hook routes data tools to a compact panel summary while passing full output to the LLM context.

### Infra

- **Plugin scaffold** — `.claude-plugin/plugin.json` manifest, `commands/`, `hooks/` directories following biff's three-layer display architecture (DES-014).
- **Published to punt-labs marketplace** — quarry is now available in the `punt-labs` Claude Code plugin marketplace alongside biff, dungeon, punt, and prfaq.

## [0.8.1] - 2026-02-24

### Infra

- **MCP smoke test script** — `docs/MCP-SMOKE-TEST.md` provides an 11-step manual verification for all MCP tools inside Claude Code (sitemap crawl, dedup, search, ingest, delete, cleanup)
- Updated PR/FAQ: 596 tests, URL/sitemap ingestion in shipped features, fixed quarry-menubar GitHub org

## [0.8.0] - 2026-02-23

### Connector

- **Sitemap crawling** — `quarry ingest-sitemap <url>` discovers all URLs from XML sitemaps (following `<sitemapindex>` recursively), applies include/exclude URL path glob filters, and ingests pages in parallel. `<lastmod>`-based dedup skips unchanged pages on re-crawl. Rate limiting with configurable delay + random jitter avoids crawl blocking.

### Tool

- `ingest-sitemap` CLI command with `--include`, `--exclude`, `--limit`, `--workers`, `--delay` options
- `ingest_sitemap` MCP tool with comma-separated pattern strings
- Gzip-compressed sitemap support (`.xml.gz`)

### Infra

- **PyPI package renamed** from `quarry-mcp` to `punt-quarry` (aligns with punt-labs naming convention). Install with `uv tool install punt-quarry`.
- Resilient child sitemap fetching — parse errors in one child sitemap no longer abort entire discovery
- Worker count validation — `workers=0` or negative values clamped to 1
- 596 tests across 30 modules

## [0.7.0] - 2026-02-15

### Index

- **`.gitignore` and `.quarryignore` sync** — directory sync now respects `.gitignore` at every level plus a `.quarryignore` override file. Hardcoded default patterns (`__pycache__/`, `node_modules/`, `.venv/`, etc.) also applied. New `pathspec` dependency.

### Tool

- **MCP `list_databases` and `use_database` tools** — discover named databases and switch between them mid-session without restarting. Closes the last parity gap between CLI `--db` flag and MCP tools.
- **Claude Desktop Extension (.mcpb)** — download and double-click to install Quarry in Claude Desktop. Configures the MCP server, downloads the embedding model, and prompts for a data directory.
- Fixed validate-before-mutate in `use_database` — invalid database names (path traversal) no longer corrupt server state

### Infra

- README rewritten for user-first experience: Desktop and Menu Bar first, CLI second
- Menu Bar App section added to README
- Fixed `read_text()` calls to specify `encoding="utf-8"` explicitly
- 568 tests across 25 modules

## [0.6.0] - 2026-02-15

### Format

- **XLSX and CSV spreadsheet ingestion** — spreadsheets are serialized to LaTeX tabular format for LLM-native consumption. Large sheets are split into row groups with column headers repeated in each section. New `spreadsheet_processor.py` module; new `openpyxl` dependency.
- **HTML ingestion** — HTML files are parsed with BeautifulSoup, boilerplate stripped (nav, footer, scripts, etc.), and converted to Markdown via markdownify. Sections split on headings with paragraph fallback. New `html_processor.py` module; new `beautifulsoup4` and `markdownify` dependencies.
- **PPTX presentation ingestion** — each slide becomes one chunk containing the title, body text, tables as LaTeX tabular, and speaker notes (after `---` separator). Empty slides are skipped. New `presentation_processor.py` module; new `python-pptx` dependency.
- **URL webpage ingestion** — fetch any HTTP(S) URL, strip boilerplate, and index for semantic search. Available via `quarry ingest-url` CLI command and `ingest_url` MCP tool. HTML processing reuses the existing pipeline; no new dependencies.
- `SPREADSHEET` and `PRESENTATION` page types added
- LaTeX table utilities (`escape_latex`, `rows_to_latex`) extracted to shared `latex_utils.py` module for reuse by spreadsheet and presentation processors

### Transform

- **SageMaker embedding backend** — offloads `embed_texts()` to a SageMaker endpoint for cloud-accelerated batch ingestion. `embed_query()` stays local via ONNX for sub-millisecond search latency. Same model (snowflake-arctic-embed-m-v1.5) on both paths; vectors are compatible.
- **Custom SageMaker inference handler** — server-side CLS-token pooling + L2 normalization reduces response size from ~67 MB to ~140 KB per batch of 32 texts
- **Batched ONNX inference** — `embed_texts()` now processes in batches of 256, preventing OOM on large documents
- Fixed ONNX model to use `sentence_embedding` output (was using wrong output index); removed unnecessary `token_type_ids` input

### Connector

- **`quarry serve` HTTP server** — lightweight HTTP API for integration with external clients (e.g. menu bar app). Supports search, ingest, document listing, and collection management.

### Index

- **Named databases** — `--db <name>` flag on all CLI commands isolates collections into separate LanceDB instances under `~/.quarry/data/<name>/`. MCP `db_name` parameter provides the same capability.
- **`page_type` and `source_format` chunk metadata** — every chunk now stores its content type (`"text"`, `"code"`, `"spreadsheet"`, `"presentation"`) and source format (file extension like `".pdf"`, `".py"`, or `"inline"` for programmatic text). Enables search-by-format filtering.
- **Auto-workers for sync** — `quarry sync` auto-selects 4 parallel workers when a cloud backend (Textract or SageMaker) is active, 1 otherwise. Explicit `--workers` still overrides.
- Inline content `document_path` changed from `"<string>"` sentinel to empty string
- **Breaking:** Existing indexes need re-ingestion (`quarry sync`) to populate new columns

### Query

- **Search metadata filters** — `page_type` and `source_format` are now filterable in both the MCP `search_documents` tool and the `quarry search` CLI command. Filters become LanceDB SQL WHERE clauses for efficient pre-filtering before vector search.
- `search_documents` results now include `page_type` and `source_format` fields
- CLI search output shows content type metadata: `[report.pdf p.3 | text/.pdf]`

### Tool

- **Breaking:** `ingest` CLI command renamed to `ingest-file`; `ingest` and `ingest_text` MCP tools renamed to `ingest_file` and `ingest_content`. Clarifies that the distinction is input mechanism (file path vs inline content), not content type.
- `quarry search --page-type code` — filter results by content type
- `quarry search --source-format .py` — filter results by source format
- `quarry search --document report.pdf` — filter results by document name
- `quarry databases --json` — machine-readable output for scripting
- `quarry doctor` and `quarry install` UX improvements: better error messages, progress indicators

### Infra

- `EMBEDDING_BACKEND` setting (`onnx` | `sagemaker`) with factory dispatch in `backends.py`
- `SAGEMAKER_ENDPOINT_NAME` setting for SageMaker endpoint configuration
- `SageMakerRuntimeClient` and `ReadableBody` protocols in `types.py`
- `quarry doctor` checks SageMaker endpoint availability when configured
- CloudFormation templates for SageMaker Serverless and Realtime endpoint deployment (`infra/sagemaker-serverless.yaml`, `infra/sagemaker-realtime.yaml`)
- `infra/manage-stack.sh` deploy/destroy/status script with region-aware bucket naming
- IAM policy template (`docs/quarry-iam-policy.json`) and AWS setup guide (`docs/AWS-SETUP.md`)
- Test environment isolation — autouse fixture strips `.envrc` env vars from pydantic-settings
- 549 tests across 25 modules

## [0.5.0] - 2026-02-13

### Transform

- **ONNX Runtime embedding backend** — replaced sentence-transformers with direct ONNX Runtime inference. Eliminates PyTorch dependency (~2 GB), model loads in <1s.
- Split `_download_model_files` (network, install-time) from `_load_model_files` (local-only, runtime) for clear separation of concerns
- Pinned embedding model to git revision `e58a8f75` in both download and load paths

### Infra

- **Breaking:** `sentence-transformers` dependency removed. Run `quarry install` to download the ONNX model if upgrading.
- Typed result structures: `IngestResult`, `SearchResult`, `DocumentSummary`, `CollectionSummary` TypedDicts in `results.py`
- `OcrBackend` protocol standardized on `Path` for `document_path` parameter
- Idempotent `configure_logging` (safe to call multiple times)
- Narrowed exception catches in sync engine (no bare `Exception`)
- Deferred botocore import in sync module (no AWS imports at load time)
- `quarry doctor` verifies both ONNX model and tokenizer are cached
- Removed stale `TODO.md` and `CODE-DESIGN-EVALUATION.md`
- 323 tests across 20 modules

## [0.4.2] - 2026-02-12

### Infra

- Restructure README: Quick Start within first 20 lines, user-focused flow, removed jargon
- Fix documented mypy command to match CI (`src/ tests/`)
- Remove misleading `EMBEDDING_MODEL` env var (revision is pinned)

## [0.4.1] - 2026-02-12

### Infra

- Pin embedding model to git revision `e58a8f75` for reproducible builds
- Load model with `local_files_only=True` — eliminates HuggingFace Hub network calls at runtime (4s → 0.6s first load)
- Runtime fails fast if model not cached (directs user to run `quarry install`)

## [0.4.0] - 2026-02-12

### Transform

- **Local OCR backend** — RapidOCR (PaddleOCR models via ONNX Runtime, CPU-only, ~214 MB). No cloud credentials required.
- Protocol types (`_OcrEngine`, `_OcrResult`) for RapidOCR — zero `getattr()`, zero `type: ignore`
- Thread-safe singleton engine initialization via double-checked locking

### Infra

- **Breaking:** Default `OCR_BACKEND` changed from `textract` to `local`. Set `OCR_BACKEND=textract` to restore previous behavior.
- New dependencies: `rapidocr>=3.6.0`, `onnxruntime>=1.18.0`, `opencv-python-headless>=4.8.0`
- `quarry doctor` checks local OCR engine health; AWS credentials now optional
- 18 unit tests for `ocr_local.py` (100% coverage)

## [0.3.0] - 2026-02-10

### Format

- Source code ingestion with tree-sitter parsing (30+ languages, required dependency)
- `PageType.CODE` enum value for distinguishing code chunks from prose

### Pipeline

- Handle MPO (iPhone multi-picture) JPEG format — converted to standard JPEG before OCR
- Handle non-UTF-8 text file encodings (UTF-8 → CP1252 → Latin-1 fallback chain)
- Downscale oversized images before OCR (halve dimensions up to 5x)
- Skip macOS resource fork files (`._*`, `.DS_Store`) and hidden directories during sync
- Fixed concurrent table creation race condition via double-checked locking

### Infra

- **Breaking:** Renamed LanceDB table from `ocr_chunks` to `chunks`. Run `quarry sync` after upgrading to re-index.
- Persistent logging to `~/.quarry/data/quarry.log` with rotation (5 MB, 3 backups)

## [0.2.1] - 2026-02-09

### Infra

- Pluggable backend abstraction: `OcrBackend` and `EmbeddingBackend` protocols in `types.py`
- `TextractOcrBackend` and `SnowflakeEmbeddingBackend` implementation classes
- Thread-safe backend factory in `backends.py` with `match/case` dispatch and instance caching
- `ocr_backend` configuration setting for selecting OCR provider

### Pipeline

- Pipeline, CLI, and MCP server now use backend factory instead of direct function imports
- Integration tests excluded from default `uv run pytest` (opt-in via `uv run pytest -m slow`)
- Fixed concurrent table creation race condition via double-checked locking

## [0.2.0] - 2026-02-09

### Pipeline

- Directory registration and incremental sync engine
- SQLite-backed registry (WAL mode) tracking directories, collections, and file records
- Delta detection via mtime+size comparison: new, changed, unchanged, deleted
- Parallel file ingestion during sync via ThreadPoolExecutor (default 4 workers)
- Exponential backoff for Textract polling (start 5s, 1.5x multiplier, cap 30s) replaces fixed interval
- Skip macOS resource fork files (`._*`) and `.Trash` during sync

### Tool

- CLI commands: `register`, `deregister`, `registrations`, `sync`
- MCP tools: `register_directory`, `deregister_directory`, `sync_all_registrations`, `list_registrations`
- `delete-collection` CLI command and `delete_collection` MCP tool
- `list_collections` MCP tool
- `status` MCP tool now reports registered directory count
- MCP tool count: 9 → 13

### Infra

- `REGISTRY_PATH` configuration variable
- 21 end-to-end integration tests covering all ingestion formats, search, collections, and overwrite

## [0.1.3] - 2026-02-08

### Infra

- PEP 561 `py.typed` marker for type-checked package consumers
- Embedding model cache now keys by model name (was single global; ignored `model_name` param after first load)
- Hardcoded `embedding_dimension: 768` extracted to `Settings.embedding_dimension` (single source of truth)
- `SCHEMA` module-level constant replaced with `_schema()` function accepting dimension parameter
- `type: ignore[assignment]` on boto3/lancedb calls replaced with explicit `cast()` for clarity
- `.pytest_cache/` added to `.gitignore`

### Tool

- MCP server tests for `search_documents`, `get_documents`, `get_page` tools
- CLI tests for `list`, `delete`, `search` commands and error handling

## [0.1.2] - 2026-02-08

### Format

- Standalone image ingestion: PNG, JPEG, TIFF (multi-page), BMP, WebP
- BMP/WebP auto-conversion to PNG via Pillow before OCR
- Multi-page TIFF support via async Textract API
- Text document ingestion: `.txt`, `.md`, `.tex`, `.docx`
- Section-aware splitting: markdown headings, LaTeX `\section`/`\subsection`, blank-line paragraphs, DOCX Heading styles

### Provider

- Sync Textract API (`DetectDocumentText`) for single-page images (no S3 upload needed)

### Tool

- `quarry doctor` command: checks Python, data directory, AWS credentials, embedding model cache, core imports
- `quarry install` command: creates `~/.quarry/data/lancedb/`, pre-downloads embedding model, prints MCP config snippet
- `ingest` MCP tool and CLI now accept all supported formats (was PDF-only)
- Raw text ingestion via `ingest_text` MCP tool (auto-detects markdown/LaTeX/plain)
- `delete_document` MCP tool and `quarry delete` CLI command
- `status` MCP tool reporting document/chunk counts, database size, and embedding model info

### Pipeline

- `ingest_document` dispatches by format, shared `_chunk_embed_store` eliminates duplication
- `image_analyzer` module with format detection and TIFF page counting
- Resource leak fixed: `fitz.open()` now uses context manager
- MCP tool handlers and CLI commands catch exceptions at boundary, log tracebacks, return user-friendly errors
- Progress calls use `%s`-style lazy formatting instead of f-strings
- Added `Raises:` docstring sections to all public functions
- Added `DEBUG` logging to `pdf_analyzer`, `text_extractor`, `text_processor`, and `database` modules
- Oversized images downscaled before OCR (re-encode as JPEG, then halve dimensions up to 5x)
- Non-UTF-8 text file encodings handled via chardet detection
- MPO (Multi-Picture Object) JPEG format converted to standard JPEG before OCR

### Infra

- Build backend from `hatchling` to `uv_build`
- Version via `importlib.metadata.version()` instead of `__version__.py`
- Default `lancedb_path` from repo-relative to `~/.quarry/data/lancedb`
- `count_chunks` database function for O(1) chunk counting
- PyPI classifiers and `[project.urls]` metadata
- `docs/TOOL-PyPI.md` publishing checklist
- `NON-FUNCTIONAL-DESIGN.md` defining logging and exception handling standards
- CHANGELOG.md

## [0.1.0] - 2026-02-08

### Format

- PDF ingestion with automatic text/image page classification

### Provider

- OCR via AWS Textract (async API with polling)
- Local vector embeddings using snowflake-arctic-embed-m-v1.5 (768-dim)

### Pipeline

- Text extraction via PyMuPDF for text-based pages
- Sentence-aware chunking with configurable overlap
- Full page text preserved alongside chunks for LLM context

### Tool

- MCP server with `search_documents`, `ingest`, `get_documents`, `get_page` tools
- CLI with `ingest`, `search`, `list` commands and Rich progress display

### Infra

- LanceDB vector storage with PyArrow schema
- 62 tests across 9 modules
