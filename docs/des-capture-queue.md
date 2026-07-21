# DES-042: Daemon-Owned Serialized Capture/Index Queue

Status: IMPLEMENTED (PR #371 — DES-042 queue portion of quarry-lxrk)
Bead: quarry-lxrk
Supersedes the residual-concurrency risk left open by DES-041 / PR #367.
Scope: daemon-internal (`quarry.daemon` + `retrieval`/`ingestion`). No wire change.

## 1. Problem

PR #367 (DES-041) made the compaction and web-fetch hooks thin clients: they
POST a capture to the resident `quarryd`, get a `202`, and move on. That
structurally killed the per-hook engine starvation of quarry-lnog — no hook
loads ONNX anymore. One hazard remains: **concurrency at the daemon.**

Captures arrive in bursts. Several sessions compact at once; a web-fetch and a
compaction land together; a `quarry backfill` replays a directory of archived
transcripts. Each POST becomes an independent asyncio task that runs
`run_in_threadpool(job.run)` immediately. Every one of those jobs then:

1. chunks its content,
2. embeds each window on the **shared** write ONNX session, and
3. deletes-then-inserts to the **same** LanceDB collection.

Two things break under a burst:

- **Write contention / lost update.** `_chunk_embed_store` does
  `delete_document(...)` then `progressive_insert(...)` against one shared write
  connection (`src/quarry/ingestion/pipeline.py:975-988`). DES-034's module-global
  `chunk_store._write_lock` (`chunk_store.py:27`) serializes each individual
  `table.add()`/`table.delete()`, so there is no torn write and no LanceDB
  optimistic-commit conflict — but the lock is released *between* the
  overwrite-delete and the flush adds, so the delete-then-insert pair is **not**
  atomic across callers. Two captures for the *same* document (the stable
  `session-<id>` name, both `overwrite=True`) interleave as
  `delete A · delete B · add A · add B`, leaving **both** chunk sets resident. The
  later-wins guarantee the stable name is supposed to provide is lost. Full
  analysis of why DES-034's single-consumer model does not already close this: §2.1.
- **Oversubscription.** The ONNX int8 model runs with `intra_op_num_threads =
  ncpu` (`src/quarry/thread_config.py:42`) — one embed batch saturates every
  core. N embed jobs in flight on a fixed core count re-creates exactly the
  oversubscription DES-041 was meant to end. The starlette/AnyIO threadpool
  admits up to 40 concurrent `run_in_threadpool` calls by default, so a 40-way
  capture burst puts 40 CPU-bound embed jobs on the scheduler at once.

The daemon must **serialize ingest work per collection** (one in-flight writer
per LanceDB table) and **bound total embed concurrency** (to 1 globally,
because embedding saturates the cores — see §4.4) instead of running every
capture in parallel the instant it arrives.

## 2. The current unbounded-concurrency path (cited)

A `POST /v1/capture` reaches embedding + write like this:

| Step | Location |
|------|----------|
| Route validates body, derives `<repo>-captures`, builds job | `daemon/routes/captures.py:31-79` |
| `state = self.ctx.tasks.begin("capture")` (status `"running"`) | `daemon/routes/captures.py:43`; `daemon/tasks.py:84-98` |
| `self.accept(state, job.run(self.ctx, state))` | `daemon/routes/captures.py:44` |
| `accept` → **`asyncio.create_task(coro)`** + track, return 202 | `daemon/routes/base.py:116-127` |
| `CaptureIngestJob.run` → `run_in_threadpool(self._capture, ctx)` | `daemon/ingest_jobs.py:174-186` |
| `_capture` → `ScrubbedIngestJob.scrub_and_ingest` → `ingest_content` | `daemon/ingest_jobs.py:181-183`, `58-80` |
| `ingest_content` → `_chunk_embed_store` (delete + `progressive_insert`) | `ingestion/pipeline.py:449-455`, `975-988` |
| `progressive_insert` embeds via the **shared cached** write backend | `ingestion/streaming.py:111`; `ingestion/backends.py:36-43` |

`POST /v1/remember` (`daemon/routes/ingestion.py:30-43`) and `POST /v1/ingest`
(`daemon/routes/ingestion.py:45-71`) take the identical `accept → create_task →
run_in_threadpool → _chunk_embed_store` path.

**Where unbounded concurrency enters:** `RouteGroup.accept`
(`daemon/routes/base.py:122`) fires `asyncio.create_task` per request with no
gate. Every accepted capture becomes a live task; each hops onto the AnyIO
threadpool (≤ 40 slots) and runs embed + write immediately. Nothing serializes
same-collection writers, and nothing bounds how many embed jobs run at once.

Note the embed backends differ by path: queries use a *fresh, uncached* session
(`http_resources.py:57-60`, `new_embedding_backend`), while ingest uses the
*module-cached singleton* (`backends.py:36-43`, `get_embedding_backend`).
`session.run()` serialises callers on an internal ORT mutex, so concurrent
ingest embeds already queue at the session — but only incidentally. They still
hold 40 threadpool threads and 40 chunk sets resident, and the LanceDB
delete+insert races remain completely ungated. Incidental serialization is not
a design; a burst still oversubscribes and can still lose an update.

### 2.1 Does DES-034's single-consumer serialization already cover this?

**No — DES-034's single consumer is scoped to one sync run, and captures do not
use it.** This is the crux the leader flagged; the evidence from the actual write
path:

- **Sync's serialization is `CollectionIngestor`** (`sync_ingest.py:41`): a
  producer `ThreadPoolExecutor` (`sync_ingest.py:137`) plans + embeds in bounded
  windows and feeds a bounded `Queue` (`sync_ingest.py:99`) that **one** consumer
  thread drains (`_consume`, `sync_ingest.py:210`). It is instantiated *per*
  `sync_collection(...)` call (`sync.py:220`) and lives only for that sync's
  duration. This is the single consumer DES-034 §2 describes.
- **Captures take a *different* writer.** `ingest_content` → `_chunk_embed_store`
  (`pipeline.py:975-988`) → `progressive_insert` (`streaming.py:111`) constructs a
  **fresh** `ProgressiveIndexer` with a `_PlainFlushTarget` (`streaming.py:87`)
  per call, with **no consumer thread and no queue**. `ProgressiveIndexer`'s own
  docstring states its writes are performed "by exactly one caller (the sync
  consumer thread or the single-doc path), so no lock is needed here —
  serialization is the caller's invariant" (`progressive.py:61-63`). It *assumes*
  a single caller; it does not itself serialize concurrent callers.
