# DES-031 v2 — Daemon-First Architecture: One Engine, Formal Wire Protocol, Pure Clients

**Status:** ACCEPTED 2026-07-12 (revises DES-031, DESIGN.md:897; not yet implemented — v2-2…v2-6 build it)
**Date:** 2026-07-12
**Author:** rmh
**Supersedes:** DES-031 ("Engine-First Architecture with Thin Interfaces"). This
proposal keeps DES-031's thesis (one engine, thin interfaces) and replaces its
framing, its ordering, and its wire-protocol under-specification.

**Revision v2.1 (2026-07-12)** — folds in the operator's rulings and the gvr
peer-review amendments:

- **Framework = FastAPI** (operator ruling). §3.3/§4.3 rewritten: FastAPI *is*
  Starlette underneath, so the WS `/mcp` route, the async task system, the
  lifespan/port-file startup, and bearer auth pass through unchanged — the v2.0
  "large risky migration" premise was false. Deleted the hand-rolled OpenAPI
  emitter and the custom `Operation` route registry (gold-plating FastAPI
  derives from the same Pydantic models).
- **Watcher decoupled** (operator ruling). The always-on filesystem watch/index
  loop leaves this epic's build scope. It becomes two beads — quarry-lxrk
  (daemon-owned watch/index loop + serialized per-collection queue) and
  quarry-tqdq (interim scheduled sync, overlaps quarry-uae). §3.2 reframed:
  the daemon is the ONE resident indexing *engine*, driven by triggers.
