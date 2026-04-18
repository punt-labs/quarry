# Async Operations: Unified Task Model for HTTP API

**Status**: IMPLEMENTED
**Date**: 2026-04-18
**Author**: rmh

## Problem Statement

The quarry HTTP API has an incoherent mix of synchronous and asynchronous
endpoints. Two mutating operations -- `/ingest` and `/sync` -- return 202
Accepted with a `task_id` and run in the background. Every other mutating
operation blocks until completion and returns the result inline:

| Endpoint | Current model | Typical latency |
|----------|--------------|-----------------|
| POST /sync | async (202 + task_id) | 5s -- 120s+ |
| POST /ingest | async (202 + task_id) | 2s -- 60s+ |
| POST /remember | **sync (200)** | 100ms -- 10s |
| DELETE /documents | **sync (200)** | 10ms -- 500ms |
| DELETE /collections | **sync (200)** | 10ms -- 500ms |
| POST /registrations | **sync (200)** | 5ms -- 50ms |
| DELETE /registrations | **sync (200)** | 10ms -- 2s (with purge) |

The CLI remote path has a 15-second default timeout
(`_DEFAULT_REMOTE_TIMEOUT`). Any synchronous endpoint that exceeds this
timeout causes a connection error on the client and a wasted server-side
computation. `remember` with a large document (embedding + DB write) and
`deregister` with `keep_data=false` (bulk delete) are the most likely to
hit this.

Meanwhile, the MCP server already runs *every* mutating operation via
`_background()` in a thread pool -- it is fully async. The HTTP API is the
only surface with this inconsistency.

### Current implementation state

The `TaskState` dataclass and `_QuarryContext` were partially migrated
toward a unified model:

- `TaskState` already has a `kind` field and a single definition (line 235).
- `SyncTaskState` and `IngestTaskState` are aliases for `TaskState` (line
  247-248).
- `_QuarryContext.__init__` defines `self.tasks: dict[str, TaskState]` and
  `self.task_refs: dict[str, asyncio.Task[None]]` (lines 267-268).

But the route handlers still reference the old fields:

- `_sync_route` reads/writes `ctx.sync_task` and `ctx.sync_task_ref` --
  attributes that do not exist in `__init__`.
- `_ingest_route` reads/writes `ctx.ingest_tasks` and
  `ctx.ingest_task_refs` -- also absent from `__init__`.
- Status routes `_sync_status_route` and `_ingest_status_route` read the
  old per-operation fields.

This is a half-finished migration. The unified `tasks` dict exists but is
not used.

## Unified TaskState Design

The existing `TaskState` dataclass is already correct:

```python
@dataclass
class TaskState:
    task_id: str
    kind: str  # "sync", "ingest", "remember", "delete", "register", "deregister", "optimize"
    status: str = "running"
    results: dict[str, object] = field(default_factory=dict)
    error: str = ""
```

No changes needed to the dataclass itself. The `kind` field distinguishes
operation types within the single `tasks` dict.

### Task store migration

All route handlers switch from per-operation fields to the unified store:

```python
# Before (sync)
ctx.sync_task = state
ctx.sync_task_ref = asyncio.create_task(...)

# After (all operations)
ctx.tasks[task_id] = state
ctx.task_refs[task_id] = asyncio.create_task(...)
```

Sync concurrency control becomes a dict scan:

```python
running_sync = next(
    (t for t in ctx.tasks.values()
     if t.kind == "sync" and t.status == "running"),
    None,
)
if running_sync is not None:
    return JSONResponse({"error": "Sync already in progress", ...}, status_code=409)
```

Remove `SyncTaskState` and `IngestTaskState` aliases after all references
are updated.

### Task creation helper

Extract a helper to eliminate repeated task-creation boilerplate:

```python
def _create_task(
    ctx: _QuarryContext,
    kind: str,
    coro: Coroutine[None, None, None],
) -> tuple[str, TaskState]:
    task_id = f"{kind}-{uuid.uuid4().hex[:12]}"
    state = TaskState(task_id=task_id, kind=kind)
    ctx.tasks[task_id] = state
    ctx.task_refs[task_id] = asyncio.create_task(coro)
    return task_id, state
```

## Route Table

### Endpoints after migration

