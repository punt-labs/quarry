# Design: Fix Compaction Death Spiral from Unguarded Concurrent Sync

**Bead**: quarry-jms6
**Pipeline**: standard-2026-04-18-b600de
**Author**: claude (COO)
**Date**: 2026-04-18

## Problem

The `quarry serve` process (PID 2109027, running 5 days) is consuming
10.7 GB RAM, 13 CPU cores sustained, with 74 days of accumulated CPU
time. The LanceDB database has grown to 83 GB for 99K chunks:

| LanceDB metric | Count |
|---|---|
| Data fragments | 133,439 (48 GB) |
| Version manifests | 268,051 (33 GB) |
| Transaction files | 278,678 (1.1 GB) |

The root cause is a compaction death spiral with five contributing factors.

### 1. Sync runs inside the serve process via HTTP

When `quarry sync` runs (spawned by every SessionStart hook), it finds
the proxy config at `~/.punt-labs/mcp-proxy/quarry.toml` and routes as
`POST /sync` to the serve process. `sync_all()` runs in the serve
process's threadpool — not in the sync subprocess.

### 2. The sync lock protects the wrong thing

`hooks.py:89-118` tracks the PID of the sync **client subprocess**, not
the serve process doing the actual work. When the client times out
(600s) or completes, its PID goes stale. The next SessionStart launches
a new client while the serve process is still running `sync_all()` from
the previous request.

### 3. No concurrency guard on the HTTP endpoint

`http_server.py:607` calls `run_in_threadpool(sync_all, ctx.db,
ctx.settings)` with no lock. Multiple concurrent HTTP sync requests
produce multiple concurrent `sync_all()` calls.

### 4. Per-document LanceDB writes

Each document sync does `delete_document()` + `insert_chunks()` = 2
LanceDB transactions per document. Each transaction creates a new data
fragment and a new version manifest that references all active fragments.

### 5. Compaction can't keep up

With 133K fragments, `optimize_table()` must read all fragments to
merge them. During that scan, new writes create new fragments. The
version manifest size grows with every write (now 185 KB each). The
cycle is self-reinforcing.

## Solution

Five changes, ordered by priority.

### Fix 1: Server-side sync lock

Add an `asyncio.Lock` to the `/sync` HTTP route. Only one `sync_all()`
runs at a time inside the serve process. Concurrent requests receive
HTTP 409 Conflict with a JSON body:

```json
{"error": "Sync already in progress", "status": "running"}
```

**File**: `src/quarry/http_server.py`

The lock is a module-level `asyncio.Lock` on `_QuarryContext` (or a
module global initialized at serve startup). The route acquires it
with `try_lock` (non-blocking) — if held, return 409 immediately.

**Rejected alternative**: queue concurrent requests and serialize them.
Queuing hides the problem — the caller should know sync is already
running and not retry.

### Fix 2: Registration subsumption

When `register_directory(conn, dir, collection)` is called, check if
`dir` is an ancestor of any existing registrations. If so, deregister
the children (they are now covered by the parent). Symmetrically, reject
registering a child directory when an ancestor is already registered —
return an error naming the parent registration.

**File**: `src/quarry/sync_registry.py`

Subsumption logic — **the parent always wins**:

```
register(dir):
  for each existing registration R:
    if dir is ancestor of R.directory:
      deregister R  # parent subsumes child — child is removed
    elif R.directory is ancestor of dir:
      reject with error  # child already covered by parent — refuse to register
  proceed with registration
```

