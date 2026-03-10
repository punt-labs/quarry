# Quarry Design

Architecture and design decisions for contributors and agents. For user-facing documentation, see [README.md](README.md).

## Architecture: Library-First Direct Delegation

Quarry follows the Punt Labs [projection strategy](https://github.com/punt-labs/punt-kit): every project starts as a library and projects outward through thin adapter layers. The core library does the work; CLI, MCP, HTTP, and plugin surfaces are thin wrappers that delegate directly.

```text
┌──────────────────────────────────────────────────────┐
│                    Consumers                         │
│  CLI (typer)  │  MCP (FastMCP)  │  HTTP (stdlib)    │
│  cli.py       │  mcp_server.py  │  http_server.py   │
└───────┬───────┴────────┬────────┴────────┬──────────┘
        │                │                 │
        └────────────────┼─────────────────┘
                         │  direct function calls
        ┌────────────────┼─────────────────┐
        │           Core Library           │
        │  pipeline · database · sync ·    │
        │  backends · config · models      │
        └──────────────────────────────────┘
```

No intermediary service layer. Each surface imports core functions and calls them with resolved arguments. This means:

- **CLI** (`cli.py`): Resolves settings, calls core functions, formats output with rich. Synchronous.
- **MCP** (`mcp_server.py`): Resolves settings, calls core functions, returns pre-formatted text. Side-effect tools use fire-and-forget (see below).
- **HTTP** (`http_server.py`): Stdlib `http.server` for the macOS menu bar app. Translates JSON requests to core function calls.
- **Plugin** (`.claude-plugin/`): Claude Code plugin shell with hooks that call CLI commands.

## Module Responsibilities

### Data Layer

| Module | Responsibility |
|--------|---------------|
| `models.py` | Immutable dataclasses: `PageType` enum, `PageContent`, `Chunk`, `PageAnalysis`. All `@dataclass(frozen=True)`. |
| `types.py` | Protocol definitions for external dependencies: `LanceDB`, `LanceTable`, `LanceQuery`, `OcrBackend`, `EmbeddingBackend`, `TextractClient`, `S3Client`, `SageMakerRuntimeClient`. Structural typing instead of `Any`. |
| `config.py` | `Settings` (pydantic `BaseSettings`), `resolve_db_paths()` for named databases, ONNX model constants. All configuration flows through `Settings`. |
| `database.py` | LanceDB operations: `insert_chunks`, `search`, `list_documents`, `delete_document`, `count_chunks`, etc. Single table `chunks`. |

### Ingestion Pipeline

| Module | Responsibility |
|--------|---------------|
| `pipeline.py` | Orchestrator: dispatches by format, chunks, embeds, stores. Entry points: `ingest_document`, `ingest_content`, `ingest_auto` (URL with sitemap discovery). |
| `chunker.py` | Text splitting with overlap. Respects page boundaries and heading structure. |
| `collections.py` | `derive_collection()` — determines collection name from path or URL. |

### Format Processors

Each processor converts a source format into `list[PageContent]`:

| Module | Formats |
|--------|---------|
| `pdf_analyzer.py` | PDF (text extraction via PyMuPDF, OCR fallback for image pages) |
| `image_analyzer.py` | PNG, JPG, TIFF, BMP, WebP (OCR) |
| `text_processor.py` | TXT, MD, DOCX |
| `code_processor.py` | 30+ languages (AST-aware splitting into functions/classes) |
| `html_processor.py` | HTML (boilerplate stripping, Markdown conversion) |
| `spreadsheet_processor.py` | XLSX, CSV (tabular serialization preserving structure) |
| `presentation_processor.py` | PPTX (slide-per-chunk with tables and speaker notes) |
| `latex_utils.py` | LaTeX (command stripping for clean text extraction) |
| `text_extractor.py` | Unified dispatcher that routes to the appropriate processor |

### Backend Abstraction

| Module | Responsibility |
|--------|---------------|
| `backends.py` | Thread-safe factory with double-checked locking. `get_ocr_backend()` and `get_embedding_backend()` return cached singleton instances. |
| `ocr_local.py` | Local OCR via EasyOCR (offline, no setup). |
| `ocr_client.py` | AWS Textract OCR (cloud, better for degraded scans). |
| `embeddings.py` | Local ONNX embedding (snowflake-arctic-embed-m-v1.5, 768-dim, 512 tokens). |
| `embeddings_sagemaker.py` | SageMaker embedding endpoint (cloud-accelerated batch ingestion). |

Backend selection is config-driven (`OCR_BACKEND`, `EMBEDDING_BACKEND` in `Settings`). The factory reads config once and caches. Protocol types (`OcrBackend`, `EmbeddingBackend`) ensure backends are interchangeable without runtime introspection.

### Sync and Registry

| Module | Responsibility |
|--------|---------------|
| `sync.py` | Directory sync: discover files, compute delta (new/changed/deleted), ingest/delete accordingly. Uses `ThreadPoolExecutor` for concurrent ingestion. |
| `sync_registry.py` | SQLite registry of watched directories. Maps directories to collections with timestamps. |

### Surfaces

| Module | Responsibility |
|--------|---------------|
| `cli.py` | Typer CLI with rich formatting. Synchronous. All commands follow the pattern: resolve settings → call core → format output. |
| `mcp_server.py` | FastMCP server. Read-only tools (`find`, `show`, `list`, `status`) are synchronous. Side-effect tools (`ingest`, `remember`, `delete`, `register_directory`, `deregister_directory`, `sync_all_registrations`) use fire-and-forget. |
| `http_server.py` | Stdlib HTTP server for `quarry serve`. JSON request/response. Used by quarry-menubar. |
| `hooks.py` | Claude Code plugin hooks: `session-start` (auto-register project), `post-web-fetch` (auto-ingest URLs), `pre-compact` (capture transcripts). All fail-open. |

### Support

| Module | Responsibility |
|--------|---------------|
| `formatting.py` | Pre-formatted plain text output using unicode box-drawing. Used by both CLI and MCP to produce consistent output. |
| `results.py` | `SyncResult` dataclass for sync operation reporting. |
| `sitemap.py` | Sitemap discovery and URL extraction for web ingestion. |
| `doctor.py` | Health checks: verifies model availability, database access, backend connectivity. |

## Key Design Decisions

### Fire-and-Forget MCP Pattern

Side-effect MCP tools (ingest, delete, sync, register, deregister, remember) return an optimistic response immediately and process in a bounded `ThreadPoolExecutor(max_workers=4)`. This is necessary because:

1. MCP tool calls block the LLM's response stream. A 30-second ingest blocks Claude from responding.
2. Quarry also deploys as a REST service (`quarry serve`) where request throughput matters.
3. The bounded pool prevents resource exhaustion under concurrent requests.

Settings and database connections are **snapshotted at the tool boundary** and passed into background `_do_*` functions. This avoids a race condition where the mutable `_db_name` global could change between the tool call and the background thread's execution.

Exceptions in background threads are logged via `logger.exception()`, not raised — the MCP caller already received the optimistic response.

### Protocol-Based Typing

Third-party libraries without type stubs (LanceDB, boto3 clients) are typed via Protocol classes in `types.py`. This gives us:

- Exact type checking without `Any`
- Mockable interfaces for testing
- No runtime cost (protocols are erased at runtime)

### Named Databases

`resolve_db_paths(settings, name)` maps a database name to a path under `QUARRY_ROOT`. Each database is fully isolated — its own LanceDB directory, sync registry, and vector index. The `use` command (CLI) and `use` tool (MCP) switch the active database. This enables work/personal separation and per-project databases.

### Embedding Model

snowflake-arctic-embed-m-v1.5: 768-dimensional, 512 token context. Auto-downloads on first use (~500 MB). Chosen for:

- Strong retrieval quality for its size
- Runs efficiently on CPU (no GPU required)
- Permissive license (Apache 2.0)

### Bearer Token Authentication

`quarry serve --api-key` (or `QUARRY_API_KEY` env var) gates all HTTP endpoints except `/health` behind `Authorization: Bearer <key>`. Design choices:

- **`/health` is exempt** — load balancers and uptime monitors need unauthenticated health checks.
- **OPTIONS is exempt** — CORS preflight requests don't carry auth headers.
- **`hmac.compare_digest`** for token comparison — prevents timing attacks.
- **Case-insensitive scheme** — `Bearer`, `bearer`, `BEARER` all accepted per RFC 7235.
- **Empty key = no auth** — `QUARRY_API_KEY=""` is treated as unset, preventing accidental trivial bypass.
- **No auth when omitted** — local use (menu bar, CLI) requires no key. Auth is opt-in for production deployment.

### Container Deployment (Fly.io)

Multi-stage Dockerfile: (1) install Python deps with `uv sync`, (2) download the embedding model at build time via `hf_hub_download`, (3) slim runtime image with baked-in model. LanceDB data lives on a Fly persistent volume at `/data` (`QUARRY_ROOT=/data`). The `--host 0.0.0.0` flag binds to all interfaces for container networking. Auto-stop scales to zero when idle; cold start is ~5s with the baked-in model (no download on first request). TLS is terminated by Fly's proxy.

### Threaded Request Handling

`QuarryHTTPServer` extends `ThreadingHTTPServer` — each request gets its own daemon thread. This prevents a slow embedding (the dominant latency) from blocking other clients. Thread safety relies on immutable shared state: `_QuarryContext` fields are set once at startup, LanceDB handles concurrent reads internally, and ONNX Runtime sessions are thread-safe for inference. Alternative considered: uvicorn/starlette (async) — rejected as unnecessary complexity for 5 synchronous endpoints with no I/O multiplexing benefit.

### Configurable CORS Origins

`--cors-origin` (repeatable) controls `Access-Control-Allow-Origin`. The server reflects the request's `Origin` header only when it matches the allow list, and adds `Vary: Origin` to signal cacheability varies by origin. When no `Origin` header is present (e.g. non-browser clients), no CORS headers are emitted. Defaults to `http://localhost` for backward compatibility with quarry-menubar.

### Log Redaction (CWE-532)

Access logs redact query strings from request lines. The `_redact_query_string` method parses HTTP request lines (`GET /path?q=secret HTTP/1.1`) to strip the query while preserving the method, path, and HTTP version. The `_handle_search` handler logs only result count, never the raw query. Error handlers use `urlparse().path` to strip queries.

### Single Table Design

All chunks live in one LanceDB table (`chunks`). Document and collection boundaries are columns, not separate tables. This simplifies cross-document search and avoids table proliferation. Filtering by document/collection/page_type/source_format uses LanceDB's built-in filter predicates on the vector search.

## ADR: Convention Hints via Hook Accumulator

**Status:** Proposed
**Date:** 2026-03-09

### Context

Claude Code agents drift from project conventions during sessions — running `uv run mypy src/` instead of the full quality gate chain, staging with `git add -A` instead of specific files, using `pip` instead of `uv`. The conventions are documented in CLAUDE.md, but long documents lose influence as context grows.

The v1.1.0 hook wiring gap (quarry-g6x, quarry-dx9, quarry-5lr) demonstrated that quarry's hook integration layer is the right place for passive, fail-open interventions. The question is how to surface relevant conventions without blocking valid workflow.

### Decision

Add a **PreToolUse hook** that accumulates recent tool calls in a state file and emits soft hints (`permissionDecision: "allow"` with `additionalContext`) when command sequences violate project conventions.

### Design

#### Two-Class Rule System

**Instant rules** fire on the current command alone. No accumulated state needed. These catch unambiguous anti-patterns:

| Pattern | Hint |
|---------|------|
| `git add -A` / `git add .` | Stage specific files by name |
| `pip install` | Use `uv` for package management |
| `git push --force` | Force-push is destructive — confirm intent |
| `git commit --no-verify` | Do not skip hooks unless explicitly asked |

**Sequence rules** require temporal context from the accumulator. These detect patterns that are only wrong *in sequence*:

| Sequence | Hint |
|----------|------|
| `git commit` without a preceding full gate chain | Run quality gates before committing |
| 2+ solo gate tools (`uv run ruff`, `uv run mypy`, ...) in a row without `&&` | Use the full gate chain instead of individual tools |

#### Accumulator Design

Adapted from biff's `DisplayQueue` pattern (`biff/src/biff/server/display_queue.py`): a timestamped rolling window of recent tool calls, persisted to a JSON file so state survives across hook invocations within a session.

```text
State file: $TMPDIR/hint-state-{session_id}.json

[
  {"ts": 1741556400.0, "tool": "Bash", "command": "uv run mypy src/"},
  {"ts": 1741556410.0, "tool": "Bash", "command": "uv run ruff check ."},
  {"ts": 1741556420.0, "tool": "Bash", "command": "git commit -m \"fix: ...\""}
]
```

Properties (following biff's accumulator discipline):

- **Time-based expiry**: Events older than 5 minutes are pruned on each hook invocation. Prevents stale context from triggering false hints.
- **Session scoping**: State file is keyed by `session_id` from the hook payload. Concurrent sessions don't cross-contaminate.
- **Pure matcher**: The sequence matcher is a pure function `(events) → hint | None`. No I/O, deterministic, testable with synthetic event lists.
- **Fail-open**: Any error in the hook (state file read failure, malformed JSON, matcher crash) produces no output. The command runs unimpeded.

#### Hook Payload Interface

**Input** (PreToolUse stdin):
```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {"command": "git commit -m \"fix: ...\""},
  "session_id": "abc123",
  "cwd": "/Users/.../quarry"
}
```

**Output** (stdout, only when a hint fires):
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "additionalContext": "Reminder: run the full quality gate before committing: uv run ruff check . && ..."
  }
}
```

The `permissionDecision: "allow"` is critical — hints are advisory, never blocking. Claude sees the `additionalContext` as a system message and can decide whether it applies. This eliminates the false-positive problem: a targeted `uv run pytest tests/test_hooks.py -v` during development is fine; the hint only fires when `git commit` follows without a full gate run.

#### Matching Strategy

Regex-based for latency. The hook runs on every Bash tool call; it must complete in <50ms. No embedding search, no LLM inference, no network calls. Pattern matching against the accumulated command strings.

The quality gate rule illustrates the sequence matching logic:

```
on git commit:
  scan events[-10:] for a command containing the full gate chain
  if not found:
    emit hint with the project's gate string
