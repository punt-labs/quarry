# Ingestion Pipeline: OO Design Report

## Scope

13 modules, 2,087 combined lines, 5 classes, 69 top-level functions.
This section covers format detection, text extraction, page processing,
and the orchestration pipeline that dispatches, chunks, embeds, and stores.

## Executive Summary

`pipeline.py` is a 1,589-line God Module with 24 top-level functions and
zero classes. It violates PY-OO-1 (domain entities must be classes),
PY-OO-2 (module size), and PY-OO-5 (state + behavior = class). The
format-specific `ingest_*` functions share identical structure: resolve
name, delete if overwriting, extract pages, call `_chunk_embed_store`.
The shared state across all of them is `(db, settings)` plus a common
set of memory kwargs. This is textbook "functions that share a parameter"
-- the trigger for Extract Class.

The six processor modules (text, code, html, presentation, spreadsheet,
pdf_analyzer) are well-sized (54-209 lines each) but purely procedural.
Each exports a `process_*_file` function that takes a path and returns
`list[PageContent]`. They should implement a common `FormatExtractor`
protocol so the pipeline can dispatch polymorphically instead of via
if/elif chains.

## Protocol: FormatExtractor

The unifying abstraction across all format-specific extraction.

```python
class FormatExtractor(Protocol):
    """Extract pages from a document in a specific format."""

    @property
    def supported_extensions(self) -> frozenset[str]: ...

    def extract_pages(
        self,
        source: Path,
        *,
        document_name: str,
    ) -> list[PageContent]: ...
```

Every processor module produces a class implementing this protocol.
The pipeline holds a registry of extractors keyed by extension and
dispatches via lookup instead of branching.

---

## Per-Module Analysis

### Module: pipeline.py (1,589 lines)

```
Current: 0 classes, 24 top-level functions
Domain nouns: pipeline orchestrator, ingest job, ingest result,
  image preparer, URL fetcher, sitemap crawler, bulk ingester
Shared state: (db, settings) passed to every ingest_* function;
  memory kwargs (agent_handle, memory_type, summary) threaded everywhere;
  progress callback created identically in every function
```

This module contains five distinct responsibilities:

1. **Format dispatch** -- `ingest_document`, `_extract_pages`
2. **Chunk-embed-store** -- `_chunk_embed_store`, `prepare_document`
3. **Image preparation** -- `_prepare_image_bytes`, `_encode_image_to_fit`, `_ingest_multipage_image`
4. **URL fetching** -- `_fetch_url`, `ingest_url`, `_ingest_url_with_delay`
5. **Sitemap crawling** -- `ingest_sitemap`, `ingest_auto`, `_bulk_ingest_entries`

Each of the `ingest_*` functions (ingest_pdf, ingest_text_file, ingest_code_file,
ingest_spreadsheet, ingest_html_file, ingest_presentation, ingest_image) follows
an identical pattern and should be eliminated once extractors implement the
`FormatExtractor` protocol.

**Proposed classes:**

