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

- tool: `quarry missions sync` (CLI), `missions_sync` (MCP, the twelfth tool)
  and `/quarry missions sync` (plugin) — Loop 2 of the agent memory loop.
  Reads this repo's `.punt-labs/ethos/missions/` sidecar (contract, results,
  reflections; quarry never calls ethos) and files each frozen round as an
  `observation` in `memory-<worker>` via `POST /v1/remember`, named
  `mission-<repo>-<id>-r<n>`. Idempotent: a round the daemon already holds is
  skipped, a name held by another checkout's round is an error and never
  overwritten (`--force` re-files a matching key only), parse failures are
  collected and exit 1, `--dry-run` posts nothing. New modules
  `mission_records`, `mission_round_parts`, `mission_store`,
  `mission_sync_types`, `mission_memory`, `cli_missions`, `mcp_missions`.
  (quarry-fbj9)
- tool: `SubagentStop` now files the subagent's final report as an
  `observation` in `memory-<handle>` (`subagent-<id8>-report`) alongside the
  raw transcript capture, which now carries a summary — Loop 3. Attribution is
  identity-validated: `agent_type` is used only when it names a vendored or
  global ethos identity; a bare `Agent()` reviewer (`general-purpose`) is
  filed unattributed, never under the repo pin's leader. Both rows go through
  the daemon's scrub-before-store route with the hook's 5 s cap; no engine
  runs in the hook (DES-041). New modules `subagent_report`,
  `subagent_capture`; `EthosConfig.subagent_handle_at`;
  `DaemonCaptureSender.send_remember`; `QuarryClient.remember(timeout=)`.
  (quarry-fbj9)
- tool: a versioned memory guide, `## Memory (quarry guide v2)`, in every
  ethos identity's `session_context` — the five moments to `remember`, what
  never to store, and why the handle is the agent's own. `quarry enable`
  refreshes the vendored `.punt-labs/ethos/identities/<handle>.ext/quarry.yaml`
  files (reported as "commit via PR") and `quarry install` refreshes the
  global ones; a v1 block is replaced in place, a customised block is left
  alone. The MCP `remember` docstring, the recall skill, `/remember`, and the
  deposited repo guide carry the same five moments. New modules `ethos_tree`
  (the read-only sidecar locator: pins, vendored/global identities, missions),
  `ethos_ext_block`, `ethos_ext_scan`. (quarry-fbj9)
- tool: one `MemoryType` vocabulary (`fact`, `observation`, `opinion`,
  `procedure`; `lesson` reserved for `learn`) enforced on `remember`,
  `ingest`, and `capture` alike — an unknown `memory_type` is a 400 with an
  identical body on all three routes instead of a silently stored row that
  neither decays nor matches a typed filter. The CLI `--memory-type` help and
  the MCP docstrings name the same set. (quarry-fbj9)
- infra: vendored, locally-optimized ethos identity registry at
  `.punt-labs/ethos/` — the 8-member `quarry` team only, produced by
  `ethos vendor` plus a prune to the quarry-team closure, with
  `resolution: repo-only` pinned in `.punt-labs/ethos.yaml`, replacing
  global-store fallback. Runtime state (`missions/`, `missions.jsonl`,
  `sessions/`, `.biff`) stays gitignored. (quarry-teuk)

### Changed

- tool: the ethos repo pin is read from `.punt-labs/ethos.yaml` first, then the
  legacy `.punt-labs/ethos/config.yaml`, at each ancestor — the file current
  ethos writes — so PreCompact/SessionEnd captures and `quarry doctor` attribute
  to the pinned identity again instead of falling back to unattributed. The
  walk stops at the repository boundary and never reads the operator's home.
  (quarry-fbj9)
- tool: `quarry mcp` (the only launcher) now configures the server's stderr
  logging; `mcp_server.main()` no longer does, and its `__main__` block is gone.
  The MCP tool-boundary decorator is `mcp_guard.ToolGuard.wrap`, shared by
  `McpTools` and the sibling `MissionTools`. The hidden `quarry hooks` typer
  sub-app is deleted — `quarry-hook` (`_hook_entry`) is the one dispatcher.
  (quarry-fbj9)
- infra: the web-search hook's shape/no-digest log lines moved from
  `hooks_agent` onto `WebSearchPayload.log_shape`/`warn_no_digest`; the
  `TranscriptReader` reads through one `_records()` seam; `EnableResult`
  carries an `EthosMemoryResult` instead of six flattened ethos fields.
  (quarry-fbj9)
- infra: decomposed the ingestion god module `ingestion/pipeline.py`
  (1,133 → ~140 lines) into focused modules — `ingest_context.py`
  (`IngestContext`/`Progress`), `extracted_document.py`,
  `format_strategies.py` (a `FormatStrategy` protocol + per-format
  strategies), `chunk_store_funnel.py` (the single DES-036 embed/store
  convergence point), and `web_ingest.py`/`bulk_ingest.py`/`sitemap_ingest.py`.
  Ingest entry-point signatures now take value objects (`IngestContext`,
  `ExtractedDocument`, `InlineIngest`, `BulkOptions`) instead of ~10-15
  positional parameters. No behavior change on any surface; the fail-closed
  overwrite-delete gate is preserved. (quarry-hb9u)
