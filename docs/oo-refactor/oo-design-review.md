# OO Design Review

Reviewer: Guido van Rossum (gvr)
Date: 2026-05-13
Document reviewed: `docs/oo-refactor/oo-design-report.md` (3,121 lines)
Draft sections reviewed: `_draft-core-data.md`, `_draft-ingestion.md`, `_draft-surfaces.md`
OO rules checked: PY-OO-1 through PY-OO-6, PY-CC-1 through PY-CC-6, PY-EN-1 through PY-EN-5, PY-IC-1 through PY-IC-9, PL-PA-1 through PL-PA-6
Reference implementations: vox `oo-design-review.md` (Ralph Johnson), lux `oo-class-design-review.md` (Ralph Johnson)

---

## 1. Verdict: GO WITH MODIFICATIONS

## 2. Summary

The report is thorough and well-structured. Every module in the codebase receives a design decision; every function is accounted for in a disposition table. The three-section split (core data, ingestion, surfaces) is a good decomposition, and the sections are internally consistent. The class proposals satisfy the "state + behavior + invariant = class" test in almost every case, and the no-class decisions are argued from principle rather than convenience.

The report has seven issues that must be corrected before execution. None of them require a redesign -- they are specification errors, missing protocols, and a few classes that violate the rules they claim to enforce.

---

## 3. Revisions Required

### R1. HealthChecker at ~450 LOC exceeds the 300-line module size limit (PY-OO-2)

The report moves `doctor.py` from 1,141 lines to ~30 -- but `HealthChecker` itself is estimated at 450 lines, sitting alone in `doctor.py`. A 450-line class in a ~480-line module fails PY-OO-2 (module limit 300, class review at 300). The 20 `_check_*` methods are cohesive by purpose (they all accumulate `CheckResult` on the same list), but 450 lines is large enough to warrant splitting.

**Fix**: Split `HealthChecker` into two classes: `HealthChecker` (the orchestrator: `run_all`, `print_results`, plus the checks that need only `_settings`) and a `StorageHealthChecker` or similar grouping for the checks that need database access (`_check_storage`, `_check_fts_health`, `_check_sync_health`, `_check_sync_directories`). Alternatively, keep one class but move it to its own `health_checker.py` module and accept that a 450-line module is justified by the exception for standalone classes with tightly cohesive methods. Either way, document the decision.

### R2. McpSession at ~420 LOC with 11 public methods approaches God class territory

`McpSession` absorbs 22 functions and ends up with 11 public methods plus private helpers. The methods are cohesive (they all resolve settings/db and delegate to core), but the estimated 420 LOC and 11-method surface area put it at the boundary. The report acknowledges that `mcp_server.py` stays at ~580 LOC total (160 for wrappers + 420 for the class). This exceeds PY-OO-2's 300-line limit.

**Fix**: Extract the `_settings`, `_db`, `_background`, `_handle_errors` infrastructure into a `McpContext` class (analogous to `CliContext`). `McpSession` then owns `_context` and its tool methods become thinner. This also matches PL-PA-3 -- the MCP tools are a surface layer delegating to core via a context object, which is the same pattern proposed for the CLI. The symmetry between `CliContext` and `McpContext` is a feature, not a coincidence.

### R3. CollectionSyncer constructor has 6 owned fields -- needs a config object (PY-OO-3)

`CollectionSyncer` owns `_directory, _collection, _db, _settings, _conn, _max_workers`. If all six are positional constructor parameters (plus `cls`), this violates PY-OO-3 (max 4 positional params excluding self/cls). The report does not address this.

**Fix**: Bundle the configuration into a `SyncConfig` frozen dataclass (`directory`, `collection`, `max_workers`) and pass `db`, `settings`, `conn` separately -- or use a `SyncContext` that holds all of them. Either way, the constructor should take no more than 4 positional parameters.

### R4. SessionBackfiller constructor has 6 owned fields -- same issue (PY-OO-3)

`SessionBackfiller` owns `_settings, _db, _dry_run, _collection_override, _project_filter, _limit`. Same pattern as R3. The `_process_project` function that it absorbs already has 8 parameters, which the report correctly flags -- but the fix (absorbing into a class) merely moves the bloat from the function signature to the constructor.

**Fix**: Bundle `_dry_run, _collection_override, _project_filter, _limit` into a `BackfillConfig` frozen dataclass. Constructor becomes `SessionBackfiller(settings, db, config)`.

### R5. BackgroundIngester constructor has 8 owned fields (PY-OO-3)

`BackgroundIngester` owns `_text_file, _document_name, _collection, _lancedb_path, _session_prefix, _agent_handle, _memory_type, _summary`. Eight positional constructor parameters is a serious PY-OO-3 violation.