```
IngestionPipeline
  Module: src/quarry/pipeline.py
  Responsibility: Orchestrate format dispatch, chunking, embedding, and storage
  Owns: _db (LanceDB), _settings (Settings), _extractors (dict[str, FormatExtractor])
  Public interface:
    ingest_document(file_path, *, overwrite, collection, document_name,
                    progress_callback, agent_handle, memory_type, summary) -> IngestResult
    ingest_content(content, document_name, *, overwrite, collection,
                   format_hint, progress_callback, agent_handle, memory_type, summary) -> IngestResult
    prepare_document(file_path, *, collection, document_name,
                     agent_handle, memory_type, summary) -> tuple[list[Chunk], NDArray] | None
    supported_extensions: frozenset[str]  (property)
  Absorbs:
    ingest_document -> method, replaces if/elif chain with extractor registry lookup
    _chunk_embed_store -> private method _chunk_embed_store
    _make_progress -> private method _make_progress
    prepare_document -> method
    _extract_pages -> eliminated; replaced by extractor.extract_pages()
    ingest_pdf -> eliminated; PdfExtractor.extract_pages() + _chunk_embed_store
    ingest_text_file -> eliminated; TextExtractor.extract_pages() + _chunk_embed_store
    ingest_code_file -> eliminated; CodeExtractor.extract_pages() + _chunk_embed_store
    ingest_spreadsheet -> eliminated; SpreadsheetExtractor.extract_pages() + _chunk_embed_store
    ingest_html_file -> eliminated; HtmlExtractor.extract_pages() + _chunk_embed_store
    ingest_presentation -> eliminated; PresentationExtractor.extract_pages() + _chunk_embed_store
    ingest_image -> eliminated; ImageExtractor.extract_pages() + _chunk_embed_store
  Dependencies: FormatExtractor protocol, chunker.chunk_pages, database.insert_chunks,
                backends.get_embedding_backend, models.Chunk, results.IngestResult
  Estimated LOC: 200

ImagePreparer
  Module: src/quarry/image_preparer.py (new)
  Responsibility: Read, convert, and downscale image bytes for OCR consumption
  Owns: (stateless -- all inputs via method params)
  Public interface:
    prepare_bytes(image_path, *, needs_conversion, max_bytes) -> bytes
  Absorbs:
    _prepare_image_bytes -> prepare_bytes
    _encode_image_to_fit -> private method _encode_to_fit
  Dependencies: PIL.Image, PIL.ImageOps
  Estimated LOC: 100

UrlFetcher
  Module: src/quarry/url_fetcher.py (new)
  Responsibility: Fetch HTML from HTTP(S) URLs with validation and timeout
  Owns: (stateless)
  Public interface:
    fetch(url, *, timeout) -> str
  Absorbs:
    _fetch_url -> fetch
  Dependencies: urllib.request, urllib.error
  Estimated LOC: 50

UrlIngester
  Module: src/quarry/url_ingester.py (new)
  Responsibility: Ingest single URLs and sitemap-discovered URLs
  Owns: _pipeline (IngestionPipeline), _fetcher (UrlFetcher)
  Public interface:
    ingest_url(url, *, overwrite, collection, document_name, timeout,
               progress_callback, agent_handle, memory_type, summary) -> IngestResult
    ingest_sitemap(url, *, collection, include, exclude, limit, overwrite,
                   workers, delay, timeout, progress_callback,
                   agent_handle, memory_type, summary) -> SitemapResult
    ingest_auto(url, *, overwrite, collection, workers, delay, timeout,
                progress_callback, agent_handle, memory_type, summary) -> IngestResult | SitemapResult
  Absorbs:
    ingest_url -> method
    _ingest_url_with_delay -> private method _ingest_with_delay
    _bulk_ingest_entries -> private method _bulk_ingest
    ingest_sitemap -> method
    ingest_auto -> method
  Dependencies: IngestionPipeline, UrlFetcher, html_processor.process_html_text,
                sitemap.discover_pages, sitemap.discover_urls, sitemap.filter_entries,
                results.SitemapResult, concurrent.futures
  Estimated LOC: 250
```

**What remains in pipeline.py after extraction:**
`IngestionPipeline` class only. SUPPORTED_EXTENSIONS becomes a
computed property from the extractor registry. Module drops from
1,589 to ~200 lines.

**Eliminated functions (17):**
- `ingest_pdf`, `ingest_text_file`, `ingest_code_file`, `ingest_spreadsheet`,
  `ingest_html_file`, `ingest_presentation`, `ingest_image` -- replaced by
  generic dispatch through `FormatExtractor` protocol
- `_extract_pages`, `_extract_pdf_pages`, `_extract_image_pages` -- replaced
  by `FormatExtractor.extract_pages()`
- `_ingest_multipage_image` -- absorbed into `ImageExtractor`
- `_prepare_image_bytes`, `_encode_image_to_fit` -- moved to `ImagePreparer`
- `_fetch_url` -- moved to `UrlFetcher`
- `ingest_url`, `_ingest_url_with_delay`, `_bulk_ingest_entries`,
  `ingest_sitemap`, `ingest_auto` -- moved to `UrlIngester`

---

### Module: text_processor.py (209 lines)

```
Current: 0 classes, 10 top-level functions
Domain nouns: text document, text format, section splitter
Shared state: format string threaded through _split_by_format -> split_* functions;
  document_name and document_path threaded through all public functions
```

Two responsibilities: (1) read text files with encoding fallback,
(2) split text into sections by detected format. The format dispatch
(`_split_by_format`) and the three splitter functions (`split_markdown`,
`_split_latex`, `split_plain`) are a classic Strategy pattern -- the
format determines which splitting algorithm to use.

**Proposed classes:**