- infra: decomposed the `hooks.py` god module (783 → ~420 lines) — extracted
  `sync_lock.py` (`SyncLock`, the background-sync file lock, now injectable) and
  `session_start_templates.py` (`SessionStartTemplates`); `_SessionStartContext`
  retained in `hooks.py` absorbing its helpers; `_as_str`/`_as_dir` removed in
  favor of `HookPayload.as_str`/`as_dir`. No behavior change on the
  session-start / post-web-fetch / pre-compact hook surfaces. As part of the
  extraction, `SyncLock` replaces the previous PID-lockfile staleness-reclaim
  scheme — which carried a double-launch race (two concurrent SessionStart hooks
  reading the same stale PID could both reclaim and launch a sync, one caller's
  unconditional unlink deleting the other's freshly acquired lock) — with a
  kernel `fcntl.flock` held for the sync subprocess's lifetime via `Popen`
  `pass_fds` fd inheritance, eliminating the race class by construction (a dead
  holder's lock is released by the kernel; no PID parsing, staleness detection,
  or reclaim unlink). Hardened further with `O_NOFOLLOW` on the lockfile open
  (CWE-59: a hostile/accidental `sync.pid` symlink can no longer redirect the
  write), `BlockingIOError` (contention) distinguished from genuine `flock`
  errors, and a fail-open probe. Single-flight is over the dispatch subprocess
  only; the daemon enqueues each sync (DES-045, always 202) serialized
  per-collection by the ingest queue (DES-042). (quarry-hb9u)

- infra: bumped runtime dependencies `rapidocr` (3.6.0 → 3.9.2) and `uvicorn`
  (0.51.0 → 0.52.4), plus the dev toolchain — `mypy` (2.3.0 → 2.3.1),
  `pytest-asyncio` (1.3.0 → 1.4.0), and `types-pyyaml`
  (6.0.12.20260724 → 6.0.12.20260815) — via Dependabot.

## [3.2.1] - 2026-09-03

### Fixed

- `tool`: G6 hook breadcrumbs (shipped as `HookTrace` in v3.2.0 via PR #496)
  never actually emitted, because the `quarry-hook` binary — the entry point
  every `PostToolUse`/`SessionStart`/`SessionEnd`/`SubagentStop`/`PreCompact`
  hook dispatches through — did not call `LoggingConfig.configure()`.
  `HookTrace.logger.info()` calls had no handler attached to the
  `quarry.hooks` logger, so every "did the hook run?" INFO line was silently
  dropped. Fixed by adding one `LoggingConfig.configure(stderr_level="WARNING")`
  call at the top of `_stdlib.run_hook()` (covers every event via one seam).
  Also extracted `handle_session_setup`'s free helpers into a cohesive
  `_SessionSetup` class and wired a `HookTrace("session-setup")` breadcrumb
  onto every exit path (including filesystem-fault error paths, via a
  `try/except OSError` around `setup.dispatch()`), so the "one breadcrumb
  per invocation" contract holds even when setup itself raises.
  `_allow_mcp_tools` also updated to emit the current `mcp__{name}__*`
  wildcard (not the retired `mcp__plugin_{name}_quarry__*` proxy namespace),
  with slug validation on the plugin name and exact-wildcard membership
  check on the settings allow-list. (quarry-ridg, PR #501)
- `tool`: G4 non-HTML `WebFetch` capture — v3.2.0 patched the refetch
  path in `CaptureIngestJob._refetch` but left `IngestJob._ingest`'s
  primary scrubbed path calling `ingest_url()` → `WebFetcher.fetch()`
  (HTML-only), so any `WebFetch` on a JSON/text/xml URL still raised
  `ValueError` in the daemon and dropped the capture. Fixed by routing
  `_ingest` through the shared `ingest_captured_body()` seam that the
  refetch path already used: HTML flows through `ingest_url(prefetched_html=…)`
  (reusing round-4's perf shortcut), non-HTML through `ingest_content()`
  with a sanitized `<!-- media_type: … -->` marker prefix (whitelist strip
  plus 128-char cap so a hostile `Content-Type` header can't break the
  single-line marker contract). Also closes the CWE-532 leak surface: on
  the non-HTML branch, `document_name` now derives from
  `CaptureUrl(source).redacted(scrub)` so URL userinfo/query/fragment
  never lands in the stored metadata; `fetch_and_route()` catches
  `OSError`/`ValueError`/`TimeoutError` around `fetch_body()` and logs
  only the redacted URL + exception class (with a `_classify_fetch_error`
  helper that appends the safe policy message for
  `URL rejected:` / `final URL rejected:` rejections so operators can
  diagnose SSRF/redirect gating without exposing raw URLs from
  network-failure messages that quote them). (quarry-jzqw, PR #502)

- `index`: the daemon's filesystem watcher scheduled `observer.schedule(root,
  recursive=True)` on the raw registered root, so on Linux, watchdog's
  per-`ObservedWatch` emitter architecture opened one inotify KERNEL
  INSTANCE (plus threads and fds) per directory in the tree — including
  `.git`, `.venv`, `node_modules`, and every other ignored subtree.
  `fs.inotify.max_user_instances` defaults to 128 (per-user, shared with
  IDEs/editors), so a real workspace exhausted it at ~120 directories,
  degrading the whole tree to scan-only (`quarry-0bej`); a failed schedule
  also leaked its partially-acquired watches, starving smaller registrations
  sharing the same daemon (`quarry-ndrj`). Fixed by returning to ONE
  recursive watch per root (one instance, one thread-pair, any tree size)
  and, on Linux, subclassing watchdog's `Inotify` wrapper
  (`quarry.daemon.inotify_prune`/`inotify_prune_chain`) so its own
  directory-descriptor walk — both the initial recursive walk and the
  auto-add path for a directory created later — skips ignored directories
  per the SAME pruning seam (`FileDiscovery.iter_watchable_dirs`/
  `is_watchable_dir`; `.gitignore` at every level, `.quarryignore`, the
  built-in scratch defaults, hidden-dir rules) a bulk scan already uses, so
  the much larger `max_user_watches` budget (65,536, per-descriptor, no
  per-user cap) is spent only on directories worth watching. macOS keeps
  watchdog's standard recursive observer unmodified (FSEvents has no
  per-directory kernel cost); the post-debounce submitter filter still
  provides the authoritative ignore check on every platform. A per-directory
  `OSError` during the initial walk (ordinary churn — a directory vanishing
  mid-walk) is skipped, not fatal; only genuine budget exhaustion
  (`ENOSPC`/`EMFILE`) aborts. The daemon's `/registrations` listing (and the
  `quarry list registrations` CLI output) now reports each collection's live
  watch status — `watched`, `degraded` (tracked but the last schedule
  attempt returned no handle), or `scan-only` (the observer is disabled, or
  the collection was never tracked) — so a silently degraded watch is
  visible instead of indistinguishable from a healthy one.

## [3.2.0] - 2026-09-01

### Added

- `tool`: G6 — per-hook-invocation breadcrumb line in `quarry.log`. Every
  `PostToolUse:*`/`SessionEnd`/`SubagentStop`/`PreCompact` handler emits one
  `quarry.hooks: <name>: entered (config=..., payload_ok=...) -> capture|skip|error`
  at INFO, so a silent-skip is visible on every exit path — including the
  `PostToolUse:WebFetch` happy paths that previously went dark. (quarry-38qs)

### Changed

- `tool`: `plugin/skills/recall/SKILL.md` — the auto-loaded recall skill now
  differentiates all four capture verbs (`find`, `remember`, `ingest`, `learn`)
  in its frontmatter description and body, including a worked `learn` example.
  A "When not to use it" bullet points repo-internal architecture questions to
  `DESIGN.md` directly. Also corrects `/ingest` documentation to reflect the
  URL-only contract (local files/directories route through
  `register_directory` + `sync_all_registrations`). (quarry-wdhi)

### Fixed

- `tool`: slash-command MCP tool references repaired (quarry-ydym). The
  seven tool-dispatching quarry slash commands (`/find`, `/ingest`,
  `/remember`, `/learn`, `/explain`, `/source`, `/quarry`) — and their
  seven `-dev` twins — still named the disconnected proxy namespace
  (`mcp__plugin_quarry_quarry__*` / `mcp__plugin_quarry-dev_quarry__*`).
  The native quarry MCP server now exposes tools as `mcp__quarry__*`
  and its dev variant as `mcp__quarry-dev__*`; the released 3.1.0
  plugin worked only because a capable assistant inferred the new
  names. Cheaper models would hard-fail. All 26 references now name
  the live tools directly.
- `tool`: the compact panel summary for `list documents`, `list collections`,
  `list databases`, and `list registrations` now reports the correct row
  count. `plugin/hooks/suppress-output.sh` previously derived the count via
  `wc -l` on the rendered table body, which over-counted continuation lines
  from wrapped variable-width columns (a 2-row table with one wrapped row
  displayed as "3 documents"). List formatters now emit an authoritative
  `▶ N <noun>` on line 1 via a shared `_Listing` renderer in
  `src/quarry/formatting.py`, and the hook reads that line as the panel
  summary. (quarry-wdhi)
- `tool`: G5 — WebSearch captures were silently dropped when Claude Code
  emitted its rendered markdown summary instead of the pre-2026-05 JSON list;
  the extractor now falls back to the plain-text digest and the missing-digest
  log upgraded from DEBUG to WARN so operators can see the skip. The WARN
  logs shape metadata only (presence, length, tool_response type) — never the
  query text, which may hold tokens or secrets (CWE-532). (quarry-38qs)
- `tool`: G4 — `PostToolUse:WebFetch` on a non-HTML URL (`application/json`,
  `text/plain`, XML, …) no longer raises `ValueError` and drops the capture.
  `WebFetcher.fetch_body` returns the body plus its declared media type; the
  daemon routes HTML through the extractor and non-HTML through the text
  pipeline (prefixed with a `<!-- media_type: … -->` marker) so the shape
  survives into the stored document, still through the PII/secret scrub choke
  point. A network failure during the refetch logs a WARN with a redacted URL
  (via `CaptureUrl.for_web_fetch`) and the exception class only — never the
  raw URL or exception text — and returns cleanly with zero chunks (CWE-532).
  (quarry-38qs)

## [3.1.0] - 2026-08-31

### Added

- `query`: `POST /v1/captures/lookup` (JSON body `{url, cwd}`) answers whether
  a URL is already indexed under the caller's `<repo>-captures` collection
  (`CapturesLookupResponse`: `{matched, document_name}`). POST avoids leaking
  the target URL into server access logs and browser/proxy history (CWE-598:
  sensitive query-string data). `QuarryClient.captures_lookup()`
  is the client-side wrapper. `PostToolUse:WebFetch` calls it BEFORE sending the
  new capture (a lookup run after would always match) via
  `WebFetchLoopCloser` (`web_fetch_loop_closer.py`); on a match the hook
  returns an `additionalContext` nudge naming the URL, a suggested `find`
  query (the last path segment, or the host for a bare path), and the stored
  `document_name` when the daemon supplies one. The lookup normalizes the URL
  identically to the write path (`CaptureUrl.for_web_fetch`): two URLs
  differing only by query string or fragment share one document; a
  trailing-slash difference does not normalize and is a distinct document.
  Fails open — an unreachable daemon or any client error returns `{}` silently
  without blocking the fetch.
- `tool`: `quarry learn` — a fourth capture verb, on CLI (`quarry learn`), MCP
  (`learn`), slash (`/quarry:learn`), and the Python client
  (`QuarryClient.learn`). A single call atomically saves a distilled lesson
  (capped at 500 characters) and registers its retrieval preference — no
  `learn`-then-`set_config` two-step. Lessons file into a project-scoped
  `<repo>-lessons` collection (`default-lessons` when unregistered) and always
  carry `memory_type="lesson"` with no `agent_handle`, so they never decay.
  `memory_type='lesson'` is now reserved and rejected with `400` on
  `remember`/`ingest`. `quarry doctor`'s memory-corpus check reports a
  `lessons=N` segment. See DES-053.
- `query`: `RrfFusion` gains a `lesson_boost` knob — a 1.5x default RRF-term
  multiplier (`Settings.retrieval_lesson_boost`) that lifts a moderately
  relevant lesson above equivalently-ranked plain content without letting an
  irrelevant lesson dominate.

- `tool`: four new agent-lifecycle hooks — `SessionEnd`, `PostToolUse:WebSearch`,
  `PostToolUse:Read`, `SubagentStop`. `SessionEnd` captures the full session
  transcript on every close (closing the "PreCompact never fires on short
  sessions" gap); `PostToolUse:WebSearch` files a scrubbed digest of the
  results under `<repo>-captures`; `PostToolUse:Read` (opt-in via
  `HookConfig.read`, default `false`) captures prose files (`.md`, `.pdf`,
  `.docx`, ...) read from outside any registered tree, gated by a four-check
  fail-closed filter (in-tree exclusion, secret-path denylist, extension
  allowlist, 200 KB size cap); `SubagentStop` archives the subagent's own
  `agent_transcript_path` (distinct from the parent's `transcript_path`) with
  `agent_id` as the wire identity. `SubagentStop` is a BLOCKING hook — the
  handler always returns `{}` and never emits a `decision` field, verified
  under crafted-adversarial payloads.
- `infra`: extract `DaemonCaptureSender` (`daemon_capture.py`) and
  `SessionTranscriptCapture` (`session_transcript.py`) out of `hooks.py`,
  along with `WebSearchPayload`, `ReadPayload`, `ReadCaptureFilter`, and
  `hooks_agent.py` for the four new handlers. `hooks.py` shrinks from 926 to
  760 lines; `handle_pre_compact` now delegates to `SessionTranscriptCapture`
  without behavior change.
- `query`: `GET /v1/coverage?collection=<repo>` returns three per-repo counts —
  `documents_indexed`, `transcripts_captured`, `memories_saved` — bounded to
  the collection and its `<repo>-captures` sibling via a single
  `WHERE collection IN (...)` scan. `CoverageResponse` (wire model),
  `CoverageCounts` (TypedDict), `ChunkCatalog.coverage`, and
  `QuarryClient.coverage` land together so a new field never exists on one
  path without the other.
- `tool`: SessionStart `additionalContext` now leads with the three canonical
  trigger rules (find-before-WebSearch, grep-for-symbols/find-for-meaning,
  remember-for-durable-knowledge) in every branch that leaves quarry
  operational — including the daemon-unreachable auto-register defer, so an
  agent that reads the "restart quarryd" diagnosis also sees the rules to
  apply once the tools come back (operator ratification R2b).
- `tool`: `/quarry:help` (and `-dev` twin) lists all seven slash commands with
  one-line descriptions, matching the org's `/help` template.
- `tool`: `plugin/skills/recall/SKILL.md` — a Claude Code plugin skill so
  agents that never invoke a quarry slash command still reach for `/find`
  before WebSearch/WebFetch, before a why/how/what-did-we-decide answer, and
  when a durable fact is worth persisting past compaction.

### Changed

- `tool`: the `remember`/`ingest` descriptions on every surface (CLI, MCP,
  slash) now carry the boundary sentence distinguishing the three capture
  verbs: "remember = a specific durable fact, ingest = a URL, learn = a
  distilled lesson that gets retrieval preference."
- `tool`: MCP server `instructions` block leads with the two find-triggering
  sentences (R1 + R2) plus an anti-rationalisation clause and a negative rule;
  the formatting-policy paragraph is demoted below the trigger vocabulary.
- `tool`: every MCP tool docstring opens with an occasion rather than a
  mechanism verb. `find` and `remember` splice R1/R2 and R3 verbatim; the
  other nine tools each get a situational "Use ..." opener naming the
  occasion an agent would reach for them (operator ratification R3a drops
  the previous clipboard / API-response / sandbox-uploaded-files framing on
  `remember`).
- `tool`: `plugin/skills/recall/SKILL.md` — frontmatter and "When to use it"
  bullets splice R1/R2/R3 verbatim so the plugin skill, the SessionStart
  context, and the MCP tool docstrings share one wording.
- `query`: agent-memory temporal decay wired through the daemon's search
  route. A new `QUARRY_RETRIEVAL_DECAY_RATE` setting (default `0.000963`, a
  30-day half-life) is threaded from `Settings` into `RetrievalConfig` at
  call time so every daemon-served hybrid query applies the exponential
  recency curve to agent memories.
- `tool`: `quarry remember`/MCP `remember`/HTTP `POST /v1/remember` route
  by `agent_handle` when no collection is named. The three surfaces now
  send `collection=""` (the empty sentinel) and the daemon owns the single
  routing rule: an empty collection with a handle lands in
  `memory-<handle>`; empty on both sides falls back to `default`; an
  explicit collection always wins.
- `tool`: `quarry doctor` grows a **Memory corpus** informational line
  (per-handle, per-type, per-collection counts) and a **Memory identity**
  warning check that fires when the resident ethos handle has zero rows in
  a corpus that otherwise contains memory — the "ethos config resolves but
  PreCompact never fired" gap.
- `tool`: SessionStart now gates on the `.punt-labs/quarry/enabled`
  marker. A repo with the marker takes the existing active flow
  (walk-up coverage, auto-register, background sync). A repo without
  the marker gets one of two read-only nudges: if there is no covering
  registration, the hook returns a message pointing at
  `quarry enable DIR`; if there IS a covering registration (drift — the
  marker was never written or was deleted), the hook surfaces both
  `quarry enable DIR` (re-adopt) and `quarry deregister COLLECTION` (drop) and
  refuses to pick automatically. The nudge and drift paths never mutate
  the registry, never launch a sync, and never deposit the guide.
  Standard `punt-kit/standards/tool-enable-disable.md` §§ 2.1, 2.3,
  2.9, 2.11.
- `tool`: `/quarry:remember`'s `argument-hint` was `<document name>`, easily
  misread as "type the content here" next to a description that says "Remember
  inline text content." The command's own arguments are the memory's name,
  not its content (content is asked separately); reworded to `<name for this
  memory>`.
- The ethos-config walker (`.punt-labs/ethos/config.yaml` ancestor lookup)
  extracts into a new `quarry.ethos_handle.EthosConfig` module so the
  hook, doctor, and future callers share one reader. `hooks.py`'s inline
  copy migrates in a small follow-up bead (temporary, intentional
  duplication).

### Removed

- `tool`: `/quarry:use` (and `-dev` twin) — the manager subcommand
  `/quarry:quarry use <db>` is the only slash-command door to switching
  databases; a bare top-level command for one subcommand was redundant. The
  CLI verb `quarry use` is unaffected.

### Fixed

- `query`: fusion decay guard now requires both a non-empty `agent_handle`
  AND a decayable `memory_type`. Previously a bulk-ingested document that
  picked up a `memory_type` tag would decay under RRF; knowledge chunks
  (empty handle) now hold their rank regardless of the tag.
- `tool`: the `researcher` sub-agent's `tools:` allowlist granted only `Read,
  Glob, Grep, WebSearch, WebFetch` — every quarry MCP tool the agent's prompt
  directs it to call (`find`, `show`, `remember`, `list`, `ingest`) was
  excluded, so "always start with local knowledge" was structurally
  unexecutable as shipped. Per DES-025, MCP tool names carry a dev/prod
  prefix (`mcp__plugin_quarry_quarry__find` vs
  `mcp__plugin_quarry-dev_quarry__find`) that an allowlist cannot enumerate
  portably. Replaced the `tools:` allowlist with `disallowedTools: [Write,
  Edit, NotebookEdit, Bash]` — DES-025's denylist pattern extended for
  research agents that fetch untrusted web content (Cursor Security M1 on
  PR #481). DES-025's minimum is `[Write, Edit]`; this agent's threat model
  warrants tightening: `NotebookEdit` and `Bash` bypass the Write/Edit denies
  as filesystem-mutation surfaces, and prompt-injected content routing to
  Bash would elevate a confused-deputy risk. Both prod and dev MCP prefixes
  are still inherited from the parent session; the researcher can call
  every quarry MCP tool without needing the plugin's install-time prefix at
  frontmatter-write time.
- `infra`: the repo's `CLAUDE.md` carried the quarry user-guide twice — once
  as a `<!-- quarry:begin -->` / `<!-- quarry:end -->` fenced block and once
  via the canonical `@.punt-labs/quarry/CLAUDE.md` import that ships the
  same twenty lines. The fence violated
  `punt-kit/standards/tool-enable-disable.md` § 2.1 (tooling never merges,
  marks, or fences user-owned `CLAUDE.md` prose) and was a pre-standard
  legacy artifact — no code in `src/` writes it. Deleted the fence; the
  canonical `@`-import remains as the single delivery path. Documented the
  ownership contract in `src/quarry/guidance.py`'s module docstring so a
  future editor does not fork the string back into a fenced block.
- `tool`: `quarry disable` now teardown-commits the marker + `@`-import
  via `Enablement.disable()` **before** deregistering the sync
  collection via the daemon. Under the old order, a mid-disable failure
  in `Enablement.disable` (hostile symlink, lock contention, filesystem
  error) left the collection already gone while the marker and import
  still declared the repo enabled — the § 2.11 forbidden state (marker
  present, no functional collection). The reordered flow leaves either
  the fully-enabled state (recoverable) or a coherent disabled surface
  (marker-absent, import-absent) with only a runtime registration
  residue a retry converges — never the invalid state in between.

- `tool`: README and `/ingest` (and `/ingest-dev`) described `ingest`/`quarry
  ingest` as accepting a local file path. It only ever accepts an `http(s)`
  URL (`src/quarry/mcp_server.py`'s `ingest` docstring already documented the
  URL-only contract correctly — the bug was in the surrounding docs, not
  there) — `quarry-wz6f` tracks restoring one-shot local file ingest as a
  real feature. A local path was rejected with an explicit error, not
  ingested. Docs corrected; the `/ingest` slash commands now route a local
  file or directory
  through `register_directory` + `sync_all_registrations` instead of calling
  `ingest`, which always failed for that case.
- `tool`: README documented `quarryd`'s versioned REST API only as an
  internal implementation detail ("the CLI, MCP server, and hooks... over a
  versioned REST API"), never as a directly usable surface, despite a real
  generated OpenAPI spec (`docs/openapi.json`, 18 endpoints) existing. Added
  an HTTP API section under Commands with a worked `curl` example and the
  loopback/auth contract.
- `tool`: README's "What It Looks Like" ingest example used a bare URL,
  which is accurate but not representative — `register`+`sync` a directory
  is the common path. Swapped the example.
- `tool`: README's Knowledge Capture section named the three capture hooks
  in a sentence rather than explaining what each does, called the
  pattern-based secret scrub a guarantee rather than best-effort, and gave
  the opt-in shadow-repo push equal billing with core capture rather than
  framing it as an extension. Rewrote as a hook table, softened the scrub
  claim, and moved the shadow repo to its own "Extension" callout.
- `tool`: README's Managing the Daemon section didn't mention that
  re-running the Quick Start installer already restarts the service on
  upgrade (`install.sh` calls `quarry install` then force-restarts as a
  belt-and-suspenders step) — the manual `launchctl`/`systemctl` commands
  read as always-required when they're only needed after upgrading the
  package some other way.
- `tool`: split the environment-variable reference and Remote Server
  deployment content out of README.md into a new `ADVANCED-SETUP.md` — the
  main audience (a Claude Code developer using the zero-config default)
  doesn't need GPU-host/TLS/TOFU deployment detail in the primary doc.
  README's Setup section is now a one-line pointer; Features, HTTP API, and
  Documentation cross-references updated to the new file.
- `tool`: README's HTTP API `curl` example used plain `http://`, but
  `quarry install` unconditionally generates TLS certs for the managed
  daemon (`quarry.service.install` calls `write_tls_files` before
  registering the service, local or `--network`), so the default installed
  daemon serves `https://`, not `http://` — the example as written would
  fail against a typical install. Fixed to `https://` with `--cacert`
  against the generated local CA.
- `tool`: README's CLI table said `quarry register <dir>` "watches" a
  directory but never said how — an operator asked directly whether sync
  runs on a cron. It doesn't: `quarryd` runs a live, debounced filesystem
  watch (`src/quarry/daemon/watch_loop.py`, `watch_debounce_s` in
  `config.py`, default 1.0s) plus a 5-minute periodic safety sweep as a
  backstop (`src/quarry/daemon/watch_reconcile.py`, `watch_safety_scan_s`,
  default 300s). Added a sentence after the CLI table.
- `tool`: moved README's Development section (the `make check`/`test`/
  `format`/`docs`/`eval` command table) into `CONTRIBUTING.md`'s existing
  Quality Gates section, which now also lists every `check-*` ratchet
  `make check` actually runs (`check-oo`, `check-coupling`,
  `check-suppressions`, `check-imports`, `check-openapi` — the old text
  only named lint/type/test). README's Development section is now a
  one-line pointer, per the required-section rule that it stay present but
  the "no narrative Contributing content in README" rule that its detail
  belongs in `CONTRIBUTING.md`.
- `tool`: moved the HTTP API subsection out from under Commands to live
  with Managing the Daemon instead — both describe the same running
  `quarryd` process, so grouping them reads better than filing HTTP
  alongside the Slash Commands/MCP Tools/CLI surfaces it isn't one of.
  Also named the HTTP API in the opening description, where it was
  previously undescribed — as the interface the thin clients (CLI, MCP
  server, Claude Code hooks) talk over and that's reachable directly too,
  not as a fourth thin client itself.

## [3.0.3] - 2026-08-22

### Fixed

- `tool`: `quarry install`/`doctor` now registers the Claude Code MCP server
  with `claude mcp add --scope user` instead of the `local` default, so the
  entry is machine-wide rather than scoped to whatever directory the install
  happened to run from. Caveat: a leftover `local`-scope entry from an older
  install, or a per-project `.mcp.json`, still shadows the `user`-scope entry
  for that one project — Claude Code resolves `local` before `user`. Remove a
  stray local entry with `claude mcp remove quarry --scope local` from inside
  that project.

## [3.0.2] - 2026-08-20

### Changed

- `infra`: the shippable plugin surface moved into a top-level `plugin/`
  directory — `plugin/.claude-plugin/`, `plugin/commands/`, `plugin/hooks/`,
  `plugin/agents/`. Once the marketplace entry switches to the `git-subdir`
  source type, `claude plugin install quarry@punt-labs` fetches that subtree
  alone instead of cloning the whole repository (`src/`, `tests/`, `tools/`,
  `benchmarks/`, `docs/`, `prfaq.pdf`) into every user's plugin cache. Nothing
  inside the surface moved relative to the plugin root, so
  `${CLAUDE_PLUGIN_ROOT}` paths in `hooks.json` are unchanged; the release and
  dev-restore scripts, the hook-wiring tests, and the docs that named the
  surface by a repo-root path were all repointed. Local dev-plugin loading is
  now `claude --plugin-dir plugin`, not `--plugin-dir .`. See DES-050.

### Fixed

- `infra`: the researcher agent is now loaded. It sat at
  `.claude-plugin/agents/researcher.md`, but Claude Code resolves a plugin's
  default agent directory as `<plugin-root>/agents` — a sibling of
  `.claude-plugin/`, not a child — and quarry's `plugin.json` declares no
  `agents` override, so the file had never been read by any session since
  DES-013 introduced it. It now lives at `plugin/agents/researcher.md`. Agent
  types are namespaced per plugin, so it registers as `quarry:researcher`.
- `infra`: the plugin's five hook scripts are now covered by the `shellcheck -x`
  gate. Coverage had been extended from `install.sh` to `scripts/*.sh` and
  stopped there, leaving the only shell that runs on a *user's* machine — on
  every session — unlinted.
- `infra`: the public-fetch TLS trust test no longer fails on a correctly
  configured machine. It asserted `len(ctx.get_ca_certs()) > 1` to prove the
  system trust store was in use, but `get_ca_certs` reports only what OpenSSL
  has actually loaded — and where the platform default is a hashed **CApath**
  directory (`SSL_CERT_DIR=/etc/ssl/certs`, no `SSL_CERT_FILE` bundle present:
  the Debian/Ubuntu layout) OpenSSL resolves CAs lazily by subject hash, so a
  fully working default context reports zero until the first handshake. The test
  was measuring the platform's cert layout, not the code. It now compares the
  fetch context's store against a freshly built platform-default context, which
  states the actual invariant and still fails a pinned single-CA context on
  either layout.
- `infra`: `scripts/restore-dev-plugin.sh` no longer runs
  `git add <commands-dir> 2>/dev/null || true` unconditionally. The add now sits
  inside the same guard as the checkout that populates it, so a checkout that
  restored nothing fails the script instead of being reported as a successful
  restore.

## [3.0.1] - 2026-08-19

### Removed

- `infra`: the `.punt-labs/ethos` git submodule. Quarry ships as a marketplace
  plugin and Claude Code clones plugin repos with `--recurse-submodules`, so
  the gitlink put the whole Punt Labs identity registry — ~1 MB, 246 files of
  identities, personalities, writing styles, talents, roles and teams — onto
  the disk of everyone who installed quarry. It is internal team data, not
  part of the product. Quarry's gitlink used an HTTPS URL, so keyless installs
  already worked and nothing about installation changes; this removes weight,
  not a failure. Agents working in the repo resolve identity from the global
  `~/.punt-labs/ethos/`. The two-line `.punt-labs/ethos.yaml` identity pointer
  stays tracked — it is project config, not the org roster.

### Fixed

- Action pin comments now state the version actually pinned.
  `actions/checkout` was pinned to v7.0.1's SHA but labelled `# v4`, and
  `codecov/codecov-action` was pinned to a SHA carrying v6 and v7 but
  labelled `# v5`. The SHA is the security control, but the comment is the only
  part a human reads — a wrong comment hides a stale pin, which is how
  `gh-action-pypi-publish` broke punt-kit's 0.12.0 release. Labels
  resolved against the GitHub API; no SHA changed.
- `markdownlint-cli2-action` is repinned from an unreleased commit on the
  action's default branch — ahead of every release tag including v24.2.0 —
  to the v24.2.0 release commit, and its `# v22` comment corrected. The
  workflow was already running code past v24.2.0 while the comment claimed
  v22; the pin now names an immutable released artifact rather than a
  moving branch head.

## [3.0.0] - 2026-08-08

### Added

- **`tool`** — **`quarryd` now logs its operations to a file.** Until now it
  logged nowhere: the daemon's entry point never configured logging, so Python
  fell back to `logging.lastResort` — a bare stderr handler at WARNING with no
  formatter. Every operational INFO line was discarded before reaching a
  handler, and anything that did escape reached the supervisor's stderr file
  with no timestamp, level, or logger name. Three incidents this week were
  diagnosed the hard way because of it.

  Operations now land in `quarryd.log`, beside the client tier's `quarry.log`
  in the same directory, with the same format and the same 5 MB × 5 rotation.
  The files are separate on purpose: one long-lived writer versus many
  short-lived ones, and interleaving them makes a line impossible to attribute
  to a process. Uncaught exceptions reach the file too — main thread, worker
  threads, and unawaited event-loop tasks — while the supervisor's stderr keeps
  its copy as a backstop.

  Log volume is governed by a stated rule: INFO is one line per user-visible
  operation or coarser, and sub-operation detail is DEBUG. Two lines that would
  have flooded the new file move to DEBUG accordingly — the per-flush chunk
  insert and the per-request search result count.

### Removed

- **`tool`** — **the daemon no longer reports database size**, on either
  `/v1/databases` or `/status`, and `quarry list databases` and `quarry status`
  no longer show it. Producing that number meant walking the whole data tree on
  every request: 10 to 19 seconds on a 1.13 GB store, payable by any client and
  stacking across clients on one daemon. It had already crossed from slow into
  broken — a daemon under load could not answer `/v1/databases` inside the
  client's 15-second timeout, so the command failed rather than lagged.
  Afterwards `quarry list databases` runs in 1.3 to 1.8 seconds and
  `quarry status` in about 0.8.

  There is no cheap replacement, and one was checked rather than assumed:
  LanceDB's `table.stats()` returns in 0.3 ms, but it measures the **live
  dataset**, not the directory — it excludes whatever the directory also holds,
  which on two different stores meant under-reporting the on-disk footprint by
  43% and by 81%. What makes up that gap varies (superseded data-file versions,
  index files), which is the point: it is not a constant a caller could correct
  for. Publishing it as the size would have been a wrong number rather than a
  fast one.

  The removal is total — no tombstones: `dir_size_bytes`, `format_size`,
  `_fmt_size`, the `DatabaseSummary` type, and the unused `discover_databases`
  are all deleted along with the fields.

- **`tool`** — **`quarry doctor` no longer reports storage size or probes the
  OCR engine**, and is roughly ten times faster for it: 1.7 seconds where it
  previously took 15 to 52 depending on how busy the machine was. The Storage
  line ran `du` across the whole data tree — 9 to 14 seconds on a 5.6 GB store,
  and almost all of the system time the command spent — while the OCR line
  instantiated RapidOCR and loaded its models to prove they load, another 1.8
  seconds. Doctor's job is a fast environment check; neither operation belonged
  in it, so both are gone rather than cached or hidden behind a flag. Every
  remaining check costs at most a quarter-second, and the daemon is untouched:
  doctor's only calls to it are two `/health` probes totalling 0.02 seconds.

  Storage size is no longer reported by any quarry surface (see the daemon
  entry above for why no cheap, honest number exists); when you want it, ask
  the filesystem: `du -sh "${QUARRY_ROOT:-$HOME/.punt-labs/quarry/data}"`.

### Security

- **`infra`** — **starlette raised to `>=1.3.1`** (resolves 1.5.0), closing five
  advisories that affected every prior release: `request.form()` limits silently
  ignored for `application/x-www-form-urlencoded`, enabling DoS (high); SSRF and
  NTLM credential theft via UNC paths in `StaticFiles` on Windows (high); missing
  Host header validation poisoning `request.url.path` and bypassing path-based
  checks (medium); arbitrary HTTP methods dispatched to `HTTPEndpoint` attributes
  via `getattr` (medium); and an unvalidated request path concatenated into the
  authority, poisoning `request.url.hostname` (low).

  The previous `starlette<1.0.0` ceiling is **removed, not raised** — it was what
  made these unfixable by any version bump, since the patches exist only in 1.x.
  `fastapi`'s floor rises to `>=0.133.0` for a dependent reason: every earlier
  release caps starlette below 1.3.1 and so cannot coexist with the secure floor.

### Fixed

- **`index`** — **the daemon no longer re-embeds a document whose content did
  not change.** A filesystem event fires on every write, and most writes leave
  the bytes identical — an editor saving in place, a branch switch restoring the
  same content, a `touch`. The watch loop's per-file path had no change
  detection at all: it deleted the document's stored chunks and re-embedded the
  whole thing every time, which is how one document came to be embedded five
  times in a row under churn. The bulk sync path already had the rule; it now
  lives in one place (`quarry.sync_change.FileChangeDetector`) that both paths
  consult, so unchanged content is a no-op and content that merely moved its
  `(mtime, size)` refreshes the registry row without touching LanceDB.

- **`index`** — **table optimization now runs once per database per rescan
  sweep, not once per collection.** A finalize compacts a database's chunks
  table and rebuilds its entire full-text index in one table-wide pass, but the
  periodic reconcile and `quarry sync` both submitted one per registered
  collection — repeating identical work N times. With six collections
  registered, one daemon log showed sustained bursts of six back-to-back
  optimize-and-rebuild pairs per sweep. The finalize stays immediate rather than
  rate-limited, because these sweeps are the FTS self-heal and must always run;
  what changed is that they are now deduplicated to the granularity the work
  actually has.

- **`tool`** — `quarry enable` now ensures `.punt-labs/quarry/captures/` is
  excluded from the target repo's `.gitignore` (creating the file if missing,
  appending the line if absent, and leaving it untouched if already present),
  and does so BEFORE any step that makes capture writing more live: within
  `Enablement.enable()` the `.gitignore` ensure now runs first, and
  `enable_project()` runs `Enablement.enable()` before writing
  `config.md` (whose `compaction: true` has no dependency on gitignore/marker
  state and is what makes hook-triggered capture writes go live). Previously
  `enable` never wrote this exclusion at all, and even after adding it the
  write-order left a fail-open window where a mid-sequence failure could
  leave a repo "capturing enabled, unprotected." Both orderings are now
  fail-closed. The step is idempotent and backfills a missing exclusion on
  an already-enabled repo; `disable` never prunes the line, since it is
  additive-only. Surfaced as `EnableResult.gitignore_ensured` and a new CLI
  summary line. The ensured `.gitignore` also now excludes
  `FileLock`'s own lock files (e.g. `.CLAUDE.md.lock`, `..gitignore.lock`) --
  `FileLock` creates one beside every host file it locks and, by design,
  never removes it, so without this every `quarry enable` left a
  machine-local artifact a bare `git add -A` could commit. (pkit-kcps)
- **`infra`** — `quarry`'s capture scrubber now redacts common English
  inflections of its profanity list (`-s`/`-es`, `-ed`, `-ing`,
  `-er`/`-ers`), not just the bare word. Two real transcripts leaked
  "fucking" and "fucked" unredacted because only exact base-form matches were
  scrubbed. Inflected forms that collide with unrelated real words, dice
  games, occupational terms, or surnames (e.g. "dicker", "Heller", "craps",
  "jerker") are excluded, per a systematic audit of every generated
  inflection — not just the agent-noun `-er` form — against a real English
  dictionary, so ordinary text is never over-redacted; whole-word boundary
  matching still protects safe substrings like "class", "passing", and
  "embassy". A consonant-doubling heuristic bug that mis-inflected the
  disyllabic "moron" ("moronned"/"moronning") is also fixed. (pkit-kcps)
- **`infra`** — `quarry`'s test suite no longer writes to the user's real
  `~/.punt-labs/quarry/` tree. A full run previously appended ~7,000 lines to
  `logs/quarry.log`, rotating away real daemon history every seven or eight
  runs. `LoggingConfig.configure()` now resolves its directory per call from
  `QUARRY_LOG_DIR` (falling back to the home path) instead of pinning it in a
  class constant at import time, and `Settings` derives its `config.toml` path
  from `quarry_root` so relocating the root relocates the config with it. Both
  are usable by any deployment that wants its logs or config elsewhere.
  (DES-047)

### Changed

- **`infra`** — the test suite is hermetic and bounded per run: `HOME` and the
  LanceDB/OpenMP thread variables are pinned at session start, the embedding
  fake is enforced at the factory (with an `embedding` marker to opt back in to
  the real model), and session invariants assert no leaked non-daemon threads
  and no writes to the production tree. No `pytest-xdist` and no workspace-wide
  concurrency mechanism — see DES-047 for why both are rejected rather than
  deferred.

## [2.1.0] - 2026-08-02

### Changed

- **`quarry doctor` Sync check** now reports the *newest* collection's sync age
  — a pipeline-liveness signal ("has anything been ingested recently?") — instead
  of the oldest. Quiet, unchanged reference collections no longer trigger a false
  ">24h stale" warning; the check only flags stale when *nothing* has synced in
  24h. (The correct index-vs-filesystem drift check is tracked separately.)
- **`quarry doctor` captures check** no longer flags by-design non-directory
  capture collections (e.g. `web-captures`) as detached, and uses neutral wording
  ("unlinked") in place of "orphaned".

### Fixed

- **Directory sync no longer crashes (or mass-deletes) on filesystem races.**
  The reconcile's per-file `stat()` was unguarded, so a file removed from disk
  between discovery and stat aborted the entire collection's reconcile — a
  crash-loop that left the collection permanently behind. Now a vanished file is
  reconciled to a delete, an unreadable file (permission/IO error) is skipped and
  retried, and — critically — the delete pass is **refused when the registered
  root itself cannot be resolved** (a transient NFS/SMB blip or `ESTALE`), so an
  empty scan from an unavailable root can no longer wipe a whole collection's
  index. A raced `.gitignore` deletion no longer aborts the directory walk either.
- Regenerated `docs/openapi.json` so `HealthResponse.fd` is no longer listed as
  `required`, matching the source: the field is optional (`FdHealth | None`) so a
  new client validating a pre-upgrade daemon's `/health` — which omits `fd` —
  does not fail validation. The committed schema had drifted from the source.

### Added

- **`make logs-errors`** (and `make logs-tail`) — a daemon-log diagnostic that
  scans the quarry daemon's log directory (`~/.punt-labs/quarry/logs`, override
  with `LOG_DIR`) for error/failure signals — `ERROR`, `Traceback`, `EMFILE`/
  "Too many open files", `Watch index failed`, `Delete failed`, and more —
  printing a per-signal count summary and the most recent matching lines
  (`LOG_LINES`, default 40). Always exits 0; it is a diagnostic, not a gate.
  Surfaces daemon incidents that `quarry doctor` does not scan.

## [2.0.2] - 2026-07-30

### Fixed

- **infra (daemon)**: the daemon file-descriptor fix now actually engages on a
  fresh install, and `quarry doctor` finally reports the **daemon's** fd headroom
  instead of the CLI shell's. 2.0.1's in-process `RLIMIT_NOFILE` raise was correct
  in isolation but silently no-op'd on a fresh `quarry install`: a freshly
  bootstrapped launchd agent inherits a hard limit of 256, and a non-root process
  cannot raise its own hard limit, so the in-process raise clamped and lifted
  nothing. The service manager now bakes the ceiling **before** spawn — launchd
  `SoftResourceLimits`/`HardResourceLimits` and systemd `LimitNOFILE` at 8192:65536,
  derived from `QUARRY_FD_LIMIT` (floored to the safe default so the override can
  only raise). Separately, `quarry doctor`'s FD-headroom check had been sampling the
  short-lived CLI's own `ulimit`, never the resident daemon — it now reads the
  daemon's fd headroom from `/health` (degrading cleanly when the daemon is
  unreachable, never falling back to a local sample). See the DES-046 amendment.

