# Backend Abstraction Design

## Vision

Quarry enables users to access all knowledge in their personal files via Claude Code and Claude Desktop. It supports all document formats. Processing backends are pluggable: users with local GPUs can do everything locally; others use cloud services; anyone can mix and match.

## Design Principles

1. **Protocol-based backends** (PEP 544). Structural subtyping — no inheritance required. Third-party backends work without importing quarry base classes.
2. **Open for extension, closed for modification** (Liskov substitution). Adding a backend means adding a file and a factory branch. Zero changes to pipeline, MCP server, or CLI.
3. **Build what exists.** Abstract only the backends that have implementations today. Transcription and audio extraction protocols ship when those backends ship.
4. **Flat module structure.** One file per backend implementation. No nested package hierarchies.
5. **Cached instances.** Models are expensive to load. Factory functions cache by key.
6. **No pipeline signature changes.** Pipeline functions already receive `Settings`. Factory calls happen inside pipeline, not at call sites.

---

## Protocols

Added to `src/quarry/types.py` alongside existing Protocol definitions (`LanceDB`, `TextractClient`, `S3Client`):

```python
class OcrBackend(Protocol):
    """Protocol for OCR backends that extract text from images/scanned documents."""

    def ocr_document(
        self,
        document_path: Path,
        page_numbers: list[int],
        total_pages: int,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """OCR multiple pages from a document (PDF or TIFF)."""
        ...

    def ocr_image_bytes(
        self,
        image_bytes: bytes,
        document_name: str,
        document_path: str,
    ) -> PageContent:
        """OCR a single-page image from bytes."""
        ...


class EmbeddingBackend(Protocol):
    """Protocol for text embedding backends."""

    @property
    def dimension(self) -> int: ...

    @property
    def model_name(self) -> str: ...

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Embed a batch of texts. Returns shape (n, dimension)."""
        ...

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Embed a search query. Returns shape (dimension,)."""
        ...
```

Two protocols. Not four. Transcription and audio extraction protocols are added when those features ship.

---

## Implementations

Existing modules gain classes that implement the protocols. No new directories.

### `src/quarry/ocr_client.py` — add `TextractOcrBackend`

Wraps existing `ocr_document_via_s3()` and `ocr_image_bytes()` functions as methods on a class. The class holds a `Settings` reference for AWS/S3/polling config. Existing free functions remain as private implementation details during migration, then get removed.

### `src/quarry/embeddings.py` — add `SnowflakeEmbeddingBackend`

Wraps existing `embed_texts()` and `embed_query()` functions. Owns the model cache (replaces module-level `_models` dict). Exposes `dimension` and `model_name` properties.

### Future: `src/quarry/ocr_tesseract.py`

When local OCR ships, one new file implements `OcrBackend` using pytesseract. One factory branch added. One optional dependency group in pyproject.toml.

---

## Factory

New file: `src/quarry/backends.py` (~40 lines)

```python
from __future__ import annotations

from quarry.config import Settings
from quarry.types import EmbeddingBackend, OcrBackend

_ocr_cache: dict[str, OcrBackend] = {}
_embedding_cache: dict[str, EmbeddingBackend] = {}


def get_ocr_backend(settings: Settings) -> OcrBackend:
    key = settings.ocr_backend
    if key not in _ocr_cache:
        match key:
            case "textract":
                from quarry.ocr_client import TextractOcrBackend
                _ocr_cache[key] = TextractOcrBackend(settings)
            case _:
                msg = f"Unknown OCR backend: '{key}'. Available: textract"
                raise ValueError(msg)
    return _ocr_cache[key]


def get_embedding_backend(settings: Settings) -> EmbeddingBackend:
    key = settings.embedding_model
    if key not in _embedding_cache:
        from quarry.embeddings import SnowflakeEmbeddingBackend
        _embedding_cache[key] = SnowflakeEmbeddingBackend(settings)
    return _embedding_cache[key]
```

Adding a new OCR backend requires:

1. `src/quarry/ocr_tesseract.py` — implements `OcrBackend`
2. One `case "tesseract":` branch in `get_ocr_backend()`
3. Optional dependency group in `pyproject.toml`

---

## Settings

One new field in `src/quarry/config.py`:

```python
ocr_backend: str = "textract"
```

Embedding backend is already identified by `embedding_model`. No separate selector needed.

