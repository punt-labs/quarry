# Quarry OO Refactoring — Session Resume

Read this file before starting any refactoring work.

## Current state (2026-05-14)

44 of 89 steps shipped to main. Phases 0–3 complete. Phase 4 in
progress (branch `oo/phase-4-services` with tooling commits only —
all extraction work was lost to a working-tree conflict).

### What is on main

| Phase | PRs | Steps | What shipped |
|-------|-----|-------|-------------|
| 0 | #280–#283 | 0.1–0.10 | Baselines, `__init__`→`__new__`, slots, function absorptions |
| 1 | #284 | 1.1–1.10 | `_sql.py`, SearchFilter, ChunkConfig, CollectionName Flyweight, 4 config dataclasses, FormatExtractor + ServiceBackend protocols |
| 2 | #285–#286 | 2.1–2.8 | `database.py` decomposed into `db/` package (7 modules, Database facade) |
| 3 | #288 | 3.1–3.16 | 7 extractors in `extractors/`, 10 modules in `ingestion/` package |
| docs | #279, #287 | — | OO design docs, package structure proposal (reviewed, GO) |

### What is on branch `oo/phase-4-services`

- `tools/suppression_ratchet.py` — committed (b640f3f)
- `.suppression-baseline.json` — committed (267 suppressions)
- Makefile targets: `check-suppressions`, `update-suppressions`
- `docs/oo-refactor/phase4-handoff.md` — this handoff
- NO extraction code — all agent work was lost (see below)

### Packages that exist

```text
src/quarry/
    db/              # 7 modules — SchemaManager, ChunkStore, ChunkSearch,
                     #   ChunkCatalog, TableOptimizer, storage utils, Database facade
    extractors/      # 8 modules — FormatExtractor protocol + 7 extractor classes
    ingestion/       # 10 modules — pipeline, url_ingester, url_fetcher,
                     #   image_preparer, text_splitter, chunker, backends, etc.
```

### Dead code that must be wired

These classes exist but no callers use them. This is the highest
priority — creating classes without wiring callers is not refactoring.

| Class | Location | What it replaces |
|-------|----------|-----------------|
| `Database` facade | `db/facade.py` | `get_db()` + manual ChunkStore/ChunkSearch/ChunkCatalog construction |
| `UrlIngester` | `ingestion/url_ingester.py` | `ingest_url()`, `ingest_sitemap()`, `ingest_auto()` in pipeline.py |
| `UrlFetcher` | `ingestion/url_fetcher.py` | `_fetch_url()` in pipeline.py |
| `ImagePreparer` | `ingestion/image_preparer.py` | `_prepare_image_bytes()` in pipeline.py |

Wire these BEFORE continuing with new extractions. Every caller of
the old functions must be updated to use the new classes.

## What to do next

### Step 1: Wire dead code (priority)

Wire the 4 dead classes listed above. Every call to `get_db()` becomes
`Database.connect()`. Every call to `ingest_url()` goes through
`UrlIngester`. Run `make check`. Ship as a PR.

### Step 2: Phase 4 — Services (Steps 4.1–4.17)

All extraction work was lost because parallel agents on the same
working tree deleted each other's untracked files. The PostToolUse
`make check` hook then reverted everything.

Backups exist in `.tmp/phase4-extractions/` for Steps 4.4–4.7
(HealthChecker, InstallWizard, EthosConfigurator, claudemd). These
were tested and passing before the wipe. They may be usable as a
starting point — verify against current main before applying.

Steps 4.1–4.3a (CollectionSyncer, FileDiscovery, SyncRegistry,
`sync/` package) and Steps 4.13–4.16 (ProjectManager,
SessionBackfiller, TextScrubber, TableRenderer) were completed by
agents but all changes were lost. No backups.

Steps 4.8–4.12 (ServiceManager, LaunchdBackend, SystemdBackend,
ProxyConfig, ConnectionValidator, CertificateAuthority, ProxyInstaller)
were never completed.

Step 4.17 (move modules into `sync/` and `services/` packages) was
never attempted.

### Step 3: Phases 5–7

| Phase | Steps | What |
|-------|-------|------|
| 5 | 5.1–5.7 | hooks/ package — SessionStartHandler, WebFetchHandler, PreCompactHandler, BackgroundIngester, transcript.py to top level |
| 6 | 6.1–6.9 | routes/ package — TaskManager, QuarryContext, 9 route modules, McpContext, McpSession |
| 7 | 7.1–7.20 | commands/ package — CliContext, RemoteClient, 16 command modules, PluginSetup, surfaces/ package |

## Critical rules

1. **Wire callers in the same PR as the extraction.** Do not create
   classes that nobody calls. That is code duplication, not refactoring.

2. **Use `isolation: "worktree"` on Agent calls** when running agents
   in parallel. Without it, agents delete each other's untracked files.

3. **Do not confirm "done" until `git log` proves the file is in a
   commit.** Files on disk get deleted by concurrent agents. Only
   committed files are durable.

4. **Do not `rm` files you did not create.** Move to `.tmp/` first.
   Verify the system works without the file. Then delete in a later
   commit.

5. **Reply to every biff message** that requests action or acknowledgment.

6. **Run local review** (code-reviewer + silent-failure-hunter) on every
   PR diff before pushing. This is Phase 5 of the workflow and is
   mandatory.

7. **`make check` must pass** before every commit. This now includes
   `check-suppressions` (suppression ratchet) in addition to lint, types,
   tests, and check-oo.

## Tools

| Tool | Command | In check chain |
|------|---------|---------------|
| OO ratchet | `make check-oo` | yes |
| OO rebaseline | `uv run python tools/oo_score.py src/quarry/ --rebaseline` | — |
| Coupling/cohesion | `make check-coupling` | no (informational) |
| Suppression ratchet | `make check-suppressions` | yes |
| ABC complexity | `make metrics` | no |
| Coverage | `make coverage` | no |

## Key documents

| File | What |
|------|------|
| `docs/oo-refactor/oo-refactoring-plan.md` | The 84-step plan (original) |
| `docs/oo-refactor/package-structure.md` | 10-package architecture (reviewed, GO) |
| `docs/oo-refactor/package-structure-review.md` | Peer review — GO WITH MODIFICATIONS |
| `docs/oo-refactor/oo-design-report.md` | Class proposals (reference) |
| `.oo-baseline.json` | Current OO scores baseline |
| `.suppression-baseline.json` | Current suppression count baseline (267) |
| `CLAUDE.md` | Project standards |

## How to start

```bash
# 1. Check out the phase 4 branch (has tooling commits)
git checkout oo/phase-4-services
git pull

# 2. Verify current state
make check

# 3. Wire dead code first (Database facade, UrlIngester, etc.)
# Then resume Phase 4 extractions from Step 4.1
```