```

The full gate string is project-specific. For quarry: `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ tests/ && uv run pyright && uv run pytest`. This could be read from a config file or hardcoded per-project in the hook.

#### Where This Lives

In quarry's plugin hook layer (`hooks/` + `src/quarry/hooks.py`), registered as a PreToolUse entry in `hooks.json`. The accumulator state file lives in `$TMPDIR` (session-scoped, gitignored).

Quarry is the right home because:
1. The hook infrastructure is already built and tested (v1.1.0 wiring).
2. The accumulator pattern is borrowed from biff but scoped to quarry's conventions use case.
3. Future evolution: swap regex matching for semantic search against a `conventions` collection when latency allows.

### Alternatives Considered

**Prompt-based PreToolUse hook** — Claude Code's newer API lets the hook provide instructions and Claude itself decides whether to block. More nuanced but adds LLM latency (~1-2s) to every Bash call. Rejected for the common case; may be reconsidered for complex rules that resist regex.

**UserPromptSubmit injection** — Inject convention reminders when the user submits a prompt. Lower friction but fires too early (before Claude has decided what commands to run) and can't react to specific commands.

**Hard blocks (`permissionDecision: "deny"`)** — Block the command outright. Higher enforcement but brittle with false positives. A blocked targeted test during development would be disruptive. Soft hints preserve flow while still nudging.

**Quarry semantic search on every tool call** — Search a `conventions` collection with the command as query. Higher quality matching but ~200ms latency per call (embedding + vector search). May be the evolution path when the regex approach hits its limits.

### Consequences

- **Convention adherence improves** without manual vigilance. The agent gets the right context at the right time.
- **False positives are non-blocking** — worst case, Claude sees an irrelevant reminder and ignores it.
- **State file adds complexity** — the accumulator must handle concurrent writes, corruption, and stale data. Biff's pattern (write-on-every-access, prune-on-read) is proven.
- **Project-specific rules** — the gate string and patterns are hardcoded to quarry's conventions initially. Generalizing to per-project config is a future step.
- **Latency budget** — regex matching on a small JSON file is sub-millisecond. The Python startup for `json.loads` dominates at ~30ms. Acceptable for PreToolUse.

## Test Architecture

794 tests total. 772 run by default; 22 integration tests require live AWS credentials and are deselected by default (`-m "not integration"`).

Test files mirror source modules: `test_pipeline.py`, `test_database.py`, `test_mcp_server.py`, etc. Tests mock at the boundary (database, backends, filesystem) using `unittest.mock.patch`. The MCP server tests verify both synchronous tool behavior and background thread execution using `threading.Event` for synchronization.
