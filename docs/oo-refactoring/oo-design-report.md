# Quarry OO Design Report

> **Quarry OO Refactoring Initiative** — all documents: [design report](oo-design-report.md) · [design review](oo-design-review.md) · [pattern review](oo-design-pattern-review.md) · [execution plan](oo-refactoring-plan.md) · [package structure](oo-package-structure.md) · [package structure review](oo-package-structure-review.md)

Generated: 2026-05-13. **Historical reference — do not edit.**

This document describes the proposed target class structure as of 2026-05-13.
It is the input to the execution plan (`oo-refactoring-plan.md`), not a live
status tracker. Classes described here may have been implemented, modified,
or superseded during execution. For current status, see `oo-refactoring-plan.md`
and `resume.md`.

This is document 1 of 3 in the quarry OO redesign:

1. **This document** — proposes the target class structure for every module
2. `oo-design-review.md` — peer review with revisions
3. `oo-refactoring-plan.md` — step-by-step execution plan

## Calibration

| Metric | merchants/game (reference) | quarry (current) | quarry (target) |
|--------|---------------------------|-------------------|-----------------|
| Total LOC | ~2,000 | 15,635 | ~12,000 |
| Modules | 14 | 42 | ~55 |
| Max module LOC | 363 | 2,008 | <500 |
| Classes | ~20 | 44 | ~90 |
| Top-level functions | ~0 | 394 | <30 (pure utilities only) |
| Methods | all | 47 | ~360 |
| method_ratio | ~1.0 | 0.08 | >=0.80 |

Note: Per-section "New classes" counts are proposed additions. The 44
current classes are spread across existing modules.

In merchants/game, every domain noun is a class. `Game` is a Facade.
`Captain` owns cargo, gold, glory. `Deck` owns cards and discard. No
module-level business logic. The ratio is inverted in quarry: 394
top-level functions, 47 methods. The target is to flip this.

## Coverage

396 functions across 42 modules. 392 accounted for in this report.
4 trivial omissions: `__init__.__getattr__` (lazy import),
`__main__.list_callback` and `remote_callback` (Typer stubs),
`logging_config.configure_logging` (1-function utility).

---

## Core Data Layer -- OO Refactor Analysis

Section owner: rmh (Raymond H)

Modules: `database.py`, `embeddings.py`, `chunker.py`, `config.py`,
`models.py`, `types.py`, `results.py`, `collections.py`

---

## Module: database.py (925 lines)

```text
Current: 0 classes, 28 top-level functions
Domain nouns: ChunkStore, SearchEngine, SchemaManager, TableOptimizer
Shared state: 22 of 28 functions take `db: LanceDB` as their first argument
```

This is the single worst OO violation in the core layer. Twenty-two
functions pass the same `LanceDB` handle. They are methods waiting for a
class -- but they serve at least four distinct responsibilities:

1. **Schema lifecycle** -- table creation, migration, FTS index creation
2. **Chunk CRUD** -- insert, batch insert, delete document, delete collection
3. **Search** -- vector search, hybrid search, RRF fusion, temporal decay
4. **Catalog queries** -- list documents, list collections, count chunks, get page text
5. **Storage maintenance** -- optimize, count fragments, discover databases
6. **Utility** -- format_size, dir_size_bytes (no LanceDB dependency at all)

### Proposed classes

```text
ChunkStore
  Module: src/quarry/chunk_store.py
  Responsibility: Insert, delete, and count chunks in a LanceDB table.
  Owns: _db (LanceDB), _table_lock (threading.Lock)
  Public interface:
    ChunkStore(db: LanceDB)  [__new__]
    insert(chunks, vectors) -> int
    batch_insert(batch) -> int
    delete_document(document_name, collection, *, count) -> int
    delete_collection(collection) -> int
    count(collection_filter) -> int
    @property db -> LanceDB
  Absorbs:
    insert_chunks -> insert
    batch_insert_chunks -> batch_insert
    delete_document -> delete_document
    delete_collection -> delete_collection
    count_chunks -> count
    _get_or_create_table -> _get_or_create_table (private)
    _try_open_table -> _try_open_table (private)
  Dependencies: LanceDB, LanceTable (protocols), Chunk, pyarrow, numpy, threading
  Estimated LOC: 180
```

```text
ChunkSearch
  Module: src/quarry/chunk_search.py
  Responsibility: Execute vector, full-text, and hybrid search with RRF fusion.
  Owns: _db (LanceDB)
  Public interface:
    ChunkSearch(db: LanceDB)  [__new__]
    vector_search(query_vector, limit, **filters) -> list[SearchResult]
    hybrid_search(query_text, query_vector, limit, **filters, decay_rate) -> list[SearchResult]
  Absorbs:
    search -> vector_search
    hybrid_search -> hybrid_search
    _build_predicates -> _build_predicates (private)
    _fuse_rrf -> _fuse_rrf (private)
    _temporal_weight -> _temporal_weight (private)
    _row_key -> _row_key (private)
    _escape_sql -> _escape_sql (private, shared with ChunkStore via module-level or classmethod)
  Dependencies: LanceDB (protocol), SearchResult, defaultdict, math, datetime
  Estimated LOC: 200

  Note: hybrid_search currently takes 11 parameters (db + 10 filters).
  The six filter parameters (document, collection, page_type, source_format,
  agent_handle, memory_type) should be bundled into a SearchFilter dataclass
  (see below). This brings hybrid_search down to 5 params: self, query_text,
  query_vector, limit, filters.
```

```text
SearchFilter
  Module: src/quarry/results.py (add to existing results module)
  Responsibility: Bundle optional search filter parameters.
  Owns: document, collection, page_type, source_format, agent_handle, memory_type
  Public interface: all fields optional (None = no filter), to_predicate() -> str | None
  Absorbs: _build_predicates logic (the function that builds SQL WHERE clauses)
  Dependencies: none
  Estimated LOC: 40

  This is a frozen dataclass -- pure value object. All fields default to None.
  The to_predicate() method replaces the current _build_predicates top-level
  function, giving the filter knowledge of its own SQL serialization.
```

```text
ChunkCatalog
  Module: src/quarry/chunk_catalog.py
  Responsibility: Read-only catalog queries over indexed documents and collections.
  Owns: _db (LanceDB)
  Public interface:
    ChunkCatalog(db: LanceDB)  [__new__]
    list_documents(collection_filter) -> list[DocumentSummary]
    list_collections() -> list[CollectionSummary]
    get_page_text(document_name, page_number, collection) -> str | None
  Absorbs:
    list_documents -> list_documents
    list_collections -> list_collections
    get_page_text -> get_page_text
  Dependencies: LanceDB (protocol), DocumentSummary, CollectionSummary
  Estimated LOC: 120
```

```text
SchemaManager
  Module: src/quarry/schema.py
  Responsibility: LanceDB table schema definition, migration, and FTS index creation.
  Owns: (stateless -- operates on a LanceTable passed to methods)
  Public interface:
    schema(embedding_dimension) -> pa.Schema  [classmethod or module-level constant]
    ensure(db: LanceDB) -> None
    migrate(table: LanceTable) -> None
    ensure_fts_index(table: LanceTable) -> None
  Absorbs:
    _schema -> schema (classmethod or constant)
    _migrate_schema -> migrate
    _ensure_fts_index -> ensure_fts_index
    ensure_schema -> ensure
    _MIGRATION_COLUMNS -> _MIGRATION_COLUMNS (class constant)
    TABLE_NAME -> TABLE_NAME (class constant or module constant)
  Dependencies: LanceDB, LanceTable (protocols), pyarrow
  Estimated LOC: 80

  Note: SchemaManager is borderline -- it could remain a set of module-level
  functions in schema.py since it has no persistent state. However, grouping
  the schema constant, migration columns, and migration logic into a class
  gives it a clear identity and keeps _MIGRATION_COLUMNS private. The class
  is small enough to satisfy PY-OO-2 easily.
```

```text
TableOptimizer
  Module: src/quarry/optimizer.py
  Responsibility: Compact table fragments, rebuild FTS index, prune versions.
  Owns: _db (LanceDB)
  Public interface:
    TableOptimizer(db: LanceDB)  [__new__]
    optimize(*, force: bool) -> None
    count_fragments() -> int
    create_collection_index() -> None
  Absorbs:
    optimize_table -> optimize
    count_fragments -> count_fragments
    create_collection_index -> create_collection_index
    FRAGMENT_THRESHOLD -> FRAGMENT_THRESHOLD (class constant)
  Dependencies: LanceDB (protocol), pathlib
  Estimated LOC: 90
```

```text
StorageInfo (free functions -> module)
  Module: src/quarry/storage.py
  Responsibility: Filesystem size measurement and database discovery.
  Public interface:
    format_size(size_bytes) -> str
    dir_size_bytes(path) -> int
    discover_databases(root) -> list[DatabaseSummary]
  Absorbs:
    format_size
    dir_size_bytes
    discover_databases
  Dependencies: pathlib, subprocess, DatabaseSummary
  Estimated LOC: 70

  These three functions have zero LanceDB dependency (discover_databases calls
  get_db and list_documents, but only as a consumer). They are utility functions
  that don't share state -- a class adds no value. Extracting them to their
  own module clears them out of database.py and keeps storage.py under 100
  lines. discover_databases will import ChunkCatalog and ChunkStore at call
  time to avoid circular dependencies.
```

### Function disposition table

| Current function | Target class | Target method |
|---|---|---|
| `_escape_sql` | `ChunkSearch` | `_escape_sql` (also used by `SearchFilter.to_predicate`) |
| `_schema` | `SchemaManager` | `schema` (classmethod) |
| `_MIGRATION_COLUMNS` | `SchemaManager` | `_MIGRATION_COLUMNS` (class attr) |
| `_migrate_schema` | `SchemaManager` | `migrate` |
| `_ensure_fts_index` | `SchemaManager` | `ensure_fts_index` |
| `ensure_schema` | `SchemaManager` | `ensure` |
| `get_db` | remains module-level in `chunk_store.py` | `get_db` (factory, not a method) |
| `_try_open_table` | `ChunkStore` | `_try_open_table` |
| `_get_or_create_table` | `ChunkStore` | `_get_or_create_table` |
| `insert_chunks` | `ChunkStore` | `insert` |
| `batch_insert_chunks` | `ChunkStore` | `batch_insert` |
| `search` | `ChunkSearch` | `vector_search` |
| `_build_predicates` | `SearchFilter` | `to_predicate` |
| `_temporal_weight` | `ChunkSearch` | `_temporal_weight` |
| `_row_key` | `ChunkSearch` | `_row_key` |
| `_fuse_rrf` | `ChunkSearch` | `_fuse_rrf` |
| `hybrid_search` | `ChunkSearch` | `hybrid_search` |
| `get_page_text` | `ChunkCatalog` | `get_page_text` |
| `list_documents` | `ChunkCatalog` | `list_documents` |
| `count_chunks` | `ChunkStore` | `count` |
| `delete_document` | `ChunkStore` | `delete_document` |
| `list_collections` | `ChunkCatalog` | `list_collections` |
| `delete_collection` | `ChunkStore` | `delete_collection` |
| `create_collection_index` | `TableOptimizer` | `create_collection_index` |
| `FRAGMENT_THRESHOLD` | `TableOptimizer` | `FRAGMENT_THRESHOLD` (class attr) |
| `count_fragments` | `TableOptimizer` | `count_fragments` |
| `optimize_table` | `TableOptimizer` | `optimize` |
| `format_size` | `storage.py` | `format_size` (free function) |
| `dir_size_bytes` | `storage.py` | `dir_size_bytes` (free function) |
| `discover_databases` | `storage.py` | `discover_databases` (free function) |