```
TextExtractor
  Module: src/quarry/extractors/text_extractor.py (new -- not to be confused
          with the existing text_extractor.py which handles PDF text pages)
  Responsibility: Extract PageContent sections from text-format files (.txt, .md, .tex, .docx)
  Owns: (stateless)
  Public interface:
    supported_extensions: frozenset[str]  (property, returns SUPPORTED_TEXT_EXTENSIONS)
    extract_pages(source, *, document_name) -> list[PageContent]
    extract_raw(text, document_name, *, format_hint) -> list[PageContent]
  Absorbs:
    process_text_file -> extract_pages
    process_raw_text -> extract_raw
    _process_docx -> private method _extract_docx
    _split_by_format -> private method _split_by_format
    _detect_format -> private method _detect_format
    read_text_with_fallback -> stays as module-level utility (used by code_processor,
      html_processor, spreadsheet_processor); OR moves to a TextReader utility class
  Dependencies: pathlib, re, docx (lazy), models.PageContent, models.PageType
  Estimated LOC: 130

TextSplitter (keep as functions -- no state, no shared data)
  Module: src/quarry/text_splitter.py (new)
  Responsibility: Split text strings into section lists by format
  Owns: compiled regex patterns (MD_HEADER, LATEX_SECTION, BLANK_LINE_SPLIT)
  Public interface:
    split_markdown(text) -> list[str]
    split_latex(text) -> list[str]
    split_plain(text) -> list[str]
    sections_to_pages(sections, document_name, document_path, page_type) -> list[PageContent]
  Absorbs:
    split_markdown (currently public) -> stays
    _split_latex -> split_latex (becomes public)
    split_plain (currently public) -> stays
    sections_to_pages (currently public) -> stays
    MD_HEADER, LATEX_SECTION, BLANK_LINE_SPLIT constants -> move here
  Dependencies: re, models.PageContent, models.PageType
  Estimated LOC: 70
```

**Rationale for keeping splitters as functions:** These are pure
transforms with no state. Making them methods on a class adds no
value -- `split_markdown(text)` is clearer than `MarkdownSplitter().split(text)`.
The OO improvement is grouping them into a cohesive module with
the constants they use, and extracting the format-aware orchestration
into `TextExtractor`.

`read_text_with_fallback` is used by 4 modules (text_processor,
code_processor, html_processor, spreadsheet_processor). It stays
as a module-level utility in `text_splitter.py` or a standalone
`file_reader.py`. No class needed -- it is a pure function with no
shared state.

---

### Module: code_processor.py (202 lines)

```
Current: 0 classes, 3 top-level functions
Domain nouns: code file, language grammar, tree-sitter parser, code section
Shared state: language string derived from extension, threaded through functions
```

**Proposed classes:**

```
CodeExtractor
  Module: src/quarry/extractors/code_extractor.py (new)
  Responsibility: Extract PageContent sections from source code files via tree-sitter
  Owns: (stateless; language lookup from _CODE_LANGUAGES)
  Public interface:
    supported_extensions: frozenset[str]  (property, returns SUPPORTED_CODE_EXTENSIONS)
    extract_pages(source, *, document_name) -> list[PageContent]
  Absorbs:
    process_code_file -> extract_pages
    _split_with_treesitter -> private method _split_treesitter
    _fallback_split -> private method _split_fallback
    _CODE_LANGUAGES dict -> class-level constant
    _DEFINITION_NODE_TYPES frozenset -> class-level constant
    SUPPORTED_CODE_EXTENSIONS -> derived from _CODE_LANGUAGES
  Dependencies: tree_sitter_language_pack, re, text_splitter.sections_to_pages,
                text_splitter.read_text_with_fallback, models.PageContent, models.PageType
  Estimated LOC: 180
```

---

### Module: html_processor.py (137 lines)

```
Current: 0 classes, 6 top-level functions
Domain nouns: HTML document, boilerplate, markdown conversion
Shared state: BeautifulSoup object passed between _strip_boilerplate, _extract_title
```

**Proposed classes:**

```
HtmlExtractor
  Module: src/quarry/extractors/html_extractor.py (new)
  Responsibility: Extract PageContent sections from HTML files and raw HTML strings
  Owns: (stateless)
  Public interface:
    supported_extensions: frozenset[str]  (property, returns SUPPORTED_HTML_EXTENSIONS)
    extract_pages(source, *, document_name) -> list[PageContent]
    extract_from_html(html, document_name, document_path) -> list[PageContent]
  Absorbs:
    process_html_file -> extract_pages
    process_html_text -> extract_from_html
    _strip_boilerplate -> private method _strip_boilerplate
    _extract_title -> private method _extract_title
    _html_to_markdown -> private method _to_markdown
    _has_markdown_headings -> private method _has_headings
    SUPPORTED_HTML_EXTENSIONS -> class-level constant
    _BOILERPLATE_TAGS -> class-level constant
  Dependencies: bs4.BeautifulSoup, markdownify, text_splitter.split_markdown,
                text_splitter.split_plain, text_splitter.sections_to_pages,
                text_splitter.read_text_with_fallback, models.PageContent, models.PageType
  Estimated LOC: 120
```

