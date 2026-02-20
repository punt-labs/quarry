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
