# Quarry OO Refactoring — Resume

## Start here

```bash
git checkout oo/phase-4-services
git pull
make check
```

`make check` must pass before any work begins. Current status: **clean**.

---

## What is done (on main, PRs #280–#288)

| Phase | Steps | What shipped |
|-------|-------|-------------|
| 0 | 0.1–0.10 | Baselines, `__init__`→`__new__`, frozen slots, function absorptions |
| 1 | 1.1–1.10 | `_sql.py`, SearchFilter, ChunkConfig, CollectionName Flyweight, 4 config dataclasses, FormatExtractor + ServiceBackend protocols |
| 2 | 2.1–2.8 | `database.py` eliminated → `db/` package (SchemaManager, ChunkStore, ChunkSearch, ChunkCatalog, TableOptimizer, storage utils, Database facade) |
| 3 | 3.1–3.16 | 7 extractor classes in `extractors/`, 10 modules in `ingestion/` package |

### What is on branch `oo/phase-4-services` (committed, not yet PR'd)

- **Step 4.1–4.2**: `CollectionSyncer` in `sync.py`, `FileDiscovery` in `sync_discovery.py`
- **Step 4.3a**: `SyncRegistry` class in `sync_registry.py` (with backward-compat shims — module-level functions still exist and delegate to the class)
- **Database facade wired**: `Database.connect()` replaces `get_db()` + manual class construction across most callers
- **Tooling**: `tools/suppression_ratchet.py` (267 suppression ceiling), `tools/oo_coupling.py`
- **Makefile targets**: `make install`, `make check-suppressions`, `make update-suppressions`, `make check-coupling`, `make update-coupling`
- **CLAUDE.md**: development loop documented (inner loop per mission, outer loop per PR)

---

## What is left

### Immediate next (Phase 4 continuation)

**Step 4.3b–4.3c: Wire callers away from SyncRegistry shims**
The shims exist to keep `make check` passing. Migration is file-by-file:
- 4.3b: Migrate `sync.py` callers to `SyncRegistry` directly, delete shims for those functions
- 4.3c: Migrate remaining callers (`hooks.py`, `enable.py`, `doctor.py`, `__main__.py`, `mcp_server.py`, `http_server.py`), delete all shims

After all shims removed, `sync_registry.py` contains only `SyncRegistry` + the two dataclasses. One PR for 4.3a–4.3c together.

**Steps 4.4–4.7: Doctor decomposition**
Extract from `doctor.py` (currently 1,141 lines):
- 4.4: `HealthChecker` → `src/quarry/health_checker.py`
- 4.5: `InstallWizard` → `src/quarry/install.py`
- 4.6: `EthosConfigurator` → `src/quarry/ethos_config.py` (enable.py imports from here, NOT from doctor.py)
- 4.7: `claudemd.py` — `inject_claude_md` function

**Steps 4.8–4.12: Service infrastructure**
- 4.8: `ServiceManager` + `LaunchdBackend` in `service.py`
- 4.9: `SystemdBackend` in `service.py`
- 4.10: `ProxyConfig` in `remote.py`
- 4.10a: `ConnectionValidator` in `remote.py`
- 4.11: `CertificateAuthority` in `tls.py`
- 4.12: `ProxyInstaller` in `proxy.py`

**Steps 4.13–4.16: Remaining service classes**
- 4.13: `ProjectManager` in `enable.py`
- 4.14: `SessionBackfiller` in `backfill.py`
- 4.15: `TextScrubber` in `scrub.py`
- 4.16: `TableRenderer` in `formatting.py`

**Steps 4.3a + 4.17: Package moves**

- 4.3a (done): `sync/` package — move `sync.py`, `sync_discovery.py`, `sync_registry.py`
- 4.17: `services/` package — move all remaining service modules

### Dead code to wire (from Phase 3, planned future missions)