- **The only serializer both paths share is op-level.** `chunk_store._write_lock`
  (`chunk_store.py:27`) wraps each individual `table.add()`/`table.delete()`
  (`chunk_store.py:66,84,89,115,131`). That is **operation-level**, not
  transaction-level: it prevents a torn Lance write or a commit conflict, but it
  is released between one capture's overwrite-delete and its flush adds, so it
  does **not** make one capture's delete-then-insert atomic against another's.

Consequence: under a concurrent burst of two captures for the same stable
`session-<id>` document, `_write_lock` guarantees no corrupt Lance write, yet the
sequence `delete A · delete B · add A · add B` can still interleave so **both**
chunk sets survive — the overwrite's later-wins guarantee is lost. DES-034 does
not close this; a **job-level** serializer is genuinely required. The per-collection
queue is that serializer, and §4.2 shows it *extends* DES-034's single-writer
invariant rather than forking a parallel mechanism.

## 3. Design: per-collection serialization + bounded embed concurrency

Two orthogonal bounds, each enforced by its own mechanism:

- **Per-collection FIFO serialization** — a correctness bound. One worker
  coroutine per collection drains that collection's own `asyncio.Queue`. Only
  one job per collection is ever in flight. FIFO. Different collections have
  different workers and progress independently.
- **Global embed concurrency** — a performance bound. All workers share one
  `asyncio.Semaphore(EMBED_CONCURRENCY)`, acquired around the job's embed+store
  section. This caps how many jobs are inside the CPU-bound region at once,
  regardless of how many collections are active.

