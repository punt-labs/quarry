# Quarry Code Design Evaluation Report

**Branch reviewed:** main  
**Review date:** 2026-02-12 (revised after ONNX migration)  
**Scope:** Naming, modules, classes, functions, types, comments — evaluated against DDD, OO principles, PEP standards, Python 3.13.

---

## Executive Summary

The quarry codebase is **solid and functional** (B- level) with strong typing discipline, protocol-based backends, and good test coverage. Recent changes (ONNX embeddings, dependency slimming) have improved it. It still falls short of an A-grade design due to: duplicated logic, inconsistent abstractions, naming that misleads in several places, docstrings that are redundant or outdated, and a pipeline-centric structure that lacks a clear domain boundary. The design docs (BACKEND-ABSTRACTION.md, NON-FUNCTIONAL-DESIGN.md) set high standards; the implementation has fewer gaps than before.

**Goal:** Reach a defensible A-level design without over-engineering. Focus on removing friction, clarifying boundaries, and fixing structural inconsistencies.

---

## Changes Since Original Review (ONNX Migration)

Main has been updated since the initial review. Notable changes:

- **Embedding backend rewritten:** sentence-transformers (~2.5 GB) replaced with ONNX Runtime. `SnowflakeEmbeddingBackend` → `OnnxEmbeddingBackend`. Uses INT8 quantized model, tokenizers + onnxruntime directly. Dependency footprint reduced.
- **EmbeddingModel removed:** The legacy protocol and free functions (`embed_texts`, `embed_query`) are gone. D1 (below) is **resolved**.
- **config.py:** `EMBEDDING_MODEL_REVISION` → `ONNX_*` constants. `s3_bucket` default changed to `""`.
- **doctor.py / run_install:** Updated for ONNX model download and cache checking.

---

## Strengths

### 1. Protocol-Based Backend Abstraction

- **OcrBackend** and **EmbeddingBackend** protocols (types.py) enable pluggable implementations via structural subtyping (PEP 544). No inheritance required.
- Factory pattern in `backends.py` with thread-safe caching and lazy imports. Adding a new backend = new file + one factory branch.
- Matches OPEN/CLOSED: pipeline, MCP, CLI unchanged when backends are added.

### 2. Strong Type Discipline

- `from __future__ import annotations` in all modules.
- Full type annotations on function signatures. Mypy strict mode.
- Protocol classes for third-party libraries (LanceDB, TextractClient, S3Client) — no `Any`.
- Modern Python: `X | Y` unions, `Annotated`, `type` statements.

### 3. Immutable Data Models

- `PageContent`, `Chunk`, `PageAnalysis`, `ImageAnalysis`, `SyncPlan`, `SyncResult`, `DirectoryRegistration`, `FileRecord` use `@dataclass(frozen=True)`.
- Aligns with CLAUDE.md: "Immutable data models."

### 4. Quality and Testing

- Ruff with comprehensive rules (E, F, B, C4, UP, N, SIM, etc.). Double quotes, 88-char lines.
- Every module has accompanying tests. Quality gates before commit.

### 5. Documentation Standards

- NON-FUNCTIONAL-DESIGN.md: logging levels, exception handling, `%s` formatting, Raises docstrings.
- BACKEND-ABSTRACTION.md: clear rationale for protocols, factory, extension pattern.
- Public functions document Args, Returns, Raises.

### 6. Exception Handling at Boundaries

- `_cli_errors` and `_handle_errors` decorators catch at CLI/MCP boundaries, log with `logger.exception()`, return user-facing messages.
- Exceptions propagate in library code; boundaries absorb and translate.

### 7. Focused Module Layout

- Flat structure: one file per concern (chunker, text_processor, pdf_analyzer, embeddings, etc.).
- Single responsibility per module. No deep nesting.

### 8. Dependency Discipline (Post-ONNX)

- sentence-transformers removed in favor of ONNX Runtime — eliminates ~2.5 GB torch dependency.
- Embedding pipeline is now self-contained: tokenizers + onnxruntime + numpy. Cleaner, faster startup.
- `_download_model_files` / `_load_model_files` separation: network only at install time, offline at runtime.

---

## Naming and Comments