---

### Module: presentation_processor.py (169 lines)

```
Current: 0 classes, 6 top-level functions
Domain nouns: presentation, slide, shape, table, speaker notes
Shared state: pptx Slide object passed between _extract_shapes, _extract_notes,
  _extract_slide_text
```

**Proposed classes:**

```
PresentationExtractor
  Module: src/quarry/extractors/presentation_extractor.py (new)
  Responsibility: Extract PageContent pages from PPTX presentations
  Owns: (stateless)
  Public interface:
    supported_extensions: frozenset[str]  (property, returns SUPPORTED_PRESENTATION_EXTENSIONS)
    extract_pages(source, *, document_name) -> list[PageContent]
  Absorbs:
    process_presentation_file -> extract_pages
    _extract_slide_text -> private method _extract_slide_text
    _extract_shapes -> private method _extract_shapes
    _extract_notes -> private method _extract_notes
    _format_slide_content -> private method _format_content
    _table_to_latex -> private method _table_to_latex
    SUPPORTED_PRESENTATION_EXTENSIONS -> class-level constant
  Dependencies: pptx (lazy), latex_utils.escape_latex, latex_utils.rows_to_latex,
                models.PageContent, models.PageType
  Estimated LOC: 150
```

---

### Module: spreadsheet_processor.py (154 lines)

```
Current: 0 classes, 4 top-level functions
Domain nouns: spreadsheet, worksheet/sheet, row group, CSV file
Shared state: (headers, rows) tuples passed between _read_xlsx/_read_csv
  and _split_rows_to_sections
```

**Proposed classes:**

```
SpreadsheetExtractor
  Module: src/quarry/extractors/spreadsheet_extractor.py (new)
  Responsibility: Extract PageContent sections from XLSX and CSV files
  Owns: (stateless)
  Public interface:
    supported_extensions: frozenset[str]  (property, returns SUPPORTED_SPREADSHEET_EXTENSIONS)
    extract_pages(source, *, document_name, max_chars) -> list[PageContent]
  Absorbs:
    process_spreadsheet_file -> extract_pages (note: return type changes from
      tuple[list[PageContent], int] to list[PageContent]; sheet_count moves to
      logging or metadata; the pipeline currently only uses the pages list)
    _read_xlsx -> private method _read_xlsx
    _read_csv -> private method _read_csv
    _split_rows_to_sections -> private method _split_rows
    SUPPORTED_SPREADSHEET_EXTENSIONS -> class-level constant
  Dependencies: csv, io, openpyxl (lazy), text_splitter.read_text_with_fallback,
                text_splitter.sections_to_pages, latex_utils.rows_to_latex,
                models.PageContent, models.PageType
  Estimated LOC: 140
```

**Note on extract_pages signature:** The `FormatExtractor` protocol
defines `extract_pages(source, *, document_name) -> list[PageContent]`.
SpreadsheetExtractor needs an additional `max_chars` parameter for
row-group splitting. Two options: (1) accept it via constructor
(becomes `_max_chars` instance attribute), making the class
settings-aware; (2) use a default that matches `Settings.chunk_max_chars`.
Option 1 is cleaner -- the pipeline passes `settings.chunk_max_chars`
at extractor construction time. The `FormatExtractor` protocol signature
stays clean; SpreadsheetExtractor's constructor takes the extra config.

---

### Module: pdf_analyzer.py (54 lines)

```
Current: 0 classes, 1 top-level function
Domain nouns: PDF page analysis, text/image classification
Shared state: none (pure function)
```

This module is small and cohesive. The single function `analyze_pdf`
is a pure transform. It does not need to become a class.

**Proposed: absorb into PdfExtractor.**

```
PdfExtractor
  Module: src/quarry/extractors/pdf_extractor.py (new)
  Responsibility: Extract PageContent pages from PDF files (text + OCR)
  Owns: _settings (Settings) -- needed for OCR backend access
  Public interface:
    supported_extensions: frozenset[str]  (property, returns frozenset({".pdf"}))
    extract_pages(source, *, document_name) -> list[PageContent]
  Absorbs:
    analyze_pdf (from pdf_analyzer.py) -> private method _classify_pages
    _extract_pdf_pages (from pipeline.py) -> inlined into extract_pages
    extract_text_pages (from text_extractor.py) -> called, not absorbed
  Dependencies: fitz (PyMuPDF), backends.get_ocr_backend, text_extractor.extract_text_pages,
                models.PageAnalysis, models.PageContent, models.PageType
  Estimated LOC: 80
```

