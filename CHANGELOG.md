# Changelog

All notable changes to quarry-mcp will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-02-09

### Added
- Directory registration and incremental sync engine
- SQLite-backed registry (WAL mode) tracking directories, collections, and file records
- Delta detection via mtime+size comparison: new, changed, unchanged, deleted
- Parallel file ingestion during sync via ThreadPoolExecutor (default 4 workers)
- CLI commands: `register`, `deregister`, `registrations`, `sync`
- MCP tools: `register_directory`, `deregister_directory`, `sync_all_registrations`, `list_registrations`
- `delete-collection` CLI command and `delete_collection` MCP tool
- `list_collections` MCP tool
- 21 end-to-end integration tests covering all ingestion formats, search, collections, and overwrite
- `REGISTRY_PATH` configuration variable

### Changed
- Exponential backoff for Textract polling (start 5s, 1.5x multiplier, cap 30s) replaces fixed interval
- `status` MCP tool now reports registered directory count
- MCP tool count: 9 to 13

## [0.1.3] - 2026-02-08

### Added
- PEP 561 `py.typed` marker for type-checked package consumers
- MCP server tests for `search_documents`, `get_documents`, `get_page` tools
- CLI tests for `list`, `delete`, `search` commands and error handling

### Fixed
- Embedding model cache now keys by model name (was single global; ignored `model_name` param after first load)
- Hardcoded `embedding_dimension: 768` extracted to `Settings.embedding_dimension` (single source of truth)
- `SCHEMA` module-level constant replaced with `_schema()` function accepting dimension parameter
- `type: ignore[assignment]` on boto3/lancedb calls replaced with explicit `cast()` for clarity

### Changed
- `.pytest_cache/` added to `.gitignore`

## [0.1.2] - 2026-02-08

### Added
- `quarry doctor` command: checks Python, data directory, AWS credentials, embedding model cache, core imports
- `quarry install` command: creates `~/.quarry/data/lancedb/`, pre-downloads embedding model, prints MCP config snippet
- PyPI classifiers and `[project.urls]` metadata
- `docs/TOOL-PyPI.md` publishing checklist (build, test, upload workflow)
- Standalone image ingestion: PNG, JPEG, TIFF (multi-page), BMP, WebP via `ingest` tool and CLI
- Sync Textract API (`DetectDocumentText`) for single-page images (no S3 upload needed)
- BMP/WebP auto-conversion to PNG via Pillow before OCR
- Multi-page TIFF support via async Textract API (same path as PDFs)
- `image_analyzer` module with format detection and TIFF page counting
- Text document ingestion: `.txt`, `.md`, `.tex`, `.docx` files via `ingest` tool and CLI
- Raw text ingestion via `ingest_text` MCP tool (auto-detects markdown/LaTeX/plain)
- Section-aware splitting: markdown headings, LaTeX `\section`/`\subsection`, blank-line paragraphs, DOCX Heading styles
- `delete_document` MCP tool and `quarry delete` CLI command to remove indexed documents
- `status` MCP tool reporting document/chunk counts, database size, and embedding model info
- `count_chunks` database function for O(1) chunk counting
- Text processor tests (`test_text_processor.py`)
- MCP server tests (`test_mcp_server.py`)
- `NON-FUNCTIONAL-DESIGN.md` defining logging and exception handling standards
- CHANGELOG.md

### Fixed
- Resource leak: `fitz.open()` in `pdf_analyzer.py` and `text_extractor.py` now uses context manager
- MCP tool handlers and CLI commands now catch exceptions at the boundary, log tracebacks, and return user-friendly errors
- `pipeline.py` progress calls use `%s`-style lazy formatting instead of f-strings
- Added `Raises:` docstring sections to all public functions that raise or propagate exceptions
- Added `DEBUG` logging to `pdf_analyzer`, `text_extractor`, `text_processor`, and `database` modules

### Changed
- Build backend from `hatchling` to `uv_build` (simpler, uv-native)
- Version via `importlib.metadata.version()` instead of `__version__.py`
- Default `lancedb_path` from repo-relative (`data/lancedb`) to `~/.quarry/data/lancedb` (works after pip install)
- `ingest` MCP tool and CLI now accept all supported formats (was PDF-only)
- Pipeline refactored: `ingest_document` dispatches by format, shared `_chunk_embed_store` eliminates duplication

## [0.1.0] - 2026-02-08

### Added
- PDF ingestion with automatic text/image page classification
- OCR via AWS Textract (async API with polling)
- Text extraction via PyMuPDF for text-based pages
- Sentence-aware chunking with configurable overlap
- Local vector embeddings using snowflake-arctic-embed-m-v1.5 (768-dim)
- LanceDB vector storage with PyArrow schema
- MCP server with `search_documents`, `ingest`, `get_documents`, `get_page` tools
- CLI with `ingest`, `search`, `list` commands and Rich progress display
- Full page text preserved alongside chunks for LLM context
- 62 tests across 9 modules