### Naming Analysis

**Module names with mismatched intent:**

| Module | Issue |
|--------|-------|
| `ocr_client.py` | Implies thin API wrapper; actually the Textract backend implementation. `ocr_textract.py` would pair with `ocr_local.py`. |
| `collections.py` | Generic; only has `derive_collection` and `validate_collection_name`. Feels like utilities. |
| `registry.py` | Vague — registry of what? It's the sync registry (SQLite for directories + file state). `sync_registry.py` or `directory_registry.py` would clarify. |
| `sync.py` | Sync *what*? It's directory/registry sync. Name doesn't convey scope. |
| `doctor.py` | Cute but opaque for newcomers. `environment_check.py` or `diagnostics.py` is more discoverable. |
| `backends.py` | Contains factory functions, not backends. `backend_factory.py` would be more accurate. |

**Function names that mislead or confuse:**

| Name | Issue |
|------|-------|
| `ingest_text` vs `ingest_text_file` | One takes raw string, the other a path. Easy to confuse. `ingest_text_content` for the string variant would distinguish. |
| `get_db` | Generic. `connect_lancedb` or `open_database` would describe behavior. |
| `get_settings` | "Get" suggests cached/shared; it constructs a new `Settings()` each call. `load_settings` or `create_settings` would be more honest. |
| `_chunk_embed_store` | Accurate but awkward; reads as three verbs. `_run_chunk_pipeline` or similar would flow better. |

**Variables and constants:**

- `doc_name` vs `document_name` — same concept, inconsistent shorthand across the codebase.
- `TEXT_THRESHOLD = 50` — magic number; name doesn't say 50 is character count. `MIN_TEXT_CHARS_FOR_TEXT_PAGE` would be self-documenting.
- `filter` in `LanceTable.count_rows` — shadows builtin; `predicate` or `where_clause` is clearer (see D9).
- `PageContent` — overloaded: physical pages (PDF) vs logical sections (text/code). Name suggests pages only (see D4).

**Good names worth preserving:** `Chunk`, `SyncPlan`, `SyncResult`, `TextractOcrBackend`, `LocalOcrBackend`, `OnnxEmbeddingBackend`, `DirectoryRegistration`, `FileRecord` — clear and consistent.

---

### Comments and Docstrings Analysis

**Redundant or low-value docstrings:**

- Protocol methods and thin wrappers that repeat the function name: "OCR a single-page image from bytes" for `ocr_image_bytes` adds little. Prefer docstrings that explain *why* or *constraints*, not restate the name.
- Some `Args`/`Returns` blocks merely restate the type. They don't help if the caller has the signature. Focus on invariants, units, or preconditions.

**Where comments would help:**

| Location | Gap |
|----------|-----|
| `_split_text` overlap logic (chunker) | Non-trivial; a brief "why" for overlap handling and tail selection would help future maintainers. |
| `_encode_image_to_fit` | Multi-step (re-encode, downscale). Strategy and intent not obvious from code alone. |
| `_get_or_create_table` double-checked locking | Subtle correctness; note which race condition it prevents. |
| `compute_sync_plan` | Why compare both `mtime` and `size`? Document intent. |

**Outdated comments:**

- `ingest_image` docstring: "Single-page images use Textract sync API" — wrong when `ocr_backend=local`. Backend-agnostic phrasing needed.
- `image_analyzer` comments reference Textract assumptions; module is now backend-agnostic.

**Missing context:**

- **Module docstrings** — Most modules lack a one-line summary of their role in the pipeline. New contributors can't quickly orient.
- **`types.py`** — No explanation of protocol groupings (infrastructure vs domain).
- **`config.py`** — No rationale for how settings are grouped (AWS, embedding, chunking, OCR).

**Good examples to emulate:**

- `_read_text_with_fallback`: explains UTF-8 → CP1252 → Latin-1 and *why* each fallback exists.
- `_prepare_image_bytes`: documents format conversion and size-reduction strategy.
- NON-FUNCTIONAL-DESIGN.md: establishes standards so code doesn't need to restate them.

**Principle:** Comments for the obvious are noise. Comments that explain *why*, document *constraints*, or capture *non-obvious intent* earn their keep.

---

