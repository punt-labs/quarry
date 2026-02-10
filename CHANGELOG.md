# Changelog

All notable changes to quarry-mcp will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Categories: `format` (file type support), `provider` (OCR/embedding backends),
`tool` (MCP/CLI surface), `pipeline` (ingestion flow), `infra` (schema, build, config).

## [Unreleased]

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