**Fix**: This is a job specification -- it should be a frozen dataclass `IngestJob` (or similar) passed to `BackgroundIngester.__new__`. `BackgroundIngester` then owns `_job: IngestJob` and dispatches from there.

### R6. UrlIngester.ingest_sitemap has ~13 parameters (PY-OO-3)

The report shows `ingest_sitemap(url, *, collection, include, exclude, limit, overwrite, workers, delay, timeout, progress_callback, agent_handle, memory_type, summary)`. Even with keyword-only arguments, this is 13 parameters. The report introduced `SearchFilter` to solve the same problem on `hybrid_search` (11 params -> 5) but does not apply the same discipline to `UrlIngester`.

**Fix**: Bundle the memory kwargs (`agent_handle, memory_type, summary`) into `ChunkConfig` (already proposed in the core data section). Bundle the sitemap-specific kwargs (`include, exclude, limit, workers, delay, timeout`) into a `SitemapOptions` frozen dataclass. The method signature becomes `ingest_sitemap(url, *, options, overwrite, collection, progress_callback, chunk_config)` -- 5 keyword params.

### R7. The `commands/` package routes are functions, not classes -- this needs explicit justification

The report proposes 16 command modules in `commands/`. Each is described as either a plain function (`find_command`) or a class (`FindCommand`). The report does not commit -- it says "FindCommand (or plain function find_command)." This ambiguity will cause inconsistency during implementation.

**Fix**: Commit to one pattern. The correct pattern per PL-PA-3 is functions: each command module exports a function (or a small number of functions) that takes `CliContext` plus parsed args and returns structured data. The command functions are stateless orchestrations -- they have no data to own, no invariant to maintain. A class per command adds ceremony without benefit. The module is the namespace. State it explicitly in the report and apply it consistently.

---

## 4. Per-Section Feedback

### 4.1 Core Data Layer

**Strengths**: The decomposition of `database.py` into 6 classes + 1 utility module is the strongest section. Every class has a clear single responsibility. `ChunkStore`, `ChunkSearch`, `ChunkCatalog`, `SchemaManager`, `TableOptimizer` -- these are domain nouns that pass the "state + behavior" test. The `SearchFilter` dataclass with `to_predicate()` is a textbook value object with behavior.

**The `_sql.py` decision is correct.** A 5-line module with one function is not worth a class. The report correctly identifies that `_escape_sql` is a shared dependency across three classes and proposes the cleanest solution.

**The `get_db` factory function staying module-level is correct.** Connection factories are not methods -- they create the object that methods operate on. The report's reasoning here is sound.

**CollectionName is well-designed.** A value object that validates in the constructor is the textbook pattern for domain strings. The `from_path` classmethod for derivation is correct per PY-CC-5.

**One observation on ChunkStore**: The report lists `@property db -> LanceDB` as a public property. Exposing the raw `LanceDB` handle defeats encapsulation -- callers should use `ChunkStore`, `ChunkSearch`, and `ChunkCatalog` rather than reaching through to the underlying connection. If callers need the handle for `ChunkSearch` or `ChunkCatalog` construction, that is a construction concern, not a runtime concern. Consider whether a `Database` facade (mentioned in the pattern triggers table) should own construction of all three.

**ChunkConfig is good but incomplete.** The report proposes `ChunkConfig` for `chunker.py` with fields `max_chars, overlap_chars, collection, source_format, agent_handle, memory_type, summary`. The memory kwargs (`agent_handle, memory_type, summary`) appear in `UrlIngester`, `IngestionPipeline`, `SessionBackfiller`, and `BackgroundIngester`. This is the same set of fields threaded through 10+ function signatures. `ChunkConfig` should be the canonical carrier for these fields across the entire ingestion path, not just the chunker. The report introduces it locally but does not propagate it to the places that need it most.

### 4.2 Ingestion Pipeline

**Strengths**: The `FormatExtractor` protocol is the right abstraction. Replacing 7 `ingest_*` functions with polymorphic dispatch through a registry is a significant design improvement. The protocol signature is minimal: `supported_extensions` and `extract_pages`. The SpreadsheetExtractor discussion (extra `max_chars` parameter via constructor) is well-reasoned.

**The extraction order is correct.** Creating `text_splitter.py` first (pure utilities, zero behavior change), then the protocol, then extractors one at a time, then the pipeline class -- this is the right sequence for incremental migration.

**ImagePreparer and UrlFetcher as stateless classes**: These are borderline. `ImagePreparer` has one public method (`prepare_bytes`) and no state. `UrlFetcher` has one public method (`fetch`) and no state. These are functions dressed as classes. The report should justify why they are classes rather than module-level functions. If the justification is testability (injecting a mock `UrlFetcher` into `UrlIngester`), say so. If it is protocol conformance, say so. If neither, they should be functions.