## Deficiencies and Action Items

Each item below is written for direct conversion to beads issues via `bd create`. Do **not** auto-create; the user will triage and convert as needed.

---

### D1. ~~Remove Legacy EmbeddingModel~~ **RESOLVED**

Embedding migration is complete. `EmbeddingModel` removed from types.py; `embed_texts` / `embed_query` free functions removed; `SnowflakeEmbeddingBackend` replaced by `OnnxEmbeddingBackend` (ONNX Runtime). No sentence-transformers dependency.

---

### D2. Extract Shared `_sections_to_pages` to Eliminate Duplication

**Severity:** High (design violation)  
**Effort:** Small

**Problem:** `text_processor.py` and `code_processor.py` each define `_sections_to_pages(sections, document_name, document_path)` with identical logic: build `PageContent` list with `page_number=i+1`, `total_pages=len(sections)`, `page_type` differing (SECTION vs CODE).

Per CLAUDE.md: "Duplication is a design failure. If I see two copies, I extract one abstraction."

**Action:**

1. Add `_sections_to_pages(sections, document_name, document_path, page_type: PageType)` to a shared module — either `text_processor.py` (and code_processor imports it) or a new `quarry/page_content.py` utility.
2. Both callers pass their respective `PageType.SECTION` or `PageType.CODE`.
3. Remove the duplicate from code_processor.

---

### D3. Standardize Path Types in OcrBackend Protocol

**Severity:** Low  
**Effort:** Trivial

**Problem:** `OcrBackend.ocr_document` takes `document_path: Path`; `ocr_image_bytes` takes `document_path: str`. Same concept, inconsistent type. Callers already have `Path` in most places.

**Action:** Change `ocr_image_bytes(document_path: str)` to `document_path: Path` in types.py and all implementations (ocr_client.py, ocr_local.py) and call sites (pipeline.py).

---

### D4. Clarify PageContent Semantics — Page vs Section

**Severity:** Medium (conceptual)  
**Effort:** Medium

**Problem:** `PageContent` is overloaded. For PDFs it represents a physical page. For text/code it represents a "section" (markdown heading, LaTeX section, code definition). The `page_number` field is repurposed as section index. This conflates two domain concepts.

**Action (choose one):**

- **Option A (minimal):** Add a docstring to `PageContent` and `page_number` explaining the dual meaning. Document that for non-PDF formats, "page" means "logical section index."
- **Option B (DDD):** Introduce a `ContentUnit` or `Section` type for logical sections; keep `PageContent` for physical pages. Refactor text/code pipeline to produce `Section` and have a single adapter to `PageContent` for the chunk pipeline. Larger change.

Recommendation: Option A unless the codebase is growing toward more format types with divergent semantics.

---

### D5. Organize types.py — Group and Document Protocol Roles

**Severity:** Low  
**Effort:** Small

**Problem:** `types.py` mixes (a) third-party duck-typing Protocols (LanceDB, TextractClient, S3Client), (b) domain protocols (OcrBackend, EmbeddingBackend). No section headers or docstrings explaining organization.

**Action:**

1. Add a module docstring: "Protocol definitions for quarry. Infrastructure protocols (LanceDB, Textract, S3) abstract external libraries. Domain protocols (OcrBackend, EmbeddingBackend) define backend contracts."
2. Group protocols with comment headers.

---

### D6. Fix sync.py Broad Exception Handling

**Severity:** Medium (violates NON-FUNCTIONAL-DESIGN)  
**Effort:** Small

**Problem:** `sync_collection` catches `Exception` in the ingest loop (line 184) and delete loop (line 204). NON-FUNCTIONAL-DESIGN.md: "Never catch Exception broadly in library code. Broad catches permitted only at outermost boundary (MCP tool handler, CLI command)."

`sync_collection` is called by `sync_all`, which is called from CLI/MCP — but the handler is inside the loop, not at the tool boundary. One failed file shouldn't stop the whole sync, but catching `Exception` is too broad.

**Action:**

1. Catch a narrower set: `(OSError, ValueError, RuntimeError, TimeoutError)` — the exceptions ingest/delete can realistically raise.
2. Document in docstring: "Catches [list] to allow sync to continue when individual files fail."
3. Re-raise or log with `logger.exception()` and continue. Current behavior (append to errors, continue) is correct; only the catch scope needs tightening.

