# Core Data Layer -- OO Refactor Analysis

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