| Method | Path | Model | Concurrency | Notes |
|--------|------|-------|-------------|-------|
| GET | /health | sync | unlimited | No auth required |
| GET | /ca.crt | sync | unlimited | No auth required |
| GET | /search | sync | unlimited | Read-only |
| GET | /show | sync | unlimited | Read-only |
| GET | /documents | sync | unlimited | Read-only |
| GET | /collections | sync | unlimited | Read-only |
| GET | /registrations | sync | unlimited | Read-only |
| GET | /databases | sync | unlimited | Read-only |
| GET | /status | sync | unlimited | Read-only |
| POST | /use | sync | unlimited | Client-side only, rejects |
| **POST** | **/sync** | **async (202)** | **reject concurrent (409)** | Unchanged |
| **POST** | **/ingest** | **async (202)** | **unlimited** | Unchanged |
| **POST** | **/remember** | **async (202)** | **unlimited** | **Convert from sync** |
| **DELETE** | **/documents** | **async (202)** | **unlimited** | **Convert from sync** |
| **DELETE** | **/collections** | **async (202)** | **unlimited** | **Convert from sync** |
| **POST** | **/registrations** | **async (202)** | **unlimited** | **Convert from sync** |
| **DELETE** | **/registrations** | **async (202)** | **unlimited** | **Convert from sync** |
| **GET** | **/tasks/{task_id}** | sync | unlimited | **New unified polling** |
| GET | /sync/{task_id} | sync | unlimited | Alias to /tasks/{task_id} |
| GET | /ingest/{task_id} | sync | unlimited | Alias to /tasks/{task_id} |
| WebSocket | /mcp | N/A | per-session | Unchanged |

### Sync concurrency control

`/sync` keeps its 409 rejection for concurrent requests. The check
scans `ctx.tasks` for any task with `kind == "sync"` and
`status == "running"`. All other operations allow unlimited concurrency
-- they operate on independent data (different documents, different
collections, different registrations) and LanceDB handles concurrent
writes at the storage layer.

### Endpoints NOT converted

Read operations (`/search`, `/show`, `/documents` GET, `/collections` GET,
`/registrations` GET, `/databases`, `/status`, `/health`, `/ca.crt`) remain
synchronous. They complete in under 100ms; the overhead of task tracking
would exceed the operation itself.

`POST /use` remains synchronous because it always returns an error (the
server is fixed to one database).

## Decision: POST /optimize

**Verdict: no HTTP surface for now.**

Optimize runs LanceDB compaction and index rebuilds. It is CPU-intensive
and locks the table for the duration. Risks of remote compaction:

1. **Compaction death spiral.** The fragment-count safety guard (>10,000
   fragments skips by default) exists specifically because compaction on a
   fragmented database can make things worse. A remote caller cannot
   inspect fragment count before deciding.

2. **Table lock contention.** During compaction, concurrent writes
   (ingest, remember, sync, delete) queue behind the table lock. A
   remotely triggered optimize could stall all other operations for
   minutes.

3. **No abort mechanism.** Once started, LanceDB compaction cannot be
   cancelled. A fire-and-forget pattern with no abort is acceptable for
   ingest (idempotent) but dangerous for compaction (non-idempotent
   structural change).

4. **Operator-only intent.** Compaction is a maintenance operation, not a
   user workflow. It should be triggered by the machine operator (CLI,
   cron, systemd timer), not by a remote client.

If a future need arises (e.g., a remote admin dashboard), optimize should
require a separate admin-scoped API key and the `--force` flag equivalent
in the request body. This is out of scope for the current migration.

## Backwards Compatibility Plan

### Polling endpoint aliases

`GET /sync/{task_id}` and `GET /ingest/{task_id}` become aliases for
`GET /tasks/{task_id}`. Implementation: the unified `_task_status_route`
handler is registered on all three paths. No redirect -- direct handler
reuse.

```python
Route("/tasks/{task_id}", _task_status_route, methods=["GET"]),
Route("/sync/{task_id}", _task_status_route, methods=["GET"]),
Route("/ingest/{task_id}", _task_status_route, methods=["GET"]),
```

The task_id prefix (`sync-`, `ingest-`, `remember-`, etc.) already
encodes the operation kind, so a client polling `/sync/sync-abc123` and
one polling `/tasks/sync-abc123` get identical responses.

### Response shape for converted endpoints

Endpoints that currently return 200 with result data will return 202 with
`{"task_id": "...", "status": "accepted"}`. This is a **breaking change**
for clients that parse the synchronous response body.

Affected clients:

1. **CLI remote paths** for `remember`, `delete`, `register`,
   `deregister`. These must switch to fire-and-forget (print task_id,
   exit 0) -- matching the existing pattern for `ingest` and `sync`.

2. **MCP server.** The MCP server uses its own `_background()` mechanism
   and does not call the HTTP API. No change needed.

3. **External consumers.** The HTTP API is not versioned and has no
   documented external consumers. The MCP WebSocket is the stable
   integration surface.

### SyncTaskState / IngestTaskState removal