| Class | Location | Replaces |
|-------|----------|---------|
| `FileDiscovery` | `sync_discovery.py` | `discover_files()`, `_content_hash()`, etc. in `sync.py` |
| `UrlIngester` | `ingestion/url_ingester.py` | `ingest_url()`, `ingest_sitemap()`, `ingest_auto()` in pipeline.py |
| `UrlFetcher` | `ingestion/url_fetcher.py` | `_fetch_url()` in pipeline.py |
| `ImagePreparer` | `ingestion/image_preparer.py` | `_prepare_image_bytes()` in pipeline.py |

These are wired when their callers are refactored (step 3.13 IngestionPipeline class, step 3.12 UrlIngester — both still in the plan).

### Remaining phases (5–7)

| Phase | Steps | What |
|-------|-------|------|
| 5 | 5.1–5.7 | `hooks/` package — SessionStartHandler, WebFetchHandler, PreCompactHandler, BackgroundIngester, transcript.py to top level |
| 6 | 6.1–6.9 | `routes/` package — TaskManager, QuarryContext, 9 route modules, McpContext, McpSession |
| 7 | 7.1–7.20 | `commands/` package — CliContext, RemoteClient, 16 command modules, PluginSetup, `surfaces/` package |

---

## Development workflow (mandatory)

Read `CLAUDE.md` → `## Development Loop` section for the authoritative loop.

**Inner loop (every mission):**

1. Delegate to the right ethos specialist — see pairing table in `CLAUDE.md`
2. `make check`
3. `make install` (builds wheel, installs locally)
4. Both review agents: `/feature-dev:code-reviewer` + `/pr-review-toolkit:silent-failure-hunter`
5. Fix every finding, re-run until clean
6. Exercise manually
7. Commit

**Outer loop (per PR):**

1. `make check` on full branch diff
2. Both review agents on full diff
3. Human IDE review
4. `make install` + end-to-end exercise
5. Open PR only when all the above are clean

**PR boundary**: one rollback-coherent unit — not one step, not one file. Phases 4.1–4.3 + tooling is one PR.

---

## Key documents

| Document | Location | What it contains |
|----------|----------|-----------------|
| 84-step refactoring plan | `docs/oo-refactor/oo-refactoring-plan.md` | All steps, dependencies, invariants |
| Package structure proposal | `docs/oo-refactor/package-structure.md` | 10-package architecture, coupling/cohesion analysis, reviewed GO |
| Package structure peer review | `docs/oo-refactor/package-structure-review.md` | GO WITH MODIFICATIONS — 3 required changes (all incorporated) |
| OO design report | `docs/oo-refactor/oo-design-report.md` | Target class structure for all 42 modules |
| OO baseline | `.oo-baseline.json` | Current OO scores per file |
| Suppression baseline | `.suppression-baseline.json` | 267 suppression ceiling |

## Tools

| Tool | Command | Purpose |
|------|---------|---------|
| `make check` | — | All gates: lint, type, test, OO ratchet, suppression ratchet |
| `make install` | — | Build wheel + install locally |
| `make check-oo` | — | OO metric ratchet |
| `uv run python tools/oo_score.py src/quarry/ --rebaseline` | — | Reset OO baseline (use after structural conversions) |
| `make check-suppressions` | — | Suppression count ratchet (267 ceiling) |
| `make check-coupling` | — | Coupling/cohesion (informational) |

## Lessons learned (session-specific, do not repeat)

1. `isolation: "worktree"` is required for parallel Agent calls — without it agents delete each other's untracked files
2. Confirm "done" only after `git log` shows the commit — files on disk disappear between Bash calls
3. `mv` to `.tmp/` before removing any file you didn't create — never `rm` on unfamiliar files
4. Reply to every biff message that requests action
5. Running agents without the right ethos specialist produces generic code — use `rmh`, `djb`, `mdm`, `adb` per domain
6. The inner loop (review agents after every mission) is mandatory — not optional before-PR step