- **Sequencing fix** (gvr #1): supervision + the autostart-nudge helper move
  from v2-6 into new **v2-3a**, landing before the fallback is removed.
- **Daemon-liveness** (gvr #2): §3.2 splits the quick "already up?" probe from a
  ~30 s post-kickstart warm budget; `/health` reports `warming` vs `ready`;
  Linux uses `systemctl --user restart`; not-running errors distinguish
  "not installed" from "installed but down."
- **Ingest payload** (gvr #5): `IngestRequest` defines both a daemon-local
  path mode and an uploaded-bytes mode (§3.3).
- **Boundary precision** (gvr): host-local `serve`/`mcp` use a lazy engine
  import guarded by the runtime-sabotage test, not import-linter; `disable` is
  reclassified — its chunk purge (enable.py:188-189) becomes a daemon call.
- **Menubar / cross-repo** (operator fact): quarry-menubar is already a pure
  HTTP client, so v2-5 is now a pure in-repo change and the cross-repo touch
  moves to v2-2. The OpenAPI doc is the shared contract the Swift client conforms
  to; quarry-menubar is cited as prior-art proof of the thin-client model.
- **Minor** (gvr): CA route corrected to `/ca.crt` (http_server.py:1300); the
  backwards-compat alias routes (http_server.py:1313-1314) are removed in v2-2;
  one real-loopback-TLS smoke test rides alongside the ASGI fixture; the
  `await_task` typed-error fix removes the hardcoded "Deregister failed" bug
  (remote_client.py:179,185).

> **Reader's note.** This is a ratifiable proposal, not an implementation.
> It cites current-code `file:line` at every point where behavior changes and
> deliberately does **not** prescribe an implementation write-set — that is the
> job of the design-to-implementation missions that follow ratification.

---

## 1. Context — the audited reality

A 5-agent audit established the following as ground truth. DES-031 v1 shipped
only its ADR; the transition it named never happened, and its "five engines per
host" table is now actively misleading.

### 1.1 "Five engines" is a latent ceiling, not steady state

The v1 ADR (now replaced by this rewrite) tabulated seven engine-loading contexts. In practice the
**steady state is one resident engine** — the `quarry serve` daemon (~1.6 GB RSS
with the ONNX model, LanceDB handles, and warmed caches). `mcp-proxy` (~5 MB Go
binary) and the menubar load **no** engine. The remaining rows are engines that
*can* be spun up per-invocation, not engines that coexist. The v1 framing sells
the design on avoiding a duplication that mostly does not occur; the real driver
(below) is always-on indexing, which v1 never states.

### 1.2 CLI — routing seam exists, local-engine branch was never removed

`__main__.py` (1,750 lines) routes every data command through
`_safe_proxy_config()` (`__main__.py:262`) → `RemoteClient(proxy_config)` when a
remote is configured (18 call sites, e.g. `__main__.py:302-304, 383-384,
484-486, 602-603, 657-660, 719-723, 746-763, 807-816, 933-944, 1452-1565`).
Each of the 14 **split** commands still carries an `else:` branch that builds the
engine in-process. Four commands are **local-only today** — `enable`, `disable`,
`optimize`, `backfill-sessions` — and never route remotely at all.
DES-031 v1 PR3 ("CLI refactor: every data command a thin client") is **0% done**.

### 1.3 Library — public names *are* the in-process engine

`__init__.py` lazy-loads (PEP 562, `__init__.py:58-72`) but the public surface
it exposes — `Database`, `get_db`, `ingest_content`, `ingest_document`,
`ingest_url`, `ChunkSearch` (`__init__.py:46-55`) — **is** the engine. There is
no `QuarryClient`. Importing `quarry` for real work means loading lancedb +
onnxruntime + the pipeline. PR5 is **0% done**.

`RemoteClient` (`remote_client.py`) is a de-facto thin HTTP client but is
**CLI-coupled**: it imports `typer` (`remote_client.py:13`), raises
`typer.Exit` (`remote_client.py:133, 182, 190, 295`) and `SystemExit`
(`remote_client.py:229, 237`), and prints to a `rich` console
(`remote_client.py:19, 132, 178, 184`). It cannot be used as a library. (Bead
quarry-bxwd.)

### 1.4 MCP — stdio path loads the engine; thin path exists but coexists

`mcp_server.py` (557 lines) constructs the engine in-process: `Database.connect`
(`mcp_server.py:83-84`), `get_embedding_backend` (`mcp_server.py:132`),
`SearchService(database)` (`mcp_server.py:141`). The **thin** path already
exists (DES-021): mcp-proxy → daemon WebSocket `/mcp`
(`http_server.py:1238-1278`, route registered at `http_server.py:1323`), which
runs `run_mcp_session` inside the daemon. The in-process `quarry mcp` stdio entry
was never dropped. PR4 is **0% done**. There is **no `quarry-server` entry
point** — only `quarry` and `quarry-hook` exist; CLAUDE.md's module table naming
`quarry-server` is stale.

### 1.5 HTTP — ~20 routes, zero schemas, two operations missing

`http_server.py` (1,475 lines) defines the routes in §1.6 below. **Every route
builds ad-hoc `dict`s and `JSONResponse`s by hand** — there is no Pydantic
request or response model and no schemas module. `optimize` and
`backfill-sessions` have **no endpoint at all** — a split-horizon gap
(logged-in users cannot run them remotely). PR2 is ~65% done (routes exist;
contract does not).

### 1.6 Current daemon route inventory (`http_server.py`)

| Route | Method | Handler | Async? |
|-------|--------|---------|--------|
| `/health` | GET | `_health_route` :265 | no |
| `/ca.crt` | GET | `_ca_cert_route` :275 (registered :1300) | no |
| `/search` | GET | `_search_route` :295 | no |
| `/documents` | GET | `_documents_route` :337 | no |
| `/documents` | DELETE | `_documents_delete_route` :348 | **202** |
| `/collections` | GET | `_collections_route` :400 | no |
| `/collections` | DELETE | `_collections_delete_route` :410 | **202** |
| `/show` | GET | `_show_route` :458 | no |
| `/remember` | POST | `_remember_route` :504 | **202** |
| `/ingest` | POST | `_ingest_route` :613 | **202** |
| `/sync` | POST | `_sync_route` :760 | **202** |
| `/captures/push` | POST | `_captures_push_route` :824 | no |
| `/tasks/{id}` | GET | `_task_status_route` :837 (registered :1312) | no |
| `/sync/{task_id}` | GET | `_task_status_route` (alias :1313) | no |
| `/ingest/{task_id}` | GET | `_task_status_route` (alias :1314) | no |
| `/databases` | GET | `_databases_route` :865 | no |
| `/use` | POST | `_use_route` :902 | no |
| `/registrations` | GET | `_registrations_route` :919 | no |
| `/registrations` | POST | `_handle_add_registration` :1017 | **202** |
| `/registrations` | DELETE | `_handle_delete_registration` :1111 | **202** |
| `/status` | GET | `_status_route` :1194 | no |
| `/mcp` | WS | `_mcp_websocket_route` :1238 | stream |
| **optimize** | — | **missing** | — |
| **backfill-sessions** | — | **missing** | — |

### 1.7 What DES-037 already solved — and what it did not

DES-037 (`retrieval/service.py`, merged #343) unified the **search** path across
all three surfaces behind a shared `SearchService` (`service.py:18-48`). CLI
(via `RemoteClient` or local), HTTP (`http_server.py:327`), and MCP
(`mcp_server.py:141`) all construct the same object and call `.search(...)`.
**Bug-class-3 drift is therefore already closed for search** — by a
shared-library seam, not by the daemon. Every **non-search** command
(ingest, remember, delete, register, deregister, sync, status, list, use,
optimize, backfill) still has two divergent code paths.

**Design consequence:** this proposal must *reuse* DES-037, not re-solve it.
`SearchService` becomes the daemon's internal search implementation behind the
`/v1/search` endpoint; clients stop constructing it directly.

### 1.8 Incidental defects the audit surfaced (fold into this work)

1. **`/mcp` re-derives a third ONNX session.** The daemon warms `ctx.embedder`
   (`http_server.py:221-228`) once at startup, but the WS `/mcp` route runs
   `run_mcp_session` (`http_server.py:1274`), whose tools call
   `get_embedding_backend(settings)` (`mcp_server.py:132`) and
   `Database.connect(...)` (`mcp_server.py:84`) **fresh** per session — a second
   model load and DB handle inside the very process that already holds warmed
   ones.
2. **`architecture.tex` §CLI-Independence asserted "The CLI does not use the
   daemon"** — false already (remote mode), and the exact opposite of the
   target; its §Design-Principles P1 ("Library-first … the core library does
   all work") described the as-built inversion this proposal removes. (Both
   corrected in v2-1.)
3. **CLAUDE.md module table names `quarry-server`** — an entry point that does
   not exist.

---

## 2. The three invariants (operator-set)

This design is judged against three hard constraints. Every decision below
serves them.

- **I1 — Hard client/engine boundary.** CLI, MCP, and the library are **pure
  clients**. None may import or construct `Database`, `embeddings`,
  `ingestion.pipeline`, `retrieval` (`SearchService`/`HybridRetriever`), or
  `SyncRegistry` in-process. The engine lives **only** in the daemon process.
- **I2 — Daemon assumed always present.** One supervised, always-on engine per
  machine. Clients assume it is there. Its first-class rationale is that the
  daemon is the **one resident indexing engine** — the always-on host that every
  indexing trigger drives, so no trigger ever has to spin up a fresh ~1.6 GB
  engine per invocation. The daemon owns the `SyncRegistry` and exposes
  `/v1/sync`; the *triggers* that drive it are built as separate work (interim
  scheduled sync now — quarry-tqdq/quarry-uae; the event-driven watch/index loop
  later — quarry-lxrk). A per-invocation CLI fundamentally cannot host a resident
  engine for those triggers to reuse.
- **I3 — Well-specified wire protocol.** A formal REST API is the single source
  of truth: shared Pydantic request/response/error models generating an OpenAPI
  document. Every operation has an endpoint, method, request/response/error
  schema, status codes, and a formalized async pattern. The API is versioned.
  A single `QuarryClient` conforms to it and is the one client used by CLI,
  MCP-thin, and library.

---

## 3. Design

### 3.1 Package topology and the boundary (I1)

Three layers, dependency arrow pointing inward (PL-MD-1, PY-IC-8). The boundary
is enforced by **package membership**, not convention.

```text
  quarry/api/        ← LAYER 1: wire contract. Pydantic models + OpenAPI.
  (schemas, errors)    Zero engine imports. Zero heavy deps. Importable alone.
        ▲     ▲
        │     │
  quarry/client/     ← LAYER 2a: QuarryClient. HTTP(S)/WS transport + typed
  (QuarryClient)       errors. Imports quarry.api only. NO engine imports.
        ▲
        │
  quarry/__main__    ← LAYER 3: CLIENT PROCESSES. Import quarry.client + quarry.api.
  quarry/hooks         Add exit/print/typer at this layer only. NEVER the engine.
  (menubar, scripts)
        …
  quarry/daemon/     ← THE ENGINE (server side only). Owns Database, embeddings,
  quarry/db, /retrieval  pipeline, SearchService, SyncRegistry, the index loop,
  quarry/ingestion       and the MCP session handler. Imports quarry.api to
  quarry/embeddings…     validate/serialize. NEVER imported by a client process.
```

**The rule, stated precisely.** A *client process* is any process whose entry
point is not `quarry serve`. A client process may import **only**
`quarry.client`, `quarry.api`, and stdlib/UI libraries (typer, rich, mcp
transport). It may **not** import `quarry.db`, `quarry.embeddings`,
`quarry.ingestion`, `quarry.retrieval`, `quarry.sync`, or any module that
transitively loads onnxruntime, lancedb, or pyarrow.

**The one narrow exception — host-local commands.** A fixed, closed set of
commands genuinely configures the *host* and has no corpus-data engine
dependency; they run in the CLI process without a daemon: `serve`, `mcp`,
`install`, `login`, `logout`, `doctor`, `version`, `uninstall`, plus `enable`
(which writes Claude Code hook config, not corpus data). `serve` and `mcp` are
special: `serve` *is* the engine entry point and `mcp` (the stdio bridge target,
until v2-4 removes it) both need the engine — they are the only client-tier
modules permitted to import the engine packages. `doctor` may probe daemon
health over loopback but must not construct the engine.

**`disable` is NOT cleanly host-local — reclassified.** Unlike `enable`,
`disable` today opens a `ChunkStore` to purge collection chunks
(enable.py:188-189: `db = get_db(...); store = ChunkStore(db)`) — that is a
corpus-data engine operation, not host config. Under I1 it must not load the
engine in the CLI process. Its chunk cleanup becomes a **daemon call**: either a
dedicated `/v1` op or folded into `deregister`'s async purge task (the daemon
already owns collection deletion). Only `disable`'s hook-config removal stays
CLI-local; the chunk purge crosses to the daemon. `disable` is therefore a
*hybrid* command (host-config-local + one daemon call), not a pure host-local.

**Enforcement (prevents regression).**

1. **Import-linter contract** in CI (`make check-coupling` already exists for
   coupling; add an `importlinter` layers contract). Forbidden edge:
   `quarry.client`, `quarry.__main__` (minus the `serve`/`mcp` engine-owning
   modules), `quarry.hooks` → any engine package. A violating import fails CI,
   not review.
2. **The host-admin exception is an ENUMERATED `ignore_imports` list, not
   lazy-import invisibility (corrected as built — PR-6).** The original text here
   assumed import-linter cannot see function-body lazy imports; with the current
   toolchain (grimp 3.15) that is **false** — grimp resolves lazy imports, so a
   deferred `from quarry.db import …` inside a command body *is* seen by the
   contract. The boundary therefore does not depend on invisibility. Two facts
   make it hold: (a) the engine's only entry point, `quarry.daemon.launcher` (the
   `quarryd` console script), lives **inside** `quarry.daemon`, so it is
   engine-side and never a contract *source*; and (b) the one sanctioned
   client-side exception — the host-admin diagnostics (`doctor`, and through it
   `install`/`uninstall`) that probe the local engine environment (model cache,
   ONNX runtime, on-disk LanceDB) to report *why* a daemon is unhealthy even when
   it is down — is an explicit, self-documenting `ignore_imports` list of the
   host-admin diagnostic lazy edges (`doctor`/`doctor_captures`) in `.importlinter`. Those imports are function-body lazy
   so the heavy engine never loads on the hot CLI/hook path, and the **runtime
   sabotage test** (enforcement #3) proves that module-scope engine-freeness. A
   new engine import from any *other* client-reachable module still fails the
   contract. (`quarry mcp` is no longer part of this exception — PR-4 made it a
   pure client with no engine import; only `quarryd`/`serve` is engine-owning.)
3. **`quarry.api` and `quarry.client` have zero engine imports** — a unit test
   imports each in a subprocess with `sys.modules` sabotaged so that importing
   lancedb/onnxruntime raises, proving the contract/client libs are
   dependency-light. This same sabotage test is what proves the `serve`/`mcp`
   lazy imports have not leaked to module scope: every client command except
   those two must import cleanly under sabotage.
4. The `serve` engine-owning code moves into a `quarry/daemon/` package so the
   forbidden-import rule can target module paths, not functions.

### 3.2 Daemon-assumed contract + the indexing rationale (I2)

**The daemon is the single engine, supervised by the OS service manager**
(launchd `KeepAlive` on macOS, systemd `Restart=always` user unit on Linux),
bound to **loopback by default** (`127.0.0.1:8420`). Clients connect there.

**Why the resident engine is load-bearing (first-class driver).** Indexing means
loading the ONNX model, holding LanceDB write handles, and keeping warmed caches
— ~1.6 GB of resident state. The daemon is the **one place that state lives**, so
every indexing trigger reuses it instead of paying a cold-start ~1.6 GB engine
build per run. This is the reason the architecture is daemon-first rather than
library-first: incremental indexing must have a resident engine to drive. A CLI
that exits after each invocation cannot host that engine; each `quarry sync`
would rebuild it from scratch.

The daemon owns the `SyncRegistry` and exposes `/v1/sync`; register / deregister
/ sync are **thin mutations** of registry state plus a task on the resident
engine:

| Operation | Client does | Daemon does |
|-----------|-------------|-------------|
| `register <dir>` | POST the path | add to `SyncRegistry`; initial index runs as a task on the resident engine |
| `deregister <coll>` | DELETE the collection | remove from registry; purge chunks as a task |
| `sync` | POST | force an immediate full scan + re-index of registered dirs on the resident engine |

**The indexing *triggers* are out of this epic's scope.** Two triggers drive the
resident engine; both are built separately so this design stays about the engine
and the wire protocol, not the scheduling mechanism:

- **Interim scheduled sync (now) — bead quarry-tqdq**, overlapping quarry-uae: a
  cron / launchd-timer / systemd-timer that periodically invokes `quarry sync`
  (a thin `/v1/sync` call) so the corpus stays reasonably current without a
  watcher. This is the near-term trigger.
- **Event-driven watch/index loop (later) — bead quarry-lxrk**: a
  daemon-owned filesystem watcher over registered directories that, on change,
  debounces and re-indexes with **no human action**. quarry-lxrk also owns the
  **single serialized per-collection indexing queue** that prevents the
  double-index race (concurrent scheduled-sync + watcher writes to the same
  collection). That concurrency concern is quarry-lxrk's, not this design's — it
  is referenced here and specified there.

`sync.py` today holds registration + change tracking; the daemon takes ownership
of the `SyncRegistry` in this epic, but the always-on watcher itself is
quarry-lxrk, not part of any v2-N PR here.

**`/health` distinguishes *warming* from *ready*.** A cold daemon has an ONNX
model to load; the process may be listening on the socket before the engine is
usable. `/health` therefore reports a lifecycle state, not just liveness:
`{"state": "warming" | "ready", "api_version": "1", "quarry_version": "…"}`.
`warming` means the socket answers but the engine is not yet loaded; `ready`
means requests will succeed. Clients treat `warming` as "keep waiting," not
"failed."

**Two distinct timeouts.** These must not share a budget:

- **Quick "already up?" probe** (~1–3 s): the common case where the daemon is
  already `ready`. A short timeout keeps every command snappy.
- **Post-kickstart warm budget (~30 s ceiling)**: after the client *itself*
  triggers a start, a cold ONNX load routinely exceeds ~3 s, so the client polls
  `/health` for `ready` under a much longer budget before giving up. Reusing the
  quick-probe timeout here would spuriously fail every cold start.

**Behavior when the daemon is NOT running — decision: supervised-with-autostart-nudge,
then fail fast.** On a client's first connection refusal to loopback:

1. The client asks the **service manager** to (re)start the unit — **not** an
   in-process engine, and **not** a bare `subprocess` fork. The supervisor owns
   lifecycle:
   - macOS: `launchctl kickstart -k gui/$UID/com.puntlabs.quarry` (the `-k`
     restarts a wedged unit).
   - Linux: `systemctl --user restart quarry` — **restart**, not `start`, because
     `start` is a no-op on a unit that is `active` but wedged (hung, not
     answering loopback); `restart` recovers it.
2. Poll `/health` for `state == "ready"` under the **post-kickstart warm budget**
   (~30 s), not the quick-probe timeout.
3. If still down, **fail fast** with a typed `QuarryConnectionError` whose
   message differentiates the two failure shapes:
   - **Unit not installed** (service manager reports no such unit): `quarry
     daemon service is not installed — run 'quarry install' to set it up`.
   - **Installed but down** (kickstart/restart issued, still unreachable):
     `cannot reach quarry daemon at 127.0.0.1:8420 after restart — run 'quarry
     doctor'`.

**Why this over the alternatives.** A silent in-process engine fallback would
resurrect the dual path and bug-class-3 drift (rejected, §4.1). A blind
`subprocess` spawn bypasses the supervisor and orphans the engine. Nudging the
supervisor honors I2 ("assumed present, supervised") while surviving a cold
machine; failing fast after a bounded window keeps single-shot scripts from
hanging. Separating "not installed" from "installed but down" sends the user to
the right fix — install vs. doctor — instead of a generic dead-end.

**Offline/degraded.** "Offline" means no *network*; the local daemon is
loopback and unaffected. Only **remote mode** (opt-in via `quarry login`) needs
the network; its failure is already a typed remote error. There is no degraded
half-mode: the daemon is up (full function) or the client fails fast.

### 3.3 The formal wire protocol (I3)

**Transport & framework — decision: adopt FastAPI (operator ruling: correctness
over migration-avoidance).** FastAPI *is* Starlette underneath — same ASGI app,
same routing, same middleware, same WebSocket support — so the pieces that made a
framework swap look risky in v2.0 pass through essentially unchanged:

- the WS `/mcp` route (`http_server.py:1323`) is a Starlette `WebSocketRoute`,
  which FastAPI supports directly;
- the async task system (`_gc_tasks`/`_begin_task`/`_on_task_done`,
  `http_server.py:165-198`) is framework-independent `asyncio` and moves as-is;
- the lifespan / port-file startup (`http_server.py:1433-1458`) is a Starlette
  lifespan and remains one under FastAPI;
- the **bearer-auth check** (`http_server.py:237-262`) stays as a Starlette/ASGI
  middleware, added unchanged — auth is not re-expressed as a FastAPI dependency.

The v2.0 "large, risky migration" premise (§4.3) was false: because FastAPI is a
thin layer over the same Starlette primitives, there is no rip-and-replace. What
FastAPI *buys* — and why the operator ruled for it — is that request validation
**and** the OpenAPI document are both **derived from the same Pydantic models**.
That deletes two pieces of hand-rolled machinery the v2.0 design would have
built:

- **DELETE the hand-rolled OpenAPI emitter** — FastAPI serves `/openapi.json`
  from the route signatures automatically. A `make openapi` target dumps
  `app.openapi()` to `docs/openapi.json` as a reviewable, diffable artifact.
- **DELETE the custom `Operation` route registry** — FastAPI's decorator routes
  with `response_model=` are the single source of truth; no parallel table to
  keep in sync. (This was the gold-plating: a registry that re-encoded what
  FastAPI already derives from typed handlers.)

**The one real tradeoff and how it's handled.** FastAPI's default validation-error
shape (`422` with its own body) differs from our uniform `ErrorBody` envelope.
Handle it with a **FastAPI exception handler** that maps both request-validation
failures and our typed daemon exceptions to `ErrorBody`, so every error on the
wire has the same shape. Auth stays middleware (above), untouched by this.

**Schemas module — `quarry/api/` (framework-independent topology, retained).**
The shared-model package is deliberately *not* coupled to FastAPI: the daemon
imports it to type its handlers, and `QuarryClient` imports the same models to
build requests and parse responses. Only the daemon links FastAPI; the client and
the contract library do not.

- `quarry/api/schemas.py` — Pydantic v2 `BaseModel`s: one request model and one
  response model per operation (e.g. `SearchRequest`, `SearchResponse`,
  `SearchHit`, `IngestRequest`, `TaskAccepted`, `TaskStatus`, `RegisterRequest`,
  `RegistrationList`, …). These replace every ad-hoc dict in `http_server.py`
  **and** the hand-rolled result parsing in `remote_client.py:134-167`. One
  model, imported by both the FastAPI handler and `QuarryClient` — **param drift
  becomes an import-time type error** (this is the structural kill of
  bug-class-3 for non-search commands, matching what DES-037 did for search).
- `quarry/api/errors.py` — the wire error envelope `ErrorBody{code: str,
  message: str, detail: str | None}` and the mapping from typed client
  exceptions (§3.4) to HTTP status, wired into the FastAPI exception handler.

**`IngestRequest` is dual-mode — path OR uploaded bytes.** Ingestion has two
legitimate sources and the request model must express both, or the remote path
silently diverges from local (the exact bug-class-3 shape this design kills):

- **daemon-local path mode**: the client sends a filesystem path the *daemon*
  can read directly. Used when the client and daemon share a filesystem
  (loopback / same host) — no bytes cross the wire, the daemon opens the file.
- **uploaded-content (bytes) mode**: the client sends the document *content*
  (bytes + filename/format hint) in the request body. Used for **remote**
  daemons, which cannot see the client's filesystem.

`IngestRequest` carries both a `path` field and a `content` field as a
discriminated payload (exactly one populated). **The client chooses by
transport**: a local-daemon `QuarryClient` sends `path`; a remote `QuarryClient`
reads the file and sends `content`. The daemon handler accepts either. This keeps
`quarry ingest` behaving identically whether the daemon is local or remote —
without it, remote ingest would 404/misbehave on a path the daemon can't see.

**`/ca.crt` and task-status aliases.** The CA cert is served at `/ca.crt`
(`http_server.py:1300`), not `/ca-cert`. The backwards-compat task-status
aliases `/sync/{task_id}` and `/ingest/{task_id}` (`http_server.py:1313-1314`)
are **removed** in v2-2 — the unified `/v1/tasks/{id}` is the only task endpoint
(forward-integration, PL-PP-1: no shims).

**Versioning — decision: path-prefixed `/v1`.** All engine operations move under
`/v1/...`. `/health` and `/openapi.json` stay unversioned (bootstrap/discovery).
`/health` reports `{"api_version": "1", "quarry_version": "…"}`. The client
reads it once per connection and caches it. On **major** mismatch (client speaks
`v1`, daemon speaks `v2`) the client raises `QuarryVersionError` with
remediation (`daemon speaks API v2 but this client speaks v1 — upgrade quarry`).
Minor/additive changes never break: unknown response fields are ignored by
Pydantic; new optional request fields default.

**Async pattern — formalized (202 + task polling).** Long operations
(ingest, remember, delete, register, deregister, sync, optimize,
backfill-sessions) return **`202 Accepted`** with `TaskAccepted{task_id}`.
Clients poll `GET /v1/tasks/{task_id}` → `TaskStatus{status: queued|running|
completed|failed, result: <op response> | null, error: ErrorBody | null}`.
`QuarryClient.await_task()` owns the poll loop (replacing the CLI-coupled
`remote_client.py:169-190`). Terminal states are `completed`/`failed`; a poll
during a connection blip reads as `running` (not an error). The typed model also
**fixes an existing bug**: `remote_client.await_task` hardcodes the failure and
timeout messages as `"Deregister failed: …"` / `"Deregister did not complete …"`
(`remote_client.py:179,185`) for **every** task type, so an ingest or sync
failure misreports as a deregister failure. `await_task` returns a typed
`TaskStatus`/raises `QuarryTimeoutError` carrying the task's own `kind`; the CLI
renders the correct operation name.

**The full contract table.** Every operation → endpoint / method / request /
response / status / async. Missing ops (`optimize`, `backfill-sessions`) are
included, closing split-horizon.

| Operation | Method | Path | Request | Response (200) | Async |
|-----------|--------|------|---------|----------------|-------|
| health | GET | `/health` | — | `HealthResponse{state, api_version, quarry_version}` | — |
| openapi | GET | `/openapi.json` | — | OpenAPI 3.1 doc | — |
| ca-cert | GET | `/ca.crt` | — | PEM bytes | — |
| search | GET | `/v1/search` | `SearchRequest` (query params) | `SearchResponse` | — |
| list-documents | GET | `/v1/documents` | `DocumentQuery` | `DocumentList` | — |
| delete-document | DELETE | `/v1/documents` | `DeleteDocumentRequest` | `202 TaskAccepted` | ✓ |
| list-collections | GET | `/v1/collections` | — | `CollectionList` | — |
| delete-collection | DELETE | `/v1/collections` | `DeleteCollectionRequest` | `202 TaskAccepted` | ✓ |
| show | GET | `/v1/show` | `ShowRequest` | `ShowResponse` | — |
| remember | POST | `/v1/remember` | `RememberRequest` | `202 TaskAccepted` | ✓ |
| ingest | POST | `/v1/ingest` | `IngestRequest` (path OR content bytes) | `202 TaskAccepted` | ✓ |
| sync | POST | `/v1/sync` | `SyncRequest` | `202 TaskAccepted` | ✓ |
| **optimize** | POST | `/v1/optimize` | `OptimizeRequest` | `202 TaskAccepted` | ✓ |
| **backfill-sessions** | POST | `/v1/backfill-sessions` | `BackfillRequest` | `202 TaskAccepted` | ✓ |
| captures-push | POST | `/v1/captures/push` | `CapturesPushRequest` | `CapturesPushResponse` | — |
| task-status | GET | `/v1/tasks/{id}` | — | `TaskStatus` | — |
| list-databases | GET | `/v1/databases` | — | `DatabaseList` | — |
| use | POST | `/v1/use` | `UseRequest` | `UseResponse` | — |
| list-registrations | GET | `/v1/registrations` | — | `RegistrationList` | — |
| register | POST | `/v1/registrations` | `RegisterRequest` | `202 TaskAccepted` | ✓ |
| deregister | DELETE | `/v1/registrations` | `DeregisterRequest` | `202 TaskAccepted` | ✓ |
| status | GET | `/v1/status` | — | `StatusResponse` | — |
| mcp | WS | `/v1/mcp` | JSON-RPC stream | JSON-RPC stream | stream |

Error responses use `ErrorBody` uniformly: `400`/`422` (validation — mapped from
FastAPI's Pydantic reject to `ErrorBody` via the exception handler), `401`
(auth), `404` (unknown document/collection/task), `409` (state conflict), `500`
(`_json_server_error`, `http_server.py:1346`). Host-local commands (`enable`,
`serve`, `mcp`, `install`, `login`, `logout`, `doctor`, `version`, `uninstall`)
have **no endpoint** by design — they are not engine operations (§3.1
exception). `disable` is the exception's exception: its hook-config removal is
host-local, but its chunk purge is a daemon call (a `/v1` op or folded into
`deregister`, §3.1).

### 3.4 QuarryClient — the one client (I3)

`quarry/client/` — a library-safe HTTP(S) client. **No `typer`, no `SystemExit`,
no console printing.** It raises a typed exception hierarchy and returns Pydantic
response models. This is the extraction of `RemoteClient` that bead quarry-bxwd
calls for.

**API surface (illustrative — final shape decided at implementation time).**

```python
class QuarryClient:
    # constructed from a resolved config (loopback by default; remote via login)
    @classmethod
    def connect(cls, config: ClientConfig) -> Self: ...

    def search(self, req: SearchRequest) -> SearchResponse: ...
    def ingest(self, req: IngestRequest) -> TaskStatus: ...      # awaits task
    def remember(self, req: RememberRequest) -> TaskStatus: ...
    def show(self, req: ShowRequest) -> ShowResponse: ...
    def status(self) -> StatusResponse: ...
    def list_documents(self, q: DocumentQuery) -> DocumentList: ...
    def register(self, req: RegisterRequest) -> TaskStatus: ...
    def deregister(self, req: DeregisterRequest) -> TaskStatus: ...
    def optimize(self, req: OptimizeRequest) -> TaskStatus: ...
    def backfill_sessions(self, req: BackfillRequest) -> TaskStatus: ...
    # …one method per row of the §3.3 table…
    def await_task(self, task_id: str) -> TaskStatus: ...
```

**Exception model (replaces `typer.Exit`/`SystemExit`).**

```text
QuarryError(Exception)                       # base
├── QuarryConnectionError   # daemon unreachable after autostart nudge (was OSError→typer.Exit, remote_client.py:90-94)
├── QuarryAuthError         # 401 (was SystemExit at remote_client.py:229/237 for TLS/CA)
├── QuarryVersionError      # API major-version skew (new)
├── QuarryNotFoundError     # 404
├── QuarryConflictError     # 409 (invalid state transition)
├── QuarryTimeoutError      # task poll exceeded deadline (was typer.Exit, remote_client.py:184-190)
└── QuarryRemoteError       # non-2xx catch-all, carries status + ErrorBody (was RemoteError, remote_client.py:26-39)
```

Each carries structured context (status, `ErrorBody`, remediation), never a
process exit.

**How the CLI re-adds exit/print at the `__main__` layer.** The engine branch in
every data command (`__main__.py`) is deleted; the command becomes: build the
request model, call `QuarryClient`, render the response with `rich`, and **catch
`QuarryError` at the command boundary** to map it to a `typer.Exit` code + a
red-console message. Exactly one place in the CLI translates typed errors to exit
codes — a small decorator around command bodies — so no engine import and no
`typer` coupling leaks into the client library. `RemoteClient` is deleted in the
same change that wires the CLI to `QuarryClient` (PY-RF-2: no dead code, no
duplicate path).

### 3.5 MCP — drop the in-process engine; MCP is served BY the daemon

**Decision: MCP surface = mcp-proxy (Go, stdio↔WS) → daemon `/v1/mcp`
WebSocket.** The daemon runs the MCP protocol server internally over its
**resident** engine. There is **no Python MCP client process that loads the
engine**, and the in-process stdio `quarry mcp` engine path is **dropped**.

- The MCP protocol handler (`run_mcp_session`) is **daemon-internal server
  code** — it lives on the engine side of the boundary, so its direct use of
  `Database`/`SearchService` is *correct*, not a violation. What changes: it must
  use the daemon's **already-warmed** resources (`ctx.embedder`,
  `ctx.database`, `http_server.py:213-228`) instead of calling
  `get_embedding_backend(settings)` / `Database.connect(...)` fresh
  (`mcp_server.py:132, 84`). This folds in incidental defect §1.8-1 (the third
  ONNX session) — one engine, warmed once, shared by REST and MCP-over-WS.
- **What happens to `quarry mcp`:** removed. Claude Code's MCP config points at
  `mcp-proxy` targeting the local daemon WS (the DES-021 transport, already
  primary per MEMORY). Users on the old stdio entry are migrated by the install
  step rewriting their `.mcp.json` (PR6). No stdio-engine fallback survives —
  keeping it would reintroduce a second engine and a second search path,
  defeating I1.

**Why proxy over an embedded thin MCP server:** an embedded thin MCP server
would be a *second* client that speaks QuarryClient — extra Python process,
extra moving part — when mcp-proxy (5 MB Go) already bridges stdio↔WS and the
daemon already serves `/mcp`. Reuse beats rebuild (§4.2).

### 3.6 How this builds on DES-037 and mcp-proxy (reuse, do not re-solve)

- **DES-037 `SearchService` stays** as the daemon's internal search
  implementation behind `/v1/search` (`http_server.py:327`). Clients stop
  constructing it (`mcp_server.py:141` moves server-side; the CLI never had it —
  it went through `RemoteClient`/local). The shared-library search seam and the
  daemon seam are complementary: DES-037 guarantees one *retrieval algorithm*;
  this proposal guarantees one *engine process* and one *wire contract* for
  everything else.
- **mcp-proxy stays** as the thin MCP transport (§3.5). This proposal does not
  touch the Go binary.

---

## 4. Alternatives considered and rejected

### 4.1 Keep an optional local-engine fallback (daemon-preferred, not assumed)

**Rejected.** A fallback ("if the daemon is down, run the engine in-process")
preserves the dual path whose divergence CLAUDE.md documents across 10 TLS review
rounds (bug-class-3). The fallback engine would drift from the daemon engine
exactly as the current `else:` branches drift from the HTTP handlers. I2 exists
precisely to remove the structure that permits drift. The strongest objection to
"assumed, no fallback" — offline/CI/first-run/single-shot — is answered in §5
without a fallback: in-process **ASGI** test daemon (no socket), supervised
autostart nudge (cold machine), install-time daemon health-gate (first run),
bounded fail-fast (single-shot). None of these require an in-process engine in a
*client*.

### 4.2 Embed a thin Python MCP server that calls QuarryClient

**Rejected** in favor of mcp-proxy → daemon WS (§3.5). The proxy already exists,
is 5 MB, and the daemon already serves `/mcp`. An embedded Python MCP server adds
a process and code for no capability gain.

### 4.3 Keep Starlette + a hand-rolled route registry and OpenAPI emitter

**Rejected — this was the v2.0 decision, overturned by the operator (correctness
over migration-avoidance).** The v2.0 premise was that adopting FastAPI meant a
"large, risky migration off Starlette" touching the WS `/mcp` route, the async
task system (`http_server.py:165-198`), the lifespan/port-file startup
(`http_server.py:1433-1458`), and auth middleware. **That premise was false.**
FastAPI *is* Starlette underneath — the same ASGI app, routing, middleware, and
WebSocket support — so all of those pieces pass through unchanged (§3.3). There
is no rip-and-replace to migrate.

Given that, the hand-rolled alternative is strictly worse: a bespoke `Operation`
route registry and a hand-written OpenAPI emitter re-encode, by hand, exactly
what FastAPI derives automatically from the same Pydantic models — two extra
pieces of machinery to build, test, and keep in sync with the handlers. That is
gold-plating. The one genuine cost of FastAPI (its `422` validation-error shape
differs from our `ErrorBody`) is a single exception handler (§3.3). We adopt
FastAPI and delete the registry + emitter from scope.

### 4.4 Keep `RemoteClient` CLI-coupled, share it via callbacks

**Rejected.** Injecting print/exit callbacks to "decouple" `RemoteClient` still
leaves a client that knows about process exit. A library must raise, not exit
(PY-EH-8 spirit; bead quarry-bxwd). Clean typed exceptions + a CLI-layer
translator is the correct seam.

---

## 5. Test story — no hand-managed daemon

**In-process ASGI fixture daemon (DES-031 v1 PR6, realized concretely).** Tests
never spawn a socket-bound daemon. A **session-scoped pytest fixture** builds the
FastAPI app via `build_app(ctx)` (`http_server.py:1286`, now returning a FastAPI
app) with a temp LanceDB and a small/stub embedder, and drives it through
**`httpx.ASGITransport`** — the app runs in-process, in-memory, no ports, no
subprocess. FastAPI is an ASGI app exactly as the Starlette app was, so
`ASGITransport` over it is identical; the framework swap does not change the test
seam. `QuarryClient` takes an **injectable transport** so the fixture wires it to
the ASGI app; production wires it to a real loopback/remote transport.
Consequences:

- **CI never needs a real ONNX daemon** for client/CLI/contract tests — the
  engine is exercised in-process behind the same wire contract clients use.
- **Contract equivalence is structural, not reactive.** Because both the daemon
  handler and `QuarryClient` import the same `quarry.api` models, a field added
  on one side that is missing on the other is an import/type error. The former
  bug-class-3 equivalence tests (CLAUDE.md testing rules 3–4) become a thin
  belt-and-braces check over a contract that already can't drift.
- **Async pattern tested once:** a `202 → poll → completed` round-trip against
  the ASGI app, asserting `TaskStatus` shape.
- **Boundary test:** the import-linter contract (§3.1) runs in `make check`; a
  unit test imports `quarry.client` and `quarry.api` with lancedb/onnxruntime
  import sabotaged and asserts success (proves clients are engine-free). The same
  sabotage asserts every CLI command *except* `serve`/`mcp` imports cleanly,
  proving those two commands' engine imports are lazy (§3.1).

**One real-loopback-TLS smoke test rides the wheel/integration gate.** The ASGI
tier is fast and hermetic but it **cannot** exercise the uvicorn socket, the TLS
framing, or the pinned-CA handshake — `ASGITransport` bypasses the socket
entirely. The CLAUDE.md class-4 TLS bug history (IP-SAN vs DNS-SAN,
`not_valid_before` skew, pinned-CA context excluding system roots) all lives
below the ASGI layer. So alongside the in-process fixture, keep **one** contract
smoke test that starts a real uvicorn daemon on loopback with TLS and drives
`QuarryClient` against it over the pinned-CA transport — enough to prove the
socket/TLS/framing path that ASGI can't reach.

Integration tier (real ONNX, real filesystem) stays opt-in
(`make test-integration`), unchanged in spirit. The always-on watch loop is no
longer part of this epic's integration surface (it moves to quarry-lxrk, §3.2).

---

## 6. Migration & sequencing — supersedes the v1 6-PR table

Split by **rollback granularity** (CLAUDE.md PR boundaries). Ordering matters:
the contract must exist before clients drop their engine paths. Each PR carries
its CHANGELOG entry and a real OO improvement on files it touches (ratchet).

| PR | Scope (rollback-coherent unit) | Folds in |
|----|--------------------------------|----------|
| **v2-1 (docs/ADR)** | Write DES-031 v2 into DESIGN.md; correct `architecture.tex` §Design-Principles P1 (Library-first → Daemon-first) and §CLI-Independence (CLI-via-daemon); fix CLAUDE.md stale `quarry-server` module row; README "how it works" diagram. Doc-only. | §1.8-2, §1.8-3 |
| **v2-2 (contract + FastAPI)** | Adopt **FastAPI** for the daemon app (Starlette-compatible: WS `/mcp`, task system, lifespan, bearer-auth middleware unchanged); `quarry/api/` package: schemas + errors (no hand-rolled route registry, no hand-rolled OpenAPI emitter — FastAPI derives both from the models); rewrite every `http_server.py` handler to type its request/response models; add the `ErrorBody` exception handler (maps FastAPI 422 + typed errors); **add `/v1/optimize` + `/v1/backfill-sessions`**; add `/v1` prefix + `state`/version in `/health`; **remove the `/sync/{task_id}` + `/ingest/{task_id}` alias routes** (http_server.py:1313-1314); FastAPI serves `/openapi.json` + `make openapi` dumps `docs/openapi.json`. **Cross-repo (biff): coordinate with quarry-menubar** — when routes move to `/v1` with Pydantic responses, land the Swift `QuarryClient` base-path/schema update in lockstep (small; it already has a client abstraction and reads `/health`). Closes split-horizon at the daemon. | §1.5, §1.6 gaps, gvr framework/alias/menubar |
| **v2-3a (supervision + autostart nudge)** | Ship supervised service units (launchd `KeepAlive` / systemd `Restart=always` user unit) and the autostart-nudge helper (kickstart on macOS, `systemctl --user restart` on Linux; warm-budget `/health` poll; not-installed vs down errors). **Lands before v2-3** so no PR ever ships an "assumed present" daemon that is neither supervised nor auto-started. | §3.2, gvr #1/#2 |
| **v2-3 (QuarryClient + CLI)** | `quarry/client/` (QuarryClient + typed exceptions, using v2-3a's nudge on connection refusal); **delete `RemoteClient`**; remove the in-process engine `else:` branch from all 18 data commands in `__main__.py`; wire commands to QuarryClient; add the CLI-layer error→exit translator; add remote paths for `optimize` + `backfill-sessions`; move `disable`'s chunk purge to a daemon call (§3.1). `serve`/`mcp` keep engine access via a lazy in-body import (§3.1). Rollback unit = CLI behavior. | §1.2, §1.3 (RemoteClient), §3.1 disable, quarry-bxwd |
| **v2-4 (MCP)** | Drop in-process `quarry mcp` engine entry; make the daemon's MCP session use `ctx.embedder`/`ctx.database`; move `/mcp` → `/v1/mcp`; install rewrites user `.mcp.json` to mcp-proxy→daemon. | §1.4, §1.8-1 |
| **v2-5 (library — pure in-repo)** | Replace `__init__.py` engine exports (`Database`, `get_db`, `ingest_*`, `ChunkSearch`, `__init__.py:46-55`) with `QuarryClient` as the public library API. **No external Python library consumer exists** (quarry-menubar is a Swift HTTP client, not a Python importer), so this is a **pure in-repo change** — no biff-coordinated migration, no shim, no staged removal. Delete the engine names outright. | §1.3 (library) |
| **v2-6 (install/tests)** | Install verifies daemon `/health` (`state == ready`) before exit 0; add the session-scoped in-process ASGI fixture + import-linter boundary contract + engine-sabotage test + **one real-loopback-TLS contract smoke test** to `make check`/the wheel gate (§5). *(Supervision moved to v2-3a; the always-on watch/index loop is out of scope — bead quarry-lxrk.)* | §3.2, §5 |

**Out of scope — the indexing triggers.** The always-on filesystem watch/index
loop (and its serialized per-collection queue / double-index-race handling) is
**bead quarry-lxrk**, not a v2-N PR here. The interim scheduled sync
(cron/launchd-timer invoking `quarry sync`) is **bead quarry-tqdq**, overlapping
quarry-uae. This epic delivers the resident engine, the `/v1/sync` endpoint, and
daemon ownership of `SyncRegistry`; the triggers that drive it ship separately
(§3.2).

**Deprecation path (no in-code shims — PL-PP-1).** The library migration (v2-5)
is a **pure in-repo removal** — quarry-menubar is a Swift HTTP client with zero
Python-engine coupling, so there is no cross-repo library consumer to sequence:
`QuarryClient` ships in v2-3, then the engine exports are **removed** in v2-5, no
alias. The only cross-repo touch in the whole epic is the Swift client's
`/v1` base-path/schema bump, which lands in **v2-2** in lockstep with the route
move (biff-coordinated). `quarry mcp` users are migrated by v2-4's install step
rewriting their MCP config — the old entry is deleted, not aliased. No `old =
new` tombstones anywhere.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| FastAPI adoption (v2-2) breaks the WS route, task system, or auth | These are Starlette primitives FastAPI inherits — WS `/mcp`, the `asyncio` task system (`http_server.py:165-198`), the lifespan/port-file startup (`http_server.py:1433-1458`), and the bearer-auth ASGI middleware (`http_server.py:237-262`) move unchanged (§3.3); the ASGI test fixture (§5) exercises them |
| FastAPI's `422` error shape diverges from `ErrorBody` | A single FastAPI exception handler maps validation + typed errors to the uniform `ErrorBody` envelope (§3.3) |
| Contract rewrite (v2-2) regresses a route's shape | OpenAPI diff (`docs/openapi.json`) in review; contract equivalence tests against the ASGI fixture; schemas are the single source both the FastAPI handler and `QuarryClient` import |
| Swift menubar client breaks when routes move to `/v1` | v2-2 coordinates the base-path/schema bump with quarry-menubar in lockstep (biff); the Swift `QuarryClient` already has a client abstraction and reads `/health`, so the change is small |
| Cold machine / wedged daemon mid-command | Supervisor autostart nudge (kickstart / `systemctl --user restart`) + warm-budget `/health` poll, then typed fail-fast distinguishing not-installed vs down (§3.2) |
| Cold ONNX load exceeds the quick probe and spuriously "fails" | Two separate timeouts — quick "already up?" probe vs ~30 s post-kickstart warm budget; `/health` reports `warming` vs `ready` (§3.2) |
| Client/daemon version skew after a partial upgrade | `/health` advertises `api_version`; client caches + raises `QuarryVersionError` on major mismatch (§3.3) |
| Loopback daemon reachable by any local user on a multi-user host | Local daemon requires the bearer token even on loopback (auth already at `http_server.py:237-262`); token in `~/.punt-labs/quarry/` mode-0600 |
| ASGI fixture cannot exercise the socket / TLS / pinned-CA path | One real-loopback-TLS contract smoke test rides the wheel/integration gate alongside the ASGI fixture (§5) — closes the CLAUDE.md class-4 blind spot |
| Remote ingest silently diverges from local (path the daemon can't see) | `IngestRequest` is dual-mode: local client sends `path`, remote client uploads `content` bytes; the daemon accepts either (§3.3) |
| First-install commands need to run before a daemon exists | Host-local exception set (§3.1) runs pre-daemon; none touch the engine, and `serve`/`mcp` pull it in via a lazy in-body import |
| Large `__main__.py` churn in v2-3 | Thin-client commands shrink the file substantially; the ratchet improves as engine branches leave (DES-031 v1 rationale #4 still holds) |

**Trust/security note.** Default binding is **loopback**; remote exposure is
opt-in via `quarry login` and rides the **existing TLS + pinned-CA** path
(`remote_client.py:224-241`, preserved in `QuarryClient`). Local auth uses the
existing bearer scheme (`http_server.py:237`). Nothing here weakens the
remote-access trust model; it inherits it unchanged.

---

## 8. Supersession note

This proposal **supersedes DES-031**. It:

- **drops the "five engines per host" framing** — the real steady state is one
  resident daemon; `mcp-proxy` and the menubar hold no engine (§1.1);
- **names the true driver** — the daemon is the **one resident indexing engine**
  every trigger reuses instead of cold-starting ~1.6 GB per run (§3.2); the
  triggers themselves (interim scheduled sync quarry-tqdq/quarry-uae; the
  event-driven watch loop quarry-lxrk) are **decoupled** out of this epic;
- **reflects DES-037** — `SearchService` already unified *search* across
  surfaces via a shared library seam; this design reuses it as the daemon's
  internal search impl and does not re-solve it (§1.7, §3.6);
- **reflects mcp-proxy** — the thin MCP transport already exists (DES-021); MCP
  is served by the daemon over `/v1/mcp` and the in-process stdio engine is
  dropped (§3.5);
- **adopts FastAPI** (operator ruling) — the wire protocol hardens from "shared
  Pydantic models" (a sentence in v1) into a formal, versioned, FastAPI-served
  OpenAPI contract with a single `QuarryClient` (§3.3–3.4); the hand-rolled route
  registry and OpenAPI emitter are dropped as gold-plating (§4.3);
- **is proven by prior art** — quarry-menubar (`../quarry-menubar`) is already a
  pure HTTP thin client: a Swift `QuarryClient` using `URLSession` to
  `127.0.0.1:8420` with pinned-CA TLS, hitting `/health`, `/status`, `/search`
  and holding **zero** Python-engine coupling. The daemon-first thin-client model
  this design mandates for Python already works in production in Swift, with
  connection profiles and pinned-CA — the Python `QuarryClient` mirrors its
  shape, and the OpenAPI doc (§3.3) is the shared contract both clients conform
  to (a concrete payoff of I3);
- **folds in the incidental defects** — the `/mcp` third-ONNX-session
  (§1.8-1 → v2-4), the stale `architecture.tex` P1/CLI-Independence + CLAUDE.md
  `quarry-server` docs (§1.8-2/3 → v2-1), the `/ca.crt` route naming and the
  task-status alias routes (§1.6/§3.3 → v2-2), and the hardcoded "Deregister
  failed" `await_task` bug (§3.3, remote_client.py:179,185 → v2-3).

On ratification, DES-031's ADR body is replaced by this content (status
ACCEPTED) and the v1 6-PR table is replaced by §6.
