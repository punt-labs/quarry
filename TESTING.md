# Testing Strategy

Quarry has **794 tests** across 35 test files. Tests are organized into three tiers that mirror the architecture: processors parse formats, the pipeline orchestrates ingestion, and surfaces (CLI, MCP, HTTP) expose functionality. Each tier tests its own concerns and mocks the tier below.

## Architecture and test boundaries

```
┌─────────────────────────────────────────────────────┐
│  Surfaces: CLI, MCP server, HTTP API                │  ← wiring tests (mocked backends)
├─────────────────────────────────────────────────────┤
│  Pipeline: ingest_document, ingest_content, sync    │  ← orchestration tests (mocked I/O)
├─────────────────────────────────────────────────────┤
│  Core: database, embeddings, chunker, processors    │  ← logic tests (real data structures)
├─────────────────────────────────────────────────────┤
│  Integration: ingest → embed → store → search       │  ← end-to-end (real LanceDB + ONNX)
└─────────────────────────────────────────────────────┘
```

### Tier 1: Core logic (real data, no mocks)

Tests exercise actual algorithms with real inputs. No network, no mocks.

| File | What it tests |
|------|---------------|
| `test_database.py` | LanceDB insert, search, delete, collection filtering, concurrent writes |
| `test_chunker.py` | Text splitting by token count, boundary detection |
| `test_embeddings.py` | ONNX embedding backend: embed, batch, query, model download |
| `test_code_processor.py` | Tree-sitter parsing for Python, JS, Go, Rust, etc. |
| `test_html_processor.py` | HTML→markdown, boilerplate stripping, heading extraction |
| `test_text_processor.py` | Plain text, markdown, LaTeX, DOCX section splitting |
| `test_presentation_processor.py` | PPTX slide extraction, table→LaTeX conversion |
| `test_spreadsheet_processor.py` | CSV/XLSX row grouping and section splitting |
| `test_image_analyzer.py` | Image format detection, dimension analysis |
| `test_pdf_analyzer.py` | PDF page analysis (text vs. scanned detection) |
| `test_models.py` | Data model construction and validation |
| `test_collections.py` | Collection name derivation and validation |
| `test_config.py` | Settings resolution, DB path logic, persistent defaults |
| `test_formatting.py` | Output formatting for all display contexts |
| `test_latex_utils.py` | LaTeX escaping, table generation |
| `test_registry.py` | SQLite sync registry: register, deregister, file tracking |
| `test_sync.py` | File discovery, ignore patterns, sync plan computation |

### Tier 2: Orchestration (mocked I/O)

Tests verify that the pipeline wires processors, embeddings, and database calls correctly. External I/O (OCR, network, filesystem) is mocked; internal logic runs real.

| File | What it tests |
|------|---------------|
| `test_pipeline.py` | `ingest_document` / `ingest_content` orchestration |
| `test_pipeline_images.py` | Image ingestion: single/multi-page, progress callbacks |
| `test_ocr_local.py` | Local OCR backend: PDF rendering, text extraction |
| `test_url_ingestion.py` | URL fetching, HTML processing, redirect handling |
| `test_sitemap.py` | Sitemap discovery, entry filtering, deduplication |
| `test_backends.py` | Backend factory dispatch (OCR, embedding) |
| `test_hooks.py` | Claude Code hook handlers: session-start, web-fetch, compact |
| `test_doctor.py` | Environment checks, install flow, configuration |

### Tier 3: Surface wiring (fully mocked)

Tests verify that CLI flags, MCP tool parameters, and HTTP endpoints correctly reach backend functions. All backends are mocked — these tests run in < 2 seconds.

| File | What it tests |
|------|---------------|
| `test_cli.py` | Typer CLI: flag passthrough, error paths, JSON output, global options |
| `test_mcp_server.py` | MCP tool functions: parameter forwarding, error handling |
| `test_http_server.py` | HTTP API: search, documents, status, CORS, port file |

**What surface tests verify:**
- Flags/parameters reach the correct backend function with correct values
- Error conditions (backend exceptions, invalid input) produce exit code 1
- `--json` output is valid JSON with expected structure
- `_cli_errors` decorator catches exceptions but propagates `SystemExit`/`KeyboardInterrupt`

**What surface tests do NOT verify:**
- Whether search returns relevant results (that's tier 1 + integration)
- Whether ingestion produces correct chunks (that's tier 2)
- Whether the embedding model works (that's tier 1)

### Integration tests (end-to-end, real everything)

`test_integration.py` uses a real ONNX embedding model and real LanceDB to verify the full pipeline: ingest a document, embed it, store it, search it, verify relevance. These tests are marked `@pytest.mark.slow` and excluded from the default test run.

| Test class | What it proves |
|------------|----------------|
| `TestTextFileIngestAndSearch` | TXT ingestion → semantic search returns relevant results |
| `TestMarkdownIngestAndSearch` | Markdown heading splits → section-level search works |
| `TestLatexIngestion` | LaTeX `\section{}` splitting produces correct chunks |
| `TestPdfIngestion` | PDF text extraction → page retrieval → search |
| `TestDocxIngestion` | DOCX heading-style splitting → search |
| `TestImageOcr` | PNG → OCR → search (requires AWS credentials) |
| `TestCollectionIsolation` | Cross-collection search filtering |
| `TestOverwriteBehavior` | Overwrite replaces content; no-overwrite duplicates |
| `TestMultiDocumentSearch` | Ranking correctness across 3 documents |
| `TestRawTextIngestion` | `ingest_content` (stdin path) → search |
| `TestCollectionDerivation` | Auto-derive collection from directory name |

## Running tests

```bash
# Fast tests only (default, ~1.5s)
uv run pytest

# Specific file
uv run pytest tests/test_cli.py -v

# Integration tests (requires ONNX model download, ~30s)
uv run pytest -m slow

# All tests
uv run pytest -m ""

# Coverage (use coverage.py directly — pytest-cov conflicts with numpy)
uv run coverage run -m pytest
uv run coverage report --show-missing
```

## Fixtures

Test fixtures live in `tests/fixtures/` and `tests/conftest.py`:

- **Static fixtures** (`fixtures/`): `photosynthesis.txt`, `french-revolution.txt`, `quantum-computing.txt`, `guide.md`, `calculus.tex` — real content for integration tests
- **Generated fixtures** (`conftest.py`): `pdf_fixture` (2-page PDF via PyMuPDF), `docx_fixture` (2-section DOCX), `png_fixture` (image with text for OCR)
- **Session-scoped model warm-up**: `_warm_embedding_model` loads the ONNX model once per session, shared across all integration tests
- **Environment isolation**: `_isolate_from_env` (autouse) strips quarry env vars so `.envrc` exports don't leak into test Settings

## Quality gates

Run before every commit:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src/ tests/ && uv run pyright && uv run pytest
```
