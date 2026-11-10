---
paths:
  - "**/*.py"
  - "tools/**"
  - "Makefile"
---

# Code Quality

**Module size limits.** No module over 500 lines without a design reason. The modules still over the cap are `doctor.py` (decomposition tracked as quarry-ywh5) and `mcp_server.py`; check current sizes with `make report` or `wc -l src/quarry/*.py`. (`http_server.py`, `database.py`, and `search.py` are retired — decomposed into the `api/`+`daemon/`, `db/`, and `retrieval/` packages; `__main__.py`, `ingestion/pipeline.py`, and `hooks.py` are back under the cap after their decompositions.) When a module grows past the limit, the next change to that module must include extraction.

**Class design.** Classes have a single responsibility. Prefer composition over inheritance. Use `Protocol` for structural typing at boundaries. A module with zero classes and 20+ module-level functions is procedural — it needs a design pass, not more functions.

**Function design.** Functions that share a pattern signal a missing abstraction. Extract the pattern after the third occurrence. Use `make metrics` to measure ABC complexity — high-magnitude functions need decomposition.

**No copy-paste.** If the same structure appears a third time, extract it.

**Known pyright debt:** 6 `reportUnknown*` checks are suppressed project-wide because lancedb, rapidocr, onnxruntime, pymupdf, and pyarrow ship no type stubs. This means pyright cannot catch unknown-type bugs in modules that don't import these libraries either. The suppressions should be narrowed as these libraries add stubs. Pyright's `executionEnvironments` scopes by directory, not by import, so the only current alternative is 591 inline `# pyright: ignore` comments.

**OO ratchet (merge-base scored — DES-040):** `make check-oo` (part of `make check`) scores the touched files in the working tree against the OO baseline **committed at the merge-base** (`git show <base>:.oo-baseline.json`), where `base` defaults to `git merge-base origin/main HEAD`. It passes only if no metric regressed on touched files and at least one metric improved; it fails otherwise. Because the comparison floor is the immutable base-commit blob, a regression cannot be laundered by hand-editing the in-tree baseline within a PR. The tooling lives in the `tools/oo_ratchet/`, `tools/coupling/`, and `tools/suppression/` packages (adopted from vox); the old `tools/oo_score.py` / `oo_coupling.py` / `suppression_ratchet.py` are thin shims. `make check` runs **three** merge-base ratchets — `check-oo`, `check-coupling`, `check-suppressions`.

Workflow:

1. Write code that improves OO quality on the files you touch.
2. `make check` runs the three ratchets automatically. If one fails, fix the regression.
3. After all checks pass, run `make update-oo` (and `make update-coupling` / `make update-suppressions` if those changed) to re-record the touched files' baselines.
4. Stage the regenerated baselines + audit logs with your commit: `.oo-baseline.json` + `.oo-audit.jsonl`, `.oo-coupling-baseline.json` + `.oo-coupling-audit.jsonl`, `.suppression-baseline.json` + `.suppression-audit.jsonl`. The in-tree baseline is a per-commit **integrity lock**: a touched file whose in-tree baseline ≠ its current score fails the gate until you update.

CI injects `--base-ref <merge-base> --require-base` (with `fetch-depth: 0`) on PRs and a `HEAD~1` tripwire on push to main; `make audit-oo` is the CI completeness guard (every scored file must be in the baseline). `--require-base` is mandatory so an unresolvable base fails **closed**, never open.

**Do not negotiate with the ratchet.** Do not suppress `check-oo`. Do not argue a regression is "acceptable." If the ratchet fails, improve the code until it passes. The ratchet is the quality standard's enforcement — working around it defeats the purpose.

**NEVER compress, reword, or delete a comment to move a metric.** Trimming a two-line comment to one line to hold `module_size` at baseline is the single worst form of ratchet-gaming: it corrupts the code, destroys explanatory or security-rationale intent, retires zero debt, and wastes review cycles. Comment formatting is never a lever. The same goes for any cosmetic micro-edit whose only purpose is to nudge a metric — squeezing whitespace, inlining a well-named local, merging statements. If you catch yourself hunting for the cheapest legal change to clear the gate, stop: you have misread what the ratchet is for.