## [2.0.1] - 2026-07-29

### Fixed

- **infra (daemon)**: the resident `quarryd` no longer exhausts file descriptors
  over long uptime. It inherited launchd/systemd's soft `RLIMIT_NOFILE` (256 on
  macOS) and never raised it, while post-DES-045 it holds one LanceDB connection
  per roster database — so at ~21 collections the aggregate of per-connection
  descriptor plateaus exceeded 256 and the daemon walked into `EMFILE` after ~16h
  (failed flushes, spool errors, cascading tracebacks). The daemon now raises its
  soft limit to a configurable target (`QUARRY_FD_LIMIT`, default 8192) at start,
  fail-safe: clamped to the hard limit, never lowering a higher inherited limit
  (an operator's systemd `LimitNOFILE` is honored), and a malformed override
  degrades to the default with a warning rather than crashing. The reader-recycler
  is unchanged — a daemon-scale plateau invariant proves the aggregate is already
  bounded, so the fix is the higher ceiling, not a new mechanism. See DES-046.

## [2.0.0] - 2026-07-27

### Added

- **transform (OCR)**: OCR now works on headless machines (servers, minimal
  containers), not only desktops. Importing `cv2` on a box without X11/GL no
  longer crashes the OCR path — a failed load degrades cleanly to "OCR
  unavailable" instead of taking down ingestion or `quarry doctor`, and the
  doctor's OCR check is advisory (a WARNING, not a required failure that aborts
  `quarry install`). `quarry install` also force-reinstalls
  `opencv-python-headless` (`--no-deps`, as the last writer of `cv2/`) so a
  direct `uv tool install` / `pip install` that pulls the GUI `opencv-python`
  (whose `cv2` links X11/GL and shadows the headless build) is repaired to
  headless — the package-side equivalent of install.sh's resolver override
  (Fixed, below), covering installs that bypass the `curl … | sh` path.
  (quarry-lb1z)
