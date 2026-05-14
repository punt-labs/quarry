# OO Design Pattern Review

Reviewer: Ralph Johnson (rej)
Date: 2026-05-13
Documents reviewed: `oo-design-report.md`, `oo-design-review.md`,
`oo-refactoring-plan.md`, `_draft-core-data.md`, `_draft-ingestion.md`,
`_draft-surfaces.md`
Source spot-checked: `database.py`, `pipeline.py`, `http_server.py`,
`__main__.py`, `hooks.py`, `service.py`, `formatting.py`, `sync.py`
Standards applied: PY-DP-1 through PY-DP-11, PY-CC-1 through PY-CC-6,
PY-EN-1 through PY-EN-5, PY-IC-1 through PY-IC-9, PY-OO-1 through PY-OO-6

---

## 1. Verdict: YES WITH GAPS

The target architecture is a proper OO system. The 44 proposed classes
correctly decompose the six god modules into single-responsibility objects.
The dependency direction is sound. The FormatExtractor protocol is the right
abstraction for the ingestion layer. The ServiceBackend protocol is the right
abstraction for platform dispatch. The reviewer revisions (R1--R7) were
correctly adopted.

Seven gaps remain. Five are missing patterns where trigger conditions exist
but the plan does not apply the required pattern. Two are procedural residue
that will survive all 78 steps unaddressed.

---

## 2. Pattern Audit

### PY-DP-1: Flyweight (Object Caching for Immutable Values)

**Trigger present?** Yes. `CollectionName` is proposed as an immutable value
object where identity should equal equality. If two callers construct
`CollectionName("default")`, they should receive the same instance.

**Plan addresses it?** No. The plan proposes `CollectionName` with
`__new__` validation and `__eq__`/`__hash__`, but does not specify a
`WeakValueDictionary` cache. The class is `@final` (immutable, leaf),
and the same collection name string appears in every ingestion call,
every search call, and every sync operation. This is the textbook
Flyweight trigger: immutable, high-frequency, identity=equality.

**Severity:** Medium. The system works without it, but the pattern is
explicitly required by PY-DP-1 when the trigger condition is met.

### PY-DP-2: Factory Pattern

**Trigger present?** Yes, in two places:

1. `BackendRegistry` owns the data (settings, cache state) needed to
   construct `OcrBackend` and `EmbeddingBackend`. The plan correctly
   makes `get_ocr_backend` and `get_embedding_backend` methods on
   `BackendRegistry`. This is a Factory applied correctly.

2. The plan proposes `IngestionPipeline` constructing extractor instances
   via a registry. The pipeline owns the Settings data needed for
   `SpreadsheetExtractor(max_chars=settings.chunk_max_chars)` and
   `PdfExtractor(settings)`. This is factory behavior, correctly applied.

**Plan addresses it?** Yes. Both cases are handled.

### PY-DP-3: State Pattern

**Trigger present?** Partially. `TaskState` in `http_server.py` has
`status: str` that transitions through `"running"`, `"completed"`,
`"failed"`. The plan moves `TaskState` into `TaskManager` but keeps
it as a dataclass with a mutable `status` field.

**Plan addresses it?** No. `TaskState` is a mutable object whose
behavior changes by state (a running task can be cancelled; a
completed task can be queried for results). The plan does not apply
the State pattern. However, the current implementation is 15 lines
and the transition logic is in `TaskManager`, not in `TaskState`
itself. The State pattern would add ceremony disproportionate to the
complexity. This is a justified non-application.

**Severity:** Low. Document the decision.

### PY-DP-4: Builder Pattern

**Trigger present?** No. No object in the codebase has data set
incrementally in arbitrary order. `ChunkConfig`, `SearchFilter`,
`SyncConfig`, `BackfillConfig`, `SitemapOptions`, and `IngestJob`
are all frozen dataclasses constructed in one shot. Correct.

### PY-DP-5: Memento Pattern

**Trigger present?** No. No undo/restore functionality exists or is
needed.

### PY-DP-6: Prototype Pattern

**Trigger present?** No. No cloning behavior exists or is needed.

### PY-DP-7: Singleton Pattern

**Trigger present?** Yes. `BackendRegistry` wraps module-level cache
state (`_ocr_cache`, `_embedding_cache`, `_lock`) that must have
exactly one global instance.