`pdf_analyzer.py` is eliminated as a standalone module. Its 54 lines
become 20 lines inside `PdfExtractor._classify_pages`.

---

### Module: image_analyzer.py (85 lines)

```
Current: 1 class (ImageAnalysis dataclass), 1 top-level function
Domain nouns: image analysis, format detection, conversion requirement
Shared state: none (pure function + value object)
```

`ImageAnalysis` is a well-designed frozen dataclass. `analyze_image`
is a pure function. Absorb into ImageExtractor.

**Proposed: absorb into ImageExtractor.**

```
ImageExtractor
  Module: src/quarry/extractors/image_extractor.py (new)
  Responsibility: Extract PageContent from image files (single and multi-page)
  Owns: _settings (Settings) -- needed for OCR backend;
        _preparer (ImagePreparer) -- for format conversion
  Public interface:
    supported_extensions: frozenset[str]  (property, returns SUPPORTED_IMAGE_EXTENSIONS)
    extract_pages(source, *, document_name) -> list[PageContent]
  Absorbs:
    analyze_image (from image_analyzer.py) -> private method _analyze
    ImageAnalysis (from image_analyzer.py) -> stays as public dataclass, re-exported
    ingest_image (from pipeline.py) -> extract_pages (pages only; chunking in pipeline)
    _ingest_multipage_image (from pipeline.py) -> private method _extract_multipage
    _extract_image_pages (from pipeline.py) -> inlined into extract_pages
  Dependencies: PIL.Image, backends.get_ocr_backend, ImagePreparer,
                models.PageContent, models.PageType
  Estimated LOC: 120

ImageAnalysis (existing dataclass, stays)
  Module: src/quarry/extractors/image_extractor.py
  Responsibility: Value object describing image format and OCR requirements
  Owns: format (str), page_count (int), needs_conversion (bool)
  Public interface: read-only frozen dataclass fields
  Absorbs: nothing (already correct)
  Dependencies: none
  Estimated LOC: 10
```

`image_analyzer.py` is eliminated as a standalone module.

---

### Module: text_extractor.py (60 lines)

```
Current: 0 classes, 1 top-level function
Domain nouns: PDF text page extraction
Shared state: none (pure function)
```

This module extracts text from text-classified PDF pages via PyMuPDF.
It is consumed only by the PDF extraction path. It stays as-is -- a
focused utility module called by `PdfExtractor`. No class needed for
a single pure function.

**Proposed: no change.** Rename to `pdf_text_extractor.py` to avoid
confusion with the new `extractors/text_extractor.py` module.

```
Module: src/quarry/pdf_text_extractor.py (renamed from text_extractor.py)
Responsibility: Extract text from text-classified PDF pages via PyMuPDF
Public interface: extract_text_pages(pdf_path, page_numbers, total_pages, *, document_name)
No class needed: single pure function, 60 lines, no shared state.
```

---

### Module: latex_utils.py (57 lines)

```
Current: 0 classes, 2 top-level functions
Domain nouns: LaTeX escaping, LaTeX table rendering
Shared state: none (pure functions + compiled translation table)
```

Two pure utility functions. No class needed. This module is well-sized,
cohesive, and correctly structured.

**Proposed: no change.**

---

### Module: ocr_local.py (188 lines)

```
Current: 3 classes (LocalOcrBackend, _OcrEngine Protocol, _OcrResult Protocol),
         4 top-level functions
Domain nouns: OCR engine, OCR result, OCR backend, PDF page renderer
Shared state: module-level _engine cache with lock
```

`LocalOcrBackend` already exists and implements the `OcrBackend` protocol.
The module is well-structured. Two issues:

1. `LocalOcrBackend.__init__` should be `__new__` per PY-CC-1, but this
   is a ratchet improvement, not a design change.
2. The module-level `_engine` cache with `get_engine()` is a Singleton
   implemented as module state. It works but should be internalized
   into `LocalOcrBackend`.

**Proposed classes:**

```
LocalOcrBackend (existing, refine)
  Module: src/quarry/ocr_local.py (no move)
  Responsibility: OCR via RapidOCR with lazy engine initialization
  Owns: _settings (Settings), class-level _engine cache
  Public interface:
    ocr_document(document_path, page_numbers, total_pages, *, document_name) -> list[PageContent]
    ocr_image_bytes(image_bytes, document_name, document_path) -> PageContent
  Absorbs:
    get_engine -> class method _get_engine (internalize the singleton)
    _extract_text -> private method _extract_text
    _render_pdf_page -> private static method _render_pdf_page
    _ocr_pages -> private method _ocr_pages
  Dependencies: fitz, PIL.Image, rapidocr (lazy), models.PageContent, models.PageType
  Estimated LOC: 170

_OcrEngine (existing Protocol, keep)
_OcrResult (existing Protocol, keep)
```

