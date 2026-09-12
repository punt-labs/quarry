# Memory subsystem activation

## Scope

Turn on three runtime behaviors that the schema already supports:

1. Temporal decay on agent-memory rows during RRF fusion.
2. Per-agent routing of `remember` writes to `memory-<handle>` collections.
3. A doctor check that reports the memory corpus by handle, type, and
   collection.

PreCompact capture routing is discussed as a fork requiring a ruling.

## Verified state (2026-08-30)

- `src/quarry/retrieval/config.py:42` — `decay_rate: float = 0.0`.
- `src/quarry/retrieval/fusion.py:73-81` — decay gates on
  `memory_type in _DECAYABLE_TYPES`, not on `agent_handle`.
- `src/quarry/retrieval/service.py:33-36` — `SearchService.__new__` accepts an
  optional `RetrievalConfig` but the daemon does not pass one.
- `src/quarry/daemon/routes/search.py:40` —
  `SearchService(self.ctx.query_database)` with no config.
- `src/quarry/mcp_server.py:213` — MCP `remember.collection: str = "default"`.
- `src/quarry/cli_ingest.py:94-96` — CLI `--collection` defaults to `"default"`.
- `src/quarry/api/ingestion.py:13` — `RememberRequest.collection: str = "default"`.
- `src/quarry/daemon/routes/ingestion.py:75-98` — `_remember_job` reads the
  body's `collection` (fallback `"default"`) and constructs `ScrubbedIngestJob`.
- `src/quarry/hooks.py:596-624` — PreCompact reads the ethos handle and
  builds `CaptureIngestRequest`; no collection field is set here — the daemon
  derives `<repo>-captures` from `cwd`.
- `src/quarry/ethos_memory.py:36,88-92` — per-handle collection name is
  `memory-<handle>`; ethos ext template already advertises it as the working
  memory collection.
- `src/quarry/config.py:30-107` — `Settings` (pydantic-settings, `.env` +
  `QUARRY_*`). No retrieval knob today.
- `src/quarry/doctor_captures.py` — captures + shadow repo checks
  (`CaptureDiagnostics`, `@final`, `__slots__ = ()`).
- Sibling doctor modules: `doctor.py`, `doctor_daemon.py`, `doctor_ethos.py`,
  `doctor_inference.py`, `doctor_sync.py`. No `doctor_agents.py`; no
  `doctor_ext.py`. The mission description's roster is out of date; the design
  targets the actual layout.

## Decisions

### D1 — Decay half-life: 30 days

`decay_rate = 0.000963` (units: 1/hour). Half-life `= ln(2) / 0.000963 ≈ 720 h
= 30 d`. `weight = exp(-decay_rate * hours_since_ingest)`.

Why 30 days:

- RRF rank-1 weight is `1/(60+0) ≈ 0.0167`; rank-9 is `1/(60+9) ≈ 0.0145`.
  A 30-day-old memory has weight `0.5`, roughly the difference between
  ranks 1 and 60. So a month-old memory that was top-1 loses to a fresh
  rank-2 hit — the intended behavior.
- 7 days is aggressive: a fact learned Monday is half-weight the following
  Monday. Typical debugging arcs and design iterations run longer than
  that; useful memories would decay under noise before they matured.
- 90 days approximates no decay for most session cadences. Fails the
  "distinguish memory from a knowledge base" test.

Config knob: add `retrieval_decay_rate: float = 0.000963` to `Settings`
(`config.py`). `QUARRY_RETRIEVAL_DECAY_RATE` is the env override, consistent
with existing `QUARRY_*` knobs. `SearchRoutes.search`
(`daemon/routes/search.py:40`) constructs
`RetrievalConfig(decay_rate=settings.retrieval_decay_rate)` and passes it to
`SearchService`.

`RetrievalConfig.decay_rate` default at the dataclass stays `0.0` — the
production value is set by the daemon at wire time. Tests that construct
`RetrievalConfig()` directly (eval fixtures, unit tests) keep the "no decay"
baseline unless they opt in.

### D1a — Fusion must gate on `agent_handle`, not just `memory_type`

`fusion.py:73-81` currently decays any row whose `memory_type` is one of
`{fact, observation, opinion, procedure}`. Nothing prevents a bulk-ingested
document from arriving with a `memory_type` tag — and the bead spec is
explicit that empty `agent_handle` is the exemption axis. Change the guard:

```python
memory_type = str(row.get("memory_type", ""))
agent_handle = str(row.get("agent_handle", ""))
if self._decay_rate > 0 and agent_handle and memory_type in _DECAYABLE_TYPES:
    ...
```

Both conditions must hold. Knowledge chunks (empty `agent_handle`) never
decay regardless of `memory_type`.

### D2 — `remember` routing: silent, server-side, empty-string sentinel