`EMBED_CONCURRENCY` defaults to **1** — strict global serialization, the safest
match to pre-burst behaviour. Three DES-032 facts fix this default (full argument
in §4.4): (a) DES-032's per-job thread caps (`intra_op = min(2, ncpu)`) are tuned
so that a *single* embed job fits DES-027's `narenas:1` arena; (b) ingest jobs
share one ONNX session whose `session.run()` serializes them on an internal ORT
mutex (DES-032 session isolation), so two capture jobs cannot embed in true
parallel anyway; (c) raising the value to 2 therefore buys **no** matmul
parallelism while pushing the arena into the >2-thread contention regime DES-032
eliminated. The setting is clamped; **2 is not recommended** and is left as a
decision for the operator (§9.1).

### 3.1 Data structures

New module `daemon/ingest_queue.py` (one abstraction: the serialized queue).

```python
@final
class IngestQueue:
    """Daemon-owned queue: one FIFO writer per collection, bounded embed globally."""

    _ctx: DaemonContext
    _workers: dict[str, CollectionWorker]   # collection -> its single worker
    _embed_gate: asyncio.Semaphore          # global embed-concurrency bound
    _admit: asyncio.Semaphore               # global queue-depth admission bound
    _closing: bool

    def try_submit(self, collection: str, job: IngestUnit, state: TaskState) -> bool:
        """Enqueue *job* on *collection*'s FIFO worker; False if the queue is full.

        Non-blocking: acquires an admission slot with no wait, lazily starts the
        collection's worker, and hands off. The caller returns 202 on True, 503
        on False. Never awaits I/O — keeps the hook's 202 immediate (I-NOBLOCK).
        """

    async def aclose(self, *, drain_timeout: float) -> None:
        """Stop admitting, drain in-flight + queued jobs, then cancel workers."""


@final
class CollectionWorker:
    """One collection's FIFO worker: drains its queue under the shared embed gate."""

    _queue: asyncio.Queue[_Queued | _Stop]
    _task: asyncio.Task[None]
    _embed_gate: asyncio.Semaphore
    _ctx: DaemonContext
```

`IngestUnit` is a small `Protocol` (`collection: str` + `async def run(ctx,
state)`) that the three existing job value objects satisfy. `_Queued` bundles
`(job, state)`; `_Stop` is the drain sentinel.

### 3.2 Worker loop

```python
async def _run(self) -> None:
    while True:
        item = await self._queue.get()
        try:
            if isinstance(item, _Stop):
                return
            item.state.status = "running"          # queued -> running here
            async with self._embed_gate:           # global concurrency bound
                await item.job.run(self._ctx, item.state)  # task_terminal inside
        finally:
            self._admit_release()                  # free one queue-depth slot
            self._queue.task_done()
```

- **One writer per collection** falls out of "one worker coroutine per queue" —
  no per-collection lock needed; the single consumer *is* the serialization. This
  is the daemon-scope realization of DES-034's single-writer invariant (§4.2).
- **Global embed bound** is the shared semaphore. With the default of 1, at most
  one job embeds+stores anywhere at a time. Cross-collection parallelism is real
  but capped.
- **Fairness across collections** (§3.5): the semaphore has FIFO waiters and is
  released between every job, so a hot collection cannot hold it across its
  backlog — each cold collection's worker takes the gate on its next turn.

### 3.3 Route change

`accept(...)` (`daemon/routes/base.py:116`) splits into two paths:

- **Sync/optimize/backfill/delete** keep `asyncio.create_task` — they are
  already singleton-gated by `reject_if_running` (`base.py:95-114`) and don't
  touch the shared embed session in a burst. Unchanged. (This is the same
  DES-026 409 concurrent-sync guard, untouched — see §4.1.)
- **capture / remember / ingest** call a new `submit(collection, job, state)`
  that does `try_submit` and returns the same `202` body on success or a `503`
  on a full queue:

```python
def submit(self, collection, job, state) -> JSONResponse:
    if not self._ctx.ingest_queue.try_submit(collection, job, state):
        self._ctx.tasks.drop(state)               # no orphan task record
        return JSONResponse(
            {"error": "ingest queue full; capture retained locally, retry via "
                      "`quarry backfill`", "status": "rejected"},
            status_code=503,
        )
    return JSONResponse({"task_id": state.task_id, "status": "accepted"}, 202)
```

