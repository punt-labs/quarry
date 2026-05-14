# Package Structure Proposal

Target layout for quarry after the 84-step OO refactoring completes.
Grounded in current import analysis (96 intra-package import statements
across 42 modules) and the planned class extractions.

---

## 1. Directory Tree

```text
src/quarry/
    __init__.py              # Public API (lazy-loaded), __all__
    __main__.py              # Typer app: arg declarations, thin delegation
    _hook_entry.py           # Claude Code hook dispatch (5 thin functions)
    _sql.py                  # SQL escape helper (shared by db/)
    py.typed                 # PEP 561 marker

    types.py                 # Protocols (6)
    results.py               # SearchResult, SearchFilter, SitemapOptions, ...
    models.py                # Chunk, PageContent, PageType, ChunkConfig
    collections.py           # CollectionName (Flyweight, @final)
    config.py                # Settings (Pydantic)
    logging_config.py        # LoggingConfig

    db/
        __init__.py          # Re-exports: Database, ChunkStore, ChunkSearch, ...
        facade.py            # Database (Facade over 5 classes)
        schema.py            # SchemaManager
        chunk_store.py       # ChunkStore
        chunk_search.py      # ChunkSearch
        chunk_catalog.py     # ChunkCatalog
        optimizer.py         # TableOptimizer
        storage.py           # get_db, format_size, dir_size_bytes

    extractors/
        __init__.py          # Re-exports: FormatExtractor + all 7 extractors
        protocol.py          # FormatExtractor (Protocol)
        text_extractor.py    # TextExtractor
        code_extractor.py    # CodeExtractor
        html_extractor.py    # HtmlExtractor
        pdf_extractor.py     # PdfExtractor
        image_extractor.py   # ImageExtractor
        presentation_extractor.py
        spreadsheet_extractor.py

    ingestion/
        __init__.py          # Re-exports: IngestionPipeline, UrlIngester, ...
        pipeline.py          # IngestionPipeline
        url_ingester.py      # UrlIngester
        url_fetcher.py       # UrlFetcher
        image_preparer.py    # ImagePreparer
        text_splitter.py     # split_markdown, split_latex, split_plain
        pdf_text_extractor.py
        chunker.py           # chunk_pages (uses ChunkConfig)
        backends.py          # BackendRegistry (singleton)
        provider.py          # ProviderSelection
        ocr_local.py         # LocalOcrBackend

    services/
        __init__.py          # Re-exports: CollectionSyncer, HealthChecker, ...
        sync.py              # CollectionSyncer
        sync_discovery.py    # FileDiscovery
        sync_registry.py     # SyncRegistry
        health_checker.py    # HealthChecker
        install.py           # InstallWizard
        ethos_config.py      # EthosConfigurator
        claudemd.py          # inject_claude_md (function)
        service.py           # ServiceManager, LaunchdBackend, SystemdBackend
        tls.py               # CertificateAuthority
        remote.py            # ProxyConfig, ConnectionValidator
        proxy.py             # ProxyInstaller
        enable.py            # ProjectManager
        backfill.py          # SessionBackfiller, BackfillConfig
        scrub.py             # TextScrubber
        sitemap.py           # SitemapDiscovery, SitemapEntry

    hooks/
        __init__.py          # Re-exports: handle_session_start, ...
        session_start.py     # SessionStartHandler
        web_fetch.py         # WebFetchHandler
        pre_compact.py       # PreCompactHandler
        background_ingester.py  # BackgroundIngester, IngestJob
        transcript.py        # extract_transcript_text (stateless)
        collection_resolver.py  # _collection_for_cwd (pure functions)

    routes/
        __init__.py          # Re-exports: build_app, QuarryContext
        search.py            # /search endpoint
        documents.py         # /documents endpoints
        collections.py       # /collections endpoints
        remember.py          # /remember endpoint
        ingest.py            # /ingest endpoint
        sync.py              # /sync endpoint
        registrations.py     # /registrations endpoints
        status.py            # /status, /health, /ca-cert, /databases
        mcp_ws.py            # /mcp WebSocket endpoint

    commands/
        __init__.py
        find.py              # find_cmd
        ingest.py            # ingest_cmd
        show.py              # show_cmd
        remember.py          # remember_cmd
        status.py            # status_cmd
        use.py               # use_cmd
        delete.py            # delete_cmd
        register.py          # register, deregister
        sync.py              # sync_cmd
        enable.py            # enable_cmd, disable_cmd
        optimize.py          # optimize_cmd
        backfill.py          # backfill_sessions_cmd
        login.py             # login_cmd, logout_cmd
        list_resources.py    # list_documents_cmd, list_collections_cmd
        remote_list.py       # remote_list_cmd
        admin.py             # install, doctor, serve, mcp, version, uninstall

    surfaces/
        __init__.py          # Re-exports: CliContext, RemoteClient, ...
        cli_context.py       # CliContext
        remote_client.py     # RemoteClient, RemoteError
        http_server.py       # build_app, QuarryContext, app assembly
        mcp_server.py        # McpSession, McpContext, FastMCP wiring
        task_manager.py      # TaskManager, TaskState
        formatting.py        # TableRenderer + format_* functions
        latex_utils.py       # LatexSerializer
        artifacts.py         # SessionArtifacts
        _stdlib.py           # PluginSetup + handle_session_setup
```text

