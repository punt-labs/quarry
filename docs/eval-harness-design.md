# Retrieval Evaluation Harness — Design

**Status:** Design ratified + twice peer-reviewed (`kpz` methodology, `adb` infra/CI, `rmh` runner/data).
**Phase 0 merged** (#343 — parameterizable retrieval seam in `src/quarry/retrieval/`) and **Phase 1 merged**
(#344 — `make eval` bootstrap harness: MRR/success@k, metadata-pollution diagnostic, determinism contract,
known-item baseline). **Next:** curate the full representative fixture (quarry-#345) → first *meaningful* baseline (the
current overall MRR 0.900 is a 5-doc smoke-test, not a real signal); then Phase 2 (Label Studio graded gold),
Phase 3 (calibrated LLM judge), then the lever bake-offs (metadata-handling, context-aware embeddings).
Last updated 2026-07-05.
**Owner:** search domain — `kpz` (worker), `adb`/`rmh` (evaluator).
**Companion:** [`retrieval-quality-improvements.md`](retrieval-quality-improvements.md) — the *why*
(token-density ranking failure) and the levers this harness measures.

This harness answers one question with numbers instead of intuition: **does a retrieval change make
quarry better or worse, on quarry's own kind of data?** It is internal developer/CI tooling — a
`make eval` test harness, **not** an end-user command.

> **Peer-review note.** The reviews found the *engineering skeleton* sound but the *measurement
> claims* not yet trustworthy at the originally-stated scale, plus two structural blockers (no
> config seam; unstable docid). This revision folds in the fixes. The load-bearing corrections:
> Phase-1 primary metric is MRR/success@k (nDCG is degenerate on single-relevant known-item);
> n=40 is a *calibration anchor*, not an A/B set (it is ~10× underpowered for the predicted deltas);
> a production `Retriever` seam must be extracted before the runner; qrels are keyed to a stable
> `(document, page)` unit; and determinism must be contracted, not assumed.

---

## 1. Requirements (ratified)

- **Consumer / relevance:** Claude first, human second. "Relevant" = the chunk that lets the agent
  answer the query.
- **Corpus profile:** a developer's knowledge base — code, design/requirements docs, plus some
  academic papers / course material.
- **Primary metrics:** **success@5/@10 and MRR** as the agent-consumer signals, **nDCG@10** as the
  graded primary once graded qrels exist (Phase 2+). "Better" = beats the current stack on these.
- **Gold set:** ~40 hand-judged queries as the **calibration anchor** for the LLM judge — *not* the
  A/B set (see §4/§5 on power).
- **Round-1 levers:** baseline vs metadata-handling vs context-aware embeddings. Late chunking and a
  reranker are round 2.
- **Form:** reproducible internal test harness with committed fixtures; splittable CI regression
  guard. Not a product surface.

---

## 2. Core concepts

- **Query set** — three buckets, **reported separately, never blended**: *natural* (conceptual
  why/how — quarry's hard, high-value case), *known-item* (answer doc known), *regression* (known
  failures kept as permanent fixtures — cookie/ruby and **"Predicate Logic in Z"**).
- **Judged unit (`JudgedUnit`)** — `(document_name, page_number)`. Stable across chunking configs
  (`page_number` is fixed by the extractor before chunking; `chunk_index` is *not* and must never be
  a key). Run rows are emitted at this granularity (best-ranked chunk per page). Chunk-level judging
  (round-2 late chunking) re-pools per chunking config. **Non-paginated formats** (code, markdown)
  may carry a null/uniform `page_number`, in which case `JudgedUnit` degrades to **document-level**
  for those buckets — acceptable for Phase-1 known-item, but a *documented* choice: Phase 0 confirms
  the extractor's `page_number` behavior for code/markdown rather than discovering it silently.
- **Qrels** — TREC-format over `JudgedUnit`. Known-item (Phase 1) is document/page-keyed and binary;
  graded (Phase 2) is 0–3 per pooled unit.
- **Pooling** — union of each compared config's top-K, judged once. **Pool depth ≥ the deepest
  reported cutoff** (so a metric's denominator is fully judged). **Incremental re-pooling per round:**
  a new config's *newly-surfaced* units get judged, closing pool bias (unjudged ≠ irrelevant). Always
  report **`judged@10`** (fraction of a config's top-10 that is judged) as the trust signal.
- **Metrics** (which apply *when*):
  - **success@5 / success@10 / MRR** — primary, all phases. Discount-free; match an agent that reads
    all of top-k. The only meaningful metrics in Phase 1 (single binary relevant).
  - **nDCG@10** — graded primary, **Phase 2+ only** (degenerate-to-MRR under single-relevant
    known-item; Phase-1 and Phase-2 nDCG are **not comparable** — never plot on one axis).
  - **bpref / condensed-nDCG** — reported alongside nDCG; robust to incomplete judgments.
  - **Recall@k** — pool-health ("could a reranker help"), with **k ≤ pool depth** (no Recall@50 off a
    depth-20 pool). Dropped if the fixture is too small to make large-k recall meaningful (§5).
  - **metadata-pollution@10** — **guardrail diagnostic only. Reported, never gated, never optimized.**
    It explains *why* nDCG/success moved; sometimes a changelog line genuinely is the answer.
- **The A/B loop** — freeze corpus + queries + qrels; run each config; compare with **bootstrap /
  randomization CIs on the delta** (not the paired t-test — per-query deltas are non-normal at small
  n), **per bucket**, with **multiple-comparison correction** (Holm) across the metric×bucket×config
  family. The winner is measured, and its uncertainty is stated.
- **Statistical power (the honest constraint)** — the predicted deltas are +1.5–1.9pp nDCG. Detecting
  those at 80% power needs **~150–300 queries**; at n=40 the study has ~15–25% power (a ~75–85%
  false-negative rate). So **40 hand-judged queries are the judge-calibration anchor; the A/B set is
  judge-scaled to ~150–300.** At n=40, results are **directional, not confirmatory** — "consistent
  with improvement, underpowered to confirm."
- **LLM-judge leniency** — LLM judges agree with humans only fair-to-moderately (κ ≈ 0.27 graded) and
  are systematically lenient. The judge is calibrated (§5) and its labels are **relative-only** — safe
  for A/B deltas, never an absolute quality claim.

---

## 3. Adopt vs build

| Piece | Decision | Notes |
|---|---|---|
| Metrics + significance | **Adopt `ranx`** (MIT; validated vs `trec_eval`; ships randomization/paired tests) | **Not pure-Python — depends on Numba/llvmlite.** Confirm Py3.13 wheels in CI; put in an **eval-only optional group** (`[project.optional-dependencies] eval`), never core deps; add `ranx.*` to the mypy `ignore_missing_imports` block. `pytrec_eval` is the C-extension fallback (needs a compiler — not lighter). |
| Annotation UI (graded 0–3) | **Adopt Label Studio** (self-host) | Phase 2 only. **Fenced:** never a dependency of `make eval`, `make check`, or any CI job; its exported qrels are committed, the running service is off every automated path. |
| LLM judge | **Build a thin rubric judge** (no framework) | Off by default until calibrated; its output qrels are **frozen and committed** (a re-run at a new model version silently changes the answer key). |
| **Retrieval config seam** | **Build in `src/quarry/`** (see §5) | Prerequisite (Phase 0): production + eval must share one retriever or they drift. |
| Corpus fixture, A/B runner, metadata-pollution metric, calibration | **Build** | Quarry-specific glue. |

---

## 4. Staged plan

Sequencing principle: **never block measurement on labeling infrastructure, and never claim more
rigor than the sample supports.**

### Phase 0 — extract the retrieval config seam (prerequisite, production code) — ✅ merged (#343)

The runner cannot exist cleanly until retrieval is parameterizable without forking. Extract into a
new `src/quarry/retrieval/`:

- `RetrievalConfig` — frozen dataclass (`rrf_k`, `fetch_multiplier`, `metric`, `exact_search`,
  `reranker`, embedding strategy). `exact_search` carries the determinism contract's flat-vs-ANN
  choice through the seam rather than monkeypatching the LanceDB query in the runner.
- `Retriever` Protocol — `retrieve(query_text, query_vector, filter, limit) -> list[SearchResult]`.
- `Reranker` Protocol + `NullReranker` (on/off is a swap, not a branch).
- `HybridRetriever(config)` — today's `hybrid_search` body. All three production call-sites
  (`__main__.py`, `http_server.py`, `mcp_server.py`) **and** the eval runner call the identical
  retriever. This is also a real OO paydown on the procedural `chunk_search.py` (ratchet-positive).
- **Equivalence gate — lands *before* the extraction:** a characterization test asserting the new
  `HybridRetriever` produces byte-identical results to today's `hybrid_search` at all three
  call-sites on a fixed corpus. Without it, a subtle refactor drift means the Phase-1 baseline
  measures a *different* retriever than ships, invalidating the regression guard. This is the one
  reproducibility risk Phase 0 introduces; it must be a Phase-0 success criterion, not discovered
  mid-build.

### Phase 1 — bootstrap (no UI, no judge) — ✅ merged (#344)

- Assemble the fixed corpus fixture (§5); ~30 queries with known-item/regression answers,
  page-keyed.
- `ranx`-scored **MRR + success@k** (not nDCG — degenerate here) + the metadata-pollution diagnostic.
- The config A/B runner over the seam: index fixture under a `RetrievalConfig` → run queries → emit a
  TREC run file (page-keyed) → score.
- `make eval` target; committed raw fixture under `tools/eval/`.
- **Claim bounded to "regression guard + navigational sanity," not "retrieval is good."** Known-item
  skews navigational; it cannot certify the conceptual queries that are the product thesis.

### Phase 2 — graded gold via Label Studio (calibration anchor)

Stand up Label Studio; **pool ≥ deepest cutoff** across baseline + round-1 levers; hand-grade 0–3 at
`JudgedUnit` granularity; export → committed TREC qrels. Yields the ~40-query graded **calibration
set**. Split queries into a **dev set (tune thresholds) and a locked test set (report only)** — never
tune levers on the test set.

### Phase 3 — calibrate the judge, then scale the A/B set

Build the thin rubric judge; run it over the pool; calibrate against the graded gold with the §5 bar.
Once it clears the bar, **judge-scale the query set to ~150–300** so the A/B is actually powered, and
run the judge over the *full retrieved set* (not just the pool) to densify qrels and kill pool bias.

### Then — the lever bake-offs

Round-1 (baseline vs metadata vs context-embeddings), each re-indexed over the fixture, compared per
bucket with CIs + correction. Round 2: late chunking (chunk-level re-pool), reranker.

---

## 5. Components

- **Retrieval seam** — `src/quarry/retrieval/` (Phase 0 above). Production code, shared by the shipped
  path and the harness.
- **Corpus fixture** — committed **raw docs** (openly-licensed papers/course material + a slice of
  quarry/lux code + design/requirements docs), *not* a fetch-manifest and *not* the LanceDB index.
  Deliberately includes the Z/course material so **"Predicate Logic in Z"** is a fixture. **Sized
  ~1–5k chunks** (still seconds to index) so retrieval is non-trivial and Recall@k is meaningful; its
  **metadata/structural-to-substantive chunk ratio matches the live index** (measure it, then curate)
  so the pollution diagnostic bites on realistic distractor density. Derived artifacts
  (index/embeddings) live in `.tmp/` (gitignored). Assembled by the leader (COO).
- **Query set** — `tools/eval/queries.jsonl` (committed): id, text, bucket, dev/test split flag, and
  for known-item the answer `JudgedUnit`(s).
- **Runner** — indexes the fixture under a `RetrievalConfig` into an **ephemeral per-config
  `Database`** (`Database.connect(.tmp/<config-hash>/lancedb)`, via the facade — accept `Database`,
  don't re-wrap). Uses `new_embedding_backend()` for model-distinct configs (the cached
  `get_embedding_backend` would serve a stale backend on a model swap); **reuses one index across
  configs that don't change embeddings** (metadata/fusion/reranker knobs), the dominant cost saving.
  Emits a page-keyed TREC run → `ranx` + pollution metric. The page-collapse (best chunk per page)
  happens **in the runner, after the retriever returns** — never inside the shared `HybridRetriever`,
  which keeps returning per-chunk results for production. Report headers are **phase-labeled**
  (Phase-1 MRR/success vs Phase-2+ graded nDCG) so a cross-phase delta cannot be eyeballed.
- **Value objects (not free-function piles):** `JudgedUnit` (`.docid`), `TrecRun`/`Qrels`
  (`.write`/`.from_path`) — single source of truth for the join key so run-emission and
  qrels-authoring cannot drift.
- **Determinism contract** (must hold before any baseline is committed):
  - `intra_op_num_threads = 1` in the harness (thread count changes float reduction order → tie flips).
  - **Pin the HF model revision hash**, not just the name.
  - Force LanceDB **exact/flat search** on the fixture (ANN is seed-dependent; the small corpus makes
    exact free).
  - Deterministic RRF tie-break by `JudgedUnit`.
  - Stamp every committed baseline with **ORT version + model revision + CPU arch**; a delta is only a
    regression if it exceeds a stated **tolerance band ε** on the same profile.
- **LLM judge** — rubric prompt (multi-criteria: exactness/coverage/topicality/fit) + calibration
  script; off until calibrated; output qrels frozen + committed.
- **Harness self-tests** — a golden run over a 2-doc micro-fixture asserting fixed MRR/success (the
  harness is `src`/`tools` code and carries its own tests per project standards).

---

## 6. Where it lives / how it runs

- `make eval` — runs the harness against the committed fixture + qrels; prints per-bucket metrics with
  CIs. Requires `uv sync --extra eval`.
- `tools/eval/` — glue: `corpus` (loads the committed raw docs → ingest), `trec` (run/qrels I/O), `runner`, `metrics`
  (ranx + pollution), `judge`. The **retrieval seam lives in `src/quarry/retrieval/`, not here.**
- **CI (later, opt-in) — split gate:**
  - **Regression subset = hard, binary, deterministic.** The named failures are known-item pass/fail
    ("is the answer page in top-k"); noise-immune; safe to gate a PR on.
  - **Aggregate metrics = informational first**, then banded on the **CI lower bound** (never the
    point estimate), as a **separate non-blocking `eval.yml` job** — not in `make check`. CI has no
    GPU and a time budget; cache the HF model download (keyed on revision); **generate the committed
    baseline in that same CI environment** so gate and baseline share arch/thread/ORT profile.
- Label Studio runs only as a documented, optional manual step (`make eval-annotate` / docs) — never
  on an automated path.

---

## 7. Open decisions (settle during build)

- **Judge calibration bar** — primary: **quadratic-weighted Cohen's κ ≥ 0.6** on pooled graded
  labels (κ 0.4–0.6 usable-with-caveats; <0.4 do not scale). Decision statistic: **pairwise
  sign-of-delta agreement** between judge-qrels and human-qrels on every round-1 config pair whose
  human-delta CI excludes zero (leniency inflates configs equally, so relative ranking can survive
  modest κ — that is what A/B needs). Publish the judge-vs-human confusion matrix; human re-adjudicates
  every ≥2-grade disagreement and every top-10-membership flip. Retire the literature's τ≥0.90 bar
  (vacuous with 2–4 configs).
- **Recall@k vs fixture size** — resolved toward "grow the fixture to ~1–5k chunks and cap reported
  cutoffs at what the pool depth supports"; confirm the exact k during build.
- **metadata-pollution classifier** — how a chunk is tagged metadata (frontmatter/changelog/TOC/
  heading). Heuristic over `page_type`/text first; refine against the fixture.
- **Chunk-level qrels for round-2 late chunking** — re-pool per chunking config; page-level qrels
  remain valid for all same-index levers.
- **Tolerance band ε** — derived empirically, not guessed: run the harness ≥10× identically on one
  machine, measure the metric noise floor, set ε above it, so the aggregate gate's false-fail rate is
  known. Recorded alongside the baseline provenance.

---

## 8. References

- `docs/retrieval-quality-improvements.md` — the levers this harness measures + the eval-tooling research.
- `ranx` — <https://github.com/AmenRa/ranx> (MIT). Label Studio — <https://github.com/HumanSignal/label-studio>.
- Peer reviews (2026-07-04): `kpz` (methodology), `adb` (infra/CI), `rmh` (runner/data) — on file.
- A DESIGN.md ADR records this once Phase 0–1 are built and settled.