The 202 accepted-body is byte-identical to today. The collection key is resolved
**at the route** (capture already does this via `CapturesCollection` at
`routes/captures.py:63-67`); the web-fetch-fallback collection derivation that
currently lives inside `IngestJob._ingest` (`ingest_jobs.py:122-124`) hoists up
to the route so the key is known before enqueue — a de-duplication that also
lets all three jobs expose `collection` uniformly (OO cleanup, §8).

### 3.4 Backpressure + the 202 contract

Captures are fire-and-forget and durable client-side: the transcript `.md` /
archive already exists before the POST, so a *dropped* capture is recoverable by
`quarry backfill` (DES-039/DES-041). The queue leans on that.

- **Admission is a non-blocking `try_acquire`** on `_admit`
  (`Semaphore(MAX_QUEUE_DEPTH)`). It never waits, so the hook's 202 stays
  immediate — **I-NOBLOCK** preserved.
- **Full queue → `503`**, not a silent `202`-then-drop. The client logs and
  moves on; the durable artifact remains for backfill — **I-DURABLE**
  preserved. `503` (retriable) is deliberately distinct from a `4xx` validation
  reject. The `503` is also where DES-026's "the caller should know" value still
  has an addressee (§4.1): a saturated queue returns a truthful, retriable signal
  rather than hiding the drop.
- `MAX_QUEUE_DEPTH = 32` default. A capture body is capped at 4 MB
  (`routes/captures.py:24`), so the worst-case resident backlog is 32 × 4 MB =
  128 MB — bounded. Configurable via `Settings`.

The alternative — block the POST until a slot frees — is rejected: it violates
I-NOBLOCK and stalls the hook (the exact regression DES-041 removed).

### 3.5 Ordering + starvation

- **FIFO per collection**: a single `asyncio.Queue` consumed by a single worker
  preserves submission order within a collection.
- **Later-wins for the same document**: two captures with the same stable
  `session-<id>` name (`routes/captures.py:98-113`), both `overwrite=True`, run
  strictly in order. The second's `_chunk_embed_store` deletes the first's
  chunks then inserts its own (`pipeline.py:975-988`). No interleave, no lost
  update, no commit conflict — the serialization is what makes the chunk-gated
  delete correct under concurrency. This is exactly the §2.1 hazard, closed.
- **No cross-collection starvation**: each collection has its own worker and
  queue, so a hot collection's backlog sits in *its* queue and cannot fill a
  cold collection's. The shared embed gate is released between jobs and grants to
  FIFO waiters, so a cold collection's worker acquires it on its next request
  rather than after the hot backlog drains. Cold-collection latency is bounded
  by ≤ `EMBED_CONCURRENCY` in-flight jobs, not by the hot backlog depth.

### 3.6 Task-status integrity

`TaskState` gains a `"queued"` initial status:

- `tasks.begin(kind)` sets `status = "queued"` (was `"running"`,
  `tasks.py:96`).
- The worker sets `status = "running"` when it dequeues the job (§3.2), before
  `job.run`. `task_terminal` (`tasks.py:39-61`) then records
  `completed`/`failed`/`cancelled→failed` exactly as today.

This is a new status **value**, not a schema change. The client's `await_task`
(`client/client.py:203-228`) only special-cases `"completed"` and `"failed"`
and **keeps polling on any other value** — so `"queued"` is backward-compatible:
a client polls through queued → running → terminal with no code change. The
`/v1/tasks` response shape (`routes/task_status.py:27-35`) is unchanged. A queued
job that a worker later fails still surfaces its error through the same path. A
rejected (503) submit creates **no** task record (`tasks.drop`), so there is no
orphan stuck in `queued`.

### 3.7 Lifecycle + shutdown drain

The queue lives for the daemon's lifetime (single resident process, DES-031 —
this is the "serialized per-collection queue" that ADR names as this bead's job;
see §4.3). It needs the running loop, so it is created inside the ASGI
**lifespan**, not in `DaemonContext.__new__`:

- `DaemonServer._lifespan` (`daemon/server.py:249-265`): after `ctx.warm()`,
  `await ctx.start_ingest_queue()`; in the `finally`, `await
  ctx.ingest_queue.aclose(drain_timeout=DRAIN_TIMEOUT)` **before** the sidecar
  cleanup.