---

### Module: sitemap.py (125 lines)

```
Current: 1 class (SitemapEntry dataclass), 4 top-level functions
Domain nouns: sitemap, sitemap entry, URL discovery, URL filtering
Shared state: none
```

`SitemapEntry` is a well-designed frozen dataclass. The functions are
cohesive -- they all operate on sitemap data. This module is 125 lines,
well under the 300-line threshold.

Two of the functions (`discover_pages`, `discover_urls`) share the
pattern of calling USP and converting results via `_pages_to_entries`.
The third (`filter_entries`) is a pure filter. These could become
methods on a `SitemapDiscoverer` class, but the module is already
small and cohesive -- the class would add ceremony without benefit.

**Proposed: no structural change.** Move consumption from pipeline.py
into `UrlIngester`. The module itself stays as-is.

```
SitemapEntry (existing dataclass, keep)
  Module: src/quarry/sitemap.py (no move)

Functions stay as module-level:
  discover_pages(url) -> list[SitemapEntry]
  discover_urls(url) -> list[SitemapEntry]
  filter_entries(entries, *, include, exclude, limit) -> list[SitemapEntry]
  _pages_to_entries(pages) -> list[SitemapEntry]
```

---

### Module: backends.py (48 lines)

```
Current: 0 classes, 3 top-level functions
Domain nouns: backend factory, backend cache
Shared state: module-level _ocr_cache, _embedding_cache, _lock
```

This is a backend factory with thread-safe caching. The module-level
cache dicts and lock are shared mutable state -- a class would
encapsulate this properly. This is a Singleton factory (PY-DP-7 trigger).

**Proposed classes:**

```
BackendRegistry
  Module: src/quarry/backends.py (no move)
  Responsibility: Thread-safe factory and cache for OCR and embedding backends
  Owns: _ocr_cache (dict), _embedding_cache (dict), _lock (threading.Lock)
  Public interface:
    get_ocr_backend(settings) -> OcrBackend
    get_embedding_backend(settings) -> EmbeddingBackend
    clear_caches() -> None  (test isolation only)
  Absorbs:
    get_ocr_backend -> method
    get_embedding_backend -> method
    clear_caches -> method
    _ocr_cache, _embedding_cache, _lock -> private attributes
  Dependencies: threading, quarry.ocr_local (lazy), quarry.embeddings (lazy),
                quarry.types.OcrBackend, quarry.types.EmbeddingBackend
  Estimated LOC: 50
```

Module-level convenience functions (`get_ocr_backend`, `get_embedding_backend`)
can remain as thin wrappers around a module-level singleton instance for
backwards compatibility during migration.

---

## Extractors Package

The six format-specific extractors form a natural package:

```
src/quarry/extractors/
    __init__.py          # __all__, re-exports FormatExtractor protocol + all extractors
    protocol.py          # FormatExtractor protocol definition
    text_extractor.py    # TextExtractor
    code_extractor.py    # CodeExtractor
    html_extractor.py    # HtmlExtractor
    presentation_extractor.py  # PresentationExtractor
    spreadsheet_extractor.py   # SpreadsheetExtractor
    pdf_extractor.py     # PdfExtractor
    image_extractor.py   # ImageExtractor + ImageAnalysis
```

`extractors/__init__.py` exports:

```python
__all__ = [
    "FormatExtractor",
    "TextExtractor",
    "CodeExtractor",
    "HtmlExtractor",
    "PresentationExtractor",
    "SpreadsheetExtractor",
    "PdfExtractor",
    "ImageExtractor",
    "ImageAnalysis",
]
```

---

## New Module: text_splitter.py

Utilities extracted from `text_processor.py` that are consumed by
multiple extractor classes and the text extractor itself.

```
src/quarry/text_splitter.py

Contents:
  MD_HEADER (compiled regex)
  LATEX_SECTION (compiled regex)
  BLANK_LINE_SPLIT (compiled regex)
  read_text_with_fallback(file_path: Path) -> str
  split_markdown(text: str) -> list[str]
  split_latex(text: str) -> list[str]
  split_plain(text: str) -> list[str]
  sections_to_pages(sections, document_name, document_path, page_type) -> list[PageContent]

Estimated LOC: 80
```