### Shared helper: _escape_sql

Used by both `ChunkSearch._build_predicates` (via `SearchFilter.to_predicate`)
and `ChunkStore` (in delete predicates) and `ChunkCatalog` (in get_page_text).
Options: (a) module-level function in a shared `_sql.py` private module,
(b) method on `SearchFilter` only, with `ChunkStore`/`ChunkCatalog` using
it via import. Option (a) is cleanest -- a 5-line `_sql.py` module with
`escape_sql` is not worth a class.

### Pattern triggers (PY-OO-6)

| Trigger | Class | Pattern |
|---|---|---|
| Single entry point to LanceDB subsystem | `ChunkStore` | Facade (PY-DP-10) -- `ChunkStore` + `ChunkSearch` + `ChunkCatalog` could be composed behind a `Database` facade if callers want a single entry point |
| Object caching for connection | `get_db` | Flyweight is not applicable here; LanceDB connections are mutable. A simple factory function remains correct. |

---

## Module: embeddings.py (265 lines)

```text
Current: 1 class (OnnxEmbeddingBackend), 3 top-level functions
Domain nouns: EmbeddingBackend (already a class)
Shared state: The 3 functions (download_model_files, _load_model_files,
              _load_local_model_files) are model-loading utilities consumed
              only by OnnxEmbeddingBackend.__init__
```

This module is the best-structured in the core layer. `OnnxEmbeddingBackend`
already encapsulates the ONNX session, tokenizer, and embedding logic. The
three top-level functions are construction helpers.

### Issues to fix

1. **`__init__` instead of `__new__`** (PY-CC-1). The constructor is `__init__`,
   not `__new__`. Must convert.

2. **Public attributes via `__init__`**. `self._dimension`, `self._tokenizer`,
   `self._session` are set in `__init__`. These are already underscore-prefixed
   (good), but the assignment happens in `__init__` not `__new__`.

3. **Constructor is 90 lines** with complex fallback logic (CUDA -> CPU).
   Extract `_create_session` as a private method or classmethod factory.

4. **Top-level functions** `download_model_files`, `_load_model_files`,
   `_load_local_model_files` -- these could become classmethods on
   `OnnxEmbeddingBackend` (`download_model_files` as a public classmethod,
   the others as private classmethods). This is a judgment call: they are
   construction-only helpers, so classmethods are natural. But
   `download_model_files` is also called standalone by `quarry install`,
   so keeping it importable as a free function has value. Either way works;
   classmethods are slightly cleaner per PY-CC-5.

### Proposed classes

```text
OnnxEmbeddingBackend (refactored in place)
  Module: src/quarry/embeddings.py
  Responsibility: ONNX Runtime text embedding with provider fallback.
  Owns: _dimension (int), _tokenizer (Tokenizer), _session (InferenceSession)
  Public interface:
    OnnxEmbeddingBackend()  [__new__, replaces __init__]
    @property dimension -> int
    @property model_name -> str
    embed_texts(texts) -> NDArray[np.float32]
    embed_query(query) -> NDArray[np.float32]
    @classmethod download_model_files(model_file) -> tuple[str, str]
  Absorbs:
    download_model_files -> @classmethod download_model_files
    _load_model_files -> _load_model_files (private classmethod)
    _load_local_model_files -> _load_local_model_files (private classmethod)
  Dependencies: onnxruntime, tokenizers, numpy, quarry.config, quarry.provider
  Estimated LOC: 265 (same module, restructured)
```

No new modules needed. The module stays under 300 lines.

---

## Module: chunker.py (114 lines)

```text
Current: 0 classes, 2 top-level functions
Domain nouns: Chunker (the act of splitting text into chunks)
Shared state: None -- chunk_pages and _split_text are pure functions
```

These are pure functions with no shared state. `chunk_pages` takes a list of
`PageContent` and produces a list of `Chunk`. `_split_text` is a helper.

### Assessment

Per PY-OO-1, functions operating on the same data structure should be a class.
But these functions don't repeatedly pass the same handle around -- they are
stateless transformations. `chunk_pages` has 8 parameters, which violates
PY-OO-3 (max 4 positional params), but the fix is a parameter object, not
a class wrapping these functions.

### Proposed classes

```text
ChunkConfig
  Module: src/quarry/chunker.py (or src/quarry/models.py)
  Responsibility: Bundle chunking parameters.
  Owns: max_chars, overlap_chars, collection, source_format,
        agent_handle, memory_type, summary
  Public interface: frozen dataclass, all fields
  Absorbs: the 5 "metadata" params of chunk_pages (collection, source_format,
           agent_handle, memory_type, summary)
  Dependencies: none
  Estimated LOC: 15

  chunk_pages then becomes:
    chunk_pages(pages: list[PageContent], config: ChunkConfig) -> list[Chunk]
  reducing from 8 params to 2.
```

The two functions remain as module-level functions. `_split_text` is a pure
text algorithm with no domain state -- a class adds no value. The module
stays at ~115 lines.

---

## Module: config.py (102 lines)

```text
Current: 1 class (Settings via pydantic BaseSettings), 4 top-level functions
Domain nouns: Settings (already a class), ConfigFile (read/write default db)
Shared state: read_default_db and write_default_db share _CONFIG_PATH
```

### Issues

1. `Settings` inherits from `pydantic_settings.BaseSettings`, which uses
   `__init__`. This is an acceptable exception -- pydantic owns the
   construction protocol. Not a PY-CC-1 violation.

2. `resolve_db_paths` takes a `Settings` and returns a modified copy.
   This is a method waiting for a class (PY-OO-5) -- it reads `settings.lancedb_path`,
   `settings.quarry_root` and returns `settings.model_copy(update=...)`.
   It should be a method on `Settings` itself.

3. `read_default_db` and `write_default_db` share `_CONFIG_PATH` and operate
   on the same TOML file. These are methods on a `ConfigFile` or can just
   become methods on `Settings` via classmethods.

4. `load_settings` is a trivial factory -- fine as a free function, or could
   become `Settings.load()` classmethod.

### Proposed classes

```text
Settings (refactored in place)
  Module: src/quarry/config.py
  Responsibility: Application settings with path resolution and persistence.
  Owns: (pydantic fields -- quarry_root, lancedb_path, registry_path, etc.)
  Public interface:
    Settings()  [pydantic __init__ -- exempted from PY-CC-1]
    @classmethod load() -> Settings  [absorbs load_settings]
    resolve_db_paths(db_name) -> Settings  [absorbs resolve_db_paths]
    @classmethod read_default_db() -> str | None  [absorbs read_default_db]
    @classmethod write_default_db(name) -> None  [absorbs write_default_db]
  Absorbs:
    resolve_db_paths -> resolve_db_paths (method)
    read_default_db -> @classmethod read_default_db
    write_default_db -> @classmethod write_default_db
    load_settings -> @classmethod load
  Dependencies: pydantic_settings, pathlib, tomllib
  Estimated LOC: 102 (same size, restructured)
```

The module-level constants (`ONNX_MODEL_REPO`, `ONNX_MODEL_REVISION`, etc.)
stay as module-level constants -- they are configuration values, not state.
`_DEFAULT_LANCEDB` and `_CONFIG_PATH` become class-level private constants
on `Settings`.

---

## Module: models.py (74 lines)

```text
Current: 4 classes (PageType enum, PageAnalysis, PageContent, Chunk --
         all frozen dataclasses), 1 function
Domain nouns: PageType, PageAnalysis, PageContent, Chunk
Shared state: None
```

This module is well-structured. All classes are frozen dataclasses (PY-CC-6
compliant). `PageType` is an enum. The one function `stored_page_type` maps
`PageType` to its stored string representation.

### Issues

1. `stored_page_type` is a free function that operates solely on a `PageType`
   value. Per PY-OO-5, it should be a method on `PageType`. Enums support
   methods.

2. Dataclasses lack `slots=True` (PY-CC-6 requires both `frozen=True` and
   `slots=True`).

### Proposed changes

```text
PageType (refactored in place)
  Module: src/quarry/models.py
  Change: Add stored_page_type as a method:
    @property
    def stored(self) -> str:
        """Return the string stored in LanceDB for this page type."""
        ...
  Absorbs: stored_page_type -> PageType.stored property
```

```text
PageAnalysis, PageContent, Chunk
  Module: src/quarry/models.py
  Change: Add slots=True to all three @dataclass decorators:
    @dataclass(frozen=True, slots=True)
```

No new modules. Module stays at ~74 lines.

---

## Module: types.py (117 lines)

```text
Current: 6 Protocol classes (LanceTable, LanceQuery, ListTablesResult,
         LanceDB, OcrBackend, EmbeddingBackend), 0 functions
Domain nouns: All are protocol definitions for structural typing
Shared state: None
```

This module is correctly structured per PY-IC-9 (types and protocols in
their own module) and PY-TS-6 (Protocol for structural interfaces). No
changes needed to the class structure.

### Issues

1. Module is 117 lines with 6 classes -- exceeds PY-OO-2's guideline of
   2-3 classes per module. However, these are all Protocol stubs (no
   implementation), so they are tightly cohesive by purpose. Splitting
   would create modules with 20 lines each, which is worse.

2. `OcrBackend` and `EmbeddingBackend` are domain protocols; `LanceTable`,
   `LanceQuery`, `ListTablesResult`, `LanceDB` are infrastructure protocols.
   A split into `types_infra.py` / `types_domain.py` is defensible but not
   urgent -- the current grouping with section comments works.

### Proposed changes

No structural changes. The module is compliant.

If the class count bothers the scorer, split into:

- `src/quarry/types.py` -- domain protocols (`OcrBackend`, `EmbeddingBackend`)
- `src/quarry/_lance_types.py` -- infrastructure protocols (`LanceDB`, `LanceTable`, `LanceQuery`, `ListTablesResult`)

This is optional. The current structure is defensible.

---

## Module: results.py (91 lines)

```text
Current: 6 TypedDict classes (IngestResult, SearchResult, DocumentSummary,
         CollectionSummary, SitemapResult, DatabaseSummary), 0 functions
Domain nouns: All are result/transfer types
Shared state: None
```

