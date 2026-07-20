# DES-031 PR-5 (v2-5) — The Library as a Thin Client

**Status:** PROPOSED (design only — not implemented)
**Date:** 2026-07-19
**Author:** rmh
**Bead:** quarry-5e5t
**Parent:** DES-031 v2 (`docs/des-031v2-daemon-first.md`), invariant I1; §3.1, §3.4, §6 (v2-5 row)
**Depends on:** PR-3 `QuarryClient` + `TargetResolver` (shipped, main `ed810f7`)
**Blocks:** PR-6 boundary contract (the library is one of the client surfaces PR-6 locks)

---

## 1. Problem

`import quarry` today re-exports the **engine**. The top-level package
(`src/quarry/__init__.py:46-55`) lazy-loads six names whose targets are the
in-process engine:

| Public name | Lazy target (`_LAZY_ATTRS`) | Engine? |
|-------------|-----------------------------|---------|
| `Database` | `quarry.db.facade:Database` | **engine** (LanceDB facade) |
| `get_db` | `quarry.db.storage:get_db` | **engine** (LanceDB connect) |
| `ChunkSearch` | `quarry.db.chunk_search:ChunkSearch` | **engine** (LanceDB query) |
| `ingest_content` | `quarry.ingestion.pipeline:ingest_content` | **engine** (ONNX + LanceDB) |
| `ingest_document` | `quarry.ingestion.pipeline:ingest_document` | **engine** (ONNX + LanceDB) |
| `ingest_url` | `quarry.ingestion.pipeline:ingest_url` | **engine** (ONNX + LanceDB) |
| `Settings` | `quarry.config:Settings` | thin (pydantic-settings) |
| `CollectionName` | `quarry.collections:CollectionName` | thin (stdlib flyweight) |

Under DES-031 I1 (`des-031v2-daemon-first.md:183-186`), the library is a **pure
client** — it must not import or construct `Database`, `embeddings`,
`ingestion.pipeline`, `retrieval`, or `SyncRegistry`. The six engine names above
are exactly what I1 forbids: accessing `quarry.Database` or `quarry.ingest_url`
pulls lancedb + onnxruntime + the pipeline into the caller's process.

PR-5 replaces those exports with `QuarryClient` (shipped in PR-3) as the public
library API and **deletes the engine names outright**. Per DES-031 §6 (v2-5 row,
`des-031v2-daemon-first.md:725`) and §Deprecation-path (`:736-744`), **no external
Python library consumer exists** — quarry-menubar is a Swift `URLSession` HTTP
client, not a Python importer — so this is a **pure in-repo removal**: no shim, no
`_old = new` alias, no staged deprecation (PL-PP-1).

---

## 2. Invariant — I1 for the library package

**I1 (library form).** `import quarry` must load **zero engine**: no `lancedb`,
`onnxruntime`, `pyarrow`, `quarry.db`, `quarry.ingestion`, `quarry.retrieval`, or
`quarry.sync` in `sys.modules` after the import, and no public name whose access
triggers such a load. The top-level package exposes only the client surface
(`QuarryClient`, `TargetResolver`, the typed error hierarchy, `TaskOutcome`) and
the wire-contract models (`quarry.api`).

The proof is a runtime engine-sabotage import test (§6), the same mechanism
DES-031 §3.1 #3 (`des-031v2-daemon-first.md:275-280`) mandates and that the API
package already carries (`tests/test_api.py:99-124`), extended to the top-level
package.

---

## 3. Who-imports-what — the deletion and rewire set

### 3.1 Nobody imports the engine names from the top level

A full-tree grep for top-level importers of the six engine names (and the two
thin ones) returns **zero real callers**:

```text
grep -rEn "from quarry import.*(Database|get_db|ingest_content|ingest_document|
  ingest_url|ChunkSearch|Settings|CollectionName)" src/ tests/ tools/
→ src/quarry/__init__.py:5-6   (the module DOCSTRING's usage example — not code)
```

The only two live top-level imports in the whole tree are non-engine and survive
untouched:

| Importer | Statement | Fate |
|----------|-----------|------|
| `tests/test_config.py:9` | `from quarry import __version__` | **keep** — `__version__` stays |
| `tests/test_fd_headroom.py:11` | `from quarry import fd_headroom` | **keep** — submodule attribute, not a `_LAZY_ATTRS` entry |

