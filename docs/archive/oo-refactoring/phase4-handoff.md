# Phase 4 Handoff

Session ran into a fundamental problem: parallel agents on the same
working tree delete each other's untracked files. The PostToolUse
`make check` hook then reverts all changes when the partial state
fails checks. Net result: all Phase 4 work lost except backups.

## What was completed this session

- Phases 0-3 shipped (PRs #280-#288). 44 of 89 steps on main.
- Package structure proposal designed, reviewed (GO), updated.
- `db/` package: 7 modules, Database facade (exists but unwired).
- `extractors/` package: 7 extractor classes.
- `ingestion/` package: 10 modules.
- Tools: oo_coupling.py, suppression_ratchet.py (needs re-copy).
- Makefile targets for coupling and suppressions.

## Phase 4 status

All agent work lost due to working tree conflicts. Backups in
`.tmp/phase4-extractions/` for Steps 4.4-4.7 (doctor decomposition).

Steps completed by agents but lost:

- 4.1-4.3a: CollectionSyncer, FileDiscovery, SyncRegistry, sync/ pkg
- 4.4-4.7: HealthChecker, InstallWizard, EthosConfigurator, claudemd
- 4.13-4.16: ProjectManager, SessionBackfiller, TextScrubber, TableRenderer
- 4.8-4.12: NOT completed (ServiceManager, backends, TLS, proxy)

Steps not attempted:

- 4.17: Move modules into services/ package

## Dead code to wire (from prior phases)

- `Database` facade: nobody uses it; all callers still use `get_db()`
- `UrlIngester`: pipeline.py still has module-level `ingest_url()`
- `UrlFetcher`: only referenced by dead UrlIngester
- `ImagePreparer`: pipeline.py still calls `_prepare_image_bytes()`

## Required approach for next session

1. Use `isolation: "worktree"` on ALL Agent calls
2. OR run agents sequentially (slower but safe)
3. Copy and commit tools/suppression_ratchet.py FIRST (before agents)
4. Wire dead code as part of each extraction, not separately
5. Reply to every biff message