**Plan addresses it?** Partially. The plan wraps the state in a class
but does not specify the Singleton `__new__` implementation. The draft
mentions "module-level convenience functions can remain as thin wrappers
around a module-level singleton instance." This is Singleton by convention
(module-level instance), not by construction (`__new__` guard). Per
PY-DP-7, the implementation should be `__new__` checking for an existing
class-level instance.

**Severity:** Low. Module-level singleton is a Python idiom that works.
The `__new__` guard is cleaner but not critical.

### PY-DP-8: PubSub (Publish-Subscribe)

**Trigger present?** Yes. The hooks system is an event-driven pipeline:
Claude Code fires events (`SessionStart`, `PostToolUse/WebFetch`,
`PreCompact`), and handler classes react. The current design uses a
`_HANDLERS` dispatch dict in `_hook_entry.py`.

**Plan addresses it?** No. The plan extracts handler classes
(`SessionStartHandler`, `WebFetchHandler`, `PreCompactHandler`) but
does not introduce a PubSub mechanism. The dispatch remains a
hardcoded dictionary mapping event names to handler functions.

This is defensible. The event set is fixed by Claude Code (quarry
cannot define new events), so loose coupling between producer and
consumer provides no value. The hardcoded dispatch is simpler and
correct. Document the reasoning.

**Severity:** None -- justified non-application.

### PY-DP-9: Null Object

**Trigger present?** No. No subsystem needs a "do nothing" stand-in.

### PY-DP-10: Facade

**Trigger present?** Yes, strongly. The LanceDB subsystem splits into
`ChunkStore`, `ChunkSearch`, `ChunkCatalog`, `SchemaManager`, and
`TableOptimizer`. Five classes with a shared `_db` handle, all
constructed by callers who currently import `database.py`. Every
surface layer (CLI, HTTP, MCP) needs store + search + catalog.

**Plan addresses it?** Mentioned but not applied. The design report
notes: "ChunkStore + ChunkSearch + ChunkCatalog could be composed
behind a Database facade if callers want a single entry point." The
refactoring plan does not include a `Database` facade step. This means
every caller must construct three objects from the same `db` handle
and thread them independently.

The peer review (section 4.1) observes: "Exposing the raw LanceDB
handle defeats encapsulation -- callers should use ChunkStore,
ChunkSearch, and ChunkCatalog rather than reaching through to the
underlying connection. If callers need the handle for ChunkSearch or
ChunkCatalog construction, that is a construction concern, not a
runtime concern. Consider whether a Database facade should own
construction of all three."

**Severity:** High. Without a facade, the decomposition of
`database.py` trades one god module for a three-class construction
ceremony repeated at every call site. The trigger condition (single
entry point to a subsystem, PY-DP-10) is unambiguously present.

### PY-DP-11: Single-Method Interfaces (Strategy)

**Trigger present?** Yes. `FormatExtractor` is a Protocol with
`supported_extensions` (property) and `extract_pages` (method). This
is the Strategy pattern: the pipeline selects a strategy (extractor)
at runtime based on file extension.

**Plan addresses it?** Yes, correctly. `FormatExtractor` is a Protocol
(structural typing), not an ABC. Seven extractors implement it. The
pipeline dispatches polymorphically via a registry keyed by extension.
This is textbook Strategy.

`ServiceBackend` is also a single-method Protocol (three methods:
`install`, `uninstall`, `status`). `LaunchdBackend` and
`SystemdBackend` implement it. The plan correctly notes this enables
adding a third platform without modifying existing code (Open/Closed
Principle).

---

## 3. Procedural Residue

After all 78 steps, the following module-level functions remain outside
of any class:

### Justified (pure utilities, no shared state)

These are correctly kept as functions per the plan's reasoning:

| Module | Functions | Reason |
|--------|-----------|--------|
| `_sql.py` | `_escape_sql` | 5-line utility, 3 consumers |
| `text_splitter.py` | `split_markdown`, `split_latex`, `split_plain`, `sections_to_pages`, `read_text_with_fallback` | Pure transforms, no state |
| `latex_utils.py` | `escape_latex`, `rows_to_latex` | Pure transforms |
| `pdf_text_extractor.py` | `extract_text_pages` | Pure function, single consumer |
| `storage.py` | `format_size`, `dir_size_bytes`, `discover_databases` | No LanceDB dependency |
| `claudemd.py` | `inject_claude_md` | Single function, ~60 lines |
| `hooks/transcript.py` | `extract_transcript_text`, `extract_message_text`, `_extract_content_texts`, `_extract_tool_result_text` | Pure transforms, stdlib only |
| `hooks/collection_resolver.py` | `_collection_for_cwd`, `_collection_for_cwd_conn`, `_resolve_settings` | Pure functions |
| `chunker.py` | `chunk_pages`, `_split_text` | Stateless transforms (ChunkConfig bundles params) |