- `aclose` sets `_closing` (rejects new submits with 503), enqueues a `_Stop`
  after each collection's pending jobs, and `await`s each queue's `join()` under
  a bounded `DRAIN_TIMEOUT` (default 30 s) so queued captures are not silently
  lost on a clean shutdown. On timeout, remaining workers are cancelled;
  `task_terminal` records `cancelled → failed` and the durable artifact is
  recoverable via backfill. Drain is bounded so shutdown never hangs.

**Resource-invariant interaction** (`tests/test_resource_invariants.py`): the
queue holds no LanceDB handles — it serializes writes onto the *same* single
shared `ctx.database` connection the fd-leak guard already covers. Serializing
concurrent writers can only *reduce* concurrent index-rebuild churn, never add
fd pressure. The plateau invariant is unaffected; a regression test confirms it
still holds with the queue in place.

## 4. Reconciliation with prior ADRs

This design was first drafted without reconciling against the settled ADRs whose
guards already touch this path. It does not contradict any of them; it composes
as set out below. (This section was added in the ratification revision.)

### 4.1 DES-026 (Sync Concurrency Control) — why queuing is legitimate here

DES-026 rejected "queue concurrent sync requests" with the reason *"queuing hides
the problem; the caller should know sync is already running."* That reasoning is
specific to a **user-initiated** `quarry sync`: a human or agent issued the
command and is waiting on its outcome, so the honest response is a `409` telling
them a sync is already in flight (`routes/base.py:95-114`, `routes/sync.py:44`).

Captures are the opposite. A capture is a **fire-and-forget background event**:
the compaction / web-fetch hook already received its `202` and moved on
(DES-031 / DES-041 thin client) — there is no caller waiting to be told anything,
so "the caller should know" has no addressee. What the workload needs instead is
that the durable artifact (the transcript `.md` / archive that predates the POST)
is never lost — which §3.4 guarantees via `503`-to-backfill on a full queue.
DES-026's objection therefore does not apply: a bounded capture queue serves a
workload DES-026 never contemplated. And where the queue *is* saturated, the `503`
still honours DES-026's underlying value — inform the caller — with a truthful,
retriable signal rather than a silent drop.

The queue also **composes with DES-026's existing guards rather than duplicating
them**:

- **Server-side sync lock (409).** DES-026's lock gates `POST /v1/sync` only
  (`reject_if_running`, `routes/base.py:95-114`). Captures never traverse that
  route, so the capture queue neither uses nor weakens the 409 path. A concurrent
  user `sync` during a capture burst still gets its 409 exactly as today.
- **`PRAGMA busy_timeout=5000` on every `SyncRegistry` connection**
  (`sync_registry.py:83`). That bounded-wait guards the **registry** SQLite
  writes — sync's domain. Captures do **not** write the registry: their
  single-document `_PlainFlushTarget.on_flush` is a deliberate no-op
  (`streaming.py:107-109`), so no watermark / registry row is touched. The capture
  queue therefore governs a **different resource** (the chunks-table writes) from
  the busy_timeout guard, and the two do not overlap. Sync's registry writes and
  captures' chunk writes stay independently serialized — busy_timeout for the
  former, the per-collection worker + `_write_lock` for the latter.

### 4.2 DES-034 (Bounded Progressive Commit) — extend the single-consumer model, don't fork it

DES-034 established the load-bearing invariant that quarry's LanceDB writes are
safe *because* "exactly one consumer thread performs writes, and serializes
producer overwrite-deletes against the consumer's adds." §2.1 shows, with
file:line evidence, that this invariant is realized **only within a single sync
run** (`CollectionIngestor`, `sync_ingest.py:41-137`); the daemon's capture path
violates it, because every concurrent capture is an independent `progressive_insert`
caller (`streaming.py:111`) with its own `ProgressiveIndexer` and no shared
consumer.

The per-collection queue is therefore **the daemon-scope generalization of
DES-034's within-sync single consumer, not a competing mechanism.** The
composition is layered, not parallel:

- **`_write_lock` (DES-034, `chunk_store.py:27`)** stays exactly as is — the
  low-level guarantee that no single `add`/`delete` tears or conflicts.
  **Untouched** by this design.
- **`ProgressiveIndexer` (DES-034, `progressive.py`)** stays exactly as is — it
  still assumes one caller and needs no lock of its own. **Untouched.**
