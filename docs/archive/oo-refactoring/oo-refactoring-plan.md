# Quarry OO Refactoring Plan

> **Quarry OO Refactoring Initiative** — all documents: [design report](oo-design-report.md) · [design review](oo-design-review.md) · [pattern review](oo-design-pattern-review.md) · [execution plan](oo-refactoring-plan.md) · [package structure](oo-package-structure.md) · [package structure review](oo-package-structure-review.md)

Generated: 2026-05-13. **Last status update: 2026-05-15.**

## Execution status

| Phase | Steps | Status | Branch/PR |
|-------|-------|--------|-----------|
| 0 | 0.1–0.10 | **DONE** | main (PRs #280–#283) |
| 1 | 1.1–1.10 | **DONE** | main (PR #284) |
| 2 | 2.1–2.8 | **DONE** | main (PRs #285–#286) |
| 3 | 3.1–3.16 | **DONE** | main (PR #288) |
| 4 | 4.1–4.2 done, 4.3a done | **IN PROGRESS** | branch `oo/phase-4-services` |
| 5–7 | all | **NOT STARTED** | — |

**Note**: 5 additional package-move steps (3.16a, 4.3a, 4.17, 5.1 mod, 7.20) were added
by `oo-package-structure.md` after this plan was written. Revised total: **89 steps**.

---

This is document 3 of 3 in the quarry OO redesign:

1. [OO Design Report](oo-design-report.md) -- proposes ~44 classes across 3 sections
2. [OO Design Review](oo-design-review.md) -- peer review with 7 revisions (R1-R7)
3. **This document** -- step-by-step execution plan. 84 steps, tests green at every step.

Incorporates 18 revisions from two review documents:

- **Peer review (R1-R7):** HealthChecker size justification, McpContext extraction,
  SyncConfig, BackfillConfig, IngestJob, SitemapOptions, commands/ as functions.
- **Pattern review (PR1-PR11):** Database Facade, CollectionName Flyweight,
  ConnectionValidator, ServiceManager, sync.py residual absorption, ChunkStore.db
  removal, QuarryContext.api_key encapsulation, presentation-layer exemptions,
  TaskState non-application of State pattern, PubSub non-application, frozen-dataclass
  encapsulation exemption, draft contradiction resolution.

## Modules at target (no steps needed)

3 of 42 modules pass all 11 OO metrics. Every other module has at least one step.

| Module | Lines | Why |
|--------|-------|-----|
| `artifacts.py` | 153 | SessionArtifacts with methods, method_ratio 0.83 |
| `types.py` | 117 | 6 Protocol classes, method_ratio 1.0 |
| `results.py` | 91 | 6 frozen dataclasses, all metrics pass |

## Modules requiring additional steps (previously excluded)

These 4 modules were incorrectly excluded from the design drafts. They
fail OO metrics (method_ratio 0.00, class_to_func_ratio 0.00) and need
refactoring. The drafts marked them "no structural change" but the plan
supersedes the drafts for these modules. (pattern-review PR3)

### Step 0.7: Convert `latex_utils.py` to `LatexSerializer` class

- **Source**: `latex_utils.py` (57 lines, 0 classes, 2 functions)
- **Class**: `LatexSerializer` -- owns escape rules and table serialization
- **Absorbs**: `escape_latex` -> `escape`, `rows_to_latex` -> `serialize_table`
- **Ratchet**: method_ratio 0.00->1.00, class_to_func_ratio 0.00->1.00

### Step 0.8: Convert `logging_config.py` to `LoggingConfig` class

- **Source**: `logging_config.py` (73 lines, 0 classes, 1 function)
- **Class**: `LoggingConfig` -- owns format strings, handler setup, level configuration
- **Absorbs**: `configure_logging` -> `configure` classmethod
- **Ratchet**: method_ratio 0.00->1.00, class_to_func_ratio 0.00->1.00

### Step 0.9: Move `provider.py` functions into `ProviderSelection`

- **Source**: `provider.py` (99 lines, 1 class, 2 functions)
- **Class**: `ProviderSelection` (exists) -- absorb `select_provider` as `from_environment` classmethod, `provider_display` as `display` method
- **Ratchet**: method_ratio 0.00->1.00, class_to_func_ratio 0.33->1.00

### Step 0.10: Move `sitemap.py` functions into `SitemapDiscovery` class

- **Source**: `sitemap.py` (125 lines, 1 class SitemapEntry, 4 functions)
- **Class**: `SitemapDiscovery` -- owns discovery logic, URL filtering
- **Absorbs**: `discover_pages`, `discover_urls`, `filter_entries`, `_pages_to_entries`
- **Keeps**: `SitemapEntry` dataclass unchanged
- **Ratchet**: method_ratio 0.00->0.80, class_to_func_ratio 0.20->0.60

Sources: oo-design-report.md (44 classes), oo-design-review.md (7 revisions),
oo-design-pattern-review.md (11 revisions),
`_draft-core-data.md`, `_draft-ingestion.md`, `_draft-surfaces.md`

Baseline: 42 modules, 15,635 LOC, 44 classes, 394 top-level functions,
method_ratio 0.08.

---

## Calibration

| Metric | merchants/game (reference) | quarry (current) | quarry (target) |
|--------|---------------------------|-------------------|-----------------|
| Total LOC | ~2,000 | 15,635 | ~12,000 |
| Modules | 14 | 42 | ~65 |
| Max module LOC | 363 | 2,008 | <500 |
| Classes | ~20 | 44 | ~90 |
| Top-level functions | ~0 | 394 | <40 |
| Methods | all | 47 | ~360 |
| method_ratio | ~1.0 | 0.08 | >=0.80 |

---

## Invariants

These hold throughout the entire refactoring. Violations are bugs.

1. **No extracted class imports from the presentation layer.** Core
   classes (`ChunkStore`, `ChunkSearch`, extractors) never import from
   `__main__`, `http_server`, `mcp_server`, or `commands/`. Enforce
   with grep: `grep -r 'from punt_quarry.__main__ import' src/quarry/chunk_store.py`
   must return nothing (and likewise for every new core module).

2. **`make check` passes after every step.** This includes `make
   check-oo` (OO quality scores), lint, type check, and all tests
   green. No exceptions. OO scores must improve or stay the same --
   never regress on touched files.

3. **No backward-compatibility wrappers (PL-PP-1).** When a function
   moves into a class, all callers are updated to use the new
   class/module directly in the same PR. No shim functions, no
   deprecated wrappers, no re-exports of dead symbols.

4. **One extraction per PR.** Each step in this plan is a separate PR.
   Do not batch extractions.

5. **Characterization tests precede extraction.** Before moving code
   out of a module, write tests that exercise the behavior through the
   existing interface. These tests must pass both before AND after the
   extraction. This is how you prove the extraction preserved behavior.

6. **`from __future__ import annotations` in every new file.** Every
   new Python file created during this refactoring must include
   `from __future__ import annotations` as its first import. Enforced
   by `make check-oo` (`future_annotations` metric).

7. **`__new__` is the constructor (PY-CC-1).** All new non-dataclass
   classes use `__new__` with `Self` return type. Dataclasses are
   exempt. Pydantic models (`Settings`) are exempt.

8. **All interfaces use `Protocol` (structural typing).** No ABCs are
   introduced. Every protocol in this refactoring uses `typing.Protocol`.
   No proposed interface includes shared implementation.

9. **Stateless extractor classes are justified by the `FormatExtractor`
   protocol.** Seven extractor classes own no instance state. They are
   classes (not functions) because the pipeline dispatches
   polymorphically via the protocol, which requires instances.

10. **`commands/`, `routes/`, `formatting.py`, `_hook_entry.py`, and
    `hooks/transcript.py` are presentation-layer modules exempt from
    `class_to_func_ratio` and `method_ratio` scoring.** Per peer-review
    R7, command modules export functions taking `CliContext` plus parsed
    args. Route modules export async handler functions. Formatting functions
    are stateless transforms calling `TableRenderer.render()`. Hook entry
    and transcript modules are dispatch wiring and pure transforms
    respectively. All are exempt from class-per-module expectations.
    (peer-review R7, pattern-review PR9)

11. **Frozen dataclasses are exempt from PY-EN-1's underscore
    requirement.** Per PY-CC-6, frozen dataclasses are pure value objects
    with no behavior beyond field storage. Their fields are immutable and
    the dataclass decorator generates `__init__`, `__eq__`, and `__hash__`
    from the field names. Public field names are correct for frozen
    dataclasses. (pattern-review PR11)

12. **`TaskState` does not use the State pattern (PY-DP-3).** The
    transition logic is ~15 lines in `TaskManager`, not in `TaskState`
    itself. Applying the State pattern would add ceremony disproportionate
    to the complexity. `TaskState` remains a mutable dataclass with a
    `status` field. (pattern-review PR10)

13. **The hooks system does not use PubSub (PY-DP-8).** The event set is
    fixed by Claude Code (quarry cannot define new events), so loose
    coupling between producer and consumer provides no value. The hardcoded
    dispatch dict in `_hook_entry.py` is simpler and correct.
    (pattern-review PR10)

---

## Phase 0: Pre-flight

Bugs and inconsistencies that must be fixed before refactoring starts.

### Step 0.1: Establish baseline metrics

Run `make check-oo`, `make metrics`, and `make coverage`. Record all
baselines. After pre-flight fixes, the remaining `check-oo` failures
should be only the structural metrics that require the full
refactoring to fix (method_ratio, module_size, class_to_func_ratio).

Record results in `.tmp/refactoring-baseline.txt` (gitignored).

**Verification:** `make check`

### Step 0.2: Fix `__init__` -> `__new__` on existing classes

Convert `OnnxEmbeddingBackend.__init__` and `LocalOcrBackend.__init__`
to `__new__` per PY-CC-1. These are the only two non-dataclass,
non-pydantic classes with `__init__` constructors.

**Source:** `src/quarry/embeddings.py`, `src/quarry/ocr_local.py`
**Tests:** Existing tests cover construction. Verify they pass unchanged.
**Ratchet:** `init_violations` metric drops by 2.

**Verification:** `make check`

### Step 0.3: Fix `slots=True` on frozen dataclasses in models.py

Add `slots=True` to `PageAnalysis`, `PageContent`, `Chunk` dataclass
decorators per PY-CC-6.

**Source:** `src/quarry/models.py`
**Tests:** Existing tests. No behavioral change.
**Ratchet:** Compliance improvement.

**Verification:** `make check`

### Step 0.4: Move `stored_page_type` to `PageType.stored` property

The free function `stored_page_type` operates solely on a `PageType`
value. Move it to a `stored` property on the `PageType` enum.
Update all callers.

**Source:** `src/quarry/models.py`
**Caller updates:** `src/quarry/database.py`, any test using `stored_page_type`
**Tests:** Existing tests cover behavior.
**Ratchet:** `method_ratio` improves (one function becomes a method).

**Verification:** `make check`

### Step 0.5: Absorb `config.py` functions into `Settings` class

Move `resolve_db_paths` -> `Settings.resolve_db_paths` (method),
`read_default_db` -> `Settings.read_default_db` (classmethod),
`write_default_db` -> `Settings.write_default_db` (classmethod),
`load_settings` -> `Settings.load` (classmethod). Move `_CONFIG_PATH`
and `_DEFAULT_LANCEDB` to class-level private constants.

**Source:** `src/quarry/config.py`
**Caller updates:** `__main__.py`, `hooks.py`, `mcp_server.py`, `http_server.py`,
`doctor.py`, `enable.py`, `backfill.py`, `_stdlib.py`, tests
**Tests:** Existing tests plus targeted tests for each classmethod.
**Ratchet:** `method_ratio` improves (4 functions become methods/classmethods).

**Verification:** `make check`

### Step 0.6: Absorb `embeddings.py` functions into `OnnxEmbeddingBackend`

Move `download_model_files` -> `@classmethod download_model_files`,
`_load_model_files` -> `@classmethod _load_model_files`,
`_load_local_model_files` -> `@classmethod _load_local_model_files`.

**Source:** `src/quarry/embeddings.py`
**Caller updates:** `__main__.py` (install command calls `download_model_files`)
**Tests:** Existing tests.
**Ratchet:** `method_ratio` improves (3 functions become classmethods).

**Verification:** `make check`

---

## Phase 1: Shared types, protocols, config objects

These have no dependencies on other new classes and are consumed by
everything that follows.

### Step 1.1: Create `_sql.py` shared helper

Extract `_escape_sql` from `database.py` into `src/quarry/_sql.py`.
One function, one module. Update imports in `database.py`.

**Source:** `src/quarry/database.py`
**Target:** `src/quarry/_sql.py`
**Absorbs:** `_escape_sql`
**Caller updates:** `database.py` (internal, no public API change)
**Tests:** Write a unit test for `_escape_sql` in `tests/test_sql.py`.
**Ratchet:** No OO metric change; enables later extractions.

**Verification:** `make check`

### Step 1.2: Create `SearchFilter` dataclass in `results.py`

Add the `SearchFilter` frozen dataclass with `to_predicate()` method.
This is a value object that bundles the 6 filter parameters currently
threaded through `hybrid_search`.

**Source:** `src/quarry/database.py` (`_build_predicates` logic)
**Target:** `src/quarry/results.py` (add to existing module)
**Class:** `SearchFilter`
**Absorbs:** `_build_predicates` logic from `database.py`
**Caller updates:** None yet -- callers adopt `SearchFilter` when
`ChunkSearch` is extracted.
**Tests:** Unit tests for `SearchFilter.to_predicate()` with all filter
combinations. Test empty filter returns `None`.
**Ratchet:** +1 class, `method_ratio` improves.

**Verification:** `make check`

### Step 1.3: Create `ChunkConfig` dataclass

Bundle the memory kwargs (`agent_handle`, `memory_type`, `summary`)
plus chunking params (`max_chars`, `overlap_chars`, `collection`,
`source_format`) into `ChunkConfig`. This is the canonical parameter
object for the entire ingestion path -- not just the chunker, but all
surfaces that thread memory kwargs: `IngestionPipeline.ingest_document`,
`UrlIngester.ingest_url`, `UrlIngester.ingest_sitemap`,
`SessionBackfiller.run`, `BackgroundIngester.run`,
`WebFetchHandler.handle`, and `PreCompactHandler.handle`.

**Target:** `src/quarry/models.py`
**Class:** `ChunkConfig` (`@dataclass(frozen=True, slots=True)`)
**Absorbs:** The 5-7 metadata params from `chunk_pages`, `ingest_document`,
`ingest_url`, etc.
**Caller updates:** `chunker.py` (`chunk_pages` signature changes).
Other callers adopt `ChunkConfig` as their respective classes are extracted.
**Tests:** Construction tests, field access tests.
**Ratchet:** +1 dataclass.

**Verification:** `make check`

### Step 1.4: Create `CollectionName` value class with Flyweight cache

Convert `collections.py` from two functions to a `CollectionName`
value class with validation in `__new__`, a `from_path` classmethod,
and a `WeakValueDictionary` cache for instance reuse. Mark the class
`@final`. (pattern-review PR2)

The same collection name string (`"default"`, project-derived names)
appears in every ingestion call, every search call, and every sync
operation. `CollectionName` is immutable and identity should equal
equality. This is the textbook Flyweight trigger (PY-DP-1): immutable,
high-frequency, identity=equality.

Implementation:

    _cache: ClassVar[WeakValueDictionary[str, CollectionName]]

    def __new__(cls, name: str) -> Self:
        if name in cls._cache:
            return cls._cache[name]
        instance = super().__new__(cls)
        # ... validation ...
        cls._cache[name] = instance
        return instance

**Source:** `src/quarry/collections.py`
**Target:** `src/quarry/collections.py` (in-place refactor)
**Class:** `CollectionName` (`@final`)
**Absorbs:** `validate_collection_name` -> `__new__`, `derive_collection` -> `from_path`
**Caller updates:** `pipeline.py`, `sync.py`, `hooks.py`, `__main__.py`,
`mcp_server.py`, `http_server.py`
**Tests:** Characterization tests for validation edge cases before refactor.
Test Flyweight: `CollectionName("x") is CollectionName("x")`.
**Ratchet:** `method_ratio` improves (2 functions -> methods).

**Verification:** `make check`

### Step 1.5: Create `SyncConfig` frozen dataclass (peer-review R3)

Bundle `directory`, `collection`, `max_workers` for `CollectionSyncer`.

**Target:** `src/quarry/sync.py` (add to existing module)
**Class:** `SyncConfig` (`@dataclass(frozen=True, slots=True)`)
**Tests:** Construction test.
**Ratchet:** +1 dataclass. Enables step 4.1.

**Verification:** `make check`

### Step 1.6: Create `BackfillConfig` frozen dataclass (peer-review R4)

Bundle `dry_run`, `collection_override`, `project_filter`, `limit`
for `SessionBackfiller`.

**Target:** `src/quarry/backfill.py` (add to existing module)
**Class:** `BackfillConfig` (`@dataclass(frozen=True, slots=True)`)
**Tests:** Construction test.
**Ratchet:** +1 dataclass. Enables step 4.14.

**Verification:** `make check`

### Step 1.7: Create `IngestJob` frozen dataclass (peer-review R5)

Bundle all 8 fields for `BackgroundIngester`: `text_file`,
`document_name`, `collection`, `lancedb_path`, `session_prefix`,
`agent_handle`, `memory_type`, `summary`.

**Target:** `src/quarry/_hook_entry.py` (add to existing module)
**Class:** `IngestJob` (`@dataclass(frozen=True, slots=True)`)
**Tests:** Construction test.
**Ratchet:** +1 dataclass. Enables step 5.6.

**Verification:** `make check`

### Step 1.8: Create `SitemapOptions` frozen dataclass (peer-review R6)

Bundle `include`, `exclude`, `limit`, `workers`, `delay`, `timeout`
for `UrlIngester.ingest_sitemap`.

**Target:** `src/quarry/models.py` or `src/quarry/results.py`
**Class:** `SitemapOptions` (`@dataclass(frozen=True, slots=True)`)
**Tests:** Construction test.
**Ratchet:** +1 dataclass. Enables step 3.12.

**Verification:** `make check`

### Step 1.9: Create `FormatExtractor` protocol

Define the protocol in `src/quarry/extractors/protocol.py`. Create
the `extractors/` package with `__init__.py`.

**Target:** `src/quarry/extractors/protocol.py`
**Class:** `FormatExtractor` (Protocol)
**Tests:** Protocol conformance test: verify a minimal stub satisfies
`isinstance` check with `runtime_checkable`.
**Ratchet:** +1 protocol class. Enables all extractor steps.

**Verification:** `make check`

### Step 1.10: Create `ServiceBackend` protocol

Define the protocol in `src/quarry/service.py` (top of file). Note:
this makes adding a third platform a single new class with no
modification to existing code (Open/Closed Principle).

**Target:** `src/quarry/service.py`
**Class:** `ServiceBackend` (Protocol) with `install()`, `uninstall()`, `status()` methods
**Tests:** Protocol conformance test.
**Ratchet:** +1 protocol class. Enables steps 4.8-4.9.

**Verification:** `make check`

---

## Phase 2: Core data layer (database.py decomposition)

database.py is 925 lines with 28 top-level functions and zero classes.
After this phase it is replaced by a `Database` facade that composes
five extracted classes. Each extraction creates one class and updates
all callers in the same PR.

### Step 2.1: Extract `SchemaManager`

**Source:** `src/quarry/database.py`
**Target:** `src/quarry/schema.py`
**Class:** `SchemaManager`
**Absorbs:** `_schema` -> `schema` (classmethod), `_MIGRATION_COLUMNS` -> class attr,
`_migrate_schema` -> `migrate`, `_ensure_fts_index` -> `ensure_fts_index`,
`ensure_schema` -> `ensure`, `TABLE_NAME` -> class constant
**Caller updates:** `database.py` (internal calls to `ensure_schema` etc.)
**Tests:** Characterization tests for schema creation, migration, FTS index.
**Ratchet:** `method_ratio` improves, `module_size` for `database.py` decreases.

**Verification:** `make check`

### Step 2.2: Extract `ChunkStore`

**Source:** `src/quarry/database.py`
**Target:** `src/quarry/chunk_store.py`
**Class:** `ChunkStore`
**Absorbs:** `insert_chunks` -> `insert`, `batch_insert_chunks` -> `batch_insert`,
`delete_document` -> `delete_document`, `delete_collection` -> `delete_collection`,
`count_chunks` -> `count`, `_get_or_create_table` -> private,
`_try_open_table` -> private. `get_db` stays as module-level factory in
`chunk_store.py` temporarily (replaced by `Database` facade in step 2.8).
**Note:** Do NOT add a public `db` property. The raw `LanceDB` handle must not
be exposed. Callers that need the handle for `ChunkSearch` or `ChunkCatalog`
construction receive it via the `Database` facade (step 2.8), not by reaching
through `ChunkStore`. (pattern-review PR7)
**Caller updates:** `pipeline.py`, `http_server.py`, `mcp_server.py`,
`__main__.py`, `hooks.py`, `sync.py`, `doctor.py`, tests
**Tests:** Characterization tests for insert, delete, count operations.
**Ratchet:** `method_ratio` improves significantly (7 functions -> methods).

**Verification:** `make check`

### Step 2.3: Extract `ChunkSearch`

**Source:** `src/quarry/database.py`
**Target:** `src/quarry/chunk_search.py`
**Class:** `ChunkSearch`
**Absorbs:** `search` -> `vector_search`, `hybrid_search` -> `hybrid_search`,
`_fuse_rrf` -> private, `_temporal_weight` -> private, `_row_key` -> private.
Uses `SearchFilter` (from step 1.2) to replace 11-param signature with 5.
**Caller updates:** `__main__.py`, `mcp_server.py`, `http_server.py`,
`hooks.py`, tests
**Tests:** Characterization tests for vector search, hybrid search, RRF fusion,
temporal decay. Test `SearchFilter` integration.
**Ratchet:** `method_ratio` improves (5 functions -> methods). `hybrid_search`
params drop from 11 to 5.

**Verification:** `make check`

### Step 2.4: Extract `ChunkCatalog`

**Source:** `src/quarry/database.py`
**Target:** `src/quarry/chunk_catalog.py`
**Class:** `ChunkCatalog`
**Absorbs:** `list_documents` -> `list_documents`, `list_collections` -> `list_collections`,
`get_page_text` -> `get_page_text`
**Caller updates:** `__main__.py`, `mcp_server.py`, `http_server.py`, tests
**Tests:** Characterization tests for list operations, page text retrieval.
**Ratchet:** `method_ratio` improves (3 functions -> methods).

**Verification:** `make check`

### Step 2.5: Extract `TableOptimizer`

**Source:** `src/quarry/database.py`
**Target:** `src/quarry/optimizer.py`
**Class:** `TableOptimizer`
**Absorbs:** `optimize_table` -> `optimize`, `count_fragments` -> `count_fragments`,
`create_collection_index` -> `create_collection_index`,
`FRAGMENT_THRESHOLD` -> class constant
**Caller updates:** `__main__.py`, `http_server.py`, tests
**Tests:** Characterization tests for optimize, fragment counting.
**Ratchet:** `method_ratio` improves (3 functions -> methods).

**Verification:** `make check`

### Step 2.6: Extract `storage.py` utility functions

Move `format_size`, `dir_size_bytes`, `discover_databases` from
`database.py` to `src/quarry/storage.py`. These remain free functions
(no shared state, no LanceDB dependency).

Note: `discover_databases` uses deferred imports of `ChunkCatalog` and
`ChunkStore` to avoid circular dependencies. This signals the function
may belong on the `Database` facade (step 2.8). If so, move it there
instead. Evaluate during implementation.

**Source:** `src/quarry/database.py`
**Target:** `src/quarry/storage.py`
**Absorbs:** `format_size`, `dir_size_bytes`, `discover_databases`
**Caller updates:** `__main__.py`, `doctor.py`, `formatting.py`, tests
**Tests:** Existing tests for size formatting and discovery.
**Ratchet:** `module_size` for `database.py` decreases.

**Verification:** `make check`

### Step 2.7: Delete `database.py`

At this point `database.py` should be empty (or contain only `get_db`
re-exported from `chunk_store.py`). Delete the module. Update any
remaining imports.

**Source:** `src/quarry/database.py`
**Caller updates:** Any remaining imports across the codebase.
**Tests:** Full test suite.
**Ratchet:** Largest module eliminated.

**Verification:** `make check`

### Step 2.8: Create `Database` facade (pattern-review PR1)

Five classes (`ChunkStore`, `ChunkSearch`, `ChunkCatalog`,
`SchemaManager`, `TableOptimizer`) all constructed from the same
`LanceDB` handle. Without a facade, the decomposition of `database.py`
trades one god module for a three-class construction ceremony repeated
at every call site. The trigger condition for PY-DP-10 (Facade --
single entry point to a subsystem) is unambiguously present.

**Target:** `src/quarry/database_facade.py`
**Class:** `Database`

    Database
      Owns: _store (ChunkStore), _search (ChunkSearch),
            _catalog (ChunkCatalog), _schema (SchemaManager),
            _optimizer (TableOptimizer)
      Public interface:
        Database(db: LanceDB)  [__new__]
        @property store -> ChunkStore
        @property search -> ChunkSearch
        @property catalog -> ChunkCatalog
        @property optimizer -> TableOptimizer
        ensure_schema() -> None  [delegates to _schema]
      Factory: get_db(path) -> Database  [replaces current get_db]

~40 lines. Eliminates three-class construction ceremony at every call site.
Replace all `get_db` call sites that construct separate classes with
`Database(db)` or the updated `get_db` factory.

**Caller updates:** Every module that previously constructed `ChunkStore`,
`ChunkSearch`, or `ChunkCatalog` separately: `pipeline.py`, `http_server.py`,
`mcp_server.py`, `__main__.py`, `hooks.py`, `sync.py`, `doctor.py`, tests.
**Tests:** Construction test, verify all five components accessible,
verify `get_db` returns `Database`.
**Ratchet:** +1 class, eliminates raw `LanceDB` handle exposure.

**Verification:** `make check`

---

## Phase 3: Ingestion pipeline

FormatExtractor protocol + individual extractors + pipeline class.
Each extractor is one step. The pipeline class is the final step.

### Step 3.1: Create `text_splitter.py`

Extract pure text-splitting utilities from `text_processor.py`. These
are consumed by multiple extractor classes.

**Source:** `src/quarry/text_processor.py`
**Target:** `src/quarry/text_splitter.py`
**Absorbs:** `split_markdown`, `split_latex` (rename from `_split_latex`),
`split_plain`, `sections_to_pages`, `read_text_with_fallback`,
`MD_HEADER`, `LATEX_SECTION`, `BLANK_LINE_SPLIT` constants
**Caller updates:** `text_processor.py`, `code_processor.py`,
`html_processor.py`, `spreadsheet_processor.py`
**Tests:** Characterization tests for each splitter function.
**Ratchet:** No OO metric change; enables extractor steps.

**Verification:** `make check`

### Step 3.2: Rename `text_extractor.py` to `pdf_text_extractor.py`

Avoid name collision with the upcoming `extractors/text_extractor.py`.
The module is consumed only by the PDF extraction path.

**Source:** `src/quarry/text_extractor.py`
**Target:** `src/quarry/pdf_text_extractor.py`
**Caller updates:** `pipeline.py`, tests
**Tests:** Existing tests.
**Ratchet:** No metric change; enables step 3.7.

**Verification:** `make check`

### Step 3.3: Extract `TextExtractor`

**Source:** `src/quarry/text_processor.py`
**Target:** `src/quarry/extractors/text_extractor.py`
**Class:** `TextExtractor` (implements `FormatExtractor`)
**Absorbs:** `process_text_file` -> `extract_pages`,
`process_raw_text` -> `extract_raw`, `_process_docx` -> `_extract_docx`,
`_split_by_format` -> `_split_by_format`, `_detect_format` -> `_detect_format`
**Caller updates:** `pipeline.py`
**Tests:** Characterization tests for text file extraction (plain, markdown,
LaTeX, docx).
**Ratchet:** `method_ratio` improves. `text_processor.py` eliminated.

**Verification:** `make check`

### Step 3.4: Extract `CodeExtractor`

**Source:** `src/quarry/code_processor.py`
**Target:** `src/quarry/extractors/code_extractor.py`
**Class:** `CodeExtractor` (implements `FormatExtractor`)
**Absorbs:** `process_code_file` -> `extract_pages`,
`_split_with_treesitter` -> `_split_treesitter`,
`_fallback_split` -> `_split_fallback`,
`_CODE_LANGUAGES`, `_DEFINITION_NODE_TYPES`, `SUPPORTED_CODE_EXTENSIONS`
**Caller updates:** `pipeline.py`
**Tests:** Characterization tests for code extraction with tree-sitter
and fallback.
**Ratchet:** `method_ratio` improves. `code_processor.py` eliminated.

**Verification:** `make check`

### Step 3.5: Extract `HtmlExtractor`

**Source:** `src/quarry/html_processor.py`
**Target:** `src/quarry/extractors/html_extractor.py`
**Class:** `HtmlExtractor` (implements `FormatExtractor`)
**Absorbs:** `process_html_file` -> `extract_pages`,
`process_html_text` -> `extract_from_html`,
`_strip_boilerplate`, `_extract_title`, `_html_to_markdown`,
`_has_markdown_headings`, `SUPPORTED_HTML_EXTENSIONS`, `_BOILERPLATE_TAGS`
**Note:** `extract_from_html` is NOT part of `FormatExtractor`. This is
an additional method specific to the URL ingestion path. `UrlIngester`
and `WebFetchHandler` depend on `HtmlExtractor` specifically (not on
`FormatExtractor`), creating a concrete dependency from the URL/hook
layer to this specific extractor. This is acceptable because the protocol
defines the minimum, and `HtmlExtractor` extends it for its unique
consumer.
**Caller updates:** `pipeline.py`, `hooks.py` (`WebFetchHandler` depends on
`extract_from_html`)
**Tests:** Characterization tests for HTML extraction from file and raw string.
**Ratchet:** `method_ratio` improves. `html_processor.py` eliminated.

**Verification:** `make check`

### Step 3.6: Extract `PresentationExtractor`

**Source:** `src/quarry/presentation_processor.py`
**Target:** `src/quarry/extractors/presentation_extractor.py`
**Class:** `PresentationExtractor` (implements `FormatExtractor`)
**Absorbs:** `process_presentation_file` -> `extract_pages`,
`_extract_slide_text`, `_extract_shapes`, `_extract_notes`,
`_format_slide_content`, `_table_to_latex`,
`SUPPORTED_PRESENTATION_EXTENSIONS`
**Caller updates:** `pipeline.py`
**Tests:** Characterization tests for PPTX extraction.
**Ratchet:** `method_ratio` improves. `presentation_processor.py` eliminated.

**Verification:** `make check`

### Step 3.7: Extract `SpreadsheetExtractor`

**Source:** `src/quarry/spreadsheet_processor.py`
**Target:** `src/quarry/extractors/spreadsheet_extractor.py`
**Class:** `SpreadsheetExtractor` (implements `FormatExtractor`)
**Absorbs:** `process_spreadsheet_file` -> `extract_pages`,
`_read_xlsx`, `_read_csv`, `_split_rows_to_sections`,
`SUPPORTED_SPREADSHEET_EXTENSIONS`
**Note:** `max_chars` accepted via constructor (per design report).
**Caller updates:** `pipeline.py`
**Tests:** Characterization tests for XLSX and CSV extraction.
**Ratchet:** `method_ratio` improves. `spreadsheet_processor.py` eliminated.

**Verification:** `make check`

### Step 3.8: Extract `PdfExtractor`

**Source:** `src/quarry/pdf_analyzer.py`, `src/quarry/pipeline.py`
**Target:** `src/quarry/extractors/pdf_extractor.py`
**Class:** `PdfExtractor` (implements `FormatExtractor`)
**Absorbs:** `analyze_pdf` -> `_classify_pages`,
`_extract_pdf_pages` from pipeline.py -> inlined into `extract_pages`
**Caller updates:** `pipeline.py`
**Tests:** Characterization tests for PDF page classification and extraction.
**Ratchet:** `method_ratio` improves. `pdf_analyzer.py` eliminated.

**Verification:** `make check`

### Step 3.9: Extract `ImagePreparer`

**Source:** `src/quarry/pipeline.py`
**Target:** `src/quarry/image_preparer.py`
**Class:** `ImagePreparer`
**Absorbs:** `_prepare_image_bytes` -> `prepare_bytes`,
`_encode_image_to_fit` -> `_encode_to_fit`
**Note:** This class is stateless with one public method. It exists as a
class (not a function) for testability -- `ImageExtractor` composes
`_preparer: ImagePreparer`, allowing mock injection in tests.
**Caller updates:** `pipeline.py`
**Tests:** Characterization tests for image preparation and encoding.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Step 3.10: Extract `ImageExtractor`

**Source:** `src/quarry/image_analyzer.py`, `src/quarry/pipeline.py`
**Target:** `src/quarry/extractors/image_extractor.py`
**Class:** `ImageExtractor` (implements `FormatExtractor`)
**Absorbs:** `analyze_image` -> `_analyze`,
`ImageAnalysis` dataclass (moved, re-exported),
`ingest_image` -> `extract_pages`,
`_ingest_multipage_image` -> `_extract_multipage`,
`_extract_image_pages` -> inlined
**Caller updates:** `pipeline.py`
**Tests:** Characterization tests for single and multi-page image extraction.
**Ratchet:** `method_ratio` improves. `image_analyzer.py` eliminated.

**Verification:** `make check`

### Step 3.11: Extract `UrlFetcher`

**Source:** `src/quarry/pipeline.py`
**Target:** `src/quarry/url_fetcher.py`
**Class:** `UrlFetcher`
**Absorbs:** `_fetch_url` -> `fetch`
**Note:** This class is stateless with one public method. It exists as a
class (not a function) for testability -- `UrlIngester` composes
`_fetcher: UrlFetcher`, allowing mock injection in tests.
**Caller updates:** `pipeline.py`
**Tests:** Characterization test for URL fetching with mock HTTP.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Step 3.12: Extract `UrlIngester`

**Source:** `src/quarry/pipeline.py`
**Target:** `src/quarry/url_ingester.py`
**Class:** `UrlIngester`
**Absorbs:** `ingest_url` -> method, `_ingest_url_with_delay` -> `_ingest_with_delay`,
`_bulk_ingest_entries` -> `_bulk_ingest`, `ingest_sitemap` -> method,
`ingest_auto` -> method. Uses `SitemapOptions` (step 1.8) and `ChunkConfig`
(step 1.3) to reduce `ingest_sitemap` from 13 params to 5. (peer-review R6)
**Caller updates:** `__main__.py`, `mcp_server.py`, `http_server.py`, tests
**Tests:** Characterization tests for URL ingestion, sitemap crawling,
auto-detection.
**Ratchet:** `method_ratio` improves significantly (5 functions -> methods).

**Verification:** `make check`

### Step 3.13: Extract `IngestionPipeline` class

**Source:** `src/quarry/pipeline.py` (remaining functions)
**Target:** `src/quarry/pipeline.py` (in-place refactor)
**Class:** `IngestionPipeline`
**Absorbs:** `ingest_document` -> method (extractor registry lookup replaces
if/elif chain), `_chunk_embed_store` -> private method,
`_make_progress` -> private method, `prepare_document` -> method,
`ingest_content` -> method. `supported_extensions` becomes a computed
property from the extractor registry.
All 7 format-specific `ingest_*` functions are eliminated -- replaced by
generic dispatch through `FormatExtractor`.
**Note:** Dependencies use post-refactor names: `ChunkStore.insert` (via
`Database` facade), not `database.insert_chunks`.
**Caller updates:** `__main__.py`, `mcp_server.py`, `http_server.py`,
`hooks.py`, `sync.py`, `backfill.py`, tests
**Tests:** Characterization tests for `ingest_document` with each format.
Verify extractor registry dispatch.
**Ratchet:** `method_ratio` improves dramatically. `pipeline.py` drops from
1,589 to ~200 lines.

**Verification:** `make check`

### Step 3.14: Refine `BackendRegistry` in `backends.py`

Wrap module-level cache state (`_ocr_cache`, `_embedding_cache`, `_lock`)
into a `BackendRegistry` class. Use Singleton by convention: a module-level
instance with thin wrapper functions.

Note: PY-DP-7 (Singleton) trigger is present -- `BackendRegistry` wraps
state that must have exactly one global instance. Module-level singleton
is the Python idiom. A `__new__` guard is cleaner but not critical; either
approach satisfies the intent.

**Source:** `src/quarry/backends.py` (in-place refactor)
**Target:** `src/quarry/backends.py`
**Class:** `BackendRegistry`
**Absorbs:** `get_ocr_backend` -> method, `get_embedding_backend` -> method,
`clear_caches` -> method, cache dicts and lock -> private attributes
**Caller updates:** All modules using `get_ocr_backend`/`get_embedding_backend`
**Tests:** Existing tests for backend creation and caching.
**Ratchet:** `method_ratio` improves (3 functions -> methods).

**Verification:** `make check`

### Step 3.15: Refine `LocalOcrBackend` in `ocr_local.py`

Absorb module-level functions and singleton cache into the class.

**Source:** `src/quarry/ocr_local.py` (in-place refactor)
**Target:** `src/quarry/ocr_local.py`
**Class:** `LocalOcrBackend`
**Absorbs:** `get_engine` -> class method `_get_engine`,
`_extract_text` -> private method, `_render_pdf_page` -> static method,
`_ocr_pages` -> private method. Module-level `_engine` cache internalized.
**Caller updates:** None (all access through `OcrBackend` protocol)
**Tests:** Existing tests.
**Ratchet:** `method_ratio` improves (4 functions -> methods).

**Verification:** `make check`

---

## Phase 4: Services

sync, service, doctor, enable, tls, remote, proxy, backfill, scrub,
formatting.

### Step 4.1: Extract `CollectionSyncer`

**Source:** `src/quarry/sync.py`
**Target:** `src/quarry/sync.py` (in-place, class added)
**Class:** `CollectionSyncer`
**Absorbs:** `sync_collection` -> `sync`, `_ingest_files` -> private,
`_refresh_files` -> private, `_delete_documents` -> private.
Uses `SyncConfig` (step 1.5) for constructor. Constructor is
`CollectionSyncer(config, db, settings, conn)` -- 4 positional params,
satisfying PY-OO-3. (peer-review R3)
**Caller updates:** `sync.py` (`sync_all` instantiates `CollectionSyncer`)
**Tests:** Characterization tests for sync with adds, refreshes, deletes.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Step 4.1a: Absorb `compute_sync_plan` and `sync_all` into `CollectionSyncer` (pattern-review PR6)

After step 4.1, `compute_sync_plan` and `sync_all` remain as module-level
functions sharing `db`, `settings`, `conn`, `max_workers` -- the same
parameter set as `CollectionSyncer`. This is the PY-OO-1 trigger
(functions sharing a parameter).

Move `compute_sync_plan` -> `CollectionSyncer.compute_plan` (classmethod
or static method taking `SyncConfig` + `conn` + `extensions`).
Move `sync_all` -> `CollectionSyncer.sync_all` (classmethod taking `db`,
`settings`, `conn`, `max_workers` -- constructs instances internally).

**Source:** `src/quarry/sync.py`
**Caller updates:** `__main__.py`, `mcp_server.py`, `http_server.py`,
`hooks.py`, tests
**Tests:** Existing sync tests.
**Ratchet:** 2 more functions become methods.

**Verification:** `make check`

### Step 4.2: Extract `FileDiscovery`

**Source:** `src/quarry/sync.py`
**Target:** `src/quarry/sync_discovery.py`
**Class:** `FileDiscovery`
**Absorbs:** `discover_files` -> `discover`, `_load_ignore_spec` -> private,
`_read_local_ignore` -> private, `_symlink_inside_root` -> private,
`_content_hash` -> static/classmethod, `_DEFAULT_IGNORE_PATTERNS`,
`_HASH_CHUNK_SIZE`
**Caller updates:** `sync.py` (`compute_sync_plan` uses `FileDiscovery`)
**Tests:** Characterization tests for file discovery with ignore rules.
**Ratchet:** `method_ratio` improves. `sync.py` drops below 300 lines.

**Verification:** `make check`

### Step 4.3: Extract `SyncRegistry`

**Source:** `src/quarry/sync_registry.py`
**Target:** `src/quarry/sync_registry.py` (in-place refactor)
**Class:** `SyncRegistry`
**Absorbs:** `open_registry` -> classmethod/factory,
`register_directory` -> `register`, `deregister_directory` -> `deregister`,
`list_registrations` -> `list_registrations`,
`get_registration` -> `get_registration`, `get_file` -> `get_file`,
`upsert_file` -> `upsert_file`, `list_files` -> `list_files`,
`delete_file` -> `delete_file`, `_init_schema` -> private,
`_migrate_schema` -> private, `_is_ancestor_of` -> private
**Caller updates:** `sync.py`, `hooks.py`, `enable.py`, `doctor.py`,
`__main__.py`, `mcp_server.py`, `http_server.py`, tests
**Tests:** Characterization tests for register, deregister, list, file CRUD.
This is the highest call-site count after `database.py` -- verify every
consumer is updated.
**Ratchet:** `method_ratio` improves significantly (12 functions -> methods).

**Verification:** `make check`

### Step 4.4: Extract `HealthChecker` (peer-review R1)

**Source:** `src/quarry/doctor.py`
**Target:** `src/quarry/health_checker.py`
**Class:** `HealthChecker`
**Absorbs:** `check_environment` -> `run_all`, `_print_check` -> `print_results`,
all 15 `_check_*` functions -> private methods, `_sync_age_result`,
`_quiet_logging`, `_human_size`, `_quarry_version`
**Size justification (peer-review R1):** ~450 LOC in a standalone module. The 20
`_check_*` methods are tightly cohesive -- they all accumulate `CheckResult`
on the same list. The 300-line exception for standalone classes with tightly
cohesive methods applies. Splitting into `HealthChecker` + `StorageHealthChecker`
was considered but rejected: the checks share `_settings` and `_db` state, and
the cohesion is high (all methods produce the same output type into the same
accumulator). Keeping one class in its own module is the simpler design.
**Caller updates:** `__main__.py`, `doctor.py` (becomes thin wrapper)
**Tests:** Characterization tests for each health check category.
**Ratchet:** `method_ratio` improves (20 functions -> methods). `doctor.py`
drops from 1,141 to ~30 lines.

**Verification:** `make check`

### Step 4.5: Extract `InstallWizard`

**Source:** `src/quarry/doctor.py`
**Target:** `src/quarry/install.py`
**Class:** `InstallWizard`
**Absorbs:** `run_install` -> `run`, `_configure_claude_code` -> private,
`_configure_claude_desktop` -> private, `_mcp_fallback_script` -> private
**Caller updates:** `__main__.py`
**Tests:** Characterization tests for install wizard steps (mocked I/O).
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Step 4.6: Extract `EthosConfigurator`

**Source:** `src/quarry/doctor.py`
**Target:** `src/quarry/ethos_config.py`
**Class:** `EthosConfigurator`
**Absorbs:** `_configure_ethos_ext` -> `configure`,
`_write_ethos_ext_session_context` -> `write_session_context`,
`_session_context_literal_block` -> `_literal_block`,
`_scan_identities_dir` -> `_scan_identities_dir`,
`_ethos_ext_message` -> `_message`,
`_SESSION_CONTEXT_TEMPLATE` -> class constant
**Caller updates:** `doctor.py`
**Tests:** Characterization tests for ethos config generation.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Step 4.7: Extract `claudemd.py`

**Source:** `src/quarry/doctor.py`
**Target:** `src/quarry/claudemd.py`
**Absorbs:** `_inject_claude_md` -> `inject_claude_md` (module-level function),
`_QUARRY_CLAUDE_MD_SECTION`, `_QUARRY_SECTION_MARKER`
**Note:** No class needed -- single function, ~60 lines.
**Caller updates:** `doctor.py`
**Tests:** Characterization tests for CLAUDE.md injection and removal.
**Ratchet:** `doctor.py` continues shrinking.

**Verification:** `make check`

### Step 4.8: Extract `ServiceManager` with `LaunchdBackend` (pattern-review PR5)

Extract both the dispatch layer and the macOS backend in one step.
After extracting `LaunchdBackend` and `SystemdBackend` (next step),
7 functions would remain in `service.py`: `install`, `uninstall`,
`detect_platform`, `_write_env_file`, `_quarry_exec_args`,
`_get_tls_hostname`, `ensure_gpu_runtime`. These share `_LABEL`,
`_ENV_FILE`, and platform detection logic -- the PY-OO-1 trigger.

Create `ServiceManager` owning `_label`, `_env_file`, `_backend`
(a `ServiceBackend` instance). The backends become an owned collaborator.

**Source:** `src/quarry/service.py`
**Target:** `src/quarry/service.py` (in-place, classes added)
**Classes:** `ServiceManager`, `LaunchdBackend` (implements `ServiceBackend`)
**ServiceManager absorbs:** `install` -> method (dispatches to `_backend`),
`uninstall` -> method, `detect_platform` -> classmethod,
`_write_env_file` -> `_write_env_file`, `_quarry_exec_args` -> `_exec_args`,
`_get_tls_hostname` -> `_tls_hostname`, `ensure_gpu_runtime` -> method,
`_LABEL`, `_ENV_FILE` -> class constants
**LaunchdBackend absorbs:** `_launchd_plist_content` -> `_plist_content`,
`_launchd_install` -> `install`, `_launchd_uninstall` -> `uninstall`,
`_launchd_status` -> `status`, `_LAUNCHD_DIR`, `_LAUNCHD_PLIST`
**Caller updates:** `__main__.py`, `http_server.py`, `doctor.py`
**Tests:** Characterization tests for launchd plist generation, platform
detection, env file writing.
**Ratchet:** `method_ratio` improves (11 functions -> methods across 2 classes).

**Verification:** `make check`

### Step 4.9: Extract `SystemdBackend`

**Source:** `src/quarry/service.py`
**Target:** `src/quarry/service.py` (in-place, class added)
**Class:** `SystemdBackend` (implements `ServiceBackend`)
**Absorbs:** `_systemd_unit_content` -> `_unit_content`,
`_systemd_install` -> `install`, `_systemd_uninstall` -> `uninstall`,
`_systemd_status` -> `status`, `_systemd_escape` -> `_escape`,
`_has_linger` -> `_has_linger`, `_SYSTEMD_DIR`, `_SYSTEMD_UNIT`
**Caller updates:** `ServiceManager` dispatches to `SystemdBackend`
**Tests:** Characterization tests for systemd unit generation and commands.
**Ratchet:** `method_ratio` improves (6 functions -> methods).

**Verification:** `make check`

### Step 4.10: Extract `ProxyConfig`

**Source:** `src/quarry/remote.py`
**Target:** `src/quarry/remote.py` (in-place refactor)
**Class:** `ProxyConfig`
**Absorbs:** `read_proxy_config` -> `read`, `write_proxy_config` -> `write`,
`delete_proxy_config` -> `delete`, `_toml_escape` -> private,
`MCP_PROXY_CONFIG_PATH`, `CA_CERT_PATH` -> constructor defaults
**Caller updates:** `__main__.py`, `doctor.py`, `mcp_server.py`
**Tests:** Characterization tests for proxy config read/write/delete.
**Ratchet:** `method_ratio` improves (3 functions -> methods).

**Verification:** `make check`

### Step 4.10a: Extract `ConnectionValidator` from `remote.py` (pattern-review PR4)

After `ProxyConfig` extraction, 6 functions remain in `remote.py`:
`ws_to_http`, `validate_connection`, `validate_connection_from_ws_url`,
`mask_token`, `fetch_ca_cert`, `store_ca_cert`. Three of these
(`validate_connection`, `fetch_ca_cert`, `store_ca_cert`) share CA
certificate state and connection validation logic. This is textbook
Extract Class (PY-RF-3).

**Source:** `src/quarry/remote.py`
**Target:** `src/quarry/remote.py` (in-place, class added)
**Class:** `ConnectionValidator`
**Absorbs:** `validate_connection` -> `validate`,
`validate_connection_from_ws_url` -> `validate_from_ws_url`,
`fetch_ca_cert` -> `fetch_ca_cert`, `store_ca_cert` -> `store_ca_cert`
**Owns:** `_ca_cert_path: Path`
**Keeps as module-level:** `ws_to_http` (pure string transform),
`mask_token` (pure string transform)
**Caller updates:** `__main__.py`, `doctor.py`, `mcp_server.py`
**Tests:** Characterization tests for connection validation with mock
HTTP, CA cert fetch/store.
**Ratchet:** `method_ratio` improves (4 functions -> methods).

**Verification:** `make check`

### Step 4.11: Extract `CertificateAuthority`

**Source:** `src/quarry/tls.py`
**Target:** `src/quarry/tls.py` (in-place refactor)
**Class:** `CertificateAuthority`
**Absorbs:** `generate_ca` -> method, `generate_server_cert` -> method,
`write_tls_files` -> method, `cert_fingerprint` -> static,
`_write_file` -> private, `_signing_public_key` -> private, `_now_utc` -> private
**Size justification:** ~340 LOC in a standalone module. Internally cohesive
(all methods relate to TLS cert generation). The 300-line exception for
standalone classes with tightly cohesive methods applies, same as
`HealthChecker` (step 4.4).
**Caller updates:** `service.py`, `http_server.py`, `__main__.py`
**Tests:** Characterization tests per Bug Class 4 requirements: IP SANs,
backdated certificates, pinned CA context, mismatched cert/key.
**Ratchet:** `method_ratio` improves (7 functions -> methods).

**Verification:** `make check`

### Step 4.12: Extract `ProxyInstaller`

**Source:** `src/quarry/proxy.py`
**Target:** `src/quarry/proxy.py` (in-place refactor)
**Class:** `ProxyInstaller`
**Absorbs:** `install` -> method, `installed_path` -> static,
`_asset_name`, `_latest_version`, `_download_url`, `_checksums_url`,
`_verify_checksum`, `_request` -> private methods
**Caller updates:** `doctor.py`, `__main__.py`
**Tests:** Characterization tests for asset name resolution, checksum verification.
**Ratchet:** `method_ratio` improves (8 functions -> methods).

**Verification:** `make check`

### Step 4.13: Extract `ProjectManager`

**Source:** `src/quarry/enable.py`
**Target:** `src/quarry/enable.py` (in-place refactor)
**Class:** `ProjectManager`
**Absorbs:** `enable_project` -> `enable`, `disable_project` -> `disable`,
`_resolve_or_register` -> private, `_bootstrap_ethos_memory` -> private,
`_write_project_config` -> private, `_append_claudemd_block` -> private,
`_remove_claudemd_block` -> private
**Caller updates:** `__main__.py`, `hooks.py`
**Tests:** Characterization tests for enable/disable with mock filesystem.
**Ratchet:** `method_ratio` improves (7 functions -> methods).

**Verification:** `make check`

### Step 4.14: Extract `SessionBackfiller` (peer-review R4)

**Source:** `src/quarry/backfill.py`
**Target:** `src/quarry/backfill.py` (in-place refactor)
**Class:** `SessionBackfiller`
**Absorbs:** `backfill_sessions` -> `run`, `_process_project` -> private,
`_get_existing_doc_names` -> private, `_count_unregistered_dirs` -> private,
`_write_backfill_capture_file` -> private.
Uses `BackfillConfig` (step 1.6) for constructor. Constructor becomes
`SessionBackfiller(settings, db, config)` -- 3 positional params.
**Remaining module-level functions:** `encode_project_path`,
`build_project_mappings`, `list_transcript_files`,
`document_name_for_transcript`, `is_already_ingested`. These are pure
utility functions with no shared state -- justified as functions.
`is_already_ingested` takes `db` but is consumed by tests and the backfiller
internally; if usage grows, absorb into `SessionBackfiller`.
**Caller updates:** `__main__.py`
**Tests:** Characterization tests for backfill with mock transcripts.
**Ratchet:** `method_ratio` improves (5 functions -> methods).

**Verification:** `make check`

### Step 4.15: Extract `TextScrubber`

**Source:** `src/quarry/scrub.py`
**Target:** `src/quarry/scrub.py` (in-place refactor)
**Class:** `TextScrubber`
**Absorbs:** `scrub` -> method, `scrub_and_log` -> method,
`_scrub_block_secrets` -> private, `_scrub_line_secrets` -> private,
`_build_profanity_re` -> private, `_replacement_for` -> private,
`_build_secret_rules` -> private, `_DEFAULT_CONFIG` -> constructor default
**Caller updates:** `hooks.py`, `backfill.py`
**Tests:** Characterization tests for secret scrubbing and profanity filtering.
**Ratchet:** `method_ratio` improves (7 functions -> methods).

**Verification:** `make check`

### Step 4.16: Extract `TableRenderer`

**Source:** `src/quarry/formatting.py`
**Target:** `src/quarry/formatting.py` (in-place, class added)
**Class:** `TableRenderer`
**Absorbs:** `format_table` -> `render`, `_render_rows` -> private,
`_fmt_cell` -> private, `visible_width` -> private.
Owns layout constants: `_width`, `_col_sep`, `_header_prefix`, `_row_prefix`.
**Note:** The 15+ `format_*` functions remain as module-level (stateless
transformations calling `TableRenderer.render()`). Module stays at ~400 LOC.
This is a known exception -- `formatting.py` is a presentation-layer module
exempt from `class_to_func_ratio` scoring per invariant 10.
(pattern-review PR9)
**Caller updates:** All functions in `formatting.py` that call `format_table`
**Tests:** Characterization tests for table rendering with various column specs.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

---

## Phase 5: Hook handlers

hooks.py is 868 lines with 23 functions. After this phase it becomes
a package with ~30 lines in `__init__.py`.

### Step 5.1: Extract `hooks/transcript.py`

Pure transcript extraction functions. No class -- these are stateless
transforms. Exempt from class expectations per invariant 10.

**Source:** `src/quarry/hooks.py`
**Target:** `src/quarry/hooks/transcript.py`
**Absorbs:** `extract_transcript_text`, `extract_message_text`,
`_extract_content_texts`, `_extract_tool_result_text`,
`_MAX_TRANSCRIPT_CHARS`, `_MAX_TOOL_RESULT_CHARS`
**Caller updates:** `hooks.py`, tests
**Tests:** Existing transcript extraction tests.
**Ratchet:** `module_size` for hooks.py decreases.

**Verification:** `make check`

### Step 5.2: Extract `hooks/collection_resolver.py`

Collection resolution functions. No class -- pure functions.

**Source:** `src/quarry/hooks.py`
**Target:** `src/quarry/hooks/collection_resolver.py`
**Absorbs:** `_collection_for_cwd`, `_collection_for_cwd_conn`, `_resolve_settings`
**Caller updates:** `hooks.py`, tests
**Tests:** Characterization tests for collection resolution.
**Ratchet:** `module_size` for hooks.py decreases.

**Verification:** `make check`

### Step 5.3: Extract `SessionStartHandler`

**Source:** `src/quarry/hooks.py`
**Target:** `src/quarry/hooks/session_start.py`
**Class:** `SessionStartHandler`
**Absorbs:** `handle_session_start` -> `handle`,
`_sync_in_background` -> private, `_is_sync_running` -> private,
`_acquire_sync_lock` -> private, `_sync_lockfile` -> private,
`_unique_collection_name` -> private, `_find_registration` -> private
**Caller updates:** `hooks.py` `__init__.py` re-exports, `_hook_entry.py`
**Tests:** Characterization tests for session start handling.
**Ratchet:** `method_ratio` improves (7 functions -> methods).

**Verification:** `make check`

### Step 5.4: Extract `WebFetchHandler`

**Source:** `src/quarry/hooks.py`
**Target:** `src/quarry/hooks/web_fetch.py`
**Class:** `WebFetchHandler`
**Absorbs:** `handle_post_web_fetch` -> `handle`,
`_extract_url` -> private, `_extract_web_fetch_content` -> private,
`_is_already_ingested` -> private
**Note:** Depends on `HtmlExtractor.extract_from_html` specifically
(not `FormatExtractor`). This is a concrete dependency, documented and
accepted (see step 3.5 note).
**Caller updates:** `hooks.py` `__init__.py`, `_hook_entry.py`
**Tests:** Characterization tests for web fetch auto-ingestion.
**Ratchet:** `method_ratio` improves (4 functions -> methods).

**Verification:** `make check`

### Step 5.5: Extract `PreCompactHandler`

**Source:** `src/quarry/hooks.py`
**Target:** `src/quarry/hooks/pre_compact.py`
**Class:** `PreCompactHandler`
**Absorbs:** `handle_pre_compact` -> `handle`,
`_archive_transcript` -> private, `_spawn_background_ingest` -> private,
`_write_capture_file` -> private, `_read_ethos_agent_handle` -> private
**Caller updates:** `hooks.py` `__init__.py`, `_hook_entry.py`
**Tests:** Characterization tests for pre-compact transcript capture.
**Ratchet:** `method_ratio` improves (5 functions -> methods).

**Verification:** `make check`

### Step 5.6: Extract `BackgroundIngester` (peer-review R5)

**Source:** `src/quarry/_hook_entry.py`
**Target:** `src/quarry/hooks/background_ingester.py`
**Class:** `BackgroundIngester`
**Absorbs:** `_ingest_background` core logic -> `run`.
Uses `IngestJob` (step 1.7) for constructor.
**Note:** Moved out of `_hook_entry.py` -- the class is a domain object,
not dispatch wiring. `_hook_entry.py` becomes pure dispatch (~60 lines,
5 thin functions), exempt from class expectations per invariant 10.
**Caller updates:** `_hook_entry.py`
**Tests:** Characterization tests for background ingestion with dedup.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Step 5.7: Convert `hooks.py` to `hooks/` package

Replace `src/quarry/hooks.py` with `src/quarry/hooks/__init__.py`
that re-exports `handle_session_start`, `handle_post_web_fetch`,
`handle_pre_compact`, and transcript/collection utilities for
backward compatibility during transition.

**Caller updates:** All imports from `quarry.hooks`
**Tests:** Full test suite.
**Ratchet:** `module_size` for hooks drops from 868 to ~30.

**Verification:** `make check`

---

## Phase 6: Surfaces (HTTP routes, MCP session)

### Step 6.1: Extract `TaskManager`

**Source:** `src/quarry/http_server.py`
**Target:** `src/quarry/task_manager.py`
**Class:** `TaskManager`
**Absorbs:** `_gc_tasks` -> `gc`, `_begin_task` -> `begin`,
`_on_task_done` -> `on_done`, `TaskState` (moved to same module)
**Note (invariant 12):** `TaskState` remains a mutable dataclass. The State
pattern (PY-DP-3) is not applied because the transition logic is ~15 lines
in `TaskManager`, not in `TaskState` itself.
**Caller updates:** `http_server.py`
**Tests:** Characterization tests for task lifecycle and GC.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Step 6.2: Rename `_QuarryContext` to `QuarryContext` and fix encapsulation (pattern-review PR8)

**Source:** `src/quarry/http_server.py` (in-place)
**Class:** `QuarryContext` (was `_QuarryContext`)
**Changes:**

- Rename to `QuarryContext` (public class).
- `db` property returns `Database` facade (step 2.8), not raw `LanceDB` handle.
- `api_key` becomes `_api_key` (private attribute). Auth checking accesses
  `_api_key` internally via a `check_auth(token: str) -> bool` method or
  via the auth middleware. The API key must not be a public property.
**Caller updates:** All references in `http_server.py`, auth middleware
**Tests:** Existing tests. Add test that `api_key` is not publicly accessible.
**Ratchet:** No metric change; enables route extraction.

**Verification:** `make check`

### Step 6.3: Extract `routes/search.py`

**Source:** `src/quarry/http_server.py`
**Target:** `src/quarry/routes/search.py`
**Absorbs:** `_search_route`
**Caller updates:** `http_server.py` `build_app()`
**Tests:** HTTP contract tests per Bug Class 3 (remote/local equivalence).
**Ratchet:** `module_size` for `http_server.py` decreases.

**Verification:** `make check`

### Step 6.4: Extract `routes/documents.py`

**Source:** `src/quarry/http_server.py`
**Target:** `src/quarry/routes/documents.py`
**Absorbs:** `_documents_route`, `_documents_delete_route`,
`_run_delete_document_task`, `_show_route`
**Caller updates:** `http_server.py` `build_app()`
**Tests:** HTTP contract tests.
**Ratchet:** `module_size` decreases.

**Verification:** `make check`

### Step 6.5: Extract `routes/collections.py`, `routes/remember.py`, `routes/ingest.py`, `routes/sync.py`

Four small route modules extracted in sequence. Each is its own PR.

**Step 6.5a:** `routes/collections.py` -- absorbs `_collections_route`,
`_collections_delete_route`, `_run_delete_collection_task`

**Step 6.5b:** `routes/remember.py` -- absorbs `_remember_route`,
`_run_remember_task`

**Step 6.5c:** `routes/ingest.py` -- absorbs `_ingest_route`,
`_run_ingest_task`, `_validate_ingest_url`

**Step 6.5d:** `routes/sync.py` -- absorbs `_sync_route`, `_run_sync_task`

Each step follows the same pattern as 6.3-6.4.

**Verification:** `make check` after each.

### Step 6.6: Extract `routes/registrations.py`

**Source:** `src/quarry/http_server.py`
**Target:** `src/quarry/routes/registrations.py`
**Absorbs:** `_registrations_route`, `_handle_list_registrations`,
`_handle_add_registration`, `_handle_delete_registration`,
`_run_register_task`, `_run_deregister_task`, `_register_sync`,
`_deregister_sync`, `_list_registrations_sync`,
`_resolve_registration_path`, `_server_home`
**Caller updates:** `http_server.py` `build_app()`
**Tests:** HTTP contract tests for registration CRUD.
**Ratchet:** `module_size` decreases significantly (~200 LOC moved).

**Verification:** `make check`

### Step 6.7: Extract `routes/status.py` and `routes/mcp_ws.py`

**Step 6.7a:** `routes/status.py` -- absorbs `_status_route`,
`_health_route`, `_ca_cert_route`, `_databases_route`, `_use_route`,
`_task_status_route`

**Step 6.7b:** `routes/mcp_ws.py` -- absorbs `_mcp_websocket_route`

**Verification:** `make check` after each.

### Step 6.8: Extract `McpContext` (peer-review R2)

**Source:** `src/quarry/mcp_server.py`
**Target:** `src/quarry/mcp_server.py` (in-place, class added)
**Class:** `McpContext`
**Absorbs:** `_settings` -> method, `_db` -> method,
`_background` -> method, `_handle_errors` -> method
**Note (peer-review R2):** Analogous to `CliContext`. `McpSession` owns
`_context` and delegates infrastructure concerns. The symmetry between
`CliContext` and `McpContext` is a feature -- both are presentation-layer
context objects per PL-PA-3.
**Caller updates:** `mcp_server.py` (internal)
**Tests:** Characterization tests for settings/db resolution.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Step 6.9: Extract `McpSession`

**Source:** `src/quarry/mcp_server.py`
**Target:** `src/quarry/mcp_server.py` (in-place refactor)
**Class:** `McpSession`
**Absorbs:** `find` -> method, `ingest` -> method, `remember` -> method,
`list_resources` -> method, `show` -> method, `delete` -> method,
`register_directory` -> method, `deregister_directory` -> method,
`sync_all_registrations` -> method, `status` -> method,
`use_database` -> method, plus all `_do_*` helpers
**Note:** The `mcp` FastMCP instance stays at module level. `@mcp.tool()`
decorators delegate to `McpSession` methods. `run_mcp_session` and `main`
stay as module-level functions.
**Caller updates:** `mcp_server.py` (internal)
**Tests:** Characterization tests for each MCP tool.
**Ratchet:** `method_ratio` improves significantly (22 functions -> methods).
`mcp_server.py` stays at ~580 LOC (160 for wrappers + 420 for class).

**Verification:** `make check`

---

## Phase 7: CLI thin layer (`__main__.py` decomposition)

`__main__.py` is 2,008 lines. After this phase it drops to ~400.
Extract context first, then remote client, then commands one at a time.

### Step 7.1: Extract `CliContext` (peer-review R7)

**Source:** `src/quarry/__main__.py`
**Target:** `src/quarry/cli_context.py`
**Class:** `CliContext`
**Absorbs:** `_emit` -> `emit`, `_progress` -> `progress`,
`_resolved_settings` -> `resolved_settings`,
`_safe_proxy_config` -> `proxy_config`, `main_callback` (state-setting portion).
Owns `_json_output`, `_verbose`, `_quiet`, `_global_db`.
**Caller updates:** `__main__.py` (every command function receives `CliContext`)
**Tests:** Characterization tests for output modes (JSON, verbose, quiet).
**Ratchet:** `method_ratio` improves. Module globals eliminated.

**Verification:** `make check`

### Step 7.2: Extract `RemoteClient`

**Source:** `src/quarry/__main__.py`
**Target:** `src/quarry/remote_client.py`
**Class:** `RemoteClient`
**Absorbs:** `_remote_https_request` -> `request`,
`_remote_https_get` -> `get`, `RemoteError`
**Caller updates:** `__main__.py` (all remote command paths)
**Tests:** Characterization tests for HTTPS request construction and
error handling.
**Ratchet:** `method_ratio` improves.

**Verification:** `make check`

### Steps 7.3-7.16: Extract command modules (peer-review R7)

Each command module is extracted in a separate PR. The pattern is
identical for each: extract the command body from `__main__.py` into
a function in `commands/<module>.py` that takes `CliContext` plus
parsed args. Per peer-review R7, command modules export functions, not
classes. Each command function is a stateless orchestration with no data
to own and no invariant to maintain. The module is the namespace.

| Step | Module | Absorbs | Est. LOC |
|------|--------|---------|----------|
| 7.3 | `commands/find.py` | `find_cmd` body, `_find_remote` | ~90 |
| 7.4 | `commands/ingest.py` | `ingest_cmd` body, `_exit_on_ingest_failure` | ~80 |
| 7.5 | `commands/show.py` | `show_cmd` body | ~60 |
| 7.6 | `commands/remember.py` | `remember` body | ~70 |
| 7.7 | `commands/status.py` | `status_cmd` body | ~50 |
| 7.8 | `commands/use.py` | `use_cmd` body | ~25 |
| 7.9 | `commands/delete.py` | `delete_cmd` body | ~55 |
| 7.10 | `commands/register.py` | `register` body, `deregister` body | ~70 |
| 7.11 | `commands/sync.py` | `sync_cmd` body, `_auto_workers`, `_format_sync_results` | ~80 |
| 7.12 | `commands/enable.py` | `enable_cmd` body, `disable_cmd` body | ~60 |
| 7.13 | `commands/optimize.py` | `optimize_cmd` body | ~40 |
| 7.14 | `commands/backfill.py` | `backfill_sessions_cmd` body | ~50 |
| 7.15 | `commands/login.py` | `login_cmd` body, `logout_cmd` body | ~90 |
| 7.16 | `commands/list_resources.py` | `list_documents_cmd`, `list_collections_cmd`, `list_registrations_cmd`, `list_databases_cmd`, `_format_registrations`, `_format_databases` | ~120 |

After step 7.16, also extract `commands/remote_list.py` (absorbs
`remote_list_cmd` body, ~50 LOC) and `commands/admin.py` (absorbs
`install`, `doctor`, `serve`, `mcp`, `version`, `uninstall` bodies, ~60 LOC).

For each step:

**Tests:** Existing CLI tests. Mock targets change from
`patch("punt_quarry.__main__.X")` to `patch("punt_quarry.commands.<mod>.X")`.
Update in the same PR.

**Ratchet:** `module_size` for `__main__.py` decreases with each step.
After all command extractions, `__main__.py` is ~400 LOC (argument
declarations, decorators, and thin delegation).

**Verification:** `make check` after each step.

---

## Phase 7b: Remaining extractions

### Step 7.19: Extract `PluginSetup`

**Source:** `src/quarry/_stdlib.py`
**Target:** `src/quarry/_stdlib.py` (in-place, class added)
**Class:** `PluginSetup`
**Absorbs:** `_deploy_commands` -> `deploy`, `_allow_mcp_tools` -> `allow_mcp_tools`,
`_allow_skill_permissions` -> `allow_skill_permissions`,
`_read_plugin_name`, `_retire_old_commands`, `_should_deploy`,
`_list_deployable_commands`, `_ensure_allow_list`, `_write_settings`,
`_RETIRED_COMMANDS`
**Caller updates:** `_stdlib.py` (`handle_session_setup` delegates)
**Tests:** Characterization tests for command deployment and permission management.
**Ratchet:** `method_ratio` improves. `_stdlib.py` drops from 452 to ~200 lines.

**Verification:** `make check`

---

## Step Summary

| Phase | Steps | Description |
|-------|-------|-------------|
| 0 | 0.1-0.10 | Pre-flight: baselines, `__init__` -> `__new__`, in-place method absorption, excluded module fixes |
| 1 | 1.1-1.10 | Shared types: `_sql.py`, `SearchFilter`, `ChunkConfig`, `CollectionName` (with Flyweight), config dataclasses, protocols |
| 2 | 2.1-2.8 | Core data: `database.py` decomposition into 5 classes + utilities, delete, `Database` facade |
| 3 | 3.1-3.15 | Ingestion: `text_splitter.py`, 7 extractors, `IngestionPipeline`, `UrlIngester`, backend refinement |
| 4 | 4.1-4.16 | Services: `CollectionSyncer` (with sync absorption), `FileDiscovery`, `SyncRegistry`, `HealthChecker`, `InstallWizard`, `EthosConfigurator`, `ServiceManager` + backends, `ProxyConfig`, `ConnectionValidator`, `CertificateAuthority`, `ProxyInstaller`, `ProjectManager`, `SessionBackfiller`, `TextScrubber`, `TableRenderer` |
| 5 | 5.1-5.7 | Hooks: transcript + resolver extraction, 3 handler classes, `BackgroundIngester`, package conversion |
| 6 | 6.1-6.9 | Surfaces: `TaskManager`, `QuarryContext` (with encapsulation fix), 9 route modules, `McpContext`, `McpSession` |
| 7 | 7.1-7.19 | CLI: `CliContext`, `RemoteClient`, 16 command modules, `PluginSetup` |

**Total steps: 84**

---

## Revision Traceability

All 18 revisions from the two review documents are incorporated inline.
This table maps each revision to the step(s) that implement it.

### Peer review (oo-design-review.md): 7 revisions

| Revision | Description | Implemented in |
|----------|-------------|----------------|
| R1 | HealthChecker size justification | Step 4.4 (size justification block) |
| R2 | Extract McpContext from McpSession | Step 6.8 |
| R3 | SyncConfig for CollectionSyncer | Steps 1.5, 4.1 |
| R4 | BackfillConfig for SessionBackfiller | Steps 1.6, 4.14 |
| R5 | IngestJob for BackgroundIngester | Steps 1.7, 5.6 |
| R6 | SitemapOptions for UrlIngester | Steps 1.8, 3.12 |
| R7 | commands/ uses functions, not classes | Invariant 10, steps 7.1, 7.3-7.16 |

### Pattern review (oo-design-pattern-review.md): 11 revisions

| Revision | Description | Implemented in |
|----------|-------------|----------------|
| PR1 | Database Facade (PY-DP-10) | Step 2.8 |
| PR2 | CollectionName Flyweight (PY-DP-1) | Step 1.4 |
| PR3 | Resolve contradictions between steps 0.7-0.10 and drafts | Steps 0.7-0.10 header note |
| PR4 | ConnectionValidator from remote.py | Step 4.10a |
| PR5 | ServiceManager from service.py | Step 4.8 |
| PR6 | sync.py residual absorption | Step 4.1a |
| PR7 | Remove ChunkStore.db property | Step 2.2 (Note block) |
| PR8 | QuarryContext.api_key encapsulation | Step 6.2 |
| PR9 | Presentation-layer module exemptions | Invariant 10, step 4.16 note |
| PR10 | Document non-application of State/PubSub | Invariants 12, 13 |
| PR11 | Frozen-dataclass encapsulation exemption | Invariant 11 |

---

## Dependency Order Verification

The ordering guarantees that every class is extracted only after its
dependencies exist:

- Phase 1 types and protocols have zero dependencies on new classes.
- Phase 2 classes depend only on Phase 1 types (`SearchFilter`, `_sql.py`).
  The `Database` facade (step 2.8) depends on all five Phase 2 classes.
- Phase 3 extractors depend on Phase 1 (`FormatExtractor` protocol) and
  Phase 2 (`Database` facade for pipeline).
- Phase 4 services depend on Phase 1 config objects and Phase 2/3 core
  classes.
- Phase 5 hooks depend on Phase 3 (`IngestionPipeline`, `HtmlExtractor`)
  and Phase 4 (`SyncRegistry`).
- Phase 6 routes/MCP depend on Phase 2/3/4 core classes.
- Phase 7 commands depend on Phase 6 (`CliContext`, `RemoteClient`) and
  all preceding phases.

No circular dependencies. Dependency direction is always inward:
commands -> routes -> services -> core data -> types.

---

## Risk Assessment

### Highest risk: `__main__.py` decomposition (2,008 lines, ~30 mock targets in tests)

Every CLI command is tested by invoking through Typer's test client or
by calling the function directly. Extracting to `commands/` changes
every import path. Mock targets in tests must all be updated. Mitigation:
one command module per PR, update mock targets in the same commit, run
full test suite at each step.

### Second highest: `database.py` deletion (925 lines, imported by 7+ modules)

Every module that calls `database.insert_chunks`, `database.hybrid_search`,
etc. must be updated. The migration order (extract classes one at a time,
keep `get_db` as module-level factory, then replace with `Database` facade)
minimizes intermediate breakage. Estimate: 50-80 import statements across
production code and tests.

### Third: `pipeline.py` FormatExtractor migration

Replacing 7 format-specific functions with protocol dispatch changes the
internal flow of the most-used API. The public API (`ingest_document`
signature) must not change.

### Low risk: everything else

Remaining extractions (hooks, sync, doctor, service, formatting, remote,
tls, proxy, enable, backfill, scrub) are all within single modules with
fewer call sites. Each can be done in one commit without intermediate
breakage.