Total: ~25 functions. These are genuine pure utilities with no shared
state and no data they could own. Keeping them as functions is correct.

### Unjustified (procedural residue that should be methods)

| Module | Functions | Problem |
|--------|-----------|---------|
| `formatting.py` | 15+ `format_*` functions | These all call `TableRenderer.render()`. They are stateless, but they share an implicit dependency on `ColumnSpec` layout definitions. After `TableRenderer` extraction, 15 module-level functions remain at ~300 LOC. The module has `class_to_func_ratio` near zero. |
| `service.py` | `install`, `uninstall`, `detect_platform`, `_write_env_file`, `_quarry_exec_args`, `_get_tls_hostname`, `ensure_gpu_runtime` | 7 functions remain after extracting `LaunchdBackend` and `SystemdBackend`. They share `_LABEL`, `_ENV_FILE`, and the platform detection logic. The `install` and `uninstall` functions are dispatch wrappers, but `_write_env_file` and `_quarry_exec_args` operate on shared constants and could be methods on a `ServiceManager` or absorbed into the backends. |
| `sync.py` | `compute_sync_plan`, `sync_all` | `compute_sync_plan` takes 5 parameters including `conn`, `extensions`, `max_workers`. `sync_all` takes `db`, `settings`, `conn`, `max_workers`. These share the same parameter set as `CollectionSyncer`. They should be classmethods or a second class. |
| `backfill.py` | `encode_project_path`, `build_project_mappings`, `list_transcript_files`, `document_name_for_transcript`, `is_already_ingested` | 5 functions remain. `build_project_mappings` and `list_transcript_files` share path-scanning logic. `is_already_ingested` takes `db` and could be a method on `SessionBackfiller`. |
| `remote.py` | `ws_to_http`, `validate_connection`, `validate_connection_from_ws_url`, `mask_token`, `fetch_ca_cert`, `store_ca_cert` | 6 functions share connection-validation logic and `CA_CERT_PATH`. `validate_connection` + `fetch_ca_cert` + `store_ca_cert` operate on the same CA trust state and could be a `ConnectionValidator` class. |
| `sitemap.py` | `discover_pages`, `discover_urls`, `filter_entries`, `_pages_to_entries` | 4 functions. Step 0.10 proposes `SitemapDiscovery`, but the ingestion draft contradicts this: "No structural change." The plan and the draft disagree. |
| `logging_config.py` | `configure_logging` | Step 0.8 proposes `LoggingConfig` class, but the surfaces draft says "No changes needed." Contradiction. |
| `provider.py` | `select_provider`, `provider_display` | Step 0.9 proposes moving these into `ProviderSelection`, but the draft says "No changes needed." Contradiction. |

### Contradictions between plan and drafts

The refactoring plan (steps 0.7--0.10) adds four modules that the
design drafts explicitly mark "no change needed":

1. `latex_utils.py` -- plan step 0.7 creates `LatexSerializer`; draft
   says "No structural change."
2. `logging_config.py` -- plan step 0.8 creates `LoggingConfig`; draft
   says "No changes."
3. `provider.py` -- plan step 0.9 moves functions into `ProviderSelection`;
   draft says "No changes."
4. `sitemap.py` -- plan step 0.10 creates `SitemapDiscovery`; ingestion
   draft says "No structural change."

These steps were added to the plan after the drafts were written (the
plan header says "These 4 modules were incorrectly excluded"). The
plan is correct that they need refactoring (all have `method_ratio`
0.00), but the drafts should be updated for consistency.

---

## 4. Missing Patterns

### MP-1: Database Facade (PY-DP-10)

**Trigger:** Five classes (`ChunkStore`, `ChunkSearch`, `ChunkCatalog`,
`SchemaManager`, `TableOptimizer`) all constructed from the same
`LanceDB` handle.

**Required:** A `Database` facade that constructs and holds all five,
exposing a unified interface. Callers import `Database`, not five
separate classes.

    Database
      Module: src/quarry/database_facade.py  (or src/quarry/db.py)
      Owns: _store (ChunkStore), _search (ChunkSearch), _catalog (ChunkCatalog),
            _schema (SchemaManager), _optimizer (TableOptimizer)
      Public interface:
        Database(db: LanceDB)  [__new__]
        @property store -> ChunkStore
        @property search -> ChunkSearch
        @property catalog -> ChunkCatalog
        @property optimizer -> TableOptimizer
        ensure_schema() -> None  [delegates to _schema]
      Factory: get_db(path) -> Database  [replaces current get_db]

