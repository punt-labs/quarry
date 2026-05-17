# Quarry OO Refactoring — Session Resume

Read this file before starting any refactoring work.

## Current state (2026-05-14)

### Branch

`oo/phase-4-services` — has Phase 4 partial work. DO NOT start from
main. Check out this branch and verify before continuing.

```bash
git checkout oo/phase-4-services
git pull
make check
```

`make check` must pass: lint, mypy, pyright, 1683 tests, OO ratchet,
suppression ratchet.

## What is on main (Phases 0–3)

| Phase | PRs | Steps | What shipped |
|-------|-----|-------|-------------|
| 0 | #280–#283 | 0.1–0.10 | Baselines, `__init__`→`__new__`, slots, function absorptions |
| 1 | #284 | 1.1–1.10 | `_sql.py`, SearchFilter, ChunkConfig, CollectionName, dataclasses, protocols |
| 2 | #285–#286 | 2.1–2.8 | `database.py` → `db/` package (7 modules, Database facade) |
| 3 | #288 | 3.1–3.16 | 7 extractors in `extractors/`, 10 modules in `ingestion/` |

## What is on branch `oo/phase-4-services`

Beyond main, this branch has (all committed, pushed):

- **Steps 4.1–4.2**: CollectionSyncer + FileDiscovery in `sync.py` and `sync_discovery.py`
- **Database facade wired**: `Database.connect()` replaces `get_db()` everywhere
- **Suppression ratchet**: `tools/suppression_ratchet.py` (267 ceiling), Makefile targets
- **Fixes**: pyright errors resolved, `load_ignore_spec` made public

## What is left in Phase 4

### Priority 1: Step 4.3 — SyncRegistry

Extract `SyncRegistry` class in `src/quarry/sync_registry.py`. Wrap
all 12 module-level functions. Wire every caller (12 callers across 7 files).
Run `grep -rn "from quarry.sync_registry import" src/ tests/`.

### Priority 2: Steps 4.4–4.7 — Doctor decomposition

Backups in `.tmp/phase4-extractions/`. Verify against current `doctor.py` first.

- 4.4: `HealthChecker` → `src/quarry/health_checker.py`
- 4.5: `InstallWizard` → `src/quarry/install.py`
- 4.6: `EthosConfigurator` → `src/quarry/ethos_config.py`
  (enable.py must import from ethos_config, not doctor)
- 4.7: `claudemd.py` — `inject_claude_md` function + constants

### Priority 3: Steps 4.8–4.16 — Remaining service classes

- 4.8: ServiceManager + LaunchdBackend (service.py)
- 4.9: SystemdBackend (service.py)
- 4.10: ProxyConfig (remote.py)
- 4.10a: ConnectionValidator (remote.py)
- 4.11: CertificateAuthority (tls.py)
- 4.12: ProxyInstaller (proxy.py)
- 4.13: ProjectManager (enable.py)
- 4.14: SessionBackfiller (backfill.py)
- 4.15: TextScrubber (scrub.py)
- 4.16: TableRenderer (formatting.py)

### Priority 4: Package moves (4.3a + 4.17)

- 4.3a: Create `sync/` package
- 4.17: Create `services/` package

## Dead code (Phase 3 leftovers)

`UrlIngester`, `UrlFetcher`, `ImagePreparer` exist but have no callers.
`pipeline.py` still uses module-level functions. Wire when extracting
`IngestionPipeline` class (step 3.13, not yet done).

## Remaining phases (5–7)

| Phase | Steps | What |
|-------|-------|------|
| 5 | 5.1–5.7 | `hooks/` package |
| 6 | 6.1–6.9 | `routes/` package |
| 7 | 7.1–7.20 | `commands/` + `surfaces/` packages |

## Critical rules

1. **Wire callers in the same PR** — no dead code.
2. **Use `isolation: "worktree"`** on parallel Agent calls.
3. **Commit immediately** after agent finishes — untracked files disappear.
4. **Confirm "done" only after `git log` shows the commit.**
5. **`mv` to `.tmp/` instead of `rm`** for unknown files.
6. **Reply to every biff message** that requests action.
7. **Local review** (code-reviewer + silent-failure-hunter) before every PR.

## Tools

| Tool | Command | In check chain |
|------|---------|---------------|
| OO ratchet | `make check-oo` | yes |
| OO rebaseline | `uv run python tools/oo_score.py src/quarry/ --rebaseline` | — |
| Coupling | `make check-coupling` | no |
| Suppressions | `make check-suppressions` | yes (267 ceiling) |

## Key documents

- `docs/oo-refactoring/oo-refactoring-plan.md` — the 84-step plan
- `docs/oo-refactoring/oo-package-structure.md` — 10-package architecture
- `.oo-baseline.json` — OO scores baseline
- `.suppression-baseline.json` — suppression count baseline (267)
- `CLAUDE.md` — project standards
