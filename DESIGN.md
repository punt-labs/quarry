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

### Log Redaction (CWE-532)

Access logs redact query strings from request lines. The `_redact_query_string` method parses HTTP request lines (`GET /path?q=secret HTTP/1.1`) to strip the query while preserving the method, path, and HTTP version. The `_handle_search` handler logs only result count, never the raw query. Error handlers use `urlparse().path` to strip queries.

### Single Table Design

All chunks live in one LanceDB table (`chunks`). Document and collection boundaries are columns, not separate tables. This simplifies cross-document search and avoids table proliferation. Filtering by document/collection/page_type/source_format uses LanceDB's built-in filter predicates on the vector search.

## Test Architecture

794 tests total. 772 run by default; 22 integration tests require live AWS credentials and are deselected by default (`-m "not integration"`).

Test files mirror source modules: `test_pipeline.py`, `test_database.py`, `test_mcp_server.py`, etc. Tests mock at the boundary (database, backends, filesystem) using `unittest.mock.patch`. The MCP server tests verify both synchronous tool behavior and background thread execution using `threading.Event` for synchronization.