---

## 2. Dependency Graph

```text
  Layer 0 (types):  types  results  models  collections  config  logging_config
                        \      |       |         |          /
                         \     |       |         |         /
  Layer 1 (data):         +--- db/ ---+    extractors/
                               |              |
  Layer 2 (process):      ingestion/ ---------+
                               |
  Layer 3 (orchestrate):  services/
                          /    |    \
  Layer 4 (events):   hooks/   |     \
                               |      \
  Layer 5 (present):      routes/   commands/   surfaces/
                               |        |          |
  Layer 6 (entry):        __main__.py        _hook_entry.py
```text

Allowed imports -- a module in layer N imports only from layers 0..N-1:

| Package | Layer | Imports from | Imports into (consumers) |
|---------|-------|-------------|--------------------------|
| types layer | 0 | stdlib, third-party only | everything |
| db/ | 1 | layer 0, _sql.py | ingestion, services, hooks, routes, commands, surfaces |
| extractors/ | 1 | layer 0 | ingestion |
| ingestion/ | 2 | layer 0, db/, extractors/ | services, hooks, routes, commands |
| services/ | 3 | layers 0-2 | hooks, routes, commands, surfaces |
| hooks/ | 4 | layers 0-3 | _hook_entry.py |
| routes/ | 5 | layers 0-3, surfaces/ | surfaces/http_server.py |
| commands/ | 5 | layers 0-3, surfaces/ | __main__.py |
| surfaces/ | 5 | layers 0-3 | routes, commands, __main__.py |

---

## 3. Coupling Analysis

Import counts measured from current code. Post-refactoring estimates
use the planned class extractions to predict which packages each
consumer will import from.

### Cross-package import counts (post-refactoring, estimated)

| Consumer -> | types layer | db/ | extractors/ | ingestion/ | services/ | hooks/ | surfaces/ |
|-------------|-------------|-----|-------------|------------|-----------|--------|-----------|
| db/ | 5 (types, results, _sql) | internal | 0 | 0 | 0 | 0 | 0 |
| extractors/ | 3 (models) | 0 | internal | 0 | 0 | 0 | 0 |
| ingestion/ | 4 (models, results, config) | 2 (ChunkStore, get_db) | 7 (all extractors) | internal | 0 | 0 | 0 |
| services/ | 6 (config, types, collections) | 4 (Database, ChunkStore, ChunkCatalog, get_db) | 0 | 3 (pipeline, ingest_content) | internal | 0 | 0 |
| hooks/ | 3 (config, types, artifacts) | 3 (ChunkCatalog, get_db) | 1 (HtmlExtractor) | 2 (ingest_content, ingest_url) | 4 (SyncRegistry, scrub, enable) | internal | 0 |
| routes/ | 3 (config, results, types) | 3 (ChunkSearch, ChunkCatalog, get_db) | 0 | 2 (IngestionPipeline, UrlIngester) | 4 (SyncRegistry, sync, service) | 0 | 2 (QuarryContext, TaskManager) |
| commands/ | 5 (config, collections, results) | 3 (ChunkSearch, ChunkCatalog, get_db) | 0 | 2 (IngestionPipeline, UrlIngester) | 5 (sync, enable, backfill, service) | 0 | 3 (CliContext, RemoteClient, formatting) |
| surfaces/ | 3 (config, types) | 2 (Database, get_db) | 0 | 0 | 0 | 0 | internal |