---

### D7. Introduce Typed Result Types for Database and Pipeline

**Severity:** Medium  
**Effort:** Medium

**Problem:** `search()`, `list_documents()`, `list_collections()` return `list[dict[str, object]]`. Callers use `r["document_name"]`, `r["chunk_index"]`, etc. No type safety. Same for `ingest_*` return type `dict[str, object]`.

**Action:**

1. Define `SearchResult`, `DocumentSummary`, `CollectionSummary` as TypedDict or frozen dataclasses in models.py (or a new quarry/results.py).
2. Update database.py and pipeline.py to return these types.
3. Update MCP/CLI to use typed access.

Improves IDE support, catches key typos at type-check time.

---

### D8. Consider Format Handler Registry for Pipeline (Open/Closed)

**Severity:** Low (future-proofing)  
**Effort:** Medium

**Problem:** `ingest_document` dispatches by suffix with an if/elif chain. Adding a new format (e.g. XLSX, EPUB) requires editing pipeline.py each time.

**Action:**

1. Define `IngestHandler = Callable[[Path, LanceDB, Settings, ...], dict[str, object]]`.
2. Register handlers: `{".pdf": _ingest_pdf, ".txt": _ingest_text_file, ...}`. Build from `SUPPORTED_*_EXTENSIONS`.
3. `ingest_document` looks up by suffix and calls; unknown suffix → ValueError.

Optional. Current if/elif is fine for ~6 formats; becomes unwieldy at 15+.

---

### D9. Rename `filter` Parameter in LanceTable Protocol

**Severity:** Low  
**Effort:** Trivial

**Problem:** `count_rows(self, filter: str | None = ...)` shadows builtin `filter`. A noqa exists; renaming is cleaner.

**Action:** Rename to `predicate` or `where_clause` in types.py and database.py call sites.

---

### D10. Make configure_logging Idempotent

**Severity:** Low  
**Effort:** Trivial

**Problem:** Both `__main__.py` and `mcp_server.py` call `configure_logging(get_settings())` at import time. Tests that import both modules may configure logging twice. No harm today, but redundant.

**Action:** In `configure_logging`, check if root logger already has handlers (or a known "configured" flag) and return early if so. Or: only call from entry points (`if __name__ == "__main__"` / `main()`), not at import.

---

### D11. Document Public API Surface (__all__)

**Severity:** Low  
**Effort:** Small

**Problem:** `quarry/__init__.py` exports only `__version__`. No `__all__`. Unclear what constitutes the stable public API vs internal modules.

**Action:** Add `__all__` listing the primary entry points: `ingest_document`, `ingest_text`, `search`, `get_db`, `get_settings`, `derive_collection`, etc. Enables `from quarry import ingest_document` and documents intent.

---

### D12. Decouple image_analyzer from Textract Assumptions

**Severity:** Low  
**Effort:** Small

**Problem:** `image_analyzer.py` has `_PIL_TO_TEXTRACT` and `needs_conversion` (BMP/WebP → PNG for Textract). The module name suggests generic format detection, but it encodes OCR-backend-specific requirements. LocalOcrBackend also needs conversion for some formats, but the coupling is implicit.

**Action:** Either (a) rename to `image_format_analyzer` and document that `needs_conversion` means "must convert for OCR backends that require PNG/JPEG," or (b) make the conversion requirements backend-agnostic (e.g. "formats requiring conversion for common OCR engines"). Clarify in docstring.

---

### D13. Improve Module and Function Naming

**Severity:** Low  
**Effort:** Small–Medium