`fd_headroom` resolves through normal submodule attribute access
(`quarry/fd_headroom.py`), not the lazy loader, so it is unaffected by changing
`_LAZY_ATTRS`.

### 3.2 Internal engine/daemon code imports from SUBMODULES — untouched

Every in-process engine caller already imports from the submodule, never the
top-level re-export. These stay exactly as they are (deleting the top-level
re-export does not touch them):

| Caller | Import | Layer |
|--------|--------|-------|
| `daemon/context.py:18` | `from quarry.db import Database` | engine (server side) |
| `daemon/routes/ingestion.py:45,78` | `from quarry.ingestion.pipeline import ingest_content, ingest_auto` | engine |
| `daemon/routes/search.py:13` | `from quarry.retrieval import SearchService` | engine |
| `daemon/routes/meta.py:13`, `databases.py:11` | `from quarry.db.storage import …` | engine |
| `mcp_server.py:14,26,27,35` | `from quarry.db / ingestion / retrieval import …` | engine (host-local `mcp`) |
| `_hook_entry.py:143,181` | `from quarry.db import Database`; `from quarry.ingestion.pipeline import ingest_content` (lazy, in-method) | engine (background subprocess) |

The deletion removes **only** the top-level public re-exports in
`__init__.py`. The submodules `quarry.db`, `quarry.ingestion.pipeline`,
`quarry.retrieval`, `quarry.db.chunk_search`, `quarry.db.storage` are the engine's
own modules and remain — they are what the daemon imports.

### 3.3 The complete rewire set

**There is no rewire set.** No production or test code imports the six engine
names (or `Settings`/`CollectionName`) from the top-level package. The deletion is
therefore a pure removal from `__init__.py` with no downstream caller changes —
the cleanest possible form of PL-PP-1.

The only additive edit outside `__init__.py` is the new sabotage test (§6).

---

## 4. The new public library API

### 4.1 What `import quarry` exposes after PR-5

The top-level package becomes the **entry door to the client**, nothing more.
A library consumer needs three things: a way to *construct* a client, the *typed
request/response models* to talk to it, and the *typed errors* to catch. Map each
to a re-export:

| Need | Public name | Source module |
|------|-------------|---------------|
| Construct against the local daemon (zero-config) | `TargetResolver` | `quarry.client.resolver` |
| Construct against an explicit config / injected transport | `QuarryClient`, `ClientConfig` | `quarry.client` |
| Catch failures | `QuarryError`, `QuarryConnectionError`, `HttpError` | `quarry.client.errors` |
| Read a task outcome | `TaskOutcome` | `quarry.client.task` |
| Build typed requests / read responses | **`quarry.api` models** | `quarry.api` (own `__all__`) |
| Version string | `__version__` | (module global) |

**Request/response models are reached through `quarry.api`, not re-exported at the
top level.** `quarry.api` already defines its own `__all__` of 30 models
(`api/__init__.py:41-71`) and is the dedicated wire-contract module (PY-IC-9:
types live in their own module). Re-exporting all 30 at the top level would push
the package interface past PL-MD-3's flag threshold (>30 names) for no benefit;
`from quarry.api import SearchRequest` is the natural, discoverable path. This is
**decision D2** (§9) — the operator may rule for a flat re-export instead.

### 4.2 Proposed `__all__`

```python
__all__ = [
    "ClientConfig",
    "HttpError",
    "QuarryClient",
    "QuarryConnectionError",
    "QuarryError",
    "TargetResolver",
    "TaskOutcome",
    "__version__",
]
```

Eight names — a narrow, stable client surface (PL-MD-3: ≤ 20). `Settings` and
`CollectionName` are **dropped** from the top-level re-export (decision D1, §9):
both are already-thin but neither is client surface. No code imports either from
the top level (§3.1); a consumer that genuinely needs `Settings` imports
`quarry.config` directly, and `CollectionName` is an engine-side value object the
client never constructs (the api models carry `collection` as a plain `str`).

### 4.3 Before / after — a library consumer