These are pure functions with no shared state. No class needed.
`read_text_with_fallback` moves here because it is the most-imported
utility from the current `text_processor.py` and has no format-specific
logic.

---

## New Module: image_preparer.py

```
src/quarry/image_preparer.py

Contents:
  ImagePreparer
    prepare_bytes(image_path, *, needs_conversion, max_bytes) -> bytes
    _encode_to_fit(img, out_fmt, save_kw, max_bytes, name) -> bytes

Estimated LOC: 100
```

---

## New Module: url_fetcher.py

```
src/quarry/url_fetcher.py

Contents:
  UrlFetcher
    fetch(url, *, timeout) -> str

Estimated LOC: 50
```

---

## New Module: url_ingester.py

```
src/quarry/url_ingester.py

Contents:
  UrlIngester
    __new__(cls, pipeline, fetcher) -> Self
    ingest_url(...) -> IngestResult
    ingest_sitemap(...) -> SitemapResult
    ingest_auto(...) -> IngestResult | SitemapResult
    _ingest_with_delay(...) -> IngestResult
    _bulk_ingest(...) -> SitemapResult

Estimated LOC: 250
```

---

## Renamed Module

```
src/quarry/text_extractor.py -> src/quarry/pdf_text_extractor.py
```

Avoids name collision with `extractors/text_extractor.py`. The module
is consumed only by `PdfExtractor` and `pipeline.py` (the latter only
via `_extract_pdf_pages` which is absorbed into `PdfExtractor`).

---

## Modules Eliminated

| Current module | Absorbed into |
|---------------|---------------|
| `pdf_analyzer.py` (54 lines) | `extractors/pdf_extractor.py` as `PdfExtractor._classify_pages` |
| `image_analyzer.py` (85 lines) | `extractors/image_extractor.py` as `ImageExtractor._analyze` + `ImageAnalysis` |
| `text_processor.py` (209 lines) | Split: pure splitters to `text_splitter.py`, format-aware extraction to `extractors/text_extractor.py` |
| `code_processor.py` (202 lines) | `extractors/code_extractor.py` as `CodeExtractor` |
| `html_processor.py` (137 lines) | `extractors/html_extractor.py` as `HtmlExtractor` |
| `presentation_processor.py` (169 lines) | `extractors/presentation_extractor.py` as `PresentationExtractor` |
| `spreadsheet_processor.py` (154 lines) | `extractors/spreadsheet_extractor.py` as `SpreadsheetExtractor` |

---

## Modules Unchanged

| Module | Lines | Reason |
|--------|-------|--------|
| `latex_utils.py` | 57 | Pure utility functions, no shared state, well-sized |
| `sitemap.py` | 125 | Cohesive module, SitemapEntry already a dataclass, under 300 lines |

---

## Modules Refined (In-Place)

| Module | Lines | Change |
|--------|-------|--------|
| `ocr_local.py` | 188 | Absorb `get_engine`, `_extract_text`, `_render_pdf_page`, `_ocr_pages` into `LocalOcrBackend`; eliminate module-level engine cache |
| `backends.py` | 48 | Wrap cache state in `BackendRegistry` class |

---

## Migration Summary

### Before: 13 modules, 2,087 lines, 5 classes, 69 functions

### After: 15 modules, ~1,800 lines, 12 classes, ~15 module-level functions

**New classes (10):**

| Class | Module | Estimated LOC |
|-------|--------|--------------|
| `FormatExtractor` (Protocol) | `extractors/protocol.py` | 15 |
| `TextExtractor` | `extractors/text_extractor.py` | 130 |
| `CodeExtractor` | `extractors/code_extractor.py` | 180 |
| `HtmlExtractor` | `extractors/html_extractor.py` | 120 |
| `PresentationExtractor` | `extractors/presentation_extractor.py` | 150 |
| `SpreadsheetExtractor` | `extractors/spreadsheet_extractor.py` | 140 |
| `PdfExtractor` | `extractors/pdf_extractor.py` | 80 |
| `ImageExtractor` | `extractors/image_extractor.py` | 120 |
| `IngestionPipeline` | `pipeline.py` | 200 |
| `UrlIngester` | `url_ingester.py` | 250 |
| `ImagePreparer` | `image_preparer.py` | 100 |
| `UrlFetcher` | `url_fetcher.py` | 50 |
| `BackendRegistry` | `backends.py` | 50 |

**Existing classes (retained):**

| Class | Module | Change |
|-------|--------|--------|
| `ImageAnalysis` | `extractors/image_extractor.py` | Moved from `image_analyzer.py` |
| `LocalOcrBackend` | `ocr_local.py` | Absorbs 4 module-level functions |
| `SitemapEntry` | `sitemap.py` | No change |
| `_OcrEngine` | `ocr_local.py` | No change |
| `_OcrResult` | `ocr_local.py` | No change |