**HtmlExtractor has an extra method beyond the protocol.** `extract_from_html(html, document_name, document_path)` is not part of `FormatExtractor`. This is fine -- the protocol defines the minimum, and `HtmlExtractor` can have additional methods for the URL ingestion path. But the report should note explicitly that `UrlIngester` depends on `HtmlExtractor` specifically (not on `FormatExtractor`), which creates a concrete dependency from the URL ingestion layer to a specific extractor. This is acceptable but should be documented.

**TextExtractor naming collision.** The report renames existing `text_extractor.py` to `pdf_text_extractor.py` and creates `extractors/text_extractor.py`. The rename is necessary but creates a migration hazard: any import of `quarry.text_extractor` must be updated simultaneously. The report's migration order puts the rename at step 8 (late), but `PdfExtractor` (step 3 in the extractors sequence) depends on it. Verify that the migration order accounts for this dependency.

### 4.3 Surfaces and Services

**Strengths**: The `__main__.py` decomposition is the highest-value change in the report. Going from 2,008 lines to ~400 is a 5x reduction. The `CliContext` pattern (absorbing `_json_output`, `_verbose`, `_quiet`, `_global_db`) is the right approach. The `RemoteClient` extraction is clean. The `commands/` package mirrors PL-PA-3 correctly.

**The hook handler classes are well-reasoned.** `SessionStartHandler`, `WebFetchHandler`, `PreCompactHandler` each own their handler function plus its closely related helpers. The `hooks/transcript.py` decision to keep functions (pure transforms, no state) is correct and well-argued.

**SyncRegistry is a textbook Extract Class.** 12 functions all taking `conn` as the first parameter -- this is the most obvious PY-OO-1 violation in the codebase, and the fix is exactly right.

**The ServiceBackend protocol + LaunchdBackend/SystemdBackend is correct.** This is the Strategy pattern applied to platform dispatch. The report should note (as the lux review did for its ServiceBackend) that this makes adding a third platform a single new class with no modification to existing code -- the Open/Closed Principle.

**CertificateAuthority at ~340 LOC**: This is over the 300-line module threshold but the report does not address it. After refactoring, `tls.py` would be ~365 lines total (340 for the class + 25 for constants). The class is internally cohesive (all methods relate to TLS cert generation), so the 300-line exception for tightly cohesive collaborating classes may apply. Document the justification.

**formatting.py at ~400 LOC after refactoring**: The report estimates `TableRenderer` at ~100 LOC + remaining functions at ~300 LOC = ~400 total. This exceeds PY-OO-2. The 15+ `format_*` functions are pure and cohesive, but the module should be noted as a known exception or split further (e.g., `format_table.py` for `TableRenderer` + `ColumnSpec`, `formatters.py` for the `format_*` functions).

---

## 5. Cross-Section Issues

### 5.1 `database.insert_chunks` vs `ChunkStore.insert` -- ingestion pipeline dependency is stale

The ingestion section's dependency graph for `IngestionPipeline` says `database.insert_chunks`. After the core data refactoring, this becomes `ChunkStore.insert`. The report should use the post-refactor names consistently, since the core data section is sequenced first.

### 5.2 ChunkConfig is defined locally but needed globally

As noted in section 4.1, `ChunkConfig` bundles `collection, source_format, agent_handle, memory_type, summary`. These same five fields appear as parameters in:

- `IngestionPipeline.ingest_document`
- `UrlIngester.ingest_url`
- `UrlIngester.ingest_sitemap`
- `SessionBackfiller.run`
- `BackgroundIngester.run`
- `WebFetchHandler.handle`
- `PreCompactHandler.handle`

The core data section defines `ChunkConfig` in `chunker.py` but the ingestion and surfaces sections do not reference it. Each surface re-lists the same five kwargs individually. `ChunkConfig` should be the canonical parameter object for all of these, propagated from the surface layer through the pipeline to the chunker.

### 5.3 HtmlExtractor is imported by two layers

The ingestion section says `UrlIngester` depends on `HtmlExtractor.extract_from_html`. The surfaces section says `WebFetchHandler` depends on `quarry.html_processor`. After refactoring, `WebFetchHandler` should depend on `HtmlExtractor.extract_from_html`, not on the eliminated `html_processor` module. Verify that the surfaces section's dependency lists use post-refactor module names.

### 5.4 The report lists 27 new classes but the count in the ingestion summary says 10