### Coupling assessment

__Low coupling (0-3 imports):__ db/ -> types layer, extractors/ -> types layer,
surfaces/ -> db/. These are narrow, well-defined interfaces.

__Medium coupling (4-7 imports):__ ingestion/ -> extractors/ (7 extractors via
the protocol), services/ -> db/ (4), commands/ -> services/ (5). The
ingestion-to-extractors coupling is expected -- the pipeline dispatches to all
7 format-specific extractors. The medium coupling to db/ and services/ is
justified because these are the domain's core operations.

__Highest afferent coupling (most consumers):__ The types layer is imported by
every package -- this is correct for value objects and protocols that define the
vocabulary. db/ is the second-most imported (6 consumers), which is expected for
the storage layer.

__Highest efferent coupling (most suppliers):__ commands/ imports from 5
packages (types, db, ingestion, services, surfaces). This is inherent to
presentation-layer orchestration -- each command wires together core operations.
The coupling is managed by thin delegation: command functions are 25-120 LOC
each, and the imports they use are stable public APIs.

__No package-to-package cycles.__ The layering is strict: every arrow points
from higher layers to lower layers. hooks/ does not import from routes/ or
commands/. routes/ does not import from commands/. services/ does not import
from hooks/.

---

## 4. Cohesion Analysis

For each package: the single responsibility, why every module belongs, and
what kind of change would modify multiple modules within the package.

### types layer (top-level)

__Responsibility:__ Define the vocabulary -- protocols, value objects, config,
and data models shared across all packages.

__Cohesion:__ Very high. Every module defines types consumed by multiple
packages. A change to the `Chunk` dataclass or `SearchFilter` fields
propagates to consumers, but the types layer itself changes only when
the domain vocabulary changes.

__Why not a `types/` package:__ These 6 modules total ~500 LOC. Adding a
package would add an import prefix (`quarry.types.models`) with zero
cohesion benefit. They are stable, small, and already have no intra-layer
dependencies.

### db/ (7 modules, ~600 LOC post-refactoring)

__Responsibility:__ LanceDB storage: schema, reads, writes, search, catalog,
optimization. All state management for the chunks table.

__Cohesion:__ High. Every module operates on the same LanceDB table handle.
A schema migration (adding a column) touches schema.py, may touch
chunk_store.py and chunk_search.py -- all within this package.

__Binding abstraction:__ The `LanceDB` connection handle. Every class in
db/ receives it at construction. The Database facade composes all five
classes from a single handle.

### extractors/ (9 modules, ~800 LOC post-refactoring)

__Responsibility:__ Convert documents from 20+ formats into `list[PageContent]`.
Pure transformation: bytes/text in, structured pages out.

__Cohesion:__ High. Every extractor implements `FormatExtractor`. Adding
a new format (e.g., EPUB) means adding one file to this package, no changes
to existing extractors. A change to the `PageContent` model affects all
extractors the same way.

__Binding abstraction:__ The `FormatExtractor` protocol. Polymorphic dispatch
in IngestionPipeline depends on this single interface.

### ingestion/ (10 modules, ~1,200 LOC post-refactoring)

__Responsibility:__ The ingestion pipeline: format detection, extraction,
chunking, embedding, and storage. Everything from "user provides a path/URL"
to "chunks are in LanceDB."

__Cohesion:__ High. A change to chunking strategy affects chunker.py and
pipeline.py. A change to embedding affects backends.py and pipeline.py.
A new ingestion source (e.g., sitemap) is a new URL path in url_ingester.py.
All modules participate in the same data flow.