Remove the type aliases after all route handlers and all test files are
updated. The aliases exist only for backwards compatibility during the
transition (lines 247-248). Tests that import `SyncTaskState` or
`IngestTaskState` switch to `TaskState`.

## CLI Changes

### Endpoints switching to fire-and-forget

Four CLI commands currently block on the synchronous HTTP response when
running in remote mode:

| Command | Current remote behavior | New behavior |
|---------|------------------------|-------------|
| `quarry remember` | Blocks, prints result | Prints `task_id`, exits 0 |
| `quarry delete` | Blocks, prints result | Prints `task_id`, exits 0 |
| `quarry register` | Blocks, prints result | Prints `task_id`, exits 0 |
| `quarry deregister` | Blocks, prints result | Prints `task_id`, exits 0 |

The new pattern matches the existing `ingest` and `sync` remote paths:

```python
remote_resp = _remote_https_request("POST", "/remember", proxy_config, body=body)
task_id = remote_resp.get("task_id", "")
status = remote_resp.get("status", "")
_emit(remote_resp, f"Remember {status}: task_id={task_id}")
return
```

### Error handling

- 400 errors (validation) are still returned synchronously before task
  creation. The CLI handles these the same as today.
- Server-side errors during background execution are only visible via
  `GET /tasks/{task_id}`. The fire-and-forget CLI does not poll -- the
  user must check task status manually if they care about the outcome.
- 409 for sync remains unchanged.

### Local paths unchanged

CLI local paths (`quarry remember` without a remote server) continue to
run synchronously. Only the remote code path changes.

## Task Garbage Collection

### Problem

The `tasks` dict grows without bound. Every async operation creates a
`TaskState` entry that is never removed. A long-running daemon processing
thousands of ingests will accumulate thousands of completed task records.

### Strategy: TTL-based eviction

Completed and failed tasks are evicted after a fixed TTL. Running tasks
are never evicted.

Add a `created_at` field to `TaskState`:

```python
@dataclass
class TaskState:
    task_id: str
    kind: str
    status: str = "running"
    results: dict[str, object] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.monotonic)
```

Eviction runs as a sweep before each new task creation:

```python
TASK_TTL_SECONDS = 3600  # 1 hour

def _gc_tasks(ctx: _QuarryContext) -> None:
    now = time.monotonic()
    expired = [
        tid for tid, t in ctx.tasks.items()
        if t.status != "running" and (now - t.created_at) > TASK_TTL_SECONDS
    ]
    for tid in expired:
        del ctx.tasks[tid]
        ctx.task_refs.pop(tid, None)
```

### Why not LRU / max-count?

A max-count cap (e.g., keep only the last 1000 tasks) would evict recent
completed tasks during burst ingestion. A client polling for a task_id
received 30 seconds ago could get a 404. TTL-based eviction guarantees
that any task_id remains valid for the full TTL window regardless of
burst volume.

### Why not a background timer?

A periodic asyncio timer adds complexity (cancellation on shutdown,
interval tuning) for marginal benefit. The sweep on task creation is
O(n) over the task dict, but n is bounded by the creation rate times
the TTL -- at most a few thousand entries in practice. A timer-based
approach is only warranted if profiling shows the sweep adds measurable
latency to task creation.

## Test Migration Plan

### Tests affected by response code changes

Tests for the five converted endpoints currently assert `status_code == 200`
with inline result data. These must change to assert `status_code == 202`
with `{"task_id": ..., "status": "accepted"}`.

Affected test classes and approximate test counts:

| Test class | Tests | Change |
|-----------|-------|--------|
| `TestRemember` | 14 tests | `test_success` asserts 200 with result -> 202 with task_id |
| `TestDelete` (document) | 4 tests | Assert 202 + task_id for success cases |
| `TestDelete` (collection) | 4 tests | Assert 202 + task_id for success cases |
| `TestRegistrations` (POST) | 5 tests | Assert 202 + task_id for success cases |
| `TestRegistrations` (DELETE) | 6 tests | Assert 202 + task_id for success cases |

Validation tests (400 errors) are unaffected -- validation still happens
synchronously before task creation.

### Tests affected by unified task store

Tests that reference `ctx.sync_task` or `ctx.ingest_tasks` directly must
switch to `ctx.tasks`:

```python
# Before
ctx.sync_task = SyncTaskState(task_id="sync-test123", status="running")

# After
ctx.tasks["sync-test123"] = TaskState(task_id="sync-test123", kind="sync", status="running")
```

The `test_concurrent_sync_returns_409` test (line 1266) and
`test_sync_status_completed` test (line 1296) both set `ctx.sync_task`
directly. These switch to `ctx.tasks[...]`.