The parent registration stays; child registrations are removed. A
subsequent attempt to register a child directory (e.g. via the
SessionStart hook's auto-register of cwd) must be rejected with an
error naming the covering parent.

The collection name for the subsuming parent is the one the caller
provides. The deregistered children's documents remain in LanceDB
under their old collection names until the next sync re-ingests them
under the parent's collection. This is acceptable — the data is not
lost, just reclassified on next sync.

**Rejected alternative**: silently merge child documents into the parent
collection at registration time. Too complex — requires re-tagging all
chunks in LanceDB. Better to let the next sync handle it naturally.

### Fix 3: Batch LanceDB writes during sync

Accumulate all chunks across documents within a single
`sync_collection()` call into a list. Perform a single `table.add()`
at the end of the collection sync, rather than one `table.add()` per
document.

Deletes must still be per-document (LanceDB delete is predicate-based,
and we need the old chunks gone before adding new ones for overwrite
semantics). But batching the adds reduces N write transactions to 1
per collection sync.

**Files**: `src/quarry/database.py`, `src/quarry/sync.py`,
`src/quarry/pipeline.py`

The `insert_chunks()` function gains a sibling `batch_insert_chunks()`
that accepts a list of `(chunks, vectors)` pairs and does a single
`table.add()`. The sync loop calls this after all documents in a
collection have been processed.

**Rejected alternative**: batch deletes too via a single predicate
(`document_name IN (...)` ). LanceDB's delete predicate syntax may not
support large IN clauses efficiently. Per-document delete is fine since
deletes are cheap compared to adds (no embedding work).

### Fix 4: Guard optimize_table() on fragment count

Before calling `table.optimize()`, check the fragment count. If it
exceeds a threshold (e.g. 10,000), log an error and skip:

```
WARNING: LanceDB table has N fragments (threshold: 10000).
Skipping optimization — manual compaction required.
Run: quarry optimize --force
```

This prevents the serve process from entering the death spiral. The
operator must run manual compaction (a new CLI command) when the
database is in a degraded state.

**Files**: `src/quarry/database.py`, `src/quarry/__main__.py`

Add `quarry optimize` CLI command that runs `optimize_table()` with
progress reporting, optionally with `--force` to bypass the threshold
guard. This gives the operator an escape hatch for degraded databases.

**Rejected alternative**: automatically run compaction in smaller
batches. LanceDB's `optimize()` doesn't support partial compaction in
v0.30. A future LanceDB version may, but we can't depend on it.

### Fix 5: Async sync endpoint

The `/sync` route should accept the request and return immediately
with a task ID. The actual `sync_all()` runs as a background
`asyncio.Task`. A separate endpoint (`GET /sync/<task_id>`) returns
status (running, completed, failed) and results.

**Files**: `src/quarry/http_server.py`, `src/quarry/__main__.py`

Response on `POST /sync`:

```json
{"task_id": "sync-2026-04-18-001", "status": "accepted"}
```

Response on `GET /sync/<task_id>`:

```json
{"task_id": "sync-2026-04-18-001", "status": "completed", "results": {...}}
```

The sync CLI command (`quarry sync`) becomes fire-and-forget: it sends
the POST, prints the task ID, and exits. The hook subprocess no longer
blocks for 600 seconds.

The server-side sync lock (Fix 1) still applies — only one sync task
runs at a time. A second POST while a sync is running returns 409 with
the existing task ID so the caller can poll it.

**Rejected alternative**: WebSocket streaming of sync progress. Over-
engineered for a background batch operation. Polling is sufficient.

## Migration

1. Deploy the fix. The serve process must be restarted to pick up the
   new code.
2. Before restarting, the operator should delete the pathological
   LanceDB data and re-ingest from scratch. With 133K fragments,
   compaction will take longer than re-ingestion:
   ```bash
   quarry serve stop  # or kill the process
   rm -rf ~/.punt-labs/quarry/data/default/lancedb/chunks.lance/
   quarry serve --port 8420 --host 0.0.0.0 --tls &
   quarry sync
   ```
3. Remove the overlapping `punt-labs` parent registration if present:
   ```bash
   quarry deregister punt-labs
   ```
   After Fix 2 ships, this case is handled automatically.

## Test plan

- **Fix 1**: test that concurrent `POST /sync` requests return 409 for
  the second caller. Test that the lock is released after sync completes
  (both success and failure).
- **Fix 2**: test registering a parent deregisters children. Test
  registering a child when parent exists returns error. Test that
  non-overlapping registrations are unaffected.
- **Fix 3**: test that a sync of N documents produces 1 `table.add()`
  call, not N. Verify chunk count and content are identical to the
  per-document path.
- **Fix 4**: test that `optimize_table()` skips when fragment count
  exceeds threshold. Test that `quarry optimize --force` bypasses the
  guard. Test the warning message.
- **Fix 5**: test that `POST /sync` returns 202 with task ID. Test
  that `GET /sync/<task_id>` returns status. Test that the CLI command
  is fire-and-forget. Test that a second POST while running returns 409
  with the existing task ID.
