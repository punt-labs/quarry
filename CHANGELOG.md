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