### Tests affected by import changes

Tests importing `SyncTaskState` or `IngestTaskState` switch to
`TaskState`. These imports appear in:

- `test_concurrent_sync_returns_409` (line 1273)
- `test_sync_status_completed` (line 1303)

### New tests required

1. **Unified polling endpoint**: `GET /tasks/{task_id}` returns the same
   response as `GET /sync/{task_id}` and `GET /ingest/{task_id}` for the
   same task_id. Test all three paths with the same task and assert
   identical response bodies.

2. **Task GC**: Create tasks with a mocked `time.monotonic`, advance
   time past the TTL, create a new task (triggering GC), verify expired
   tasks are gone and running tasks survive.

3. **Converted endpoint 202 responses**: For each of the 5 converted
   endpoints, test that:
   - Success returns 202 with `task_id` and `status == "accepted"`.
   - The background task eventually sets `status == "completed"` with
     the expected `results` shape.
   - The background task sets `status == "failed"` with `error` when the
     underlying operation raises.

4. **Kind field correctness**: Each operation creates a TaskState with
   the correct `kind` value. Parametrize over all 7 kinds.

### Migration order

1. Add `GET /tasks/{task_id}` route and its tests.
2. Register `/sync/{task_id}` and `/ingest/{task_id}` as aliases.
3. Migrate sync routes to use `ctx.tasks` dict, update sync tests.
4. Migrate ingest routes to use `ctx.tasks` dict, update ingest tests.
5. Convert `remember` to async, update remember tests.
6. Convert `delete` (document + collection) to async, update delete tests.
7. Convert `register` and `deregister` to async, update registration tests.
8. Update CLI remote paths for remember, delete, register, deregister.
9. Add task GC with TTL.
10. Remove `SyncTaskState` and `IngestTaskState` aliases.

Each step is independently shippable and testable. Steps 3-4 fix the
existing broken references (routes using attributes not in `__init__`).
Steps 5-7 are the actual feature work. Step 8 must ship with or after
steps 5-7 to avoid the CLI parsing a 202 as a success result.

## Rejected Alternatives

### Just increase the timeout

Increasing `_DEFAULT_REMOTE_TIMEOUT` from 15s to 120s would paper over
the immediate problem for `remember` but creates worse problems:

- The CLI blocks for up to 2 minutes with no progress indication.
- A network interruption at second 90 wastes the server's completed
  work -- the client never sees the result.
- `deregister` with bulk delete can exceed any reasonable timeout on a
  large collection.
- The fundamental asymmetry remains: some endpoints are async, some are
  not. Every new long-running endpoint requires a timeout judgment call.

The 202 pattern eliminates the timeout problem entirely. The server
acknowledges receipt in milliseconds; the work completes regardless of
client connectivity.

### WebSocket streaming

Replace the 202-poll pattern with WebSocket push notifications for task
progress:

- **Complexity**: requires a persistent connection, reconnection logic,
  message framing, and multiplexing (multiple tasks on one socket).
- **Client burden**: the CLI uses `http.client` (stdlib). Adding
  WebSocket support requires a third-party dependency or a custom
  implementation.
- **Existing surface**: the MCP WebSocket already exists for rich
  bidirectional communication. Adding a second WebSocket surface for
  HTTP task progress creates two competing real-time channels.
- **Marginal value**: quarry tasks are fire-and-forget from the CLI's
  perspective. The agent (via MCP) does not need to poll -- it
  already gets results inline via the MCP tool response. The only
  consumer that would benefit from streaming is a hypothetical web
  dashboard, which does not exist.

The 202-poll pattern is simpler, uses stdlib HTTP, and matches the
existing ingest/sync implementation. WebSocket streaming would be
justified only if a real-time dashboard becomes a product requirement.

### Server-Sent Events (SSE)

SSE is simpler than WebSockets for server-to-client push, but shares the
same fundamental problem: the CLI uses `http.client`, which does not
support SSE natively. It also requires keeping the HTTP connection open
for the duration of the task, which defeats the purpose of async
(decoupling client lifetime from task lifetime).

### Per-operation task stores

Keep separate dicts per operation (`sync_tasks`, `ingest_tasks`,
`remember_tasks`, etc.) instead of a unified `tasks` dict:

- Duplicates the task lifecycle code (create, poll, GC) for each
  operation type.
- Makes the unified polling endpoint (`GET /tasks/{task_id}`) require
  searching across multiple dicts.
- The `kind` field on `TaskState` already distinguishes operations
  within a single store.

A unified store with a `kind` discriminator is the standard pattern
(cf. Celery task results, Kubernetes job status).
