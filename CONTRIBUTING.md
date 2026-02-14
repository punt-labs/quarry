# Contributing to Quarry

## Development Setup

```bash
git clone https://github.com/jmf-pobox/quarry-mcp.git
cd quarry-mcp
uv sync --frozen --extra dev
```

## Quality Gates

Every commit must pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ tests/
uv run pytest
```

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

The pipeline dispatches by file extension in `pipeline.py:ingest_document()`. To add a format:

1. **Create a processor module** (e.g. `src/quarry/xlsx_processor.py`) that converts the format into `list[PageContent]`. Each `PageContent` has `page_number`, `text`, and `page_type` (TEXT, CODE, IMAGE, or SECTION).

2. **Register the extension** in the processor's `SUPPORTED_*_EXTENSIONS` frozenset, or create a new one and add it to `SUPPORTED_EXTENSIONS` in `pipeline.py`.

3. **Add a dispatch branch** in `ingest_document()` following the existing pattern:
   ```python
   if suffix in SUPPORTED_NEW_EXTENSIONS:
       return ingest_new_format(file_path, db, settings, ...)
   ```

4. **Write tests** in `tests/test_pipeline.py` (unit) and `tests/test_integration.py` (end-to-end with real embeddings). Integration tests are marked `@pytest.mark.slow`.

5. **Update README** format table and supported formats list.

## Architecture

Quarry's pipeline has four stages:

```
Input -> Pages -> Chunks -> Vectors -> LanceDB
```

- **Input**: Format-specific processors convert files to `list[PageContent]`
- **Pages**: Uniform text with metadata (page number, type, source format)
- **Chunks**: Sentence-aware splitting via `chunker.py` (respects `CHUNK_MAX_CHARS`)
- **Vectors**: 768-dim embeddings from snowflake-arctic-embed-m-v1.5 (ONNX Runtime)
- **LanceDB**: Vector storage with metadata columns for filtering

### Design Documents

- [Backend Abstraction](docs/BACKEND-ABSTRACTION.md) -- pluggable OCR and embedding backends
- [Non-Functional Design](docs/NON-FUNCTIONAL-DESIGN.md) -- logging and exception handling patterns

### Key Abstractions

- `OcrBackend` / `EmbeddingBackend` protocols in `types.py` -- implement these for new backends
- `PageContent` dataclass in `models.py` -- the universal intermediate representation
- `IngestResult` TypedDict in `results.py` -- standard return from all ingest functions

## Pull Request Process

1. Push branch, open PR
2. Request Copilot review
3. Address feedback
4. Ensure CI passes
5. Merge when all feedback is resolved and quality gates pass