The core data section says 18 classes total (6 new + 12 existing modified). The ingestion section says 12 new classes (10 listed in the "New classes" table + `ImagePreparer` + `UrlFetcher` -- but the table lists 13 rows, not 10). The surfaces section says 27 new classes (which appears to be the total across all three sections, but the number is listed only in the surfaces summary, not reconciled against the other sections). A consolidated class count across all three sections would prevent confusion.

### 5.5 Protocol vs ABC is consistent -- but implicit

Every protocol in the report uses structural typing (`Protocol`). No ABCs are proposed. This is the right choice for this codebase -- `FormatExtractor`, `ServiceBackend`, `LanceDB`, `LanceTable`, `OcrBackend`, `EmbeddingBackend` are all structural contracts with no shared implementation. The report should state this as a design principle: "All interfaces in this refactoring use `Protocol` (structural typing). No ABCs are introduced because no proposed interface includes shared implementation."

---

## 6. Risk Assessment

### Highest risk: `__main__.py` decomposition (2,008 lines, 47 functions, ~30 call sites in tests)

Every CLI command is tested by invoking it through Typer's test client or by calling the function directly. Extracting to `commands/` changes every import path. Mock targets in tests (`patch("punt_quarry.__main__.X")`) must all be updated. This is the same risk the vox review identified for its `voxd.py` monolith extraction. The mitigation is the same: one command module per PR, update mock targets in the same commit, run the full test suite at each step.

### Second highest: `database.py` deletion (925 lines, imported by 7+ modules plus tests)

Every module that calls `database.insert_chunks`, `database.hybrid_search`, `database.list_documents`, etc. must be updated. The report's migration order (extract classes one at a time, keep `get_db` as module-level) is the right approach, but the sheer number of call sites makes this a high-volume mechanical change. Estimate: 50-80 import statements across production code and tests.

### Third: `pipeline.py` FormatExtractor migration (7 `ingest_*` functions, 3 surface layers)

Replacing 7 format-specific functions with protocol dispatch changes the internal flow of the most-used API (`ingest_document`). The report's mitigation -- keeping a module-level `ingest_document` as a thin wrapper during migration -- is the right approach. The risk is that the wrapper masks a behavioral difference (e.g., different exception handling in the protocol path vs. the function path).

### Low risk: everything else

The remaining extractions (hooks, sync, doctor, service, formatting, remote, tls, proxy, enable, backfill, scrub, _hook_entry, _stdlib) are all within single modules with fewer call sites. Each can be done in one commit without intermediate breakage.

---

## 7. Observations (Not Blocking)

### O1. The `__new__` mandate is not addressed

The report mentions PY-CC-1 only for `OnnxEmbeddingBackend` and `LocalOcrBackend` (converting `__init__` to `__new__`). All 27 new classes will need `__new__` constructors. The report should state explicitly: "All new classes use `__new__` per PY-CC-1. Dataclasses are exempt." This prevents the same issue the vox review caught -- 14 new classes introduced with `__init__` because the plan did not specify `__new__`.

### O2. Stateless classes should be justified

`ImagePreparer`, `UrlFetcher`, `TextExtractor`, `CodeExtractor`, `HtmlExtractor`, `PresentationExtractor`, `SpreadsheetExtractor` -- seven of the proposed extractor classes own no instance state (marked "stateless" in the report). A class with no state is a namespace, not an object. The justification for making them classes (rather than modules with functions) is the `FormatExtractor` protocol: the pipeline dispatches polymorphically via the protocol, which requires instances. This is a valid reason. The report should state it once, clearly, as a design decision.

### O3. `routes/` modules are function collections, not class modules

The `routes/` package splits `http_server.py` into 9 route modules. Each module contains 1-4 async handler functions. None contain classes. The report does not address the OO metrics for these modules (they will have `method_ratio=0.0`, `class_to_func_ratio=0.0`). If the OO score tool measures these files, they will fail. The report should either (a) confirm the tool exempts route handlers, (b) propose wrapping handlers in a class per route group, or (c) document that route modules are presentation-layer wiring and exempt from the class-per-module expectation.

### O4. The `commands/` modules have the same issue as O3

16 command modules, each containing 1-2 functions, no classes. Same metric concern. Same fix needed.

### O5. `_hook_entry.py` -- the BackgroundIngester class is the right extraction but the module name is unusual

`_hook_entry.py` is a private entry point module (the underscore signals "internal"). After extracting `BackgroundIngester`, the module is 60 lines with 5 thin dispatch functions. This is fine. But the class name `BackgroundIngester` does not belong in a module named `_hook_entry` -- it should live in its own module (`_background_ingester.py`) or in the `hooks/` package. The hook entry point should be pure dispatch, containing no domain classes.