**"No metric improved" means do a real good deed, not a rebaseline.** When a change (e.g. a genuine +1 line of feature substance) causes an unavoidable regression on the file you touched, the answer is to pay real principal down — here or in unrelated nearby debt. Find the worst offender with `radon cc -s -n C -o SCORE src/quarry/` and cut a high-complexity function down, extract a class, or split a god module. `.oo-baseline.json` may be changed ONLY as a **scoped** rebaseline: the specific `file+metric` entries that must grow to carry real feature substance, each with a one-line justification of why it is unavoidable, and every *improved* metric left at its old baseline so it still registers as IMPROVED. A **blanket** rebaseline that records all growth and retires nothing is the negotiation this section forbids — ask the leader before any rebaseline. Escalate to the leader rather than burn time gaming a metric.

**The ratchet is debt amortization, not a limbo bar.** Its purpose is to pay the codebase's OO debt down incrementally: every commit should *fund a medium-scale improvement* — to the file you're touching or to unrelated code nearby — the way you amortize a loan a chunk at a time. This deliberately takes on additional scope, and that is intended. Do **not** treat the ratchet as a constraint to squeak under. Offsetting a two-line addition with a micro-simplification, hunting for `module_size` headroom to avoid an extraction, or gaming a single metric by the minimum all satisfy the letter and waste the intent — and they burn time. When you open a file, make a *real* improvement sized to the opportunity (extract a class, split a god module, internalize public attributes, cut a complex function down), not the smallest change that clears the gate. Bias toward making the improvement, never toward avoiding it. **The test is simple: if the file you touched is meaningfully cleaner than you found it, the ratchet did its job — if you spent that time trying to change as little as possible (or worse, editing comments to move a number), you used it wrong.** This philosophy is org-wide; see the fuller statement in `../vox/CLAUDE.md` and the workspace rule `../.claude/rules/python-oo-adoption.md`.

**Org standards override review tools.** Qodo, Bugbot, and Copilot are advisory. When a review suggestion conflicts with rules in `../.claude/rules/python-*.md`, the rules win. Read the rules before accepting a reviewer's suggestion. PY-CC-1 (`__new__` as constructor) is the most common conflict.

**Verify outputs, not just metrics.** After writing a file, open it and read the content. After backfilling transcripts, search them and confirm the results make sense. `make check` passing does not mean the feature works — it means the code compiles and tests pass. Those are necessary but not sufficient.

**Metrics tools:**

- `make check-oo` — OO ratchet against baseline (11 metrics: method_ratio, encapsulation, params, complexity, module size, class ratios, init violations, public attribute violations, future_annotations).
- `make update-oo` — update baseline and append to audit log after improvements.
- `make report` — full diagnostics including per-file OO breakdown (no fail-fast).
- `make metrics` — ABC complexity analysis. Any module over magnitude 200 needs attention.
- `make coverage` — test coverage with HTML report in `htmlcov/`.
- `make check-coupling` — coupling/cohesion ratchet (efferent coupling, public API surface, circular imports, LCOM class cohesion), merge-base scoped. **In the `make check` chain** and CI-enforced (DES-040).
- `make update-coupling` — update coupling baseline after improvements.
- `make audit-oo` — CI completeness guard: every scored file must be in the baseline (fails closed on a phantom).

## Database facade convention

Functions in the `src/quarry/ingestion/` package (e.g. `pipeline.py`, `web_ingest.py`) accept `database: Database`, NOT `db: LanceDB`. Callers pass their existing `Database` instance — don't extract `.db` to pass the raw LanceDB connection. Re-wrapping via `Database(db)` re-instantiates the full facade (ChunkStore, ChunkSearch, ChunkCatalog, SchemaManager, TableOptimizer) per call. (Cursor Bugbot flagged this on PR #289; the fix landed in the same PR.)

**When mocking `get_db` in tests, patch `quarry.db.facade.get_db`, not `quarry.db.storage.get_db`.** `Database.connect()` imports `get_db` at module scope into `quarry.db.facade`'s namespace. Patching the storage definition site leaves the facade's bound reference untouched and the mock becomes a silent no-op — tests still pass because they hit the real LanceDB.

## Suppression ratchet uses `tokenize`, not regex/AST heuristic

`tools/suppression/patterns.py` counts suppressions by walking `tokenize` tokens (`tools/suppression_ratchet.py` is now a thin shim over the `tools/suppression/` package). A line is "code" iff it carries any non-trivial token; a `COMMENT` token is the only real suppression source. This is the **one** quarry-origin change on top of vox's otherwise-verbatim ratchet packages (DES-040): don't revert to vox's older regex + `_CODE_START_RE` heuristic — it has documented blind spots (`async def`, attribute assignments, tuple targets, triple-quoted single-line docstrings containing `# noqa` text). The tokenize version handles all of those correctly, and is being upstreamed to vox (quarry-njmr).