**Rule.** When a `remember` request arrives with `agent_handle` set and no
explicit `collection`, route to `memory-<handle>`. Explicit `collection` wins.
No `agent_handle` and no `collection` → `default`.

**Sentinel.** Change three defaults from `"default"` to `""`:

- `api/ingestion.py:13` — `RememberRequest.collection: str = ""`.
- `mcp_server.py:213` — MCP `remember(collection: str = "")`.
- `cli_ingest.py:94-96` — CLI `--collection` default `""`.

Empty is the "unset" signal; the daemon decides the effective collection.

**Chokepoint.** One place, one rule: `daemon/routes/ingestion.py:75-98`
(`_remember_job`). Add before the `ScrubbedIngestJob` construction:

```python
raw_collection = self._str_field(body, "collection", "")
agent_handle = self._str_field(body, "agent_handle", "")
if not raw_collection:
    collection = f"memory-{agent_handle}" if agent_handle else "default"
else:
    collection = raw_collection
```

Both MCP and CLI go through this route, so the surfaces cannot drift
(bug class 3).

**Auto-create.** LanceDB stores `collection` as a column value in the
single `chunks` table (`db/schema.py:47`); a new value writes without any
table-creation step. `catalog.list_collections()`
(`db/chunk_catalog.py:89`) surfaces the collection the first time a row
lands. First-write is create.

**Rejected alternatives.**

- (b) Require explicit `collection=memory-<handle>`. Places the routing
  burden on every caller (four surfaces, three of them agents). Guarantees
  the current failure mode continues.
- (c) Opt-in flag (`--memory` / `route_to_memory=True`). Adds a
  parameter to every caller for a rule the daemon can apply
  unambiguously from `agent_handle`.

### D3 — PreCompact collection: keep `<repo>-captures` (needs ratification)

**Recommendation.** Option (a): PreCompact continues to write to
`<repo>-captures`; identity discovery comes from `SearchFilter.agent_handle`
at read time. No change to `hooks.py:596-624` or the daemon capture route.

**Reasoning.**

- Durability lives per-project. `hooks.py:606-614` writes the scrubbed
  transcript to `<cwd>/.punt-labs/quarry/captures/`; the shadow repo
  (`doctor_captures.py:60-156`) is the private per-project git that owns
  that tree. Rerouting the LanceDB row to `memory-<handle>` while the
  durable file stays under the repo splits custody across two axes with no
  reader that needs both.
- `find --agent-handle rmh` already ranges over all collections when no
  `--collection` is passed. The `SearchFilter` predicate at
  `results.py:214-221` is a WHERE clause on the shared `chunks` table, not
  a collection scan. "My memory across repos" is a read-side query, not a
  storage arrangement.
- Option (c) dual-write doubles chunk counts (currently 107,362) and embed
  cost for a view already achievable through the filter. Complicates
  `chunk_count` telemetry and the shadow-repo model.

**Cost of (a).** The mental model gap the bead flags survives: a naive
`find --collection memory-rmh` will not return PreCompact captures.
Mitigation: the SessionStart context (`hooks.py:301-312`) and the ethos
`session_context` template (`doctor_ethos.py:12-36`) already tell agents
to filter by `agent_handle`. If the mismatch persists in practice, the
follow-up is a curated surface (`find --mine`) that filters by the current
session's handle, not a storage rewrite.

**Operator ratification required.** This is the design fork the mission
flagged. The alternative view — that "memory" should mean a single
per-handle collection regardless of source — is coherent and would justify
option (b) or (c) instead. Ruling needed before implementation dispatches.

### D4 — Doctor check: new `doctor_memory.py` module

**Placement.** New file `src/quarry/doctor_memory.py`, class
`MemoryDiagnostics`. Not `doctor_captures.py`: captures and memory are
separate concerns (SRP; the captures module already covers unlinked
collections and the shadow repo). Not `doctor_ethos.py`: that owns the
identity ext-file propagation, not the corpus itself. A new module
matches the existing one-class-per-concern split.

**Interface.**

```python
@final
class MemoryDiagnostics:
    __slots__ = ()

    @staticmethod
    def corpus(db_path: Path) -> CheckResult: ...
    @staticmethod
    def identity_active(cwd: str, db_path: Path) -> CheckResult: ...
```

**`corpus` output.** Non-failing informational check
(`required=False`). Message is one line per non-empty group:

```text
memory: rmh=42 claude=18 kpz=7; types: fact=30 observation=25 procedure=12;
collections: memory-rmh=42 memory-claude=18 quarry-captures=8 …
```

Query: single scan on the `chunks` table (mirrors
`chunk_catalog.list_collections()` at
`db/chunk_catalog.py:89-121`), selecting `agent_handle`, `memory_type`,
`collection`. Rows with empty `agent_handle` are excluded from the
per-handle count and type count; the collection count reports every
collection so the operator can see the split between `memory-*` and
`*-captures`.