- **infra (install.sh)**: `--no-plugin` flag and `QUARRY_NO_PLUGIN=1` env var to
  install the harness-neutral CLI while skipping the Claude Code
  marketplace-register + plugin-install steps (per punt-kit `install-cli-only.md`).
  For non-Claude harnesses (Codex, Cursor, a plain terminal) and enterprise-policy
  Claude users whose org blocks marketplace installs — `claude` is present so the
  capability auto-skip never fired, and a piped `curl … | sh` had nowhere to put a
  flag. Both `sh -s -- --no-plugin` and `QUARRY_NO_PLUGIN=1 sh` work over a pipe;
  the env var is honored only when exactly `1`. The skip is scoped to the plugin
  steps — binary, PATH, model, TLS, per-repo login, and the health check all still
  run — and the success message states the CLI works without printing the
  "Restart Claude Code" line. Unknown flags now exit 2 with a usage string so a
  misspelled `--no-plguin` is not silently ignored over a pipe.
- **tool (mcpb)**: on-top Claude Desktop / CoWork access to the same local index.
  The `.mcpb` bundle registers the thin `quarry mcp` stdio client, which connects
  to the same resident `quarryd` daemon that backs the CLI and Claude Code — so
  Desktop searches exactly the data you have already indexed, not a separate
  store. It embeds no engine and is not a standalone install: quarry (the `quarry`
  binary + a running daemon) must be present first, then `quarry install`
  auto-configures Desktop or you double-click the bundle to add it yourself.