**Remaining module-level functions (~15):**

| Function | Module | Reason |
|----------|--------|--------|
| `split_markdown` | `text_splitter.py` | Pure function, no state |
| `split_latex` | `text_splitter.py` | Pure function, no state |
| `split_plain` | `text_splitter.py` | Pure function, no state |
| `sections_to_pages` | `text_splitter.py` | Pure function, no state |
| `read_text_with_fallback` | `text_splitter.py` | Pure function, used by 4+ modules |
| `escape_latex` | `latex_utils.py` | Pure function, no state |
| `rows_to_latex` | `latex_utils.py` | Pure function, no state |
| `discover_pages` | `sitemap.py` | Thin wrapper around USP library |
| `discover_urls` | `sitemap.py` | Thin wrapper around USP library |
| `filter_entries` | `sitemap.py` | Pure filter function |
| `extract_text_pages` | `pdf_text_extractor.py` | Pure function, single consumer |

---

## Pattern Triggers Identified (PY-OO-6)

| Trigger | Location | Pattern |
|---------|----------|---------|
| Single entry point to a subsystem | `IngestionPipeline` | Facade (PY-DP-10) |
| One class owns another's creation data | `BackendRegistry` creates OCR/Embedding backends | Factory (PY-DP-2) |
| Exactly one global instance | `BackendRegistry` cache | Singleton (PY-DP-7) |
| Object caching for immutable values | OCR engine in `ocr_local.py` | Flyweight-like (PY-DP-1) |

---

## Dependency Graph (After Refactoring)

```
IngestionPipeline
  -> FormatExtractor protocol
  -> TextExtractor, CodeExtractor, HtmlExtractor, PresentationExtractor,
     SpreadsheetExtractor, PdfExtractor, ImageExtractor
  -> chunker.chunk_pages
  -> database.insert_chunks
  -> BackendRegistry (get_embedding_backend)

PdfExtractor
  -> pdf_text_extractor.extract_text_pages
  -> BackendRegistry (get_ocr_backend)

ImageExtractor
  -> ImagePreparer
  -> BackendRegistry (get_ocr_backend)

UrlIngester
  -> IngestionPipeline
  -> UrlFetcher
  -> HtmlExtractor.extract_from_html
  -> sitemap.discover_pages, discover_urls, filter_entries

TextExtractor -> text_splitter.*
CodeExtractor -> text_splitter.sections_to_pages, read_text_with_fallback
HtmlExtractor -> text_splitter.split_markdown, split_plain, sections_to_pages, read_text_with_fallback
SpreadsheetExtractor -> text_splitter.sections_to_pages, read_text_with_fallback; latex_utils.*
PresentationExtractor -> latex_utils.*
```

No circular dependencies. Dependency direction is always inward:
extractors depend on utilities, pipeline depends on extractors,
URL ingestion depends on pipeline.

---

## Risk and Sequencing

**Highest-risk change:** Eliminating the 7 `ingest_*` functions in
`pipeline.py`. Every CLI command, MCP tool, and HTTP endpoint that calls
`ingest_document` is a consumer. The public API (`ingest_document` signature)
must not change -- `IngestionPipeline.ingest_document` must accept the
same kwargs. The function-based `ingest_document` at module level can
remain as a thin wrapper that constructs a default `IngestionPipeline`
and delegates, preserving backwards compatibility during migration.

**Recommended sequence:**

1. Create `text_splitter.py` -- extract pure utilities, update imports.
   Zero behavior change. Every consumer tested.
2. Create `extractors/protocol.py` -- define `FormatExtractor`.
3. Create extractors one at a time (text, code, html, presentation,
   spreadsheet, pdf, image). Each is a standalone step with its own
   test. Each eliminates one `process_*` module.
4. Create `ImagePreparer`, `UrlFetcher` -- extract from pipeline.py.
5. Create `IngestionPipeline` class in pipeline.py with extractor registry.
   Keep module-level `ingest_document` as thin wrapper.
6. Create `UrlIngester` -- extract URL/sitemap functions from pipeline.py.
7. Refine `backends.py` (BackendRegistry) and `ocr_local.py` (absorb functions).
8. Rename `text_extractor.py` to `pdf_text_extractor.py`.
9. Delete eliminated modules, update all imports.

Each step is one refactoring loop iteration per PY-RF-1: measure, apply,
test, check, measure, compare, commit.
