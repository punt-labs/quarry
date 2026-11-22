---
paths:
  - "**/*.py"
---

# OO Standards Adoption

## PL-OA-1: Ratchet — Every Commit Improves OO Quality

**Statement**: The OO Python rules (PY-CC, PY-EN, PY-TS, PY-IC, PY-OP, PY-DP,
PY-EH, PY-OO, PY-CS) represent the target standard. Existing code does not yet
fully comply. The rule is: every commit must improve compliance, never regress.

When you touch a file:

1. Record the baseline: `make check-oo` scores touched files against the
   committed `.oo-baseline.json`; `uv run --extra dev oo-ratchet oo <file>` runs the
   same scoring directly
2. Make your change
3. Re-run the scoring — no metric may worsen
4. If you can improve a metric while making your change, do so
5. After the code passes, `make update-oo` rewrites `.oo-baseline.json` and
   appends to `.oo-audit.jsonl`; stage both in the same commit as the source
   they measure

The baseline is never edited by hand and the ratchet is never suppressed. You
are not required to fix an entire file in one pass. You are required to leave
every file better than you found it.

**Criterion**:

- Pass: OO score does not regress on any touched file; at least one metric improves
- Fail: OO score worsens on a touched file; or opportunity to improve was ignored

**Tooling**:

- `make check-oo` — score touched files against the committed baseline
- `make update-oo` — rewrite the baseline after the code passes
- `uv run --extra dev oo-ratchet oo <file>` — direct scoring, before and after a change
- `uv run --extra dev oo-ratchet oo <directory> --threshold` — per-file breakdown

## PL-OA-2: The punt-oo-ratchet Package Is Required

**Statement**: Depend on the published PyPI package `punt-oo-ratchet==0.4.0`
in the dev extra, locked in `uv.lock`. Its source is
[`python-oo-ratchet`](https://github.com/punt-labs/python-oo-ratchet).
Do not embed its implementation under `tools/`. The stdlib-only package
provides `oo-ratchet {oo,coupling,suppressions}`; all three run in `make check`.
Existing baseline and audit filenames remain compatible.

**Metrics and thresholds**:

| Metric | Target | What it catches |
|--------|--------|----------------|
| method_ratio | >= 0.80 | Procedural modules (functions not in classes) |
| encapsulation_ratio | == 1.0 | Public attributes (missing _ prefix) |
| avg_params | <= 4.0 | Parameter bloat (should be an object) |
| max_complexity | <= 10 | God functions (worst single function) |
| avg_complexity | <= 5.0 | Sustained complexity (many moderately-complex functions) |
| module_size | <= 300 code-bearing lines | God modules; excludes comments/docstrings/blanks |
| classes_per_module | <= 3 | Excludes exceptions, enums, dataclasses, Protocols and TypedDicts |
| class_to_func_ratio | >= 0.5 | Procedural code disguised as a module |
| init_violations | == 0 | **init** used instead of **new** |
| public_attr_violations | == 0 | self.name without underscore |
| future_annotations | == 1 | Missing from **future** import annotations |

**Criterion**:

- Pass: `oo-ratchet oo` exists in the project and runs without error
- Fail: tool missing or broken

## PL-OA-3: Apply the Refactoring Protocol for Legacy Code

**Statement**: When working on a file that violates OO standards, follow
the refactoring protocol in `python-refactoring-protocol.md`. Key principle:
one transformation per step, tests pass at each step, score improves at
each step. Never big-bang rewrite.

The refactoring protocol applies when:

- You are asked to improve OO quality of existing code
- You touch a file with low OO scores and have scope to improve it
- A code review flags OO violations in code you changed

The refactoring protocol does NOT apply when:

- You are writing new code (write it correctly from the start)
- The change is a one-line bug fix with no reasonable OO improvement nearby