- **infra (install.sh)**: `QUARRY_LOCAL_WHEEL=/path/to/wheel` installs a
  working-tree wheel instead of the PyPI-pinned release — for offline/air-gapped
  installs, pre-release testing, and the clean-machine harness. Unset (the
  default) installs `punt-quarry==<VERSION>` from PyPI as before.
- **infra (test harness)**: a clean-machine Docker gate, `make test-install-clean`
  (CI: `.github/workflows/install-harness.yml`, path-filtered to `install.sh` +
  `tests/harness/**`). It builds a working-tree wheel and runs `install.sh`
  end-to-end as a fresh, unprivileged user in a pinned `python:3.13-slim`
  container — no uv, no quarry, no prior state — asserting the CLI-only path
  across both skip triggers (claude-absent auto-skip and claude-present
  `--no-plugin`/`QUARRY_NO_PLUGIN=1` operator-driven skip), that no
  marketplace/plugin step runs, that the "Restart Claude Code" line is absent,
  and that `quarry version`/`quarry doctor` plus a real `remember`→`find`
  round-trip work. Closes the gap the mock unit tests and venv wheel test could
  not: does `install.sh` actually install a working quarry from scratch.

### Fixed

- **infra (gpu runtime)**: match the `onnxruntime-gpu` build to the host's
  loadable CUDA major so `quarry install` stops breaking GPU on CUDA-12 hosts.
  The swap installed the newest `onnxruntime-gpu` unconditionally (an unbounded
  `>=1.18.0` spec); as of 1.27.0 that wheel links `libcudart.so.13` (CUDA 13), so
  on a host with only system CUDA 12 `import onnxruntime` raised at import time
  and the daemon was left with an unimportable onnxruntime — strictly worse than
  the CPU wheel, and re-broken on every `quarry install`. The swap now probes
  `ldconfig` for the resolvable CUDA runtime major, selects the matching version
  range (`12 → >=1.19.0,<1.27.0`, `13 → >=1.27.0`), and verifies the installed
  wheel actually imports with CUDA before declaring success — restoring CPU if it
  does not. A host with no mappable CUDA runtime (or a decode/probe failure) keeps
  CPU and reports the new `GpuStatus.CUDA_UNSUPPORTED` in `quarry doctor` (naming
  detected vs supported majors) instead of silently mis-pinning. Verified
  end-to-end on an RTX 5080 / CUDA-12 host: `quarry install` selects
  `onnxruntime-gpu 1.26.0` and `CUDAExecutionProvider` comes up.