__Binding abstraction:__ `IngestionPipeline` is the facade. It composes
extractors, the chunker, the embedding backend, and ChunkStore into a
single `ingest_document()` call.

__Why not merge with extractors/:__ Extractors are stateless format
converters. Ingestion orchestrates I/O (network fetches, database writes,
embedding model calls). They change for different reasons: extractors
change when formats change; ingestion changes when the pipeline topology
changes.

### services/ (15 modules, ~2,500 LOC post-refactoring)

__Responsibility:__ Application-level operations that compose db/ and
ingestion/ into user-facing behaviors: sync, health checks, service
management, TLS, project enable/disable, backfill, scrubbing.

__Cohesion:__ Medium. This is the broadest package. The binding concept
is "operations that an agent or CLI command invokes but that are not
tied to any single presentation surface." sync.py and sync_discovery.py
are tightly coupled (both change when sync logic changes). tls.py and
service.py are tightly coupled (both change when deployment changes).
The remaining modules (enable.py, backfill.py, scrub.py) are independent.

__Why not split further:__ The alternative is 4-5 micro-packages
(`sync/`, `deploy/`, `health/`, etc.) with 2-3 modules each. This adds
import depth without meaningful cohesion gain. The 15-module services/
package has clear internal structure via naming (sync_*, service-related
modules) without needing sub-packages. If any sub-group grows past 6
modules, revisit.

__Anti-pattern check -- is this a grab bag?__ No. Every module in
services/ shares two properties: (1) it composes core operations
(db/, ingestion/) into domain workflows, (2) it is consumed by hooks/,
commands/, and routes/ but never by db/ or extractors/. The dependency
direction is the cohesion criterion.

### hooks/ (6 modules, ~400 LOC post-refactoring)

__Responsibility:__ React to Claude Code lifecycle events. Each handler
maps one event type to a domain action (sync on session start, ingest
on web fetch, capture on pre-compact).

__Cohesion:__ High. All modules respond to Claude Code events. A change
to the hook event schema affects all handlers. The transcript and
collection_resolver modules are shared utilities consumed by multiple
handlers within the package.

__Binding abstraction:__ The Claude Code hook event contract (stdin JSON
with event type, payload). Every handler has the same shape:
`handle(event_data) -> dict`.

### routes/ (10 modules, ~800 LOC post-refactoring)

__Responsibility:__ HTTP API endpoints. Each module defines route handlers
for one resource (search, documents, collections, etc.).

__Cohesion:__ High. All modules are aiohttp route handlers registered on
the same application. A change to the HTTP API contract (new query param,
new response field) touches exactly one route module. QuarryContext is the
shared request-scoped state.

__Binding abstraction:__ The aiohttp application and QuarryContext. Every
route handler receives `request` and accesses `QuarryContext` for db,
settings, and auth.

### commands/ (17 modules, ~1,100 LOC post-refactoring)

__Responsibility:__ CLI command bodies. Each module is a function that
takes `CliContext` + parsed args, calls services/db, and returns output.

__Cohesion:__ High. Every module is a CLI command function. They all
receive `CliContext` and share the same output patterns (JSON or table
via formatting). A change to CLI output format affects commands/ and
surfaces/formatting.py.

__Binding abstraction:__ `CliContext` (owns output mode, settings, db).

### surfaces/ (9 modules, ~1,500 LOC post-refactoring)

__Responsibility:__ Presentation-layer infrastructure shared by CLI, HTTP,
and MCP surfaces. Output formatting, server assembly, context objects,
remote client.

__Cohesion:__ Medium. The binding concept is "presentation-layer machinery
that is not tied to a specific command or route." CliContext and formatting
serve commands/. QuarryContext and http_server serve routes/. McpSession
and mcp_server serve the MCP surface.

__Anti-pattern check -- is this a grab bag?__ Borderline. The three
surface types (CLI, HTTP, MCP) are distinct. The justification for grouping:
(1) all are layer 5 (presentation infrastructure), (2) they share no
upward dependencies (none import from commands/ or routes/), (3) they
are consumed by layer 5 siblings and layer 6 entry points. Splitting into
`cli/`, `http/`, `mcp/` packages would create 3 packages of 2-3 modules
each -- the organizational overhead exceeds the benefit at this scale.

