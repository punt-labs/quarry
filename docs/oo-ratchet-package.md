# Published OO ratchet gates

Quarry uses the PyPI dev dependency `punt-oo-ratchet==0.3.0`, locked in
`uv.lock`. The source is [python-oo-ratchet](https://github.com/punt-labs/python-oo-ratchet).
`make check` runs `oo-ratchet oo`, `oo-ratchet coupling`, and
`oo-ratchet suppressions` on `src/quarry/`. Use the existing `make update-*`
targets to update baselines; the six root baseline/audit paths are unchanged.
The implementation and its tests now belong to the package repository.

## Package 0.3.0 counting migration (PL-1141)

The OO baseline adopts 0.3.0 through one CLI invocation over all 472 stored
paths: `--rebaseline-files "PATH1,PATH2,..." --reason "adopt 0.3.0 counting"
--source PL-1141`. Exactly one `rebaseline-files` audit record records the
improvements; no regressed-metric waivers are added.

| OO metric | Files up | Files down | New counting |
|-----------|---------:|-----------:|--------------|
| `module_size` | 0 | 472 | Code-bearing lines only; no blanks, comments or docstrings |
| `classes_per_module` | 0 | 162 | Excludes exceptions, enums, dataclasses, Protocols and TypedDicts |

All other OO metrics stay unchanged. Coupling scores do not change between
0.2.0 and 0.3.0 on the same source. The efferent-coupling cap of 10 rather than
7 is the 0.3.0 package default, adopted per PL-1188 as Jim's package policy
(`__main__.py` remains 15). Coupling state-write detection
is more conservative, including in-place mutations; Quarry's default-mode
scores are unaffected. The coupling baseline and audit are not rewritten:
five pre-existing drift cells (two efferent coupling and three public-name
counts) are deliberately retained, not repaired as part of this migration.
The suppression baseline and audit are also unchanged, at 186.

The 0.3.0 `CouplingScorer` reports these current modules at 8, 9 or 10 internal
imports (paths relative to `src/quarry/`). These exceed the old default of 7;
`__main__.py` is listed for completeness but retains its separate cap of 15,
so it would not fail under the old policy. No configuration or waivers are added.

| Module | `efferent_coupling` |
|--------|-------------------:|
| `__main__.py` | 8 |
| `daemon/backfill_task.py` | 8 |
| `daemon/captured_body_ingest.py` | 8 |
| `daemon/launcher.py` | 10 |
| `daemon/routes/captures.py` | 8 |
| `daemon/routes/content_group.py` | 9 |
| `daemon/watch_submit.py` | 10 |
| `db/facade.py` | 9 |
| `doctor_daemon.py` | 8 |
| `doctor_runner.py` | 9 |
| `extractors/__init__.py` | 8 |
| `ingestion/__init__.py` | 10 |
| `ingestion/file_indexer.py` | 10 |
| `ingestion/format_strategies.py` | 8 |
| `mcp_server.py` | 10 |
| `repair.py` | 9 |
| `service.py` | 9 |
| `sync.py` | 9 |

Suppression waivers identical to entries already in the comparison base's
ledger now retire. They cannot provide reusable headroom after a cleanup;
a later increase requires a fresh audited relaxation. Quarry has no suppression
waiver ledger at this migration. Existing Makefile and CI flags are unchanged.

The following sections record the earlier 0.1.0/0.2.0 migrations, not a new
rebaseline of coupling or suppressions for 0.3.0.

## Cutover comparison

Before removing the embedded implementation, all three old and published
gates passed on the same unmodified quarry tree. The verdicts match, but
the metrics are not byte-identical:

- OO complexity differs in 175 of 472 scored modules. The package counts
  conditional expressions and match cases, and avoids charging nested function
  bodies to their enclosing function.
- Coupling metrics differ in 118 of 472 modules. The package measures shared
  data rather than helper dispatch, includes public fields, exempts additional
  data/value shapes, and improves absolute and package-relative import handling.
- Suppressions are 186 rather than 184: file-level directives in
  `gpu_runtime.py` and `service.py` are now counted. Both base and working tree
  scan to 186, so the gate still passes. The published checker rescans base-ref
  source instead of trusting potentially stale committed suppression counts.
  Per-file ignore entries also appear in its per-file report.

No quarry-only fix was found missing from the package. In particular:

- PL-825: coupling `--tripwire` retains the one-time adoption bootstrap on
  post-merge main, with an additional fail-closed guard requiring an in-tree
  baseline. CI still passes `HEAD~1 --require-base --tripwire` for coupling.
- PL-100: fail-closed source read/parse handling and repository-relative POSIX
  path normalization are retained.
- PL-101: tokenize-based suppression counting and its conservative fallback
  are retained, with additional coding-cookie and file-level directive support.

PR gates still use the merge-base with `--require-base`; push gates still use
`HEAD~1`. OO and coupling baselines receive a metric-scoped migration from
main, audited with source `PL-1161`, rather than a full-tree reset.
The suppression snapshot is now 186, recorded through 0.2.0's audited
`--rebaseline --reason ... --source PL-1175`. The gate still rescans both sides.

## Package 0.2.0 and suppression migration (PL-1175)

Version 0.2.0 adds the audited suppression rebaseline, permitting the scanner-only
184 → 186 migration without changing source or weakening ordinary `--update`.
The two additional counts are existing file-level directives in `gpu_runtime.py`
and `service.py`. The snapshot also now includes per-file-ignore breakdowns;
their aggregate count remains 30. `make update-suppressions` succeeds at 186
without changing counts, and `make check-suppressions` passes at 186 → 186.
An ordinary update still records its own audit entry and snapshot timestamp.

The installed 0.1.0 → 0.2.0 source diff has no OO implementation changes.
Coupling adds `--rebaseline-files` with `--reason`, repository-specific caps,
and an opt-in `stateless-values = "any"` mode under
`[tool.oo-ratchet.coupling]`. Its default stateless-value test no longer descends
into nested classes; threshold reports now judge max metrics per file, including
the existing `__main__.py` allowances and any configured caps. Quarry has no
coupling configuration, so it retains the default `"dataclass"` mode and no
caps. Comparing both versions on the merged source gives identical per-file
scores for all 472 OO and all 472 coupling modules, and identical aggregate
threshold grades. Existing Makefile and CI flags remain supported and unchanged.

## Audited scorer-only migration (PL-1161)

Start from main's baselines. For each shared file and metric, use the package
value only when the embedded and package scorers disagree on identical current
source. Otherwise retain main's value, even when stale. Counts below compare
shared entries on main with this scoped result.

| Gate | Metric | Modules up | Modules down |
|------|--------|-----------:|-------------:|
| OO | `avg_complexity` | 168 | 7 |
| OO | `max_complexity` | 81 | 2 |
| Coupling | `avg_lcom` | 46 | 63 |
| Coupling | `max_lcom` | 46 | 63 |
| Coupling | `efferent_coupling` | 3 | 0 |

Other metrics do not move on shared baseline entries; in particular,
`circular_imports` is unchanged despite the package's SCC detection fix.
OO has 175 changed shared entries; coupling has 111.

- Complexity changes come from counting conditional expressions and match
  arms/guards/or-patterns, and assigning nested function bodies to their own
  scope rather than double-charging their enclosing function.
- LCOM now counts public and private data fields, excludes helper call targets,
  and exempts BaseModel/data/value shapes where appropriate. On the same current
  source, old versus published scorers move `avg_lcom` up in 49 modules and down
  in 67, and `max_lcom` up in 48 and down in 67. Baseline coverage and values
  explain why the migration counts above differ.
- The package's import resolution adds fan-out in three modules
  (`captured_body_ingest`, `ingest_labels`, `backfill_task`), recognizing
  `from package import module` aliases. Other fan-out values stay unchanged.
- Public-name counting is identical in both implementations; no shared
  public-name baseline values change.

The migration also removes two obsolete entries (`insights_report.py`
and `status_report.py`) from both baselines and adds 17 previously missing
coupling entries. OO coverage goes from 474 to 472 entries; coupling coverage
goes from 457 to 472. These coverage corrections do not represent source changes
in this PR.

At the original migration, package 0.1.0's OO `--rebaseline-files` replaced whole
rows and enforced caps; coupling had no equivalent. Version 0.2.0 adds coupling's
equivalent, but neither CLI supports metric-scoped migration.
The migration therefore uses the package's baseline serializer and audit API,
recording a `rebaseline` entry with exact metric deltas, removals, coverage
additions, source `PL-1161`, and the scorer-only selection rule as its reason.
This supersedes the earlier full-tree reset without granting relaxation waivers.

### Stale values deliberately retained

Both scorers agree on the actual values below. All are coupling metrics;
paths are relative to `src/quarry/`. OO has no retained stale values.

| File | Metric | Main baseline | Actual |
|------|--------|--------------:|-------:|
| `artifacts.py` | `public_names` | 4 | 3 |
| `daemon/inotify_prune.py` | `efferent_coupling` | 0 | 1 |
| `daemon/routes/databases.py` | `efferent_coupling` | 2 | 1 |
| `db/__init__.py` | `public_names` | 10 | 7 |
| `db/storage.py` | `efferent_coupling` | 2 | 1 |
| `db/storage.py` | `public_names` | 5 | 2 |
| `enabled_marker.py` | `efferent_coupling` | 0 | 1 |
| `ingestion/text_splitter.py` | `public_names` | 9 | 3 |
| `skills_sync.py` | `efferent_coupling` | 4 | 3 |
| `skills_sync.py` | `public_names` | 2 | 1 |
| `skills_sync.py` | `max_lcom` | 0.6 | 0 |
| `skills_sync.py` | `avg_lcom` | 0.6 | 0 |
| `sync_registry.py` | `max_lcom` | 0.366 | 0.373 |
| `sync_registry.py` | `avg_lcom` | 0.366 | 0.373 |