Backend-specific settings (e.g., `textract_poll_initial`) remain on `Settings`. Each backend reads what it needs and ignores the rest. When a backend needs unique settings, add them with the backend's prefix (e.g., `tesseract_lang: str = "eng"`).

---

## Pipeline Integration

No public function signatures change. Internally, pipeline calls the factory:

```python
# In _chunk_embed_store():
backend = get_embedding_backend(settings)
vectors = backend.embed_texts(texts)

# In ingest_pdf():
ocr = get_ocr_backend(settings)
ocr_results = ocr.ocr_document(
    file_path, image_pages, total_pages, document_name=doc_name
)
```

`mcp_server.py` and `__main__.py` change only where they call `embed_query` directly (search commands). Those 2 call sites switch to `get_embedding_backend(settings).embed_query(query)`.

---

## Module Layout (delta from current)

```text
src/quarry/
  types.py        MODIFY  add OcrBackend + EmbeddingBackend protocols
  backends.py     NEW     factory functions (~40 lines)
  ocr_client.py   MODIFY  add TextractOcrBackend class
  embeddings.py   MODIFY  add SnowflakeEmbeddingBackend class
  config.py       MODIFY  add ocr_backend field
  pipeline.py     MODIFY  use factory internally
  mcp_server.py   MODIFY  use factory for embed_query (2 sites)
  __main__.py     MODIFY  use factory for embed_query (1 site)
```

1 new file. 7 modified files. 0 new directories.

---

## Optional Dependencies (future)

Added when backends ship, not before:

```toml
[project.optional-dependencies]
ocr-tesseract = ["pytesseract>=0.3.10"]
ocr-easyocr = ["easyocr>=1.7.0"]
transcription = ["faster-whisper>=1.0.0"]
local = ["punt-quarry[ocr-tesseract,transcription]"]
```

---

## Implementation Phases

### Phase 1: Protocols + implementations + factory

- Add `OcrBackend` and `EmbeddingBackend` to `types.py`
- Add `TextractOcrBackend` class to `ocr_client.py`
- Add `SnowflakeEmbeddingBackend` class to `embeddings.py`
- Create `backends.py` with factory functions
- Add `ocr_backend` to `Settings`
- Tests for new classes and factory

### Phase 2: Pipeline wiring

- Replace direct function calls in `pipeline.py` with factory calls
- Replace `embed_query` calls in `mcp_server.py` and `__main__.py`
- Update test mocking (patch factory instead of module functions)

### Phase 3 (future): Audio transcription

- Add `TranscriptionBackend` protocol to `types.py`
- Add `WhisperTranscriptionBackend` in `src/quarry/transcription_whisper.py`
- Add `ingest_audio()` to `pipeline.py`
- Add `transcribe` MCP tool and CLI command
- FFmpeg audio extraction is a utility function in the transcription module, not a separate protocol

### Phase 4 (future): Local OCR

- Add `TesseractOcrBackend` in `src/quarry/ocr_tesseract.py`
- Add `case "tesseract":` to factory
- Add `ocr-tesseract` optional dependency group
- Update `quarry doctor` to show available backends

---

## What This Design Explicitly Rejects

| Rejected | Reason |
|----------|--------|
| Generic `BackendRegistry[T]` | match/case factory is simpler for 2-3 backends |
| 4 protocols upfront | Build protocols when implementations exist |
| `backends/` package hierarchy | Flat layout for an 18-file project |
| `AudioExtractor` protocol | FFmpeg is a utility function |
| `PageType.AUDIO` now | Add when audio transcription ships |
| Pipeline signature changes | Factory calls happen inside pipeline |
| 13 new files | 1 new file is sufficient |
| `whisper_model` / `whisper_device` settings | Add with transcription feature |

---

## Extension Pattern

When someone wants to add a new backend:

```text
1. Create src/quarry/ocr_<name>.py
   - Define a class with ocr_document() and ocr_image_bytes() methods
   - It structurally satisfies OcrBackend protocol (no import needed)

2. Add case "<name>": branch in backends.py:get_ocr_backend()

3. Add optional dependency group in pyproject.toml (if new deps needed)

4. Add tests in tests/test_ocr_<name>.py
```

No changes to pipeline.py, mcp_server.py, **main**.py, or any existing backend.