---

## 5. OO Design Principles

### Single Responsibility

Each package has one reason to change:

| Package | Reason to change |
|---------|-----------------|
| types layer | Domain vocabulary changes |
| db/ | Storage schema or query implementation changes |
| extractors/ | Document format support changes |
| ingestion/ | Pipeline topology or chunking strategy changes |
| services/ | Application workflow logic changes |
| hooks/ | Claude Code event handling changes |
| routes/ | HTTP API contract changes |
| commands/ | CLI interface changes |
| surfaces/ | Presentation infrastructure changes |

### Open/Closed

Adding new capabilities does not modify existing packages:

- __New document format:__ Add one file to extractors/ implementing
  `FormatExtractor`. Register it in ingestion/pipeline.py's extractor
  registry. No changes to db/, services/, hooks/, routes/, or commands/.

- __New CLI command:__ Add one file to commands/. Register the typer
  command in __main__.py. No changes to services/ or db/.

- __New service backend (e.g., Docker):__ Add one class implementing
  `ServiceBackend` in services/service.py. No changes to the existing
  `LaunchdBackend` or `SystemdBackend`.

- __New HTTP endpoint:__ Add one file to routes/. Register the route in
  surfaces/http_server.py. No changes to services/ or db/.

### Dependency Inversion

High-level packages depend on abstractions, not concrete classes:

- ingestion/ depends on `FormatExtractor` (Protocol), not on
  `TextExtractor` or `PdfExtractor` directly. The protocol is in
  extractors/protocol.py. Concrete extractors are injected into the
  pipeline's extractor registry.

- services/service.py depends on `ServiceBackend` (Protocol), not on
  `LaunchdBackend` or `SystemdBackend`. The backend is selected at
  runtime by `ServiceManager.detect_platform()`.

- The types layer (`types.py`) defines `LanceDB`, `LanceTable`, and
  `OcrBackend` protocols that db/ and ingestion/ depend on without
  coupling to lancedb or rapidocr implementation details.

### Acyclic Dependencies

The layer numbering (0-6) guarantees acyclicity. A module at layer N
imports only from layers 0 through N-1. There are no edges from lower
layers to higher layers.

Proof by inspection of the dependency table in section 3: every non-zero
cell is above and to the left of the consumer row. No row has a non-zero
entry in a column representing a package at the same or higher layer.

Specific potential cycles that do NOT exist:
- hooks/ does not import from routes/ or commands/
- services/ does not import from hooks/ (backfill imports
  extract_transcript_text -- this function moves to hooks/transcript.py
  but services/backfill.py will import it directly, creating a
  services/ -> hooks/ edge. __Resolution:__ Move transcript extraction
  functions to the types layer (a new `transcript.py` at top level)
  since they are pure transforms with zero quarry dependencies. This
  eliminates the cycle.)
- routes/ does not import from commands/
- surfaces/ does not import from routes/ or commands/

---

## 6. What Stays Top-Level and Why

| Module | LOC | Reason |
|--------|-----|--------|
| `__init__.py` | 70 | Package root. Lazy-loads public API. |
| `__main__.py` | ~400 | CLI entry point. Arg declarations + delegation. |
| `_hook_entry.py` | ~60 | Claude Code dispatch. Fixed event set. |
| `_sql.py` | 20 | Shared by db/ and results.py. Too small for a package. |
| `types.py` | 117 | Protocols shared by all packages. |
| `results.py` | 91 | Value objects shared by all packages. |
| `models.py` | ~150 | Core data models shared by all packages. |
| `collections.py` | ~80 | CollectionName consumed everywhere. |
| `config.py` | ~120 | Settings consumed everywhere. |
| `logging_config.py` | ~73 | LoggingConfig consumed by entry points. |

Total: ~1,180 LOC at top level (7% of codebase). These modules form layer 0
-- they have zero dependencies on quarry internals and are imported by
every package above them.