**Before (engine in the caller's process — what I1 forbids):**

```python
from quarry import Database, ingest_url, ChunkSearch  # loads lancedb + onnxruntime

db = Database.connect(lancedb_path)          # ~1.6 GB engine in THIS process
ingest_url("https://example.com/doc", db, settings)
hits = ChunkSearch(db).search(query_vector, limit=10)
```

**After (thin client — the engine lives only in the daemon):**

```python
import quarry
from quarry.api import IngestRequest, SearchRequest

client = quarry.TargetResolver.connect()     # resolves the local daemon; no engine
client.ingest_url(IngestRequest(url="https://example.com/doc"))
resp = client.search(SearchRequest(query="what did we decide about X", limit=10))
for hit in resp.hits:
    print(hit.document, hit.score)
```

`TargetResolver.connect()` (`client/resolver.py:43-51`) is the zero-argument
factory: it resolves the target in precedence order (env → stored login →
loopback daemon) and returns a connected `QuarryClient`, failing closed with a
typed `QuarryConnectionError` (autostart nudge) if the daemon is down — never a
silent in-process engine fallback. For explicit control a consumer builds
`QuarryClient.connect(ClientConfig(...))` directly, or injects an
`ASGITransport`-backed transport in tests (`client/client.py:71-88`).

---

## 5. The lazy-loader decision — keep PEP 562, retarget it

**Decision: KEEP the PEP 562 `__getattr__` lazy loader; retarget `_LAZY_ATTRS`
from the engine modules to the client/api modules. A plain eager import is
rejected — it regresses the `quarry-hook` fast path ~6x.**

### 5.1 Why the loader existed, and why the reason changed

The current loader (`__init__.py:58-72`) defers the heavy **engine** import so the
lightweight `quarry-hook` entry point stays stdlib-cheap. That specific rationale
evaporates in PR-5: there is no engine to defer. But a *new* reason takes its
place — `QuarryClient` and the api models are not free either. They pull in
`pydantic` + `httpx` (and, via `httpx._main`, `rich.console`).

### 5.2 Measured cost (fresh interpreter, warm cache, min of 3 runs)

| Import | Cost | Notes |
|--------|------|-------|
| `import quarry` (lazy, today) | **51 ms** | metadata + stdlib only |
| `import quarry.client, quarry.api` (eager equivalent) | **310 ms** | pydantic + httpx + rich |
| `pydantic` + `httpx` alone | 173 ms | the bulk of the delta |

`quarry-hook = "quarry._hook_entry:main"` — importing that entry point runs
`quarry/__init__.py` **on every hook invocation**. The hook fast path
(`_hook_entry.py` → `_stdlib.py`, `_frontmatter.py`, `background_ingest.py`) is
verified stdlib-only (no pydantic, no httpx). `_hook_entry.py:11` documents its
budget as ~0.1 s. An **eager** `from quarry.client import QuarryClient` in
`__init__.py` would add **~260 ms** to every `import quarry`, turning the 51 ms
hook path into ~310 ms — a ~6x regression on the exact budget the loader was built
to protect.

Verified today: after `import quarry`, neither `quarry.client` nor `pydantic` is
in `sys.modules`. Retargeting the loader preserves that: `import quarry` stays
51 ms; the ~260 ms client import is paid **only on first attribute access**
(`quarry.QuarryClient`, `quarry.TargetResolver`, …).

### 5.3 The retargeted `_LAZY_ATTRS`

```python
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "QuarryClient": ("quarry.client", "QuarryClient"),
    "TargetResolver": ("quarry.client", "TargetResolver"),
    "ClientConfig": ("quarry.client", "ClientConfig"),
    "QuarryError": ("quarry.client", "QuarryError"),
    "QuarryConnectionError": ("quarry.client", "QuarryConnectionError"),
    "HttpError": ("quarry.client", "HttpError"),
    "TaskOutcome": ("quarry.client", "TaskOutcome"),
}
```

The `__getattr__` body (`__init__.py:58-72`) is unchanged — only the table and the
`TYPE_CHECKING` block (`__init__.py:19-29`) change. The module docstring's
rationale is rewritten: the loader now defers **pydantic + httpx** to keep the
`quarry-hook` path stdlib-cheap, not to defer the engine.

---

## 6. The engine-sabotage test

Extend the DES-031 §3.1 #3 sabotage mechanism (already at
`tests/test_api.py:99-124`) to the **top-level package**. The test runs a fresh
interpreter so the main test process — which has the engine loaded — cannot mask a
hidden import.