This is 40 lines. It eliminates the three-class construction ceremony
at every call site. It matches PY-DP-10 exactly. The peer review
suggested it. The plan acknowledged it. Neither applied it.

### MP-2: CollectionName Flyweight (PY-DP-1)

**Trigger:** `CollectionName` is immutable, identity=equality, and
the same values (`"default"`, project-derived names) appear thousands
of times per session.

**Required:** `WeakValueDictionary` cache in `__new__`, `@final`.

### MP-3: ConnectionValidator class for remote.py

**Trigger:** 3 functions (`validate_connection`,
`validate_connection_from_ws_url`, `fetch_ca_cert` + `store_ca_cert`)
share CA certificate state and connection validation logic.

**Required:** A `ConnectionValidator` class owning `_ca_cert_path` with
methods `validate`, `validate_from_ws_url`, `fetch_ca_cert`,
`store_ca_cert`. This is textbook Extract Class (PY-RF-3).

### MP-4: ServiceManager for service.py residual functions

**Trigger:** 7 functions remain after backend extraction. `install`
and `uninstall` dispatch to backends. `_write_env_file` and
`_quarry_exec_args` share `_LABEL`, `_ENV_FILE` constants. This is
the same "functions sharing a parameter" trigger as PY-OO-1.

**Required:** A `ServiceManager` class owning `_label`, `_env_file`,
`_backend` with methods `install`, `uninstall`, `write_env_file`,
`exec_args`. The backends become an owned collaborator.

### MP-5: SyncOrchestrator for sync.py residual functions

**Trigger:** `compute_sync_plan` and `sync_all` share `db`, `settings`,
`conn`, `max_workers` -- the same parameters as `CollectionSyncer`.

**Required:** Either make these classmethods on `CollectionSyncer` or
extract a `SyncOrchestrator` that owns `_db`, `_settings`, `_conn`
and has `compute_plan` and `sync_all` methods.

---

## 5. Encapsulation Gaps

### EG-1: ChunkStore exposes `@property db -> LanceDB`

The design report lists `@property db -> LanceDB` as a public property
on `ChunkStore`. This exposes the raw LanceDB handle, violating
PY-EN-2 (properties for read-only access of internal state, not for
leaking dependencies). Callers that need the handle for `ChunkSearch`
or `ChunkCatalog` construction should receive it via the `Database`
facade (MP-1), not by reaching through `ChunkStore`.

**Fix:** Remove the `db` property. Introduce the `Database` facade.

### EG-2: QuarryContext cached_property exposures

The plan renames `_QuarryContext` to `QuarryContext` and exposes `db`,
`embedder`, `settings`, `api_key`, `cors_origins`, `uptime_seconds`
as properties or cached_properties. The `db -> LanceDB` exposure has
the same problem as EG-1. `api_key -> str | None` exposes a credential
as a readable property.

**Fix:** `db` should return the `Database` facade. `api_key` should be
a private attribute accessed only by the auth middleware, not a public
property.

### EG-3: ColumnSpec dataclass with public fields

`ColumnSpec` is a frozen dataclass with public fields (`header`,
`min_width`, `fixed`, `align`). PY-EN-1 says "never expose raw data
attributes publicly." However, PY-CC-6 says frozen dataclasses are
for "pure value objects with no behavior beyond field storage."
`ColumnSpec` has no behavior. This is a conflict between PY-EN-1 and
PY-CC-6.

**Resolution:** PY-CC-6 governs. Frozen dataclasses are exempt from
PY-EN-1's underscore requirement because their fields are immutable
and the dataclass decorator generates `__init__`, `__eq__`, and
`__hash__` from the field names. This should be stated as an invariant.

### EG-4: SearchFilter fields are public

`SearchFilter` is a frozen dataclass whose fields (`document`,
`collection`, `page_type`, `source_format`, `agent_handle`,
`memory_type`) are public. Same analysis as EG-3: frozen dataclass,
no mutation possible, PY-CC-6 governs.

### EG-5: No encapsulation issues in proposed non-dataclass classes

All proposed non-dataclass classes use underscore-prefixed attributes.
`ChunkStore._db`, `ChunkSearch._db`, `SessionStartHandler._settings`,
`McpSession._executor`, etc. This is correct.

---