**Problem:** Several names mislead or obscure intent (see Naming and Comments section). Examples: `ocr_client.py` (actually Textract backend), `registry.py` (sync registry), `get_settings` (constructs, doesn't cache), `ingest_text` vs `ingest_text_file` (easy to confuse).

**Action:**

1. Rename modules: `ocr_client.py` → `ocr_textract.py` (or keep as-is if disruptive; add module docstring instead).
2. Rename `registry.py` → `sync_registry.py` (or `directory_registry.py`) if low-impact.
3. Rename `ingest_text` → `ingest_text_content` for clarity.
4. Rename `get_settings` → `load_settings` or document that it creates fresh instances.
5. Rename `TEXT_THRESHOLD` → `MIN_TEXT_CHARS_FOR_TEXT_PAGE`.
6. Standardize `doc_name` vs `document_name` in a single convention.

Apply incrementally; avoid breaking renames in one shot.

---

### D14. Improve Comments and Docstrings

**Severity:** Low  
**Effort:** Small–Medium

**Problem:** Redundant docstrings restate names; complex logic lacks "why" comments; some comments are outdated (Textract-specific when backend is pluggable); module-level context is missing.

**Action:**

1. Add one-line module docstrings to all quarry modules explaining role in the pipeline.
2. Add "why" comments to `_split_text` overlap logic, `_encode_image_to_fit`, `_get_or_create_table` double-checked locking, `compute_sync_plan`.
3. Update `ingest_image` and `image_analyzer` docstrings to be backend-agnostic.
4. Trim or enhance redundant protocol/wrapper docstrings — prefer invariants and constraints over restating the name.
5. Add types.py and config.py module docstrings (see D5 for types.py).

---

### D15. Clarify Vestigial `embedding_model` Setting

**Severity:** Low  
**Effort:** Trivial

**Problem:** `OnnxEmbeddingBackend` does not take `Settings`; it uses fixed ONNX model constants. `get_embedding_backend(settings)` still uses `settings.embedding_model` as the cache key, but the backend ignores it. If a second embedding backend is added later, the design is ready; for now the setting is vestigial.

**Action:** Either (a) add a module docstring or config.py comment noting that `embedding_model` is the cache key for future multi-backend support and is currently unused by OnnxEmbeddingBackend, or (b) remove it from Settings and use a constant cache key until a second backend ships. Prefer (a) to avoid churn.

---

## Summary Table

| ID   | Title                                     | Severity | Effort |
|------|-------------------------------------------|----------|--------|
| D1   | ~~Remove legacy EmbeddingModel~~ **RESOLVED** | —        | —      |
| D2   | Extract shared _sections_to_pages         | High     | Small  |
| D3   | Standardize Path in OcrBackend            | Low      | Trivial|
| D4   | Clarify PageContent page vs section       | Medium   | Small–Medium |
| D5   | Organize types.py with docstrings         | Low      | Small  |
| D6   | Fix sync.py broad Exception catch         | Medium   | Small  |
| D7   | Typed result types for DB/pipeline        | Medium   | Medium |
| D8   | Format handler registry (optional)         | Low      | Medium |
| D9   | Rename filter param in LanceTable         | Low      | Trivial|
| D10  | Make configure_logging idempotent         | Low      | Trivial|
| D11  | Document public API __all__               | Low      | Small  |
| D12  | Decouple image_analyzer from Textract     | Low      | Small  |
| D13  | Improve module and function naming        | Low      | Small–Medium |
| D14  | Improve comments and docstrings          | Low      | Small–Medium |
| D15  | Clarify vestigial embedding_model setting | Low      | Trivial|

---

## Recommended Priority Order

1. **D2** — Duplication removal (design violation, quick win).
2. **D6** — Exception handling compliance.
3. **D3, D9, D10, D15** — Trivial cleanups.
4. **D5, D11, D12** — Documentation and clarity.
5. **D13, D14** — Naming and comments (can be done incrementally alongside other work).
6. **D4** — Conceptual clarity (choose minimal vs full DDD based on roadmap).
7. **D7** — Typed results (good ROI for maintainability).
8. **D8** — Defer until format count justifies it.

---

## Design Philosophy Note

The codebase is a **document processing pipeline** with pluggable backends, not a classic DDD aggregate model. The right abstraction is:

- **Pipelines and protocols** over heavy domain objects.
- **Data in / data out** with immutable carriers.
- **Explicit over implicit** — no magic, clear boundaries.

Pushing for full DDD (aggregates, repositories, domain events) would over-design. The gaps are mostly: cleaning migration artifacts, removing duplication, tightening types, and clarifying boundaries. That path leads to an A design without unnecessary complexity.