- **infra (install.sh)**: the CLI-only install could not complete on a headless
  machine (server, minimal container). `rapidocr` pulls the GUI `opencv-python`,
  whose `cv2` build dynamically links X11/GL libraries (`libGL.so.1`,
  `libxcb.so.1`) absent without a desktop; it ships the same `cv2` module as
  quarry's pinned `opencv-python-headless` and shadows it, so `import cv2` failed
  to load. That made `quarry install`/`quarry doctor` report required-check
  failures (`Local OCR`, `Core imports: Failed: cv2`), which aborted `install.sh`
  under `set -e`. The installer now passes a uv override (`opencv-python;
  sys_platform == "never"`) that drops the GUI build for the whole resolution,
  leaving `opencv-python-headless` as the sole `cv2` provider — verified
  end-to-end on a bare `python:3.13-slim` container (`Local OCR: RapidOCR engine
  OK`, `Core imports: 8 modules OK`).

- **infra (mcpb)**: restore the Claude Desktop `.mcpb` download. The README link
  (`releases/latest/download/punt-quarry.mcpb`) 404'd because no release ever
  attached the asset and `scripts/build-mcpb.sh` still read a `manifest.json` that
  had been removed (b2c9ffb). The manifest is now generated at build time into
  `dist/mcpb-staging/` — never the repo root, which would re-strip the plugin's
  slash commands — from `scripts/mcpb-manifest.template.json` with the version
  sourced from `pyproject.toml`. It reflects the current daemon-first
  architecture: the `quarry mcp` stdio client, the 11 current MCP tool names, and
  the `~/.punt-labs/quarry` data dir. The release workflow now builds and attaches
  the bundle on every `v*` tag and verifies both that the asset is downloadable
  and that the README install-URL SHA is not stale.