## 6. Dependency Direction Issues

### DD-1: No inward violations

The dependency direction is correct throughout. The plan enforces
invariant 1: "No extracted class imports from the presentation layer."
The layering is:

    types.py, results.py, models.py           (Layer 1: Types)
        ^
    chunk_store.py, chunk_search.py, ...      (Layer 2: Core)
        ^
    pipeline.py, sync.py, service.py, ...     (Layer 3: Services)
        ^
    commands/, routes/, mcp_server.py         (Layer 4: Presentation)

Every arrow points inward. No core module imports from commands,
routes, or the MCP server.

### DD-2: One potential circular risk

`storage.py` contains `discover_databases`, which the design report
says "will import ChunkCatalog and ChunkStore at call time to avoid
circular dependencies." This is a lazy import to break a cycle where
a utility module (storage) needs core classes (ChunkCatalog). The
solution (deferred import) is correct but signals that `discover_databases`
may belong on the `Database` facade rather than in a standalone
utility module.

### DD-3: HtmlExtractor concrete dependency

`UrlIngester` depends on `HtmlExtractor.extract_from_html` (not on
`FormatExtractor`). `WebFetchHandler` also depends on `HtmlExtractor`
specifically. The peer review noted this. The plan documents it. This
is a concrete dependency from two service-layer classes to a specific
extractor, bypassing the protocol. It is justified (the protocol does
not include `extract_from_html`), but it means `HtmlExtractor` cannot
be replaced without modifying two consumers.

---

## 7. Constructor Discipline

### CD-1: `__new__` mandate is stated in plan invariant 7

The plan states: "All new non-dataclass classes use `__new__` with
`Self` return type. Dataclasses are exempt. Pydantic models (Settings)
are exempt." This is correct and covers PY-CC-1.

### CD-2: Existing `__init__` violations addressed in step 0.2

`OnnxEmbeddingBackend.__init__` and `LocalOcrBackend.__init__` are
converted in step 0.2. Correct.

### CD-3: `slots=True` on frozen dataclasses addressed in step 0.3

`PageAnalysis`, `PageContent`, `Chunk` get `slots=True`. All new
dataclasses in the plan specify `frozen=True, slots=True`. Correct
per PY-CC-6.

### CD-4: Factory pattern for `CollectionName.from_path`

`CollectionName.from_path` is a `@classmethod` alternative constructor
per PY-CC-5. Correct.

### CD-5: No factory guard patterns proposed

