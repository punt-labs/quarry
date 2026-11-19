# Published OO ratchet gates

Quarry uses the PyPI dev dependency `punt-oo-ratchet==0.1.0`, locked in
`uv.lock`. The source is [python-oo-ratchet](https://github.com/punt-labs/python-oo-ratchet).
`make check` runs `oo-ratchet oo`, `oo-ratchet coupling`, and
`oo-ratchet suppressions` on `src/quarry/`. Use the existing `make update-*`
targets to update baselines; the six root baseline/audit files are unchanged.
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
`HEAD~1`. No baselines were regenerated to hide scorer changes. Future source
edits are evaluated by the published scorer against the existing OO/coupling
baseline and must satisfy its integrity locks through the normal update flow.