- **index (watch)**: the always-on watch loop no longer indexes temp, scratch,
  VCS, or gitignored paths. A registered OS-temp directory (a stale `/private/tmp`
  registration on the operator's machine) caused the daemon to watch and
  re-OCR/re-index the macOS system temp dir — which every process writes to —
  continuously, pinning CPU for hours. A single `ScratchGuard` now refuses OS-temp
  roots (`/tmp`, `/private/tmp`, `/var/tmp`, `/private/var/tmp`, `/var/folders`,
  compared casefolded so a case variant can't slip past on APFS) and a repo's own
  `<gitroot>/.tmp` (anchored on any ancestor git repo) — applied at initial scan,
  live watch, reconcile, and explicit `quarry sync`, fail-closed per root, and
  refusing an already-registered temp root at watch time (so a stale registration
  cannot storm on restart). Below a permitted root, the scan also skips
  `.gitignore`d paths (via `FileDiscovery`'s pathspec matching) and a
  VCS/build/cache always-skip set. (quarry-dpww; DES-045b.)

- **index (daemon)**: `quarryd` now stays bounded under continuous dev load.
  LanceDB's compaction and full-text-index rebuild ran on a tokio pool the Rust
  core sizes to the machine's core count, uncapped — so ordinary background
  indexing could spike the daemon to 3-4× a core (291-403% observed) on an
  otherwise-quiet machine. The daemon now caps `LANCE_CPU_THREADS` /
  `LANCE_IO_THREADS` (a fail-closed clamp with a floor of 2 — a lower or inherited
  value is clamped into range, and a `1` is raised to `2` because `1` stalls
  LanceDB's runtime) alongside
  the existing ONNX/OMP limits, so the compaction/FTS work is bounded
  core-count-independently; and the per-collection finalize (optimize + FTS
  rebuild) is coalesced to at most one per `watch_optimize_min_interval_s`
  (default 30 s) per database — including the bulk-churn path — with the periodic
  reconcile as the eventual backstop. Measured on the operator's 8-core machine:
  where the pre-cap daemon held 403% at 30 min of uptime, the capped daemon idles
  and bursts to at most ~2 cores briefly during a compaction. The FTS index is
  LanceDB's native inverted index (not a separate uncapped Tantivy pool).
  (quarry-exz9; DES-045c and the DES-032 amendment.)

## [1.20.0] - 2026-07-25

### Added

- **index (watch)**: DES-045 always-on filesystem watch loop — the daemon now
  watches every registered directory across every database in its roster and
  indexes changes continuously, as a producer onto the existing DES-042
  serialized queue (no second queue, no direct LanceDB writes). A debounced edit
  burst coalesces to one reindex of the final bytes (`watch_debounce_s`, default
  1.0s); a continuously-rewritten file still indexes via `watch_max_delay_s`
  (5.0s); a large burst (> `watch_bulk_threshold`, 50 distinct paths) collapses
  to a single bulk scan rather than thousands of admissions. Small deltas submit
  per-file jobs; deletes remove the document; the FTS rebuild is coalesced to a
  single post-quiescence pass so per-file indexing never reopens the quarry-0dss
  descriptor leak (proven across ≥2 databases by a resource-invariant test). The
  queue's routing key is now `(database, collection)`, extending the
  single-writer-per-table invariant across the whole roster. Watching is on by
  default (`watch_enabled=true`); `watch_use_polling` selects watchdog's
  stat-walk fallback. New core dependency: `watchdog>=4.0`.
- **index (watch lifecycle)**: DES-045a removal lifecycle, orphan sweep, and
  keep-data re-adopt. Registering a directory that subsumes an existing narrower
  registration now tears down the child's watch and purges its chunks before
  installing the parent watch; a deregister or subsume purge shed under a full
  queue is retried by the periodic safety-scan reconcile. A durable
  orphan sweep purges only collections the registry has **explicitly marked for
  purge** (the `pending_purge_collections` table — marked on a non-keep-data
  deregister or a subsume eviction, in the same transaction as the directory-row
  delete), never an open-world "everything not currently registered"; so captures,
  agent memories, and `remember` targets — collections that legitimately have no
  directory registration — are structurally never swept. The marker survives a
  daemon restart (a shed purge is drained by the next sweep), and
  `CollectionPurgeJob` re-checks the mark at execution time so a disable→re-enable
  toggle can't race the sweep into deleting live chunks. A new
  `retained_collections` marker records the
  directory a `--keep-data` disable was taken from, so a *different* directory
  reusing an archived collection's leaf name (`backend`, `docs`, …) can no longer
  silently adopt its chunks — a cross-project search-merge that was previously
  undetectable. Re-enabling the *same* directory re-adopts its kept collection
  and auto-freshens it: the re-adopt reconciles stored documents against disk and
  prunes files deleted while it was disabled (pruning keys on the authoritative
  stored path and only on definite absence, so a nested/basename-stored doc or a
  transiently-unreadable file is never wrongly deleted). The retained set and the
  chunk-bearing collection set are both surfaced on `GET /v1/registrations` for
  remote/local parity. The fresh collection-name picker now avoids every
  chunk-bearing name (`registered ∪ retained ∪ chunk_collections`) on both the
  client and the session-start hook, so a different directory can never be
  auto-assigned a name that already holds another project's chunks — closing a
  family of cross-project auto-merges; it fails **closed** when the daemon is
  unreachable (defers the auto-registration with a nudge rather than arm a latent
  merge). The whole removal + naming lifecycle is modeled in Z
  (`docs/spec/watch_lifecycle.tex`) and ProB model-checked (invariants I1–I10,
  incl. non-directory collections; each with a negative control) — the model and
  adversarial review caught a series of data-safety defects before merge, one of
  which (an open-world sweep that would have wiped all captures and agent memories
  on a 5-minute default timer) was catastrophic; see DES-045a.
- **infra (boundary)**: DES-031 v2 client/engine boundary lock (PR-6) — the
  daemon-first split is now enforced structurally, not by convention. A new
  import-linter contract (`.importlinter`, wired into `make check` via
  `check-imports` and into CI) fails the build if any client process
  (`quarry.__main__`, `quarry.hooks`, `quarry.mcp_server`) or client library
  (`quarry.client`, `quarry.api`) imports an engine package (`quarry.db`,
  `quarry.embeddings`, `quarry.ingestion`, `quarry.retrieval`, `quarry.sync`,
  `quarry.daemon`); the only sanctioned exceptions are the host-admin diagnostic
  commands' lazy engine imports. The runtime engine-sabotage guard now covers the
  full client surface (poisons `lancedb`/`onnxruntime`/`pyarrow` and imports each
  client module), catching a lazy engine import that leaks to module scope where
  the static contract cannot see it. A reusable in-process ASGI daemon fixture
  (real handlers over Starlette, no socket, no ONNX) makes daemon-mandatory
  CLI/MCP tests hermetic — verifiable with the daemon stopped — and one
  real-loopback-TLS smoke (`make test-slow`) proves the pinned-CA wire contract
  end-to-end without destabilising the fast CI suite.

- **index (daemon)**: serialized capture/index queue (DES-042) — the daemon now
  drains capture, remember, and ingest jobs through a per-collection FIFO worker
  instead of firing an unbounded `asyncio.create_task` per request. One in-flight
  writer per LanceDB collection restores DES-034's single-writer precondition
  under a burst (two same-document overwrites no longer interleave into two
  resident chunk sets), and a global embed semaphore (hard-clamped to one job at
  a time — DES-032) bounds CPU oversubscription. Admission is a non-blocking
  bounded gate: a full queue returns `503` (retriable; the durable capture
  artifact stays recoverable via `quarry backfill`) rather than blocking the hook
  or silently dropping. A background task now begins in a new interim `queued`
  status that the worker flips to `running` on dequeue; the `/v1/tasks` response
  shape is unchanged and existing clients already poll through it. Tunable via
  `ingest_embed_concurrency`, `ingest_queue_depth`, and `ingest_drain_timeout_s`;
  a clean shutdown drains queued jobs within the drain timeout.

- **tool (daemon REST)**: `POST /v1/capture` — the capture front door, sharing
  one always-scrubbing `ScrubbedIngestJob` with `remember`. The daemon derives
  the target `<repo>-captures` collection server-side from the client's working
  directory (falling back to `default-captures`) and scrubs before storing, so
  a client never picks the collection or trusts a pre-scrub. Reached via the new
  `client.capture()` and `CaptureIngestRequest`.

- **tool (CLI)**: `QuarryClient` — a typed, pure-transport client the CLI drives
  for every data command. It carries a `QuarryError` hierarchy (`QuarryError`,
  `QuarryConnectionError`, and `HttpError` — whose `status` selects the exit
  code, 409 being "already in progress") that the CLI maps to exit codes in one
  place, and a typed `TaskOutcome` for polled background tasks. `TargetResolver`
  is the single daemon-target resolver: explicit `QUARRY_URL`/`QUARRY_TOKEN`,
  then a stored remote login, then the local daemon on `127.0.0.1` via
  `serve.port` + live `serve.token` (fail-closed with an autostart hint when the
  daemon is down).

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
- **tool (daemon)**: new `quarryd` engine binary — the sole process that
  loads the engine (embedding model, LanceDB, ingestion, retrieval). It
  refuses a non-loopback bind without an operator key, and mints a 256-bit
  loopback `serve.token` when none is supplied. Supervised units exec
  `quarryd` directly.
- **infra (security)**: loopback `serve.token` (mode-0600, atomic write) —
  the daemon now requires a bearer on every request including `127.0.0.1`,
  closing the exposure where any local user on a multi-user host could reach
  the unauthenticated daemon. `ClientConfig` (`quarry/client`) resolves it:
  a loopback target reads the token live (it rotates each daemon restart, so
  a stored token is never trusted), a remote target keeps its stored bearer,
  and a missing loopback token fails closed with an actionable error.

### Changed

- **tool (daemon REST)**: `POST /v1/backfill-sessions` `limit` is now a pure
  pagination knob that agrees with the local `backfill_sessions` path — `0` (the
  wire default, and an empty body) means "all", a positive value caps the scan,
  and no ceiling is imposed. The daemon previously rewrote a missing/`<=0` limit
  and any value above `500` into a `DEFAULT_REMOTE_BACKFILL_LIMIT = 500` cap,
  which silently diverged from the CLI's `limit=0` "all" default (bug class 3,
  remote/local divergence). The 500 cap was a magic number standing in for
  resource safety; a resource-invariant test (`resource` marker) proves a
  single-connection backfill of hundreds of transcripts holds open file
  descriptors flat (growth 0 across 250 transcripts), so the run is bounded by
  construction — it streams one transcript at a time and never rebuilds the FTS
  index per transcript — and needs no transcript-count cap.

- **tool (MCP)**: `quarry mcp` is now a thin FastMCP client of the daemon
  (DES-031 v2.2, R1/R2). Every tool body calls `QuarryClient` over the daemon's
  `/v1` REST API instead of loading `Database`/embeddings/ingestion in-process,
  so `import quarry.mcp_server` and running `quarry mcp` load zero engine (no
  LanceDB/ONNX); the in-process engine MCP path is deleted, not shimmed. The
  eleven-tool surface (`find`, `ingest`, `remember`, `list`, `show`, `delete`,
  `register_directory`, `deregister_directory`, `sync_all_registrations`,
  `status`, `use`) is unchanged. A down daemon surfaces as a clean MCP error
  string, never an in-process fallback. Remote MCP now rides `QuarryClient`'s
  TLS + pinned-CA login config. The Claude Code plugin (`plugin.json`) and the
  `quarry install` MCP-client config now spawn `quarry mcp` directly, dropping
  the `mcp-proxy … else quarry mcp` shim; mcp-proxy itself is untouched and
  remains a supported tool for other consumers.

- **index (captures)**: the session-compaction and web-fetch hooks post to the
  running daemon instead of spawning a cold ~1.6 GB engine subprocess per
  compaction — ending the load-average spike from many concurrent cold-starts.
  The hooks now import no engine, only the thin client; a down daemon still
  writes the durable transcript archive and scrubbed `.md`, and
  `backfill-sessions` indexes them later.
- **index (sync)**: directory sync now excludes `.punt-labs/quarry/captures/`
  structurally (built-in ignore list), so scrubbed captures can never be folded
  into a project's main collection regardless of the repo's `.gitignore`.

- **tool (CLI)**: every data command (`find`, `ingest`, `show`, `remember`,
  `status`, `delete`, `register`, `deregister`, `sync`, `enable`, `disable`,
  `optimize`, `backfill-sessions`, `list …`, `captures push`) now runs
  unconditionally through the daemon over `QuarryClient` — there is no in-process
  engine path and no local-vs-remote fork. A running `quarryd` is required; when
  it is down, commands fail closed with a start-the-service hint.
- **tool (CLI)**: `quarry ingest` accepts a URL only. Local files and
  directories are covered by `quarry register <dir>` + `quarry sync`; a non-URL
  argument is rejected with that pointer.
- **tool (CLI)**: `quarry backfill-sessions --limit` is forwarded to the daemon,
  which applies its own bound; `--limit 0` now takes the daemon's default rather
  than "all". The no-op `--provider` flag is removed (the daemon owns provider
  selection).
- **tool (CLI)**: `remote list --ping` now reports the daemon's `/health`
  (`state`, `api_version`, `quarry_version`) instead of an ad-hoc reachability
  string.
- **infra (build)**: `httpx` is now a runtime dependency (`QuarryClient`'s
  transport), moved from the dev extras.
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
- **infra (service)**: the launchd/systemd unit now execs `quarryd` instead
  of `quarry serve`, and systemd uses `Restart=always` (launchd already
  `KeepAlive`) so the engine respawns on any exit. Re-run `quarry install`
  to regenerate the unit.
- **infra (install)**: the install `/health` gate now requires
  `state == "ready"`, not a bare HTTP 200 — a warming daemon returns 200
  with `state == "starting"`, so a bare 200 could green-light an unready
  daemon.

### Removed

- **index (captures)**: the detached `background_ingest` engine subprocess and
  its `_hook_entry` dispatch — superseded by the daemon capture path (no more
  cold engine spawned per compaction).
- **tool (CLI)**: `RemoteClient`/`RemoteError` (superseded by `QuarryClient` and
  the typed `QuarryError` hierarchy) and the in-process engine path from every
  CLI data command.
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
- **tool (CLI)**: removed the `quarry serve` subcommand (no shim, PL-PP-1) —
  start the engine with `quarryd` (the supervised unit does this for you).
- **infra (library API)**: `import quarry` no longer re-exports the engine.
  The names `Database`, `get_db`, `ChunkSearch`, `ingest_content`,
  `ingest_document`, and `ingest_url` are removed from the top-level package
  (no shim, PL-PP-1) — so `import quarry` stays engine-free and stdlib-cheap.
  The library is now a thin client: the top-level surface is `QuarryClient`,
  `TargetResolver`, `ClientConfig`, the `QuarryError`/`QuarryConnectionError`/
  `HttpError` hierarchy, `TaskOutcome`, and `__version__`; request/response
  models live in `quarry.api`. Engine-side callers import from `quarry.db` /
  `quarry.ingestion.pipeline` / `quarry.retrieval` directly.

### Security

- **index (memory/captures)**: `remember` and session captures are now scrubbed
  of secrets, PII, and profanity on the daemon before any chunk is stored —
  previously the database copy of a remembered note or a session transcript
  landed in cleartext (only the git-committed `.md` was scrubbed). A failed
  scrub writes zero chunks. Forward-only: existing cleartext is left to a future
  purge.
- **infra (loopback auth)**: the daemon no longer serves unauthenticated
  loopback requests. It writes a mode-0600 `serve.token` and requires it on
  every request, and the loopback classifier is fixed to recognize
  `localhost`/`::1`/`127.0.0.0/8` (the old `127.0.0.1`-literal check
  misclassified them) while treating `0.0.0.0` and unresolved names as
  remote (fail closed — an operator key is required).
- **ingest (SSRF, redirect + sitemap crawl)**: server-side fetches now re-run
  the SSRF address gate at every hop, not only on the initial source. Three
  bypasses are closed. (1) A caller-supplied public URL that HTTP-redirected to
  a private, loopback, link-local, CGNAT, or cloud-metadata address was followed
  with no per-hop check — a guarded redirect handler now rejects each 30x
  `Location` against its resolved address before the hop is followed, and the
  final resolved URL is validated. (2) The sitemap crawler (ultimate-sitemap-
  parser) fetched sitemap-indexes, `robots.txt` `Sitemap:` lines, and nested
  sub-sitemaps server-side and recursed through them before quarry saw any leaf
  URL, so an internal address listed at any depth was fetched ungated — the
  crawler now runs through a web client that SSRF-gates every URL it fetches at
  every recursion depth, refusing blocked targets fail-closed (the crawl skips
  them, never connects). (3) An IPv4-mapped IPv6 address (`::ffff:100.64.x`)
  carrying a CGNAT address slipped the IPv4-only CGNAT check — the gate now
  judges a mapped address by its embedded IPv4. Both CLI and MCP ingest are
  covered (they share the daemon fetch path). Complementary to resolved-IP
  pinning, which remains a separate follow-up for the residual DNS-rebind
  TOCTOU.
- **ingest (SSRF, DNS-rebind pin)**: server-side fetches now connect only to the
  address they validated, closing the residual DNS-rebinding TOCTOU noted above.
  Previously the admission gate resolved a host and rejected on a blocked
  address, but `http.client` re-resolved the host independently at connect, so
  an attacker's DNS could return a public address at admission and an internal
  one at connect. New pinned HTTP(S) connections perform exactly one
  `getaddrinfo` inside `connect`, validate every returned address fail-closed
  (all-records: any blocked address refuses the whole connection), and connect
  the socket to a validated IP literal from that same result — there is no
  second, independently-resolved lookup for a rebinder to poison. TLS is
  untouched: SNI, certificate verification, and the `Host` header stay bound to
  the hostname (never the pinned IP), and the public-fetch context keeps the
  system trust store (deliberately not the daemon-RPC pinned-CA context — the
  pin narrows the address, not the trust). Both CLI and MCP ingest are covered
  (they share the daemon fetch path), and every redirect hop re-pins its new
  host. Closes the DNS-rebind follow-up (quarry-kmzo, quarry-ljym).

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