No proposed class uses the factory guard pattern (PY-CC-3/PY-CC-4).
The trigger condition ("one class owns data required for constructing
another, and the constructed class should refuse direct instantiation")
does not clearly apply. `BackendRegistry` creates backends, but
backends should remain directly constructable for testing. No issue.

---

## 8. Composition over Inheritance

### CI-1: Zero inheritance hierarchies proposed

No class in the plan inherits from another proposed class. All
polymorphism is via Protocol (structural typing). This is correct.
The plan explicitly states invariant 8: "All interfaces use Protocol.
No ABCs are introduced."

### CI-2: Composition correctly used

`UrlIngester` composes `_pipeline` (IngestionPipeline) and `_fetcher`
(UrlFetcher). `McpSession` composes `_context` (McpContext).
`ImageExtractor` composes `_preparer` (ImagePreparer). These are all
correct applications of PY-IC-1.

### CI-3: `ServiceBackend` Protocol is correct

`LaunchdBackend` and `SystemdBackend` implement `ServiceBackend`
structurally. No shared implementation exists between them. Protocol
is the right choice over ABC per PY-TS-6.

---

## 9. Missing Classes (Domain Nouns Not Captured)

### MC-1: No `Database` facade (covered in MP-1)

### MC-2: No `ConnectionValidator` (covered in MP-3)

### MC-3: No `ServiceManager` (covered in MP-4)

### MC-4: `formatting.py` format functions lack a home

The 15+ `format_*` functions in `formatting.py` are pure but they all
construct `ColumnSpec` lists and call `TableRenderer.render()`. They
could be methods on specialized formatter classes (`SearchFormatter`,
`DocumentFormatter`, `StatusFormatter`) that own their column layouts.
This would bring `formatting.py` from ~400 LOC with `class_to_func_ratio`
near zero to 3-4 small classes at 80-100 LOC each.

However, the counter-argument is real: these are stateless transforms
with no shared data. Making them methods on classes that own column
layouts is a marginal improvement. The current plan's approach
(`TableRenderer` + free functions) is acceptable if the module is
exempt from `class_to_func_ratio` scoring, similar to `commands/`
and `routes/`.

**Recommendation:** Exempt `formatting.py` from `class_to_func_ratio`
in the OO scorer, or extract formatter classes if the score must pass.

### MC-5: `_hook_entry.py` dispatch functions

After extracting `BackgroundIngester`, `_hook_entry.py` retains 5
dispatch functions (`main`, `_session_setup`, `_session_start`,
`_post_web_fetch`, `_pre_compact`). These are entry-point wiring,
analogous to `__main__.py` command stubs. The plan correctly treats
them as presentation-layer dispatch exempt from class expectations.

---

## 10. Specific Revisions Required

1. **Add a `Database` facade class (step 2.2a or step 2.7).** After
   extracting `ChunkStore`, `ChunkSearch`, `ChunkCatalog`,
   `SchemaManager`, and `TableOptimizer`, add a `Database` facade
   that constructs and holds all five from a single `LanceDB` handle.
   This is PY-DP-10, and the trigger is unambiguous. 40 lines. Remove
   `ChunkStore.db` property. Update `get_db` to return `Database`.

2. **Apply Flyweight to `CollectionName`.** Add a
   `WeakValueDictionary` cache in `CollectionName.__new__`. Mark the
   class `@final`. This is PY-DP-1 applied to an immutable value
   object with identity=equality.

3. **Resolve contradictions between plan steps 0.7--0.10 and the design
   drafts.** The plan correctly adds `LatexSerializer`,
   `LoggingConfig`, `ProviderSelection` absorption, and
   `SitemapDiscovery`. Update the draft documents to match, or add a
   note in the plan stating the drafts are superseded for these modules.

4. **Extract `ConnectionValidator` from `remote.py`.** 3+ functions
   share CA cert state and validation logic. Extract to a class per
   PY-RF-3 (Extract Class).

5. **Extract `ServiceManager` from `service.py`.** 7 residual functions
   share `_LABEL`, `_ENV_FILE`, and platform detection. The backends
   should be owned by a manager, not dangling with free dispatch
   functions.

6. **Address `sync.py` residual functions.** `compute_sync_plan` and
   `sync_all` share parameters with `CollectionSyncer`. Make them
   classmethods on `CollectionSyncer` or extract a `SyncOrchestrator`.

7. **Remove `ChunkStore.db` property.** Exposing the raw `LanceDB`
   handle defeats the decomposition. The `Database` facade (revision 1)
   eliminates the need.

8. **Make `QuarryContext.api_key` private.** The API key should not be
   a public property. Move auth checking into a method or middleware
   that accesses `_api_key` internally.

9. **Exempt presentation-layer modules from `class_to_func_ratio`.** The
   plan's invariant 10 exempts `commands/` and `routes/` from class
   expectations. Extend this exemption to `formatting.py`, `_hook_entry.py`,
   and `hooks/transcript.py` -- or add the exemption to the OO scorer
   configuration.

10. **Document justified non-applications of PY-DP-3 (State) and
    PY-DP-8 (PubSub).** The plan should state why `TaskState` does not
    use the State pattern (transition logic is trivial, ~15 lines) and
    why the hooks system does not use PubSub (event set is fixed by
    Claude Code, loose coupling adds no value).

11. **State the frozen-dataclass encapsulation exemption.** Add to the
    plan's invariants: "Frozen dataclasses are exempt from PY-EN-1's
    underscore requirement per PY-CC-6. Their fields are immutable;
    the dataclass decorator generates equality and hashing from field
    names."

---

## 11. Summary Counts

| Category | Count |
|----------|-------|
| Patterns correctly applied | 5 (Factory x2, Strategy x2, Singleton-by-convention) |
| Patterns with trigger, not applied | 2 (Facade, Flyweight) |
| Patterns with trigger, justifiably not applied | 2 (State, PubSub) |
| Patterns with no trigger | 4 (Builder, Memento, Prototype, Null Object) |
| Procedural residue (justified) | ~25 functions across 9 modules |
| Procedural residue (unjustified) | ~50 functions across 7 modules |
| Encapsulation gaps | 2 real (ChunkStore.db, QuarryContext.api_key), 2 non-issues (frozen dataclasses) |
| Dependency direction issues | 0 violations, 1 risk (storage.py lazy import), 1 concrete dep (HtmlExtractor) |
| Plan/draft contradictions | 4 modules (latex_utils, logging_config, provider, sitemap) |
| Revisions required | 11 |