- **The per-collection worker (this design)** supplies the missing top layer: for
  a given collection there is only ever **one** `progressive_insert` caller in
  flight, restoring the single-writer precondition DES-034's ingest primitives are
  built to trust. It is the same invariant DES-034 gives sync, now provided for
  captures.

**Why not literally reuse `CollectionIngestor` for captures?** Because it is a
batch producer/consumer coupled to the sync **registry**: its `FlushTarget.on_flush`
writes `files`-table watermarks and its `run(files: list[Path])` is a
whole-collection sweep (`sync_ingest.py:44-137`, `265-298`). Captures are single
inline documents that keep **no** resume watermark (`_PlainFlushTarget.on_flush`
is a no-op, `streaming.py:107-109`). Forcing captures through `CollectionIngestor`
would drag registry / resume machinery they do not use. And there is no persistent
sync consumer to hang captures on — `CollectionIngestor` exists only for a sync's
duration (`sync.py:220`), whereas captures arrive continuously *between* syncs and
need a **resident** serializer. So the clean reuse is at the **invariant** level
(one writer per collection), realized by a lightweight resident per-collection
worker — not by instantiating the heavyweight sync ingestor per capture.

### 4.3 DES-031 (Daemon-First) — this is the sanctioned follow-on

DES-031 explicitly decouples "the event-driven watch/index loop with a serialized
per-collection queue (`quarry-lxrk`)" as a **later** bead built on the single
resident daemon. This design *is* that queue, and fits DES-031's framing directly:
it lives for the daemon's lifetime (§3.7, single resident process — I2), holds no
engine handles a client could import (I1 unaffected — the queue is
daemon-internal), and adds no wire surface (§5 scope; I3's contract unchanged).
The "serialized per-collection queue" phrase in DES-031 is realized literally here.

### 4.4 DES-032 (Daemon Thread Limits) — the semaphore bounds jobs, DES-032 bounds threads

DES-032 caps **threads per embed job**: `intra_op_num_threads = min(2, ncpu)`,
`inter_op_num_threads = 1`, `OMP_NUM_THREADS = min(2, ncpu)`, tuned specifically
for DES-027's `narenas:1,tcache:false` arena (more than ~2 threads serialize on
the arena lock and throughput collapses — DES-032's evidence table: 0.1 texts/s
under contention). This design's `EMBED_CONCURRENCY` semaphore caps the
**orthogonal** axis: how many embed *jobs* run at once. The two multiply, and the
interaction is the crux of decision §9.1:

- **EMBED_CONCURRENCY = 1** → at most `1 × min(2, ncpu) = 2` embed threads active
  — exactly DES-032's single-job design point. No new arena pressure.
- **EMBED_CONCURRENCY = 2** → up to `2 × 2 = 4` embed threads contending on the
  single arena — precisely the >2-thread regime DES-032 tuned the caps to avoid.
  It re-introduces the arena-lock serialization DES-032 eliminated.

DES-032's **session isolation** decision compounds this: ingest uses the
module-cached singleton backend (`backends.py:36-43`) whose `session.run()`
serializes callers on an internal ORT mutex, while search gets a *separate*
session (`http_resources.py`) so a query never waits on an ingest batch. Because
captures share that one ingest session, two capture jobs cannot embed in true
parallel regardless of the semaphore — they queue at the ORT mutex. So
`EMBED_CONCURRENCY = 2` buys **no matmul parallelism** while it **does** add the
arena contention above and a second resident chunk set. Every DES-032 consideration
points the same way: keep `EMBED_CONCURRENCY = 1`.

The semaphore does not touch DES-032's thread caps or QoS hints; those remain the
unconditional per-job bound. The two mechanisms are complementary — the semaphore
bounds the job count, DES-032 bounds the threads inside each job — never
overlapping.

## 5. Scope boundary (confirmed)

Daemon-internal only. No wire change: request shapes, the `202` accepted-body,
and the `/v1/tasks` response schema are untouched (only a new interim status
*value*, `"queued"`, which existing clients already tolerate). No client change.
The `503`-on-full is a new *failure* response on an existing route, not a new
route or parameter. Local/remote parity (bug class 3) is unaffected — ingest has
no local CLI path that bypasses the daemon post-DES-031.