**`identity_active` output.** Warning-only check
(`required=False`). Reads the ethos handle at `cwd` via the same walker
already in `hooks.py:377-408` — extract to
`quarry.ethos_handle.read_agent_handle(cwd) -> str` so the doctor and
the hook share it. If the handle is set AND the current registration's
collection has chunks AND that handle has zero rows in the database:

```text
identity 'rmh' active in this repo but has zero memory rows; check that
ethos config resolves and PreCompact fires
```

Warns without failing. If the handle is empty or the repo is unregistered,
the check passes silently.

**Wiring.** Add to the `check_environment` sequence in `doctor.py`
alongside `CaptureDiagnostics.unlinked` and
`EthosExtDiagnostics.configure`. Ordering: after captures, before the
ethos ext check.

## Write set for the implementation mission

| File | Change |
|---|---|
| `src/quarry/config.py` | Add `retrieval_decay_rate: float = 0.000963` field on `Settings` after `fd_limit` (`config.py:85`). |
| `src/quarry/retrieval/config.py:42` | Leave `decay_rate: float = 0.0` — the production value is threaded from `Settings` at wire time. Docstring updated to name the wire source. |
| `src/quarry/retrieval/fusion.py:73-81` | Add `agent_handle` predicate to the decay guard (D1a). |
| `src/quarry/daemon/routes/search.py:40` | Pass `RetrievalConfig(decay_rate=self.ctx.settings.retrieval_decay_rate)` to `SearchService`. Verify `ctx.settings` is available; if not, thread `Settings` through `RouteGroup`. |
| `src/quarry/api/ingestion.py:13` | Change `RememberRequest.collection` default from `"default"` to `""`. |
| `src/quarry/mcp_server.py:213` | Change MCP `remember(collection: str = "default", ...)` to `""`; update the docstring to name the routing rule. |
| `src/quarry/cli_ingest.py:94-96` | Change `--collection` default from `"default"` to `""`. |
| `src/quarry/daemon/routes/ingestion.py:75-98` | Apply the routing rule in `_remember_job` before constructing `ScrubbedIngestJob` (D2). |
| `src/quarry/ethos_handle.py` (NEW) | Extract `_read_ethos_agent_handle` from `hooks.py:377-408` into a module-level function `read_agent_handle(cwd: str) -> str`. Update the hooks call site to import from here. Nothing else in `hooks.py` changes. |
| `src/quarry/doctor_memory.py` (NEW) | `MemoryDiagnostics` per D4. |
| `src/quarry/doctor.py` | Wire `MemoryDiagnostics.corpus` and `.identity_active` into `check_environment` after the captures check. |
| `tests/test_fusion.py` | Add: decay skipped when `agent_handle=""` regardless of `memory_type`; decay applied when both set; knowledge chunks (no handle, no type) never decay. |
| `tests/test_ingestion_routes.py` (or the daemon route test file that covers `/remember`) | (a) `agent_handle="rmh"`, no `collection` → job.collection == "memory-rmh"; (b) explicit `collection="foo"` overrides; (c) no handle, no collection → "default"; (d) `agent_handle=""`, `collection=""` → "default". |
| `tests/test_mcp_server.py` | Equivalence test: MCP `remember` with `agent_handle="rmh"` and no `collection` sends `collection=""` on the wire; server-side test proves the resulting job lands in `memory-rmh`. |
| `tests/test_cli_ingest.py` | Same for CLI `quarry remember --agent-handle rmh` → collection `memory-rmh` at the daemon boundary. |
| `tests/test_doctor_memory.py` (NEW) | Corpus counts across handles/types/collections; identity_active warns when the current handle has zero rows and the repo has any indexed chunks; passes silently when handle empty. |
| `tests/test_daemon_routes_search.py` | `SearchService` receives the settings-derived `decay_rate`. |
| `tests/test_ethos_handle.py` (NEW) | Behavior of the extracted `read_agent_handle` walker; existing coverage in `test_hooks.py` moves here. |

Not in the write set:

- `src/quarry/hooks.py:596-624` — no change under D3 recommendation. Only
  changes if the operator rules for option (b) or (c) instead.
- `src/quarry/results.py` — `SearchFilter` already carries
  `agent_handle`/`memory_type`; no change.
- `src/quarry/db/schema.py` — the three memory columns already exist.

## OO expectations for the implementation mission

- `MemoryDiagnostics` is `@final`, `__slots__ = ()`, `staticmethod`-only,
  same shape as `CaptureDiagnostics` at `doctor_captures.py:19-23`.
- `ethos_handle.py` is a single-function module (walker only). If the
  implementer sees a second concern arriving (e.g., ethos ext read),
  extract to a class then.
- The `_remember_job` change adds one branch. If the daemon route file
  crosses the module-size threshold, extract a `MemoryRouting` value class
  that owns the rule — do not leave a free helper next to the class
  (PY-OO-7).