**Assertion 1 — `import quarry` is engine-free.** After `import quarry`, none of
`lancedb`, `onnxruntime`, `pyarrow`, `quarry.db`, `quarry.ingestion`,
`quarry.retrieval`, `quarry.sync` is in `sys.modules`.

**Assertion 2 — accessing the public names stays engine-free.** After touching
every `__all__` client name (`quarry.QuarryClient`, `quarry.TargetResolver`,
`quarry.QuarryError`, …), the same engine set is still absent. This proves the
retargeted `_LAZY_ATTRS` points at the client, not the engine — a future
mis-retarget back to `quarry.db` fails here.

**Assertion 3 — the engine names are gone.** `quarry.Database`,
`quarry.get_db`, `quarry.ingest_content`, `quarry.ingest_document`,
`quarry.ingest_url`, `quarry.ChunkSearch` each raise `AttributeError`. This is the
PL-PP-1 proof: the names are removed, not aliased.

**Assertion 4 (perf contract, recommended) — the loader stays lazy.** After a bare
`import quarry`, `pydantic` and `quarry.client` are **absent** from `sys.modules`;
they appear only after the first client-name access. This codifies §5's
hook-budget protection as an executable contract, so a future eager
`from quarry.client import …` in `__init__.py` fails the suite rather than
silently regressing the hook path. (The engine-sabotage assertions 1–3 pass
whether the client is eager or lazy — the client is engine-free either way — so
this fourth assertion is the only guard on the laziness itself.)

Sketch (one fresh-interpreter subprocess, mirroring `test_api.py:109-124`):

```python
code = (
    "import sys, quarry;"
    "engine = ('lancedb','onnxruntime','pyarrow','quarry.db','quarry.ingestion',"
    "          'quarry.retrieval','quarry.sync');"
    "assert not [m for m in engine if m in sys.modules], 'engine at import';"
    "assert 'pydantic' not in sys.modules and 'quarry.client' not in sys.modules;"
    "_ = quarry.QuarryClient, quarry.TargetResolver, quarry.QuarryError;"
    "assert not [m for m in engine if m in sys.modules], 'engine after access';"
    "import pytest;"
    "  # each removed engine name now raises AttributeError\n"
    "[__import__('quarry').__getattr__ or None];"
    "print('ok')"
)
```

(The removed-name `AttributeError` checks are cleaner as in-process
`pytest.raises(AttributeError)` cases in the same test module; the subprocess
covers the `sys.modules` assertions that need a clean interpreter.)

---

## 7. Alternatives considered and rejected

### 7.1 Deprecation shim / aliased engine names — REJECTED

Keep `Database = _deprecated_alias("quarry.db.facade", "Database")` or a
`DeprecationWarning`-emitting `__getattr__` branch for one release.

**Rejected.** PL-PP-1 forbids backwards-compatibility shims, and the premise for a
shim — an external consumer that would break — is **absent**. DES-031 §6
(`des-031v2-daemon-first.md:725,736-744`) establishes on operator authority that
quarry-menubar is a Swift HTTP client with zero Python-engine coupling, so there
is no Python importer of `quarry.Database` anywhere outside this repo, and §3.1
confirms there is none inside it either. A shim would carry dead code, keep the
engine reachable from a "client" import (re-opening the I1 hole), and defer a
removal that costs nothing to do now. Delete the names outright.

### 7.2 Eager import — plain `from quarry.client import QuarryClient` — REJECTED

Drop the lazy loader entirely now that the exports are light.

**Rejected on measured data (§5).** "Light" is relative: pydantic + httpx add
~260 ms, and `import quarry` runs on every `quarry-hook` invocation. Eager import
regresses the documented ~0.1 s hook budget ~6x. The loader is cheap to keep and
its mechanism is unchanged; only its target table moves.

### 7.3 Re-export all 30 `quarry.api` models at the top level — DEFERRED to operator

Make `from quarry import SearchRequest` work directly.