## 6. Alternatives considered and rejected

1. **Per-collection worker, no global embed bound.** Preserves per-collection
   serialization but lets N collections embed in parallel — the exact
   oversubscription of quarry-lnog. Rejected: the embed bound is the whole point.
2. **One global `asyncio.Lock` around every write.** Correct and bounds
   concurrency to 1, but couples the CPU-bound embed and the I/O-bound store
   under one lock: a large `remember` on collection A blocks a tiny capture on B
   for A's entire embed. The two-bound design lets B's worker embed while A
   flushes. Rejected in favor of the decoupled semaphore.
3. **DB-level lock only (rely on LanceDB commit-conflict retry).** Leaves
   oversubscription unsolved and turns lost-update into retry storms under a
   burst; retries re-embed. Rejected.
4. **Global FIFO queue + a pool of N workers taking a per-collection
   `asyncio.Lock`.** Head-of-line blocking: if the head job's collection is
   locked, a worker idles while ready jobs for free collections wait behind it.
   Per-collection queues avoid this entirely. Rejected.
5. **Shrink the AnyIO threadpool limiter to 1.** Blunt: it serializes *all*
   `run_in_threadpool` work — search, delete, `getaddrinfo` SSRF checks, scrub —
   not just ingest, starving reads. Rejected.
6. **Process isolation per ingest (DES-035).** Already RESERVED/DEFERRED. Reloads
   the ONNX model per subprocess, defeating DES-031's single resident engine.
   Rejected as heavier than the problem.
7. **Route captures through the existing sync `CollectionIngestor`.** The literal
   "reuse the single consumer" reading. Rejected because `CollectionIngestor` is
   coupled to the sync registry (files-table watermarks) and exists only for a
   sync's duration; captures write no registry row and arrive between syncs, so
   they need a resident serializer, not a per-sync one. The reuse that *is* taken
   is at the invariant level (§4.2), not the class level.

## 7. Test plan

New `tests/test_ingest_queue.py`, driven by an instrumented `IngestUnit` stub
that records embed-section enter/exit timestamps and per-collection in-flight
counts (no real ONNX needed for the concurrency assertions):

- **Same-collection serialization.** Submit N jobs to one collection; assert
  max simultaneous in-embed-section == 1 for that collection, FIFO completion
  order, and — with real ingest — the final document holds only the last
  writer's chunks (later-wins, no lost update).
- **DES-034 composition (the §2.1 regression).** With the queue active, two
  same-document captures submitted concurrently to one collection produce a
  document with **only the last** writer's chunks. As a control, the pre-queue
  path (concurrent `progressive_insert` under `_write_lock` alone) is shown to
  leave **both** chunk sets resident — the exact interleave the queue closes. This
  test is the executable proof that op-level `_write_lock` does not subsume the
  job-level serializer.
- **Cross-collection bound.** Submit N jobs across M collections; assert
  per-collection in-flight == 1 and *total* in-flight ≤ `EMBED_CONCURRENCY`
  (== 1 at the default), and all complete.
- **DES-026 non-interference.** A capture burst in flight does not affect
  `POST /v1/sync`'s 409 concurrent-sync guard (a concurrent sync still 409s), and
  captures issue **no** `SyncRegistry` writes — assert the `_PlainFlushTarget.on_flush`
  no-op path so the busy_timeout guard is never engaged by a capture.
- **Backpressure.** Fill to `MAX_QUEUE_DEPTH`; assert the next submit returns
  `503` with `status: rejected` and creates **no** task; assert a submit after
  drain returns `202`.
- **Fairness.** One hot collection with a deep backlog + one cold collection
  with a single job; assert the cold job completes within ≤ `EMBED_CONCURRENCY`
  job-durations, not after the whole hot backlog.
- **Task-status integrity.** Poll a job through `queued → running → completed`;
  a raising job → `failed` with the error surfaced; `await_task` reaches a
  terminal outcome. Assert the `/v1/tasks` body shape is unchanged.
- **Shutdown drain.** Submit K jobs, trigger lifespan shutdown; assert queued +
  in-flight jobs reach a terminal status within `DRAIN_TIMEOUT`; on a forced
  timeout, remaining jobs are `failed` (recoverable), not lost.
