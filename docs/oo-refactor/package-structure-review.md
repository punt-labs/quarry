# Package Structure Review

Reviewer: rej (Ralph Johnson)
Date: 2026-05-14. **All required modifications incorporated into `package-structure.md` on 2026-05-14.**

**Verdict: GO WITH MODIFICATIONS**

The proposal is sound. The layering is correct, the coupling analysis is honest,
and the dependency direction is acyclic. The modifications below were required
before this blueprint guides execution — all have been incorporated.

---

## Severity 1: Must fix before execution

### S1.1: `services/` is a grab bag despite the denial

The proposal acknowledges medium cohesion, then argues dependency direction
is the cohesion criterion. That is not cohesion -- that is layering. Cohesion
means the modules change together for the same reason. `tls.py` and `scrub.py`
do not change for the same reason. `sync.py` and `proxy.py` do not change
for the same reason.

15 modules with 4 sub-domains is 4 packages waiting to happen. The proposal
rejects splitting because "2-3 modules each adds overhead." That argument
weighs overhead against nothing -- the cost of the grab bag is invisible
until someone asks "where does deployment logic live?" and the answer is
"services/, along with text scrubbing and health checks."

**Required change:** Split into at least two packages. The natural seam is
`sync/` (sync.py, sync_discovery.py, sync_registry.py -- 3 modules, tightly
coupled, change together) and `services/` (the remaining 12). The 6-module
trigger the proposal sets for splitting is already met if you count sync_*
as a sub-domain. A `deploy/` package (service.py, tls.py, remote.py, proxy.py)
is a strong candidate for a third split if the team is willing.

### S1.2: `enable.py` -> `doctor.py` cycle inside `services/`

`enable.py` imports `_write_ethos_ext_session_context` from `doctor.py`.
`doctor.py` imports from `service.py` and `proxy.py`. After the extractions,
`EthosConfigurator` (from doctor.py) is consumed by `ProjectManager` (from
enable.py), while `HealthChecker` (from doctor.py) consumes `ServiceManager`
and `ProxyInstaller`. This is an intra-package dependency diamond that the
proposal does not mention.

**Required change:** Document the `enable -> doctor` dependency. The resolution
is the same as the `backfill -> hooks` cycle: extract
`_write_ethos_ext_session_context` to the types layer (or to `ethos_config.py`
directly, since that is where it belongs post-extraction -- step 4.6 already
creates `EthosConfigurator`). Verify that after step 4.6, `enable.py` imports
from `ethos_config.py`, not from `doctor.py`.

### S1.3: `http_server.py` imports `mcp_server.py` -- layer 5 intra-dependency

The proposal places both in `surfaces/` at layer 5. But `http_server.py` line
1287 imports `run_mcp_session` from `mcp_server.py` (for the WebSocket MCP
endpoint). After extraction, `routes/mcp_ws.py` will depend on
`surfaces/mcp_server.py`. The dependency table in section 3 says routes/
imports from surfaces/ (2 imports), but does not call out this specific MCP
dependency.

**Required change:** Document this explicitly. The import is legitimate
(the HTTP server hosts the MCP WebSocket endpoint), but it means
`routes/mcp_ws.py` depends on `surfaces/mcp_server.py` specifically.
If `surfaces/` is ever split, this dependency constrains the split.

---

## Severity 2: Should fix -- design improvement

### S2.1: The 7-layer model is one layer too many for 15K LOC

Layers 4 (hooks/) and 5 (routes/, commands/, surfaces/) could collapse into
one presentation layer without losing meaningful separation. hooks/ has 6
modules and ~400 LOC. It exists as a separate layer solely because it sits
between services/ and the entry points. But hooks/ consumers are just
`_hook_entry.py` -- a single dispatch file. The layering distinction between
"event handler" and "route handler" is real in a 100K-line system; in a
15K-line system it adds a layer boundary that no one will ever accidentally
violate.

**Recommendation:** Collapse hooks/ to layer 4 alongside routes/, commands/,
and surfaces/. This gives 6 layers, which is plenty for a codebase this size.
The acyclicity is preserved because hooks/ does not import from its siblings.

### S2.2: `extractors/` and `ingestion/` separation is correct but fragile

The proposal says extractors are "stateless format converters" and ingestion
"orchestrates I/O." This is the right split. But `ingestion/pipeline.py` must
maintain a registry of all 7 extractors, and `hooks/web_fetch.py` imports
`HtmlExtractor` directly (not through the protocol). These concrete
dependencies mean the packages are not as decoupled as the layer diagram
suggests.

**Recommendation:** Add a sentence to the coupling analysis acknowledging
that `ingestion/ -> extractors/` coupling is 7 concrete imports plus the
protocol, not just "7 extractors via the protocol." The protocol enables
dispatch; the concrete imports are for registration. Both are legitimate,
but the analysis should be precise.

### S2.3: `commands/` at 17 modules is the second-largest package

17 command modules is a flat directory of 17 files. That is navigable today,
but the proposal's own 6-module trigger for splitting applies here. The natural
groupings: data commands (find, show, remember, delete), admin commands
(install, doctor, serve, version, uninstall), registration commands (register,
sync, enable, optimize), and remote commands (login, logout, remote_list).

**Recommendation:** Not blocking, but set the same 6-module splitting trigger
that the proposal sets for services/. If commands/ grows past 20 modules,
split into sub-packages.

---

## Severity 3: Observations

### S3.1: Import path breakage is manageable

`from quarry.database import X` is already gone -- the codebase uses
`from quarry.db import X`. The `db/` and `extractors/` packages exist.
The remaining package moves (ingestion/, services/, hooks/, routes/,
commands/, surfaces/) will each require bulk import updates, but the
1-PR-per-package-move strategy is realistic. The highest-risk move is
`hooks.py` -> `hooks/` because it is a module-to-package conversion
with the same import path.

### S3.2: OO principles are honestly applied

The DIP claim is real: `FormatExtractor` and `ServiceBackend` are genuine
protocols with concrete implementations injected at runtime. The OCP claim
is real: adding a new extractor requires one file plus one registration.
The ADP claim is demonstrated by the layer numbering. The SRP claims are
reasonable at the package level, with the exception of services/ (S1.1).

### S3.3: transcript.py cycle resolution is correct

Moving `extract_transcript_text` to the types layer eliminates the
`services/backfill.py` -> `hooks/transcript.py` cycle. The functions are
pure transforms with zero quarry dependencies. Placing them at layer 0
is the right call.

### S3.4: Missing from the proposal

- **Test file reorganization.** The proposal describes production code layout
  but says nothing about whether `tests/` mirrors the new package structure.
  88 steps will move mock targets. The test directory layout should be stated.
- **`__init__.py` re-export policy.** The proposal says each package has
  `__init__.py` with re-exports, but does not state whether consumers should
  import from `quarry.db` (the package) or `quarry.db.chunk_store` (the
  module). Pick one convention and document it.
- **`database.py` is already deleted.** The proposal references it as if it
  exists (section 2, "Step 2.7: Delete database.py"). The db/ package already
  contains 8 modules. Phase 2 of the refactoring plan may need rebasing.

---

## Summary of required changes

1. Split `services/` into at least `sync/` + `services/` (S1.1)
2. Document and resolve `enable.py` -> `doctor.py` dependency (S1.2)
3. Document `routes/mcp_ws.py` -> `surfaces/mcp_server.py` dependency (S1.3)
4. State the test directory layout convention (S3.4)
5. State the import convention: package `__init__` or direct module (S3.4)
6. Rebase Phase 2 against the db/ package that already exists (S3.4)