These are data transfer objects at serialization boundaries -- TypedDict is
the correct choice per PY-OO-4 ("TypedDict IS appropriate for
snapshot/memento data, for kwargs typing, for serialization boundaries").
They cross the HTTP API and JSON response boundary.

### Issues

1. Six TypedDicts in one module exceeds the 2-3 classes guideline (PY-OO-2).
   Same argument as `types.py` -- these are pure data definitions with zero
   behavior, tightly related by purpose.

2. `SearchFilter` (proposed above) should be added here as a frozen dataclass,
   not a TypedDict, because it has behavior (`to_predicate`).

### Proposed changes

Add `SearchFilter` dataclass:

```text
SearchFilter
  Module: src/quarry/results.py
  Responsibility: Bundle optional search filter parameters with SQL serialization.
  Owns: document (str | None), collection (str | None), page_type (str | None),
        source_format (str | None), agent_handle (str | None),
        memory_type (str | None)
  Public interface:
    @dataclass(frozen=True, slots=True)
    to_predicate() -> str | None
  Dependencies: none (uses _escape_sql from _sql.py or inline)
  Estimated LOC: 40
```

No other changes. The existing TypedDicts are correctly typed transfer objects.

---

## Module: collections.py (51 lines)

```text
Current: 0 classes, 2 top-level functions
Domain nouns: CollectionName (a validated string value)
Shared state: None
```

Two pure validation functions: `derive_collection` (derive from path or
explicit name) and `validate_collection_name` (strip, reject empty/quotes).

### Assessment

These could become a `CollectionName` value class that validates on
construction (PY-CC-2: establish all invariants in the constructor). A
`CollectionName` wrapping a `str` that rejects invalid values is the
textbook value-object pattern. But the cost-benefit is marginal for 50
lines of straightforward validation.

### Proposed classes

```text
CollectionName
  Module: src/quarry/collections.py
  Responsibility: Validated, immutable collection name string.
  Owns: _value (str)
  Public interface:
    CollectionName(name: str)  [__new__, validates]
    CollectionName.from_path(file_path, explicit) -> CollectionName  [classmethod]
    @property value -> str
    __str__ -> str (returns the name)
    __eq__, __hash__ (value equality)
  Absorbs:
    validate_collection_name -> __new__ validation logic
    derive_collection -> @classmethod from_path
  Dependencies: pathlib
  Estimated LOC: 45
```

This is a clean application of PY-OO-1 (domain noun with data + behavior)
and PY-CC-2 (invariants in constructor). The name string is validated once
at construction; downstream code passes `CollectionName` objects instead of
raw strings, eliminating the need for repeated validation.

---

## New module summary

| New module | Classes | Estimated LOC | Source |
|---|---|---|---|
| `src/quarry/chunk_store.py` | `ChunkStore` | 180 | database.py functions |
| `src/quarry/chunk_search.py` | `ChunkSearch` | 200 | database.py functions |
| `src/quarry/chunk_catalog.py` | `ChunkCatalog` | 120 | database.py functions |
| `src/quarry/schema.py` | `SchemaManager` | 80 | database.py functions |
| `src/quarry/optimizer.py` | `TableOptimizer` | 90 | database.py functions |
| `src/quarry/storage.py` | (free functions) | 70 | database.py functions |
| `src/quarry/_sql.py` | (free function) | 10 | database.py `_escape_sql` |

| Modified module | Changes | LOC delta |
|---|---|---|
| `src/quarry/embeddings.py` | `__init__` -> `__new__`, functions -> classmethods | ~0 |
| `src/quarry/config.py` | functions -> methods on Settings | ~0 |
| `src/quarry/models.py` | `stored_page_type` -> PageType.stored property, add `slots=True` | -3 |
| `src/quarry/results.py` | Add `SearchFilter` dataclass | +40 |
| `src/quarry/collections.py` | functions -> `CollectionName` class | ~0 |
| `src/quarry/types.py` | No changes | 0 |
| `src/quarry/database.py` | **Deleted** -- all content migrated to new modules | -925 |

### Net effect

- **Before**: 8 modules, 12 classes, 40 top-level functions, 1739 total LOC
- **After**: 13 modules (database.py deleted, 7 new), 18 classes, ~5 top-level functions, ~1770 total LOC
- Largest module: `chunk_search.py` at ~200 lines (well under 300)
- Every module has 1-2 classes (PY-OO-2 compliant)
- All 22 `db`-parameter functions become methods (method_ratio improvement)
- `hybrid_search` drops from 11 params to 5 (PY-OO-3 compliant via SearchFilter)

### Migration order

1. Extract `_sql.py` (zero callers change -- internal helper)
2. Extract `schema.py` with `SchemaManager` (used by ChunkStore and TableOptimizer)
3. Extract `results.py` addition: `SearchFilter`
4. Extract `chunk_store.py` with `ChunkStore` (biggest consumer of database.py)
5. Extract `chunk_search.py` with `ChunkSearch`
6. Extract `chunk_catalog.py` with `ChunkCatalog`
7. Extract `optimizer.py` with `TableOptimizer`
8. Extract `storage.py` (free functions)
9. Delete `database.py`
10. Refactor `embeddings.py` in place (`__init__` -> `__new__`)
11. Refactor `config.py` in place (functions -> methods)
12. Refactor `models.py` in place (stored_page_type -> property, add slots)
13. Refactor `collections.py` in place (functions -> CollectionName class)

Each step is one commit. Tests must pass at every step.

### Callers to update

`database.py` is imported by: `pipeline.py`, `http_server.py`, `mcp_server.py`,
`__main__.py`, `hooks.py`, `sync.py`, `doctor.py`, and tests. Every call site
must be updated to use the new class-based API. The migration order above is
chosen to minimize intermediate breakage -- `get_db` stays as a module-level
factory function in `chunk_store.py`, preserving the simplest import path
for callers.

---

## Ingestion Pipeline: OO Design Report

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

```text
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

```text
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

```text
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

```text
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

```text
Current: 0 classes, 3 top-level functions
Domain nouns: code file, language grammar, tree-sitter parser, code section
Shared state: language string derived from extension, threaded through functions
```

**Proposed classes:**

```text
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

```text
Current: 0 classes, 6 top-level functions
Domain nouns: HTML document, boilerplate, markdown conversion
Shared state: BeautifulSoup object passed between _strip_boilerplate, _extract_title
```

**Proposed classes:**

```text
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

```text
Current: 0 classes, 6 top-level functions
Domain nouns: presentation, slide, shape, table, speaker notes
Shared state: pptx Slide object passed between _extract_shapes, _extract_notes,
  _extract_slide_text
```

**Proposed classes:**

```text
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

```text
Current: 0 classes, 4 top-level functions
Domain nouns: spreadsheet, worksheet/sheet, row group, CSV file
Shared state: (headers, rows) tuples passed between _read_xlsx/_read_csv
  and _split_rows_to_sections
```

**Proposed classes:**

```text
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

```text
Current: 0 classes, 1 top-level function
Domain nouns: PDF page analysis, text/image classification
Shared state: none (pure function)
```

This module is small and cohesive. The single function `analyze_pdf`
is a pure transform. It does not need to become a class.

**Proposed: absorb into PdfExtractor.**

```text
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

```text
Current: 1 class (ImageAnalysis dataclass), 1 top-level function
Domain nouns: image analysis, format detection, conversion requirement
Shared state: none (pure function + value object)
```

`ImageAnalysis` is a well-designed frozen dataclass. `analyze_image`
is a pure function. Absorb into ImageExtractor.

**Proposed: absorb into ImageExtractor.**

```text
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

```text
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

```text
Module: src/quarry/pdf_text_extractor.py (renamed from text_extractor.py)
Responsibility: Extract text from text-classified PDF pages via PyMuPDF
Public interface: extract_text_pages(pdf_path, page_numbers, total_pages, *, document_name)
No class needed: single pure function, 60 lines, no shared state.
```

---

### Module: latex_utils.py (57 lines)

```text
Current: 0 classes, 2 top-level functions
Domain nouns: LaTeX escaping, LaTeX table rendering
Shared state: none (pure functions + compiled translation table)
```

Two pure utility functions. No class needed. This module is well-sized,
cohesive, and correctly structured.

**Proposed: no change.**

---

### Module: ocr_local.py (188 lines)

```text
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

```text
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

```text
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

```text
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

```text
Current: 0 classes, 3 top-level functions
Domain nouns: backend factory, backend cache
Shared state: module-level _ocr_cache, _embedding_cache, _lock
```

This is a backend factory with thread-safe caching. The module-level
cache dicts and lock are shared mutable state -- a class would
encapsulate this properly. This is a Singleton factory (PY-DP-7 trigger).

**Proposed classes:**

```text
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

```text
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

```text
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

```text
src/quarry/image_preparer.py

Contents:
  ImagePreparer
    prepare_bytes(image_path, *, needs_conversion, max_bytes) -> bytes
    _encode_to_fit(img, out_fmt, save_kw, max_bytes, name) -> bytes

Estimated LOC: 100
```

---

## New Module: url_fetcher.py

```text
src/quarry/url_fetcher.py

Contents:
  UrlFetcher
    fetch(url, *, timeout) -> str

Estimated LOC: 50
```

---

## New Module: url_ingester.py

```text
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

```text
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

**New classes (13):**

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

```text
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

---

## Surfaces and Services: OO Design Report

Covers CLI, HTTP API, MCP server, hooks, formatting, sync, system service,
doctor, remote config, TLS, proxy, enable/disable, backfill, artifacts,
scrubbing, hook framework, logging, and provider detection.

---

## Module 1: `__main__.py` (2008 lines)

```text
Module: __main__.py (2008 lines)
Current: 2 classes (_OrderedGroup, RemoteError), 47 top-level functions
Domain nouns: CLI application, remote HTTPS client, command output (JSON/text),
              progress reporter, settings resolver, proxy config reader
Shared state: _json_output, _verbose, _quiet, _global_db (module globals);
              proxy_config dict threaded through every remote command;
              settings + db resolved identically in every local command
```

This is the single worst module in the codebase. 2008 lines, 47 top-level
functions, four mutable module globals. Every command that supports remote
mode duplicates the same proxy-config-check-then-branch pattern. Every local
command duplicates the settings-resolve-then-get-db pattern.

Per PL-PA-3 (Commands Layer), CLI commands that orchestrate multiple core
calls should extract to a `commands/` package. The CLI module should be thin:
argument parsing and delegation.

### Target structure

The 2008-line monolith splits into a thin CLI shell plus a `commands/`
package, a remote HTTP client class, and a CLI context object.

#### Class: `CliContext`

```text
CliContext
  Module: src/quarry/cli_context.py
  Responsibility: Hold resolved CLI state (output mode, verbosity, database name)
  Owns: _json_output, _verbose, _quiet, _global_db
  Public interface:
    emit(data, text) -> None
    progress(label) -> ContextManager
    resolved_settings(db="") -> Settings
    is_remote() -> bool
    proxy_config() -> dict | None
  Absorbs: _emit, _progress, _resolved_settings, _safe_proxy_config, main_callback (state-setting portion)
  Dependencies: quarry.config, quarry.remote, rich.console, rich.progress
  Estimated LOC: ~120
```

#### Class: `RemoteClient`

```text
RemoteClient
  Module: src/quarry/remote_client.py
  Responsibility: Make authenticated HTTPS requests to a remote quarry server
  Owns: _config (proxy config dict)
  Public interface:
    request(method, path, body=None, timeout=15.0) -> dict
    get(path) -> dict
  Absorbs: _remote_https_request, _remote_https_get, RemoteError
  Dependencies: http.client, ssl, json, urllib.parse, quarry.remote (ws_to_http)
  Estimated LOC: ~130
```

#### Package: `commands/`

Each command function takes a `CliContext` and the parsed arguments, returns
structured data or raises `typer.Exit`. The CLI module becomes pure
argument-declaration boilerplate.

```text
commands/__init__.py
  Estimated LOC: ~10

commands/find.py
  FindCommand (or plain function find_command)
  Absorbs: find_cmd body, _find_remote
  Estimated LOC: ~90

commands/ingest.py
  Absorbs: ingest_cmd body, _exit_on_ingest_failure
  Estimated LOC: ~80

commands/show.py
  Absorbs: show_cmd body
  Estimated LOC: ~60

commands/remember.py
  Absorbs: remember body
  Estimated LOC: ~70

commands/status.py
  Absorbs: status_cmd body
  Estimated LOC: ~50

commands/use.py
  Absorbs: use_cmd body
  Estimated LOC: ~25

commands/delete.py
  Absorbs: delete_cmd body
  Estimated LOC: ~55

commands/register.py
  Absorbs: register body, deregister body
  Estimated LOC: ~70

commands/sync.py
  Absorbs: sync_cmd body, _auto_workers, _format_sync_results
  Estimated LOC: ~80

commands/enable.py
  Absorbs: enable_cmd body, disable_cmd body
  Estimated LOC: ~60

commands/optimize.py
  Absorbs: optimize_cmd body
  Estimated LOC: ~40

commands/backfill.py
  Absorbs: backfill_sessions_cmd body
  Estimated LOC: ~50

commands/login.py
  Absorbs: login_cmd body, logout_cmd body
  Estimated LOC: ~90

commands/remote_list.py
  Absorbs: remote_list_cmd body
  Estimated LOC: ~50

commands/list_resources.py
  Absorbs: list_documents_cmd, list_collections_cmd, list_registrations_cmd,
           list_databases_cmd, _format_registrations, _format_databases
  Estimated LOC: ~120

commands/admin.py
  Absorbs: install, doctor, serve, mcp, version, uninstall bodies
  Estimated LOC: ~60
```

#### Remaining `__main__.py`

```text
__main__.py (thin CLI shell)
  Keeps: typer.Typer() declaration, @app.command decorators, argument
         annotations, _OrderedGroup, _version_callback, _cli_errors,
         hooks_app subcommands, main_callback (stripped to flag parsing)
  Delegates: every command body to commands/<module>
  Estimated LOC: ~400
```

### Function migration table

| Current function | Target location |
|---|---|
| `main_callback` (state portion) | `CliContext.__init__` |
| `_emit` | `CliContext.emit` |
| `_progress` | `CliContext.progress` |
| `_resolved_settings` | `CliContext.resolved_settings` |
| `_safe_proxy_config` | `CliContext.proxy_config` |
| `_cli_errors` | stays in `__main__.py` (decorator) |
| `_version_callback` | stays in `__main__.py` |
| `RemoteError` | `remote_client.py` |
| `_remote_https_request` | `RemoteClient.request` |
| `_remote_https_get` | `RemoteClient.get` |
| `_find_remote` | `commands/find.py` |
| `_exit_on_ingest_failure` | `commands/ingest.py` |
| `find_cmd` body | `commands/find.py` |
| `ingest_cmd` body | `commands/ingest.py` |
| `show_cmd` body | `commands/show.py` |
| `remember` body | `commands/remember.py` |
| `status_cmd` body | `commands/status.py` |
| `use_cmd` body | `commands/use.py` |
| `delete_cmd` body | `commands/delete.py` |
| `register` body | `commands/register.py` |
| `deregister` body | `commands/register.py` |
| `_auto_workers` | `commands/sync.py` |
| `_format_sync_results` | `commands/sync.py` |
| `sync_cmd` body | `commands/sync.py` |
| `enable_cmd` body | `commands/enable.py` |
| `disable_cmd` body | `commands/enable.py` |
| `optimize_cmd` body | `commands/optimize.py` |
| `backfill_sessions_cmd` body | `commands/backfill.py` |
| `login_cmd` body | `commands/login.py` |
| `logout_cmd` body | `commands/login.py` |
| `remote_list_cmd` body | `commands/remote_list.py` |
| `list_documents_cmd` body | `commands/list_resources.py` |
| `list_collections_cmd` body | `commands/list_resources.py` |
| `list_registrations_cmd` body | `commands/list_resources.py` |
| `list_databases_cmd` body | `commands/list_resources.py` |
| `_format_registrations` | `commands/list_resources.py` |
| `_format_databases` | `commands/list_resources.py` |
| `install` body | `commands/admin.py` |
| `doctor` body | `commands/admin.py` |
| `serve` body | `commands/admin.py` |
| `mcp` body | `commands/admin.py` |
| `version` body | `commands/admin.py` |
| `uninstall` body | `commands/admin.py` |
| hook_session_start, hook_post_web_fetch, hook_pre_compact | stay in `__main__.py` (thin) |

---

## Module 2: `http_server.py` (1530 lines)

```text
Module: http_server.py (1530 lines)
Current: 2 classes (TaskState, _QuarryContext), 29 top-level functions
Domain nouns: server context, background task, route handler, auth checker,
              URL validator, body-size checker, app factory, port file
Shared state: _QuarryContext instance shared via app.state.ctx;
              TaskState dict managed through ctx.tasks
```

Four distinct responsibilities: (1) shared server context, (2) background
task lifecycle, (3) route handlers, (4) server startup/shutdown. The 15+ route
handlers are all module-level functions that manually call `_check_auth` and
`_ctx(request)` on every entry.

### Target structure

#### Class: `QuarryContext` (keep, refine)

```text
QuarryContext
  Module: src/quarry/http_server.py (or src/quarry/http_context.py if split further)
  Responsibility: Hold shared server state (settings, db, embedder, API key, CORS)
  Owns: _settings, _api_key, _cors_origins, _start_time
  Public interface:
    db -> LanceDB (cached_property)
    embedder -> EmbeddingBackend (cached_property)
    settings -> Settings (property)
    api_key -> str | None (property)
    cors_origins -> frozenset[str] (property)
    uptime_seconds -> float (property)
  Absorbs: current _QuarryContext (rename to public)
  Dependencies: quarry.config, quarry.database, quarry.backends
  Estimated LOC: ~50
```

#### Class: `TaskManager`

```text
TaskManager
  Module: src/quarry/task_manager.py
  Responsibility: Track background asyncio tasks with TTL-based garbage collection
  Owns: _tasks (dict[str, TaskState]), _task_refs (dict[str, asyncio.Task])
  Public interface:
    begin(kind) -> TaskState
    get(task_id) -> TaskState | None
    on_done(task_id, asyncio_task) -> None
    gc() -> None
  Absorbs: _gc_tasks, _begin_task, _on_task_done, TaskState
  Dependencies: asyncio, time, uuid
  Estimated LOC: ~70
```

#### Module: `routes/` package or `http_routes.py`

Route handlers grouped by resource. Each handler receives `request` and
delegates to core via `_ctx(request)`. The auth check moves to ASGI
middleware or a shared decorator.

```text
routes/__init__.py
  Estimated LOC: ~5

routes/search.py
  Absorbs: _search_route
  Estimated LOC: ~60

routes/documents.py
  Absorbs: _documents_route, _documents_delete_route, _run_delete_document_task, _show_route
  Estimated LOC: ~100

routes/collections.py
  Absorbs: _collections_route, _collections_delete_route, _run_delete_collection_task
  Estimated LOC: ~70

routes/remember.py
  Absorbs: _remember_route, _run_remember_task
  Estimated LOC: ~80

routes/ingest.py
  Absorbs: _ingest_route, _run_ingest_task, _validate_ingest_url
  Estimated LOC: ~100

routes/sync.py
  Absorbs: _sync_route, _run_sync_task
  Estimated LOC: ~70

routes/registrations.py
  Absorbs: _registrations_route, _handle_list_registrations,
           _handle_add_registration, _handle_delete_registration,
           _run_register_task, _run_deregister_task,
           _register_sync, _deregister_sync, _list_registrations_sync,
           _resolve_registration_path, _server_home
  Estimated LOC: ~200

routes/status.py
  Absorbs: _status_route, _health_route, _ca_cert_route,
           _databases_route, _use_route, _task_status_route
  Estimated LOC: ~120

routes/mcp_ws.py
  Absorbs: _mcp_websocket_route
  Estimated LOC: ~40
```

#### Remaining `http_server.py`

```text
http_server.py (app factory + serve)
  Keeps: build_app, serve, _validate_host_key, _write_port_file,
         _remove_port_file, _check_bearer_auth, _check_auth,
         _coerce_bool_field, _check_body_size, CORS/auth constants
  Estimated LOC: ~250
```

### Function migration table

| Current function | Target location |
|---|---|
| `TaskState` | `task_manager.py` |
| `_gc_tasks` | `TaskManager.gc` |
| `_begin_task` | `TaskManager.begin` |
| `_on_task_done` | `TaskManager.on_done` |
| `_QuarryContext` | `QuarryContext` (rename public) |
| `_validate_ingest_url` | `routes/ingest.py` |
| `_coerce_bool_field` | stays in `http_server.py` (shared utility) |
| `_check_body_size` | stays in `http_server.py` (shared utility) |
| `_check_bearer_auth` | stays in `http_server.py` |
| `_ctx` | stays in `http_server.py` |
| `_check_auth` | stays in `http_server.py` |
| `_health_route` | `routes/status.py` |
| `_ca_cert_route` | `routes/status.py` |
| `_search_route` | `routes/search.py` |
| `_documents_route` | `routes/documents.py` |
| `_documents_delete_route` | `routes/documents.py` |
| `_run_delete_document_task` | `routes/documents.py` |
| `_collections_route` | `routes/collections.py` |
| `_collections_delete_route` | `routes/collections.py` |
| `_run_delete_collection_task` | `routes/collections.py` |
| `_show_route` | `routes/documents.py` |
| `_remember_route` | `routes/remember.py` |
| `_run_remember_task` | `routes/remember.py` |
| `_ingest_route` | `routes/ingest.py` |
| `_run_ingest_task` | `routes/ingest.py` |
| `_run_sync_task` | `routes/sync.py` |
| `_sync_route` | `routes/sync.py` |
| `_task_status_route` | `routes/status.py` |
| `_databases_route` | `routes/status.py` |
| `_use_route` | `routes/status.py` |
| `_registrations_route` | `routes/registrations.py` |
| `_handle_list_registrations` | `routes/registrations.py` |
| `_handle_add_registration` | `routes/registrations.py` |
| `_handle_delete_registration` | `routes/registrations.py` |
| `_run_register_task` | `routes/registrations.py` |
| `_run_deregister_task` | `routes/registrations.py` |
| `_register_sync` | `routes/registrations.py` |
| `_deregister_sync` | `routes/registrations.py` |
| `_list_registrations_sync` | `routes/registrations.py` |
| `_resolve_registration_path` | `routes/registrations.py` |
| `_server_home` | `routes/registrations.py` |
| `_status_route` | `routes/status.py` |
| `_mcp_websocket_route` | `routes/mcp_ws.py` |
| `build_app` | stays in `http_server.py` |
| `serve` | stays in `http_server.py` |
| `_validate_host_key` | stays in `http_server.py` |
| `_write_port_file` | stays in `http_server.py` |
| `_remove_port_file` | stays in `http_server.py` |

---

## Module 3: `mcp_server.py` (581 lines)

```text
Module: mcp_server.py (581 lines)
Current: 0 classes, 22 top-level functions
Domain nouns: MCP session, database selector, background task runner,
              tool handler, settings resolver
Shared state: _db_name ContextVar, _executor ThreadPoolExecutor,
              mcp FastMCP instance; _settings() and _db() called
              repeatedly with identical pattern
```

Every tool handler follows the same pattern: resolve settings, get db,
do work (possibly in background), format result. The `_do_*` helper
functions exist only because background execution needs a callable.

The module is close to the 500-line limit. The primary issue is structural:
22 module-level functions with zero classes, violating PY-OO-1.

### Target structure

#### Class: `McpSession`

```text
McpSession
  Module: src/quarry/mcp_server.py
  Responsibility: Hold per-session state and provide tool implementations
  Owns: _db_name (ContextVar still used for session isolation),
        _executor (ThreadPoolExecutor)
  Public interface:
    find(...) -> str
    ingest(...) -> str
    remember(...) -> str
    list_resources(...) -> str
    show(...) -> str
    delete(...) -> str
    register_directory(...) -> str
    deregister_directory(...) -> str
    sync_all_registrations() -> str
    status() -> str
    use_database(...) -> str
  Absorbs: _settings, _db, _background, _handle_errors,
           find, ingest, _do_ingest, remember, _do_remember,
           list_resources, show, delete, _do_delete,
           register_directory, _do_register, deregister_directory,
           _do_deregister, sync_all_registrations, _do_sync,
           status, use_database
  Dependencies: quarry.backends, quarry.collections, quarry.config,
                quarry.database, quarry.formatting, quarry.pipeline,
                quarry.provider, quarry.sync, quarry.sync_registry
  Estimated LOC: ~420
```

The `mcp` FastMCP instance stays at module level (FastMCP requires
module-level tool registration). The `@mcp.tool()` decorators delegate
to `McpSession` methods. `run_mcp_session` and `main` stay as module-level
functions.

#### Remaining module-level

```text
mcp_server.py
  Keeps: mcp = FastMCP(...), @mcp.tool() wrapper functions (thin),
         run_mcp_session, main, _db_name ContextVar
  Estimated LOC: ~160
```

---

## Module 4: `hooks.py` (868 lines)

```text
Module: hooks.py (868 lines)
Current: 0 classes, 23 top-level functions
Domain nouns: session-start handler, web-fetch handler, pre-compact handler,
              sync lock, collection resolver, transcript extractor,
              transcript archiver, background ingest spawner, capture file writer
Shared state: settings resolved via _resolve_settings() in every handler;
              registry connection opened/closed in every handler;
              _collection_for_cwd pattern repeated across handlers
```

Three handlers (session start, web fetch, pre compact) share a common
infrastructure: resolve settings, open registry, find collection for cwd.
Each handler is 100-200 lines with interleaved helpers. The transcript
extraction functions (`extract_message_text`, `extract_transcript_text`,
`_extract_content_texts`, `_extract_tool_result_text`) form a cohesive
cluster that belongs in a separate module.

### Target structure

#### Class: `SessionStartHandler`

```text
SessionStartHandler
  Module: src/quarry/hooks/session_start.py
  Responsibility: Auto-register cwd and launch background sync
  Owns: _settings (resolved lazily)
  Public interface:
    handle(payload) -> dict
  Absorbs: handle_session_start, _sync_in_background, _is_sync_running,
           _acquire_sync_lock, _sync_lockfile, _unique_collection_name,
           _find_registration
  Dependencies: quarry._stdlib, quarry.sync_registry, quarry.config
  Estimated LOC: ~200
```

#### Class: `WebFetchHandler`

```text
WebFetchHandler
  Module: src/quarry/hooks/web_fetch.py
  Responsibility: Auto-ingest URLs from PostToolUse WebFetch events
  Owns: (stateless beyond resolved settings)
  Public interface:
    handle(payload) -> dict
  Absorbs: handle_post_web_fetch, _extract_url, _extract_web_fetch_content,
           _is_already_ingested
  Dependencies: quarry._stdlib, quarry.database, quarry.pipeline,
                quarry.html_processor
  Estimated LOC: ~120
```

#### Class: `PreCompactHandler`

```text
PreCompactHandler
  Module: src/quarry/hooks/pre_compact.py
  Responsibility: Capture conversation transcript before context compaction
  Owns: (stateless beyond resolved settings)
  Public interface:
    handle(payload) -> dict
  Absorbs: handle_pre_compact, _archive_transcript, _spawn_background_ingest,
           _write_capture_file, _read_ethos_agent_handle
  Dependencies: quarry._stdlib, quarry.config, quarry.artifacts, quarry.scrub
  Estimated LOC: ~200
```

#### Module: `hooks/transcript.py`

```text
TranscriptExtractor (or module-level functions — these are pure transformations)
  Module: src/quarry/hooks/transcript.py
  Responsibility: Extract conversation text from Claude Code JSONL transcripts
  Public interface:
    extract_transcript_text(path) -> str
    extract_message_text(record) -> str | None
  Absorbs: extract_transcript_text, extract_message_text,
           _extract_content_texts, _extract_tool_result_text,
           _MAX_TRANSCRIPT_CHARS, _MAX_TOOL_RESULT_CHARS
  Dependencies: json, pathlib (stdlib only)
  Estimated LOC: ~110
```

#### Module: `hooks/collection_resolver.py`

```text
CollectionResolver (or module-level functions)
  Module: src/quarry/hooks/collection_resolver.py
  Responsibility: Resolve the registered collection covering a working directory
  Public interface:
    collection_for_cwd(cwd) -> str | None
    collection_for_cwd_conn(conn, cwd) -> str | None
  Absorbs: _collection_for_cwd, _collection_for_cwd_conn, _resolve_settings
  Dependencies: quarry.config, quarry.sync_registry
  Estimated LOC: ~60
```

#### Package: `hooks/__init__.py`

```text
hooks/__init__.py
  Re-exports: handle_session_start, handle_post_web_fetch, handle_pre_compact,
              extract_transcript_text, extract_message_text,
              _collection_for_cwd, _collection_for_cwd_conn
              (for backwards compatibility during transition)
  Estimated LOC: ~30
```

### Function migration table

| Current function | Target location |
|---|---|
| `_find_registration` | `hooks/session_start.py` |
| `_unique_collection_name` | `hooks/session_start.py` |
| `_resolve_settings` | `hooks/collection_resolver.py` |
| `_sync_lockfile` | `hooks/session_start.py` |
| `_is_sync_running` | `hooks/session_start.py` |
| `_acquire_sync_lock` | `hooks/session_start.py` |
| `_sync_in_background` | `hooks/session_start.py` |
| `handle_session_start` | `hooks/session_start.py` |
| `_collection_for_cwd_conn` | `hooks/collection_resolver.py` |
| `_collection_for_cwd` | `hooks/collection_resolver.py` |
| `_extract_url` | `hooks/web_fetch.py` |
| `_extract_web_fetch_content` | `hooks/web_fetch.py` |
| `_is_already_ingested` | `hooks/web_fetch.py` |
| `handle_post_web_fetch` | `hooks/web_fetch.py` |
| `_read_ethos_agent_handle` | `hooks/pre_compact.py` |
| `_extract_tool_result_text` | `hooks/transcript.py` |
| `_extract_content_texts` | `hooks/transcript.py` |
| `extract_message_text` | `hooks/transcript.py` |
| `extract_transcript_text` | `hooks/transcript.py` |
| `_archive_transcript` | `hooks/pre_compact.py` |
| `_spawn_background_ingest` | `hooks/pre_compact.py` |
| `_write_capture_file` | `hooks/pre_compact.py` |
| `handle_pre_compact` | `hooks/pre_compact.py` |

---

## Module 5: `formatting.py` (405 lines)

```text
Module: formatting.py (405 lines)
Current: 1 class (ColumnSpec), 20 top-level functions
Domain nouns: table renderer, column spec, search result formatter,
              document formatter, status formatter, action summary formatter
Shared state: TABLE_WIDTH constant, _COL_SEP/_HEADER_PREFIX/_ROW_PREFIX constants
```

This module is close to the 500-line limit but internally cohesive. The
`ColumnSpec` dataclass is well-designed. The 20 functions split into three
groups: (1) table rendering engine, (2) data formatters, (3) action summary
formatters. All are pure functions with no shared mutable state.

This is one case where functions are the right abstraction. The formatters
are stateless transformations. Per PY-OO-1, only domain nouns with data
*and* behavior need classes. These formatters have no data.

### Target structure

The module is clean enough to keep as-is. Two refinements:

1. Extract the table rendering engine into a `TableRenderer` class that
   owns the layout constants, making them configurable per instance.
2. Keep the format_* functions as module-level (they are thin wrappers
   around `format_table` or string formatting).

#### Class: `TableRenderer`

```text
TableRenderer
  Module: src/quarry/formatting.py
  Responsibility: Render constrained-width tables with header and data rows
  Owns: _width, _col_sep, _header_prefix, _row_prefix
  Public interface:
    render(specs, rows) -> str
  Absorbs: format_table, _render_rows, _fmt_cell, visible_width
  Dependencies: textwrap (stdlib)
  Estimated LOC: ~100
```

#### Remaining functions

```text
formatting.py
  Keeps: ColumnSpec, truncate, _fmt_size,
         format_search_results, format_documents, format_document_detail,
         format_collections, format_databases, format_registrations,
         format_status, format_ingest_summary, format_sitemap_summary,
         format_sync_summary, format_delete_summary, format_register_summary,
         format_deregister_summary, format_switch_summary
  These call TableRenderer.render() instead of format_table().
  Estimated LOC: ~300
```

---

## Module 6: `sync.py` (660 lines)

```text
Module: sync.py (660 lines)
Current: 2 classes (SyncPlan, SyncResult), 11 top-level functions
Domain nouns: sync plan, sync result, file discoverer, content hasher,
              ignore spec, collection syncer
Shared state: db and conn passed through every function; settings threaded
              through the call chain; plan_to_ingest/to_refresh/to_delete
              operated on by separate functions
```

The two dataclasses are well-designed value objects. The main issue is
`sync_collection` at ~120 lines orchestrating ingest/refresh/delete phases,
and `_ingest_files` at ~80 lines. The module exceeds 500 lines.

### Target structure

#### Class: `CollectionSyncer`

```text
CollectionSyncer
  Module: src/quarry/sync.py
  Responsibility: Sync a single registered directory with LanceDB
  Owns: _directory, _collection, _db, _settings, _conn, _max_workers
  Public interface:
    sync(progress_callback=None) -> SyncResult
  Absorbs: sync_collection, _ingest_files, _refresh_files, _delete_documents
  Dependencies: quarry.config, quarry.database, quarry.pipeline,
                quarry.sync_registry
  Estimated LOC: ~300
```

#### Class: `FileDiscovery`

```text
FileDiscovery
  Module: src/quarry/sync_discovery.py
  Responsibility: Find and filter files under a directory respecting ignore rules
  Owns: _directory, _extensions, _root_spec
  Public interface:
    discover() -> list[Path]
    content_hash(path) -> str  (static/classmethod)
  Absorbs: discover_files, _load_ignore_spec, _read_local_ignore,
           _symlink_inside_root, _content_hash, _DEFAULT_IGNORE_PATTERNS,
           _HASH_CHUNK_SIZE
  Dependencies: pathlib, pathspec, os
  Estimated LOC: ~130
```

#### Remaining module-level

```text
sync.py
  Keeps: SyncPlan, SyncResult, compute_sync_plan, sync_all
  compute_sync_plan uses FileDiscovery.discover() + FileDiscovery.content_hash()
  sync_all instantiates CollectionSyncer per registration
  Estimated LOC: ~200
```

---

## Module 7: `sync_registry.py` (307 lines)

```text
Module: sync_registry.py (307 lines)
Current: 2 classes (DirectoryRegistration, FileRecord), 12 top-level functions
Domain nouns: directory registration, file record, SQLite registry
Shared state: sqlite3.Connection passed as first argument to every function
```

12 functions all take `conn: sqlite3.Connection` as the first argument.
This is the textbook PY-OO-1 violation: functions operating on the same
data structure should be methods on a class.

### Target structure

#### Class: `SyncRegistry`

```text
SyncRegistry
  Module: src/quarry/sync_registry.py
  Responsibility: SQLite-backed registry of directories and file records
  Owns: _conn (sqlite3.Connection)
  Public interface:
    register(directory, collection) -> DirectoryRegistration
    deregister(collection) -> list[str]
    list_registrations() -> list[DirectoryRegistration]
    get_registration(collection) -> DirectoryRegistration | None
    get_file(path) -> FileRecord | None
    upsert_file(record, commit=True) -> None
    list_files(collection) -> list[FileRecord]
    delete_file(path, commit=True) -> None
    commit() -> None
    close() -> None
  Absorbs: open_registry (becomes classmethod/factory), register_directory,
           deregister_directory, list_registrations, get_registration,
           get_file, upsert_file, list_files, delete_file,
           _init_schema, _migrate_schema, _is_ancestor_of
  Dependencies: sqlite3, pathlib, datetime
  Estimated LOC: ~280
```

#### Remaining

```text
sync_registry.py
  Keeps: DirectoryRegistration, FileRecord (frozen dataclasses — value objects)
  Estimated LOC: ~30
```

---

## Module 8: `service.py` (572 lines)

```text
Module: service.py (572 lines)
Current: 0 classes, 17 top-level functions
Domain nouns: launchd service, systemd service, GPU runtime detector,
              TLS hostname resolver, env file, service installer
Shared state: _LABEL, _ENV_FILE, _LAUNCHD_DIR, _SYSTEMD_DIR constants;
              platform detection repeated in install() and uninstall()
```

The module has two platform backends (launchd, systemd) with identical
interfaces (install, uninstall, status) and a GPU runtime manager. Per
PY-IC-8 (Dependency Direction), these backends should implement a common
Protocol.

### Target structure

#### Protocol: `ServiceBackend`

```text
ServiceBackend (Protocol)
  Module: src/quarry/service.py
  Responsibility: Define the interface for platform-specific service management
  Public interface:
    install() -> None
    uninstall() -> None
    status() -> bool
```

#### Class: `LaunchdBackend`

```text
LaunchdBackend
  Module: src/quarry/service.py (or src/quarry/service_launchd.py if split)
  Responsibility: macOS launchd service management
  Owns: _label, _plist_path, _log_dir
  Public interface: implements ServiceBackend
  Absorbs: _launchd_plist_content, _launchd_install, _launchd_uninstall,
           _launchd_status, _LAUNCHD_DIR, _LAUNCHD_PLIST
  Dependencies: subprocess, pathlib, xml.sax.saxutils
  Estimated LOC: ~120
```

#### Class: `SystemdBackend`

```text
SystemdBackend
  Module: src/quarry/service.py (or src/quarry/service_systemd.py if split)
  Responsibility: Linux systemd user service management
  Owns: _unit_path, _env_file_path
  Public interface: implements ServiceBackend
  Absorbs: _systemd_unit_content, _systemd_install, _systemd_uninstall,
           _systemd_status, _systemd_escape, _SYSTEMD_DIR, _SYSTEMD_UNIT,
           _has_linger
  Dependencies: subprocess, pathlib, textwrap
  Estimated LOC: ~120
```

#### Remaining module-level

```text
service.py
  Keeps: detect_platform, install, uninstall (dispatch to backend),
         ensure_gpu_runtime, _write_env_file, _quarry_exec_args,
         _get_tls_hostname, ServiceBackend protocol
  install() and uninstall() select backend via detect_platform()
  Estimated LOC: ~250
```

### Function migration table

| Current function | Target location |
|---|---|
| `_write_env_file` | stays (shared by both backends) |
| `_quarry_exec_args` | stays (shared by both backends) |
| `_launchd_plist_content` | `LaunchdBackend._plist_content` |
| `_launchd_install` | `LaunchdBackend.install` |
| `_launchd_uninstall` | `LaunchdBackend.uninstall` |
| `_launchd_status` | `LaunchdBackend.status` |
| `_systemd_escape` | `SystemdBackend._escape` |
| `_systemd_unit_content` | `SystemdBackend._unit_content` |
| `_systemd_install` | `SystemdBackend.install` |
| `_systemd_uninstall` | `SystemdBackend.uninstall` |
| `_systemd_status` | `SystemdBackend.status` |
| `_has_linger` | `SystemdBackend._has_linger` |
| `ensure_gpu_runtime` | stays |
| `detect_platform` | stays |
| `install` | stays (dispatches to backend) |
| `uninstall` | stays (dispatches to backend) |
| `_get_tls_hostname` | stays |

---

## Module 9: `doctor.py` (1141 lines)

```text
Module: doctor.py (1141 lines)
Current: 1 class (CheckResult), 31 top-level functions
Domain nouns: health check, check result, install wizard, MCP configurator,
              ethos configurator, CLAUDE.md injector
Shared state: CheckResult returned from every _check_* function;
              Settings loaded in check_environment
```

31 top-level functions, 1141 lines. Three distinct responsibilities:
(1) individual health checks (15 `_check_*` functions), (2) install wizard
(`run_install`, 8 steps with their own helpers), (3) ethos/CLAUDE.md
configuration (`_inject_claude_md`, `_configure_ethos_ext`, `_session_context_literal_block`,
`_write_ethos_ext_session_context`, `_scan_identities_dir`, etc.).

### Target structure

#### Class: `HealthChecker`

```text
HealthChecker
  Module: src/quarry/doctor.py
  Responsibility: Run environment health checks and report results
  Owns: _settings (Settings), _results (list[CheckResult])
  Public interface:
    run_all() -> list[CheckResult]
    print_results() -> int  (exit code)
  Absorbs: check_environment, _check_python_version, _check_data_directory,
           _check_embedding_model, _check_local_ocr, _check_provider,
           _check_imports, _check_storage, _check_fts_health,
           _check_sync_health, _check_sync_directories,
           _check_enable_status, _check_orphaned_captures,
           _check_mcp_proxy, _check_claude_code_mcp,
           _check_claude_desktop_mcp, _sync_age_result,
           _quiet_logging, _print_check, _human_size, _quarry_version
  Dependencies: quarry.config, quarry.database, quarry.provider,
                quarry.sync_registry, quarry.hooks
  Estimated LOC: ~450
```

#### Class: `InstallWizard`

```text
InstallWizard
  Module: src/quarry/install.py
  Responsibility: Create data dirs, download model, configure MCP, register daemon
  Owns: (stateless — each step is idempotent)
  Public interface:
    run() -> int  (exit code)
  Absorbs: run_install, _configure_claude_code, _configure_claude_desktop,
           _mcp_fallback_script
  Dependencies: quarry.service, quarry.embeddings, quarry.proxy, HealthChecker
  Estimated LOC: ~180
```

#### Class: `EthosConfigurator`

```text
EthosConfigurator
  Module: src/quarry/ethos_config.py
  Responsibility: Write quarry session context into ethos identity extensions
  Owns: _identities_dir (Path)
  Public interface:
    configure() -> CheckResult
    write_session_context(quarry_yaml, handle) -> str
  Absorbs: _configure_ethos_ext, _write_ethos_ext_session_context,
           _session_context_literal_block, _scan_identities_dir,
           _ethos_ext_message, _SESSION_CONTEXT_TEMPLATE
  Dependencies: yaml, pathlib
  Estimated LOC: ~140
```

#### Module: `claudemd.py`

```text
claudemd.py
  Responsibility: Inject/remove quarry capabilities section in CLAUDE.md
  Public interface:
    inject_claude_md() -> str
  Absorbs: _inject_claude_md, _QUARRY_CLAUDE_MD_SECTION, _QUARRY_SECTION_MARKER
  Dependencies: pathlib (stdlib only)
  Estimated LOC: ~60
```

#### Remaining `doctor.py`

```text
doctor.py
  Keeps: CheckResult (value object), check_environment (thin entry point
         that creates HealthChecker and calls run_all)
  Estimated LOC: ~30
```

### Function migration table

| Current function | Target location |
|---|---|
| `CheckResult` | stays in `doctor.py` (value object) |
| `_quarry_version` | `HealthChecker._version` |
| `_quiet_logging` | `HealthChecker._quiet_logging` |
| `_check_python_version` | `HealthChecker._check_python_version` |
| `_check_data_directory` | `HealthChecker._check_data_directory` |
| `_check_embedding_model` | `HealthChecker._check_embedding_model` |
| `_check_local_ocr` | `HealthChecker._check_local_ocr` |
| `_check_provider` | `HealthChecker._check_provider` |
| `_check_imports` | `HealthChecker._check_imports` |
| `_check_storage` | `HealthChecker._check_storage` |
| `_human_size` | `HealthChecker._human_size` (or stays module-level) |
| `_check_fts_health` | `HealthChecker._check_fts_health` |
| `_sync_age_result` | `HealthChecker._sync_age_result` |
| `_check_sync_health` | `HealthChecker._check_sync_health` |
| `_check_sync_directories` | `HealthChecker._check_sync_directories` |
| `_check_enable_status` | `HealthChecker._check_enable_status` |
| `_check_orphaned_captures` | `HealthChecker._check_orphaned_captures` |
| `_check_mcp_proxy` | `HealthChecker._check_mcp_proxy` |
| `_check_claude_code_mcp` | `HealthChecker._check_claude_code_mcp` |
| `_check_claude_desktop_mcp` | `HealthChecker._check_claude_desktop_mcp` |
| `_print_check` | `HealthChecker._print_check` |
| `check_environment` | thin wrapper calling `HealthChecker` |
| `_mcp_fallback_script` | `install.py` |
| `_configure_claude_code` | `InstallWizard._configure_claude_code` |
| `_configure_claude_desktop` | `InstallWizard._configure_claude_desktop` |
| `run_install` | `InstallWizard.run` |
| `_inject_claude_md` | `claudemd.py` |
| `_SESSION_CONTEXT_TEMPLATE` | `ethos_config.py` |
| `_session_context_literal_block` | `EthosConfigurator._literal_block` |
| `_write_ethos_ext_session_context` | `EthosConfigurator.write_session_context` |
| `_ethos_ext_message` | `EthosConfigurator._message` |
| `_scan_identities_dir` | `EthosConfigurator._scan_identities_dir` |
| `_configure_ethos_ext` | `EthosConfigurator.configure` |

---

## Module 10: `remote.py` (303 lines)

```text
Module: remote.py (303 lines)
Current: 1 class (PermissionWarning), 10 top-level functions
Domain nouns: proxy config, CA certificate, connection validator, token masker
Shared state: MCP_PROXY_CONFIG_PATH, CA_CERT_PATH constants;
              proxy config dict read/written by multiple functions
```

The module is at 303 lines — right at the PY-OO-2 threshold. The functions
cluster around two nouns: proxy config (read/write/delete) and connection
validation (validate, fetch cert, store cert).

### Target structure

#### Class: `ProxyConfig`

```text
ProxyConfig
  Module: src/quarry/remote.py
  Responsibility: Read, write, and delete mcp-proxy TOML configuration
  Owns: _config_path (Path), _ca_cert_path (Path)
  Public interface:
    read() -> dict
    write(url, token, ca_cert_path=None) -> None
    delete() -> bool
    classmethod default() -> ProxyConfig
  Absorbs: read_proxy_config, write_proxy_config, delete_proxy_config,
           _toml_escape, MCP_PROXY_CONFIG_PATH, CA_CERT_PATH
  Dependencies: tomllib, os, re, pathlib
  Estimated LOC: ~150
```

#### Remaining functions

```text
remote.py
  Keeps: PermissionWarning, ws_to_http, validate_connection,
         validate_connection_from_ws_url, mask_token,
         fetch_ca_cert, store_ca_cert
  These are stateless connection utilities — functions are appropriate.
  Estimated LOC: ~150
```

---

## Module 11: `tls.py` (364 lines)

```text
Module: tls.py (364 lines)
Current: 0 classes, 7 top-level functions
Domain nouns: CA certificate, server certificate, certificate fingerprint,
              TLS directory, key pair
Shared state: TLS_DIR constant; CA cert/key bytes passed between
              generate_ca and generate_server_cert
```

The module is cohesive. The functions form a pipeline: generate CA, generate
server cert (signed by CA), write files. The `_signing_public_key` helper
narrows cryptography's type union.

### Target structure

#### Class: `CertificateAuthority`

```text
CertificateAuthority
  Module: src/quarry/tls.py
  Responsibility: Generate and manage a self-signed CA and server certificates
  Owns: _tls_dir (Path)
  Public interface:
    generate_ca() -> tuple[bytes, bytes]
    generate_server_cert(ca_cert_pem, ca_key_pem, hostname) -> tuple[bytes, bytes]
    write_tls_files(hostname) -> bool
    cert_fingerprint(cert_pem) -> str  (staticmethod)
  Absorbs: generate_ca, generate_server_cert, write_tls_files,
           cert_fingerprint, _write_file, _signing_public_key, _now_utc
  Dependencies: cryptography, hashlib, ipaddress, datetime, os, pathlib
  Estimated LOC: ~340
```

#### Remaining

```text
tls.py
  Keeps: TLS_DIR constant (re-exported for backwards compatibility),
         _CERT_VALID_YEARS, _EC_CURVE constants
  Estimated LOC: ~25
```

---

## Module 12: `proxy.py` (166 lines)

```text
Module: proxy.py (166 lines)
Current: 0 classes, 8 top-level functions
Domain nouns: proxy binary, release asset, checksum verifier, installer
Shared state: _REPO, _INSTALL_DIR, _BINARY_NAME constants
```

Small, cohesive module. The functions form a pipeline: detect platform
asset, fetch latest version, download, verify checksum, install. Could
be one class but the module is well under 300 lines.

### Target structure

#### Class: `ProxyInstaller`

```text
ProxyInstaller
  Module: src/quarry/proxy.py
  Responsibility: Download and install the mcp-proxy binary from GitHub
  Owns: _repo, _install_dir, _binary_name
  Public interface:
    install(version=None) -> str
    installed_path() -> str | None  (staticmethod)
  Absorbs: install, installed_path, _asset_name, _latest_version,
           _download_url, _checksums_url, _verify_checksum, _request
  Dependencies: hashlib, platform, shutil, tempfile, urllib.request
  Estimated LOC: ~150
```

---

## Module 13: `enable.py` (367 lines)

```text
Module: enable.py (367 lines)
Current: 2 classes (EnableResult, DisableResult), 7 top-level functions
Domain nouns: project enabler, project disabler, ethos bootstrapper,
              project config writer, CLAUDE.md block manager
Shared state: _GLOBAL_IDENTITIES constant; registry connection opened
              in both enable_project and disable_project
```

The two result dataclasses are clean value objects. The functions split into
two groups: enable (3 functions) and disable (1 function), plus shared
helpers (`_bootstrap_ethos_memory`, `_write_project_config`, `_append_claudemd_block`,
`_remove_claudemd_block`).

### Target structure

#### Class: `ProjectManager`

```text
ProjectManager
  Module: src/quarry/enable.py
  Responsibility: Enable and disable quarry knowledge capture for project directories
  Owns: _directory (Path)
  Public interface:
    enable(collection_override="") -> EnableResult
    disable(keep_data=False) -> DisableResult
  Absorbs: enable_project, disable_project, _resolve_or_register,
           _bootstrap_ethos_memory, _write_project_config,
           _append_claudemd_block, _remove_claudemd_block
  Dependencies: quarry.config, quarry.database, quarry.hooks, quarry.sync_registry,
                quarry.doctor
  Estimated LOC: ~330
```

#### Remaining

```text
enable.py
  Keeps: EnableResult, DisableResult (frozen dataclasses)
  Estimated LOC: ~40
```

---

## Module 14: `backfill.py` (314 lines)

```text
Module: backfill.py (314 lines)
Current: 3 classes (BackfillStats, ProjectMapping, _Accumulator), 10 top-level functions
Domain nouns: backfill stats, project mapping, transcript processor,
              backfill session runner
Shared state: _Accumulator mutated across the processing loop;
              db and settings threaded through functions
```

Well-structured module near the size limit. Three clean dataclasses.
The `_process_project` function takes 8 parameters (including keyword-only)
which signals parameter bloat.

### Target structure

#### Class: `SessionBackfiller`

```text
SessionBackfiller
  Module: src/quarry/backfill.py
  Responsibility: Scan and ingest historical Claude Code session transcripts
  Owns: _settings, _db, _dry_run, _collection_override, _project_filter, _limit
  Public interface:
    run() -> BackfillStats
  Absorbs: backfill_sessions, _process_project, _get_existing_doc_names,
           _count_unregistered_dirs, _write_backfill_capture_file
  Dependencies: quarry.config, quarry.database, quarry.pipeline,
                quarry.hooks, quarry.artifacts, quarry.scrub, quarry.sync_registry
  Estimated LOC: ~200
```

#### Remaining

```text
backfill.py
  Keeps: BackfillStats, ProjectMapping, _Accumulator (value objects),
         encode_project_path, build_project_mappings,
         list_transcript_files, document_name_for_transcript,
         is_already_ingested (pure utility functions)
  Estimated LOC: ~110
```

---

## Module 15: `artifacts.py` (153 lines)

```text
Module: artifacts.py (153 lines)
Current: 1 class (SessionArtifacts), 1 module-level function
Domain nouns: session artifacts, commit SHA, PR number, branch name, bead ID
Shared state: compiled regex patterns (module constants)
```

This module is already well-designed. `SessionArtifacts` is a frozen
dataclass with behavior (`from_text`, `format_header`, `format_frontmatter`).
The module-level aliases (`extract_artifacts`, `format_artifacts_header`,
`format_artifacts_frontmatter`) maintain the pre-refactor API.

### Target structure

No changes needed. Module is 153 lines, has one class with clear
responsibility, uses frozen dataclass correctly, and follows the
standards. The module-level function aliases are acceptable as a
thin backwards-compatibility layer during migration.

---

## Module 16: `scrub.py` (291 lines)

```text
Module: scrub.py (291 lines)
Current: 2 classes (_SecretRule, ScrubConfig), 7 top-level functions
Domain nouns: secret rule, scrub config, text scrubber, profanity filter,
              redaction counter
Shared state: _BLOCK_RULES, _LINE_RULES (module-level compiled rules);
              _DEFAULT_CONFIG (module-level singleton)
```

Well-structured module under 300 lines. `_SecretRule` and `ScrubConfig`
are clean frozen dataclasses. The scrubbing pipeline is stateless
(config in, text in, scrubbed text out).

### Target structure

#### Class: `TextScrubber`

```text
TextScrubber
  Module: src/quarry/scrub.py
  Responsibility: Scrub secrets and profanity from text using regex rules
  Owns: _config (ScrubConfig), _block_rules, _line_rules, _profanity_re
  Public interface:
    scrub(text) -> tuple[str, dict[str, int]]
    scrub_and_log(text, label) -> str
  Absorbs: scrub, scrub_and_log, _scrub_block_secrets, _scrub_line_secrets,
           _build_profanity_re, _replacement_for, _build_secret_rules,
           _DEFAULT_CONFIG
  Dependencies: re, collections.Counter, logging
  Estimated LOC: ~200
```

#### Remaining

```text
scrub.py
  Keeps: _SecretRule, ScrubConfig (value objects),
         DEFAULT_PROFANITY (constant)
  Estimated LOC: ~90
```

---

## Module 17: `_hook_entry.py` (190 lines)

```text
Module: _hook_entry.py (190 lines)
Current: 0 classes, 6 top-level functions
Domain nouns: hook dispatcher, background ingest runner
Shared state: _HANDLERS dict mapping event names to callables
```

This module is a lightweight dispatcher. The `main()` function reads
`sys.argv`, looks up a handler, and calls it. The `_ingest_background`
function is the longest at ~90 lines — it parses argv, reads a temp file,
deduplicates, and ingests.

### Target structure

The module is under 200 lines and serves as an entry point (`__main__`
pattern). The `_ingest_background` function could extract into a class
but the module overall is appropriate as-is.

#### Class: `BackgroundIngester`

```text
BackgroundIngester
  Module: src/quarry/_hook_entry.py
  Responsibility: Dedup and ingest text from a temp file in a detached process
  Owns: _text_file, _document_name, _collection, _lancedb_path,
        _session_prefix, _agent_handle, _memory_type, _summary
  Public interface:
    run() -> None
  Absorbs: _ingest_background (core logic, not the argv parsing)
  Dependencies: quarry.config, quarry.database, quarry.pipeline,
                quarry.logging_config
  Estimated LOC: ~80
```

#### Remaining

```text
_hook_entry.py
  Keeps: main, _session_setup, _session_start, _post_web_fetch,
         _pre_compact, _HANDLERS dict
  Estimated LOC: ~60
```

---

## Module 18: `_stdlib.py` (452 lines)

```text
Module: _stdlib.py (452 lines)
Current: 1 class (HookConfig), 15 top-level functions
Domain nouns: hook config, hook runner, command deployer, permission manager,
              session setup handler, settings writer
Shared state: none (all functions are pure or read from filesystem)
```

This module has two distinct responsibilities: (1) hook config loading and
hook stdin/stdout plumbing (HookConfig, load_hook_config, read_hook_stdin,
run_hook — ~150 lines), and (2) plugin session setup (command deployment,
permission management, settings writing — ~300 lines).

### Target structure

#### Class: `PluginSetup`

```text
PluginSetup
  Module: src/quarry/_stdlib.py (or split to src/quarry/_plugin_setup.py)
  Responsibility: Deploy plugin commands and manage MCP tool permissions
  Owns: _plugin_root (Path), _plugin_name (str)
  Public interface:
    deploy(commands_dir) -> list[str]
    allow_mcp_tools(settings_path) -> str | None
    allow_skill_permissions(settings_path) -> str | None
  Absorbs: _deploy_commands, _allow_mcp_tools, _allow_skill_permissions,
           _read_plugin_name, _retire_old_commands, _should_deploy,
           _list_deployable_commands, _ensure_allow_list, _write_settings,
           _RETIRED_COMMANDS
  Dependencies: filecmp, json, shutil, pathlib (stdlib only)
  Estimated LOC: ~200
```

#### Remaining

```text
_stdlib.py
  Keeps: HookConfig, load_hook_config, _parse_auto_capture, _bool_field,
         read_hook_stdin, run_hook, handle_session_setup (thin, delegates
         to PluginSetup)
  Estimated LOC: ~200
```

---

## Module 19: `logging_config.py` (73 lines)

```text
Module: logging_config.py (73 lines)
Current: 0 classes, 1 function
Domain nouns: logging configuration
Shared state: none
```

A single function configuring stdlib logging. 73 lines. No changes needed.
This is the correct abstraction level — a module-level function for a
one-shot configuration action.

### Target structure

No changes. Module is well under 300 lines, has one function with one
responsibility, uses `logging.config.dictConfig` correctly.

---

## Module 20: `provider.py` (99 lines)

```text
Module: provider.py (99 lines)
Current: 1 class (ProviderSelection), 2 top-level functions
Domain nouns: provider selection, ONNX runtime
Shared state: PROVIDER_MODEL_MAP constant; provider_display lru_cache
```

99 lines. `ProviderSelection` is a clean frozen dataclass. `select_provider`
is a stateless detection function. `provider_display` is a cached display
helper. No changes needed.

### Target structure

No changes. Module is compact, has one class with clear responsibility,
and two coherent functions.

---

## Summary

### New files created

| File | LOC | Source module(s) |
|---|---|---|
| `src/quarry/cli_context.py` | ~120 | `__main__.py` |
| `src/quarry/remote_client.py` | ~130 | `__main__.py` |
| `src/quarry/commands/__init__.py` | ~10 | new |
| `src/quarry/commands/find.py` | ~90 | `__main__.py` |
| `src/quarry/commands/ingest.py` | ~80 | `__main__.py` |
| `src/quarry/commands/show.py` | ~60 | `__main__.py` |
| `src/quarry/commands/remember.py` | ~70 | `__main__.py` |
| `src/quarry/commands/status.py` | ~50 | `__main__.py` |
| `src/quarry/commands/use.py` | ~25 | `__main__.py` |
| `src/quarry/commands/delete.py` | ~55 | `__main__.py` |
| `src/quarry/commands/register.py` | ~70 | `__main__.py` |
| `src/quarry/commands/sync.py` | ~80 | `__main__.py` |
| `src/quarry/commands/enable.py` | ~60 | `__main__.py` |
| `src/quarry/commands/optimize.py` | ~40 | `__main__.py` |
| `src/quarry/commands/backfill.py` | ~50 | `__main__.py` |
| `src/quarry/commands/login.py` | ~90 | `__main__.py` |
| `src/quarry/commands/remote_list.py` | ~50 | `__main__.py` |
| `src/quarry/commands/list_resources.py` | ~120 | `__main__.py` |
| `src/quarry/commands/admin.py` | ~60 | `__main__.py` |
| `src/quarry/task_manager.py` | ~70 | `http_server.py` |
| `src/quarry/routes/__init__.py` | ~5 | new |
| `src/quarry/routes/search.py` | ~60 | `http_server.py` |
| `src/quarry/routes/documents.py` | ~100 | `http_server.py` |
| `src/quarry/routes/collections.py` | ~70 | `http_server.py` |
| `src/quarry/routes/remember.py` | ~80 | `http_server.py` |
| `src/quarry/routes/ingest.py` | ~100 | `http_server.py` |
| `src/quarry/routes/sync.py` | ~70 | `http_server.py` |
| `src/quarry/routes/registrations.py` | ~200 | `http_server.py` |
| `src/quarry/routes/status.py` | ~120 | `http_server.py` |
| `src/quarry/routes/mcp_ws.py` | ~40 | `http_server.py` |
| `src/quarry/hooks/__init__.py` | ~30 | `hooks.py` |
| `src/quarry/hooks/session_start.py` | ~200 | `hooks.py` |
| `src/quarry/hooks/web_fetch.py` | ~120 | `hooks.py` |
| `src/quarry/hooks/pre_compact.py` | ~200 | `hooks.py` |
| `src/quarry/hooks/transcript.py` | ~110 | `hooks.py` |
| `src/quarry/hooks/collection_resolver.py` | ~60 | `hooks.py` |
| `src/quarry/sync_discovery.py` | ~130 | `sync.py` |
| `src/quarry/install.py` | ~180 | `doctor.py` |
| `src/quarry/ethos_config.py` | ~140 | `doctor.py` |
| `src/quarry/claudemd.py` | ~60 | `doctor.py` |

### Modules after refactoring (LOC estimates)

| Module | Before | After | Classes | Notes |
|---|---|---|---|---|
| `__main__.py` | 2008 | ~400 | 1 (_OrderedGroup) | Thin CLI shell |
| `http_server.py` | 1530 | ~250 | 1 (QuarryContext) | App factory + serve |
| `mcp_server.py` | 581 | ~420 | 1 (McpSession) | Tool implementations |
| `hooks/__init__.py` | 868 | ~30 | 0 | Re-exports |
| `hooks/session_start.py` | -- | ~200 | 1 (SessionStartHandler) | New |
| `hooks/web_fetch.py` | -- | ~120 | 1 (WebFetchHandler) | New |
| `hooks/pre_compact.py` | -- | ~200 | 1 (PreCompactHandler) | New |
| `hooks/transcript.py` | -- | ~110 | 0 | Pure functions OK |
| `hooks/collection_resolver.py` | -- | ~60 | 0 | Pure functions OK |
| `formatting.py` | 405 | ~400 | 2 (ColumnSpec, TableRenderer) | Minor refine |
| `sync.py` | 660 | ~200 | 0 | Orchestration only |
| `sync_discovery.py` | -- | ~130 | 1 (FileDiscovery) | New |
| `sync_registry.py` | 307 | ~310 | 3 (SyncRegistry, DirectoryRegistration, FileRecord) | Methods on class |
| `service.py` | 572 | ~490 | 3 (ServiceBackend, LaunchdBackend, SystemdBackend) | Protocol + backends |
| `doctor.py` | 1141 | ~30 | 1 (CheckResult) | Value object only |
| `install.py` | -- | ~180 | 1 (InstallWizard) | New |
| `ethos_config.py` | -- | ~140 | 1 (EthosConfigurator) | New |
| `claudemd.py` | -- | ~60 | 0 | Pure function OK |
| `remote.py` | 303 | ~300 | 2 (ProxyConfig, PermissionWarning) | config as class |
| `tls.py` | 364 | ~365 | 1 (CertificateAuthority) | Functions → methods |
| `proxy.py` | 166 | ~150 | 1 (ProxyInstaller) | Functions → methods |
| `enable.py` | 367 | ~370 | 3 (ProjectManager, EnableResult, DisableResult) | Functions → methods |
| `backfill.py` | 314 | ~310 | 4 (SessionBackfiller, BackfillStats, ProjectMapping, _Accumulator) | Process as class |
| `artifacts.py` | 153 | 153 | 1 (SessionArtifacts) | No change |
| `scrub.py` | 291 | ~290 | 3 (TextScrubber, _SecretRule, ScrubConfig) | Scrubber as class |
| `_hook_entry.py` | 190 | ~140 | 1 (BackgroundIngester) | Extract class |
| `_stdlib.py` | 452 | ~400 | 2 (HookConfig, PluginSetup) | Extract class |
| `logging_config.py` | 73 | 73 | 0 | No change |
| `provider.py` | 99 | 99 | 1 (ProviderSelection) | No change |

### Classes introduced

Total new classes: 26

1. `CliContext` — CLI output state and settings resolution
2. `RemoteClient` — Authenticated HTTPS client for remote quarry
3. `QuarryContext` — Rename of `_QuarryContext` (public)
4. `TaskManager` — Background asyncio task lifecycle
5. `McpSession` — MCP tool implementations with session state
6. `SessionStartHandler` — Auto-register and background sync
7. `WebFetchHandler` — Auto-ingest fetched URLs
8. `PreCompactHandler` — Transcript capture before compaction
9. `TableRenderer` — Constrained-width table rendering engine
10. `CollectionSyncer` — Sync a single directory with LanceDB
11. `FileDiscovery` — File discovery with ignore rules
12. `SyncRegistry` — SQLite registry (absorbs 12 functions)
13. `ServiceBackend` — Protocol for platform service management
14. `LaunchdBackend` — macOS launchd implementation
15. `SystemdBackend` — Linux systemd implementation
16. `HealthChecker` — Environment health checks
17. `InstallWizard` — Install wizard steps
18. `EthosConfigurator` — Ethos identity extension config
19. `ProxyConfig` — mcp-proxy TOML config management
20. `CertificateAuthority` — TLS cert generation and management
21. `ProxyInstaller` — mcp-proxy binary download and install
22. `ProjectManager` — Enable/disable project knowledge capture
23. `SessionBackfiller` — Historical transcript backfill
24. `TextScrubber` — Secret and profanity scrubbing
25. `BackgroundIngester` — Detached process ingest runner
26. `PluginSetup` — Plugin command deployment and permissions

Plus `McpSession` tools stay registered at module level via FastMCP
decorators that delegate to the class.

### Priority order

1. **`__main__.py`** (2008 → ~400) — Biggest win. Extract `commands/`, `CliContext`, `RemoteClient`.
2. **`http_server.py`** (1530 → ~250) — Extract `routes/`, `TaskManager`.
3. **`doctor.py`** (1141 → ~30) — Extract `HealthChecker`, `InstallWizard`, `EthosConfigurator`, `claudemd.py`.
4. **`hooks.py`** (868 → ~30) — Extract to `hooks/` package.
5. **`sync.py`** (660 → ~200) — Extract `CollectionSyncer`, `FileDiscovery`.
6. **`mcp_server.py`** (581 → ~420) — Extract `McpSession`.
7. **`service.py`** (572 → ~490) — Extract `ServiceBackend` protocol + backends.
8. **`_stdlib.py`** (452 → ~400) — Extract `PluginSetup`.
9. **`sync_registry.py`** (307 → ~310) — Wrap in `SyncRegistry` class.
10. **Remaining modules** — `enable.py`, `backfill.py`, `scrub.py`, `tls.py`, `remote.py`, `proxy.py`, `_hook_entry.py`. Each under 400 lines; refactor opportunistically.
11. **No change needed**: `artifacts.py`, `logging_config.py`, `provider.py`, `formatting.py` (minor).
