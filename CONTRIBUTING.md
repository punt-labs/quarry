# Contributing to Quarry

## Development Setup

```bash
git clone https://github.com/punt-labs/quarry.git
cd quarry
uv sync --extra dev
```

## Quality Gates

Every commit must pass:

```bash
make check
```

This runs lint, type checking, tests, and the OO/coupling/suppression ratchets
in one command (`make lint` + `make type` + `make test` + `make check-oo` +
`make check-coupling` + `make check-suppressions` + `make check-imports` +
`make check-openapi`).

| Command | Purpose |
|---------|---------|
| `uv sync --extra dev` | Install dependencies |
| `make check` | All quality gates |
| `make test` | Test suite only |
| `make format` | Auto-format |
| `make docs` | Build the LaTeX documents |
| `make eval` | Retrieval-quality eval harness (MRR/success@k) |

## Branch Discipline

All changes go on feature branches off `main`:

| Prefix | Use |
|--------|-----|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `refactor/` | Code improvements |
| `docs/` | Documentation only |

Commit messages: `type(scope): description` (e.g. `feat(format): add XLSX ingestion`).

## Adding a New Format

The pipeline dispatches by file extension in
`src/quarry/ingestion/format_strategies.py:resolve_strategy()`. To add a format:

1. **Create an extractor module** in `src/quarry/extractors/` (e.g.
   `xlsx_extractor.py`) that satisfies the `FormatExtractor` protocol
   (`extractors/protocol.py`): one `extract_pages(path, *, document_name=None)`
   method returning `list[PageContent]`. Each `PageContent` has `page_number`,
   `text`, and `page_type` (TEXT, CODE, IMAGE, or SECTION).

2. **Register the extension** in the extractor's `SUPPORTED_*_EXTENSIONS`
   frozenset. `SUPPORTED_EXTENSIONS` — the union the sync planner and the
   daemon's watch loop consult — is built in
   `ingestion/format_strategies.py` and re-exported from
   `ingestion/pipeline.py` and `quarry.ingestion`; it is never edited by hand.

3. **Add a dispatch entry.** A text-like format (one extractor, one stats
   field) is a `TextLikeFormat` row in `ingestion/text_format.py`'s
   `TEXT_LIKE_FORMATS` table — no new strategy class. A format that needs its
   own extraction flow (PDF and images do, because they resolve an OCR
   backend) is a new `FormatStrategy` (`ingestion/extracted_document.py`)
   returned from `resolve_strategy()`.

4. **Write tests** in `tests/test_format_strategies.py` and
   `tests/test_pipeline.py` (unit) and `tests/test_integration.py`
   (end-to-end with real embeddings). Integration tests are marked
   `@pytest.mark.slow`.

5. **Update README** format table and supported formats list.

## Architecture

Quarry's pipeline has four stages:

```text
Input -> Pages -> Chunks -> Vectors -> LanceDB
```

- **Input**: Format-specific extractors (`src/quarry/extractors/`) convert files to `list[PageContent]`
- **Pages**: Uniform text with metadata (page number, type, source format)
- **Chunks**: Sentence-aware splitting via `ingestion/chunker.py` (respects `CHUNK_MAX_CHARS`)
- **Vectors**: 768-dim embeddings from snowflake-arctic-embed-m-v1.5 (ONNX Runtime)
- **LanceDB**: Vector storage with metadata columns for filtering

### Design Documents

- [Architecture](docs/architecture.tex) -- system architecture, logging and exception handling standards
- [DESIGN.md](DESIGN.md) -- architectural decision records (DES-001 through DES-055)
- [Z Specification](docs/claude-code-quarry.tex) -- formal specification of the Claude Code plugin state machine

### Key Abstractions

- `OcrBackend` / `EmbeddingBackend` protocols in `types.py` -- implement these for new backends
- `FormatExtractor` protocol in `extractors/protocol.py` and `FormatStrategy` in `ingestion/extracted_document.py` -- the two seams a new format plugs into
- `PageContent` dataclass in `models.py` -- the universal intermediate representation
- `IngestResult` TypedDict in `results.py` -- standard return from all ingest functions
- `MemoryType` enum in `memory_types.py` -- the closed `memory_type` vocabulary every write route validates against (DES-055)

## Pull Request Process

1. Push branch, open PR
2. Request Copilot review
3. Address feedback
4. Ensure CI passes
5. Merge when all feedback is resolved and quality gates pass