---

## 7. Anti-Patterns Avoided

__Grab-bag package.__ services/ is the highest risk for low cohesion (15
modules, 4 sub-domains). Mitigated by: consistent dependency direction
(all consume db/ingestion, all consumed by hooks/commands/routes), clear
naming conventions (sync_*, service-related), and a size trigger (if any
sub-group exceeds 6 modules, extract to sub-package).

__God package.__ No package exceeds 2,500 LOC post-refactoring (services/).
For comparison, the current pipeline.py alone is 1,612 LOC.

__High instability.__ commands/ has the highest efferent coupling (5
supplier packages). This is acceptable because commands/ is a leaf
package -- nothing imports from it except __main__.py. High efferent
coupling in a leaf is the Stable Abstractions Principle working as
designed: concrete, volatile modules depend on stable abstractions.

__Circular dependency.__ The one near-cycle (services/backfill.py ->
hooks/transcript.py) is resolved by moving transcript extraction to the
types layer. See section 5.

__Over-packaging.__ The proposal does not split services/ into sync/,
deploy/, health/ sub-packages. At 2-3 modules each, these would add
package ceremony (6 more `__init__.py` files) without improving
navigability. The threshold for splitting is 6+ modules in a sub-domain.

---

## 8. Changes Needed to the Refactoring Plan

1. __New package: `ingestion/`.__ Steps 3.1-3.15 create extractors and
   pipeline classes but leave pipeline.py, url_ingester.py, url_fetcher.py,
   image_preparer.py, text_splitter.py, pdf_text_extractor.py, chunker.py,
   backends.py, provider.py, and ocr_local.py at the top level. Add step
   3.16: move these 10 modules into `ingestion/`, create `__init__.py`
   with `__all__`.

2. __New package: `services/`.__ Steps 4.1-4.16 create service classes in
   their existing modules. Add step 4.17: move 15 modules into `services/`,
   create `__init__.py`.

3. __New package: `surfaces/`.__ Steps 6.1-6.9 and 7.1-7.2 create
   presentation infrastructure classes. Add step 7.20: move cli_context.py,
   remote_client.py, formatting.py, latex_utils.py, artifacts.py,
   task_manager.py, _stdlib.py, and residual http_server.py/mcp_server.py
   into `surfaces/`.

4. __Move `transcript.py` to top level.__ The plan puts transcript
   functions in `hooks/transcript.py` (step 5.1). These are pure transforms
   with zero quarry dependencies -- they belong in the types layer to avoid
   a services/ -> hooks/ cycle (backfill.py imports extract_transcript_text).
   Move to `src/quarry/transcript.py` at top level.

5. __`IngestJob` placement.__ Step 1.7 puts IngestJob in `_hook_entry.py`.
   It should be created directly in `hooks/background_ingester.py` if the
   hooks/ package exists by then (step 5.6), or in models.py temporarily
   if not.

6. __Package-move steps are bulk import updates.__ Each adds one step:
   create `__init__.py`, move files, update all import paths. These are
   mechanical but high-touch (every consumer import changes). One PR each.

Total new steps: 4 (steps 3.16, 4.17, 5.1 modification, 7.20).
Revised total: 88 steps.

---

## 9. Migration Schedule by Phase

| Phase | Steps | Package moves |
|-------|-------|--------------|
| 0 (pre-flight) | 0.1-0.10 | none |
| 1 (types) | 1.1-1.10 | none |
| 2 (done) | 2.1-2.8 | db/ created |
| 3 (ingestion) | 3.1-3.16 | extractors/ populated, ingestion/ created |
| 4 (services) | 4.1-4.17 | services/ created |
| 5 (hooks) | 5.1-5.7 | hooks/ created, transcript.py to top level |
| 6 (HTTP/MCP) | 6.1-6.9 | routes/ created |
| 7 (CLI) | 7.1-7.20 | commands/ populated, surfaces/ created |

At each package-move step, `make check` must pass. The move is purely
mechanical: `git mv` + import path updates + `__init__.py` creation.
No behavioral changes.
