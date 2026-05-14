# Surfaces and Services: OO Design Report

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

Total new classes: 27

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
