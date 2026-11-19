# Published OO ratchet gates

Quarry uses the PyPI dev dependency `punt-oo-ratchet==0.1.0`, locked in
`uv.lock`. The source is [python-oo-ratchet](https://github.com/punt-labs/python-oo-ratchet).
`make check` runs `oo-ratchet oo`, `oo-ratchet coupling`, and
`oo-ratchet suppressions` on `src/quarry/`. Use the existing `make update-*`
targets to update baselines; the six root baseline/audit paths are unchanged.
The implementation and its tests now belong to the package repository.

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
`HEAD~1`. OO and coupling baselines were regenerated with the package's
`--rebaseline --source PL-1161`, appending audited resets rather than leaving
old scorer values as latent false regressions. Suppression baseline/audit
files remain unchanged: that gate rescans both sides at 186.

## Audited baseline reset (PL-1161)

Counts below compare entries present in both the committed baseline and the
regenerated baseline, not old/new scorers run on the same tree.

| Gate | Metric | Modules up | Modules down |
|------|--------|-----------:|-------------:|
| OO | `avg_complexity` | 168 | 7 |
| OO | `max_complexity` | 81 | 2 |
| Coupling | `avg_lcom` | 47 | 64 |
| Coupling | `max_lcom` | 47 | 64 |
| Coupling | `efferent_coupling` | 5 | 3 |
| Coupling | `public_names` | 0 | 5 |

Other metrics do not move on shared baseline entries; in particular,
`circular_imports` is unchanged despite the package's SCC detection fix.
OO has 175 changed shared entries; coupling has 117.

- Complexity changes come from counting conditional expressions and match
  arms/guards/or-patterns, and assigning nested function bodies to their own
  scope rather than double-charging their enclosing function.
- LCOM now counts public and private data fields, excludes helper call targets,
  and exempts BaseModel/data/value shapes where appropriate. On the same current
  source, old versus published scorers move `avg_lcom` up in 49 modules and down
  in 67, and `max_lcom` up in 48 and down in 67. Existing baseline drift explains
  why the reset counts above differ.
- The package's import resolution adds fan-out in three modules
  (`captured_body_ingest`, `ingest_labels`, `backfill_task`), recognizing
  `from package import module` aliases. The other two fan-out increases and
  three decreases are existing baseline drift, not a package scoring change.
- Public-name counting is identical in both implementations; all five
  decreases repair stale baseline entries rather than reflecting a package
  change.

The full-tree reset also removes two obsolete entries (`insights_report.py`
and `status_report.py`) from both baselines and adds 17 previously missing
coupling entries. OO coverage goes from 474 to 472 entries; coupling coverage
goes from 457 to 472. These coverage corrections and stale-value repairs do
not represent source changes in this PR.