**Not recommended, but a live decision (D2, §9).** It flattens discovery but
pushes the package interface past PL-MD-3's >30-name flag and duplicates
`quarry.api`'s `__all__`. `from quarry.api import SearchRequest` is the
PY-IC-9-aligned path (types in their dedicated module). Recommend keeping the
top-level surface at the eight client names.

### 7.4 Keep `Settings` / `CollectionName` in the top-level `__all__` — REJECTED (D1)

They are already-thin (non-engine), so they *could* stay.

**Rejected.** Neither is client surface, and §3.1 shows zero top-level importers of
either. Keeping unused re-exports widens the interface (PL-MD-3) for no consumer.
`quarry.config.Settings` and `quarry.collections.CollectionName` remain importable
from their own modules for any engine-side or host-config caller. Flagged as D1
(§9) because it is a scope choice beyond the literal "delete the engine names."

---

## 8. Proposed write-set

The design phase owns the write-set; the implementer confirms or refines it.

| File | Change |
|------|--------|
| `src/quarry/__init__.py` | Delete the six engine `_LAZY_ATTRS` entries + `Settings`/`CollectionName`; add the seven client entries; rewrite `__all__` (§4.2); rewrite the `TYPE_CHECKING` block to import the client names; rewrite the module docstring (usage example → thin client; loader rationale → defer pydantic/httpx for the hook path). |
| `tests/test_init.py` *(new)* | The §6 engine-sabotage + removed-name + laziness test for the top-level package. (No `tests/test_init.py` exists today; PL-PL-3 wants a test file mirroring the module.) |
| `CHANGELOG.md` | Under `## [Unreleased] / Changed` (or `Removed`): "`import quarry` now exposes `QuarryClient` + `TargetResolver` (thin client); the engine exports `Database`, `get_db`, `ingest_content/document/url`, `ChunkSearch` are removed — import from `quarry.db` / `quarry.ingestion.pipeline` for engine-side use." |
| `README.md` | If any "library API" snippet shows `from quarry import Database/ingest_*`, update it to the §4.3 thin-client form. (Grep before editing; may be none.) |

**No rewire edits** — §3.3: nothing imports the deleted names.

**OO ratchet (debt amortization).** `__init__.py` is small and already compliant;
the honest improvement riding this PR is the **new test file** raising coverage on
the package surface, plus tightening the interface (8 narrow names vs 9 mixed).
The implementer runs `make check` (three merge-base ratchets) and, if
`__init__.py` shows no improvable metric, pays down a nearby offender per the
CLAUDE.md "real good deed" rule rather than gaming a number. `make update-oo`
records the touched-file baselines.

---

## 9. Decisions the operator must rule on

- **D1 — Drop `Settings` and `CollectionName` from the top-level `__all__`?**
  Recommend **yes** (§4.2, §7.4): neither is client surface, both stay importable
  from their own modules, zero top-level importers exist. Ruling "no" keeps them
  as thin re-exports at the cost of a wider interface.

- **D2 — Re-export the `quarry.api` models at the top level, or reach them via
  `quarry.api`?** Recommend **via `quarry.api`** (§4.1, §7.3): narrow top-level
  surface, PY-IC-9-aligned, no `__all__` duplication. Ruling "flat re-export"
  makes `from quarry import SearchRequest` work but pushes past PL-MD-3's flag.

Both are additive to the core mandate (delete the six engine names, add
`QuarryClient`), which stands regardless of the rulings.

---

## 10. Sequencing

- **Depends on:** PR-3 (`QuarryClient` + `TargetResolver`), shipped on main
  (`ed810f7`, #366). All re-export targets already exist and are engine-free
  (verified: `import quarry.client, quarry.api` loads no lancedb/onnxruntime/
  pyarrow).
- **Independent of:** PR-4 (MCP). No shared files; the library and MCP surfaces do
  not interact.
- **Prerequisite for:** PR-6, which locks the client/engine boundary across all
  surfaces (import-linter layers contract + the engine-sabotage suite). The
  library is one of the surfaces PR-6 fences; its top-level package must already be
  engine-free for PR-6's contract to pass. Land PR-5 **before** PR-6.

Order: **PR-3 (done) → PR-5 → PR-6.**