- **Wire non-regression.** Assert the accepted `202` body is byte-identical to
  the pre-queue response for capture/remember/ingest.
- **Resource-invariant.** Confirm `tests/test_resource_invariants.py`'s fd
  plateau still holds with the queue serializing writes on the shared
  connection.

Evaluator checklist (recurring bug classes): Class 2 (exception boundaries) —
`try_submit` and `aclose` must never propagate; a worker that raises records
`failed`, never crashes the loop. Class 3 (remote/local divergence) — N/A, no
local ingest path. The daemon-mandatory tests must stay hermetic (inject the
queue via the lifespan; verify with the daemon stopped).

## 8. Proposed write-set

The design mission owns the final set; this is the proposal.

- **NEW** `src/quarry/daemon/ingest_queue.py` — `IngestQueue`, `CollectionWorker`,
  `_Queued`/`_Stop`, the `IngestUnit` Protocol. One abstraction per module.
- **NEW** `tests/test_ingest_queue.py` — the §7 concurrency suite.
- **EDIT** `src/quarry/daemon/context.py` — hold `_ingest_queue`; async
  `start_ingest_queue()` + `aclose_ingest_queue()`; `ingest_queue` property.
- **EDIT** `src/quarry/daemon/server.py` — `_lifespan` starts the queue after
  `warm()` and drains+closes it on shutdown before sidecar cleanup.
- **EDIT** `src/quarry/daemon/routes/base.py` — add `submit(collection, job,
  state)`; keep `accept` for the singleton-gated routes (the DES-026 409 path is
  untouched).
- **EDIT** `src/quarry/daemon/routes/captures.py`,
  `src/quarry/daemon/routes/ingestion.py` — resolve the target collection at the
  route and call `submit`.
- **EDIT** `src/quarry/daemon/ingest_jobs.py` — expose `collection` uniformly on
  the three jobs (satisfy `IngestUnit`); hoist the web-fetch-fallback collection
  derivation out of `IngestJob._ingest`.
- **EDIT** `src/quarry/daemon/tasks.py` — `begin` sets `"queued"`; add `drop`
  for a rejected submit.
- **EDIT** `src/quarry/config.py` — `ingest_embed_concurrency` (default 1; clamp
  ≤ 2, but see §9.1 — 2 conflicts with DES-032 and is not recommended),
  `ingest_queue_depth` (default 32), `ingest_drain_timeout_s` (default 30).
- **UNCHANGED (by design):** `chunk_store._write_lock`, `ProgressiveIndexer`
  (`progressive.py`), and `streaming.progressive_insert` are **not** modified — the
  per-collection worker layers *above* them (§4.2). The queue supplies the
  single-writer-per-collection precondition those DES-034 primitives already
  assume; it does not replace their op-level guarantees.

## 9. Decisions the operator must rule on

1. **`EMBED_CONCURRENCY` default — 1 or 2?** Recommend **1**, and clamp the
   setting so 2 is an explicit, deliberate opt-in only. DES-032's thread caps are
   tuned for a *single* embed job under `narenas:1`; setting 2 both buys **no**
   matmul parallelism (jobs queue at the shared ORT session mutex regardless) and
   re-enters the >2-thread arena-contention regime DES-032 eliminated (§4.4). The
   original draft's "configurable to 2" is retained only as an escape hatch; if
   the operator wants it, that is an explicit acceptance of the DES-032 conflict.
   Absent that, the clamp ceiling should be 1.
2. **Full-queue response — `503` reject vs. bounded block.** Recommend **`503`**.
   It preserves I-NOBLOCK, leans on client-side durability + backfill, and is the
   one place DES-026's "the caller should know" still has an addressee — a truthful,
   retriable signal instead of a silent drop (§4.1). A bounded block would keep the
   capture but stall the hook (the DES-041 regression).
3. **New `"queued"` status value — acceptable given "no wire change"?** Recommend
   **yes** (orthogonal to DES-026/032). It is a new response *value*, not a schema
   change, and existing clients already poll through unknown non-terminal statuses
   (`client.py:223-228`). The alternative is to keep the initial status `"running"`
   while queued (less truthful but zero new values).
