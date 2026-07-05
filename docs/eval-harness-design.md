# Retrieval Evaluation Harness — Design

**Status:** Design ratified (operator, 2026-07-04); Phase 1 to build.
**Owner:** search domain — `kpz` (worker), `adb`/`rmh` (evaluator).
**Companion:** [`retrieval-quality-improvements.md`](retrieval-quality-improvements.md) — the *why* (token-density
ranking failure) and the levers this harness measures.

This harness exists to answer one question with numbers instead of intuition: **does a retrieval
change make quarry better or worse, on quarry's own kind of data?** It is internal developer/CI
tooling — a `make eval` test harness, **not** an end-user command. No one using Claude Code runs it.

---

## 1. Requirements (ratified)

- **Consumer / relevance definition:** Claude first, human second. "Relevant" = the chunk that lets
  the agent answer the query; human usefulness is the secondary tiebreak.
- **Corpus profile:** a developer's knowledge base — code, design docs, requirements docs, plus some
  academic papers / course material. (Not a single-domain corpus.)
- **Primary metric:** **nDCG@10.** "Better" = beats the current stack on nDCG@10.
- **Gold set:** ~40 hand-judged queries as the trusted anchor; a calibrated LLM judge to scale.
- **Round-1 levers (what we measure first):** baseline vs metadata-handling vs context-aware
  embeddings. Late chunking and a reranker are round 2.
- **Form:** a reproducible internal test harness with committed fixtures; can gate CI as a
  regression guard. Not a product surface.

---

## 2. Core concepts (so the design reads clearly)

- **Query set** — the questions we evaluate with. Three buckets: *natural* (how an agent actually
  searches), *known-item* (answer doc is known), and *regression* (known failures kept as permanent
  fixtures — the cookie/ruby cases and **"Predicate Logic in Z"**).
- **Qrels (relevance judgments)** — the answer key: for each query, which chunks are relevant and how
  relevant. Two kinds:
  - **Known-item** — you already know the right doc; the label is free (no UI). A legitimate IR mode
    and exactly the regression-guard question for our known failures.
  - **Graded (0–3)** — perfect / relevant / marginal / irrelevant, per candidate passage. Captures
    ranking nuance; requires an annotation UI.
- **Pooling** — you cannot judge every passage against every query. Classic TREC method: run all the
  configs under comparison, take the **union of each one's top-K**, and judge only that pool. Bounds
  the labeling (≈40 queries × ~20 pooled passages ≈ 800 judgments — a finite session, not a slog).
- **Metrics:**
  - **nDCG@10** (primary) — graded, position-aware; rewards putting the *most* relevant passage
    highest. Measures our exact failure.
  - **Recall@50** — did retrieval surface the good passage *at all* before ranking? (A reranker
    can't fix what was never retrieved.)
  - **MRR@10** — rank of the first relevant hit.
  - **Metadata-pollution@10** (quarry-specific) — fraction of the top-10 that is frontmatter /
    changelog / TOC / heading chunks. A direct meter for our named bug; should fall toward zero
    without hurting nDCG.
- **The A/B loop** — freeze corpus + queries + qrels; run each config; compare metrics with
  **statistical significance**, not just raw deltas. The winner is measured, not argued.
- **LLM-judge leniency** — LLM judges agree with human labels only fair-to-moderately (Cohen's κ ≈
  0.27 graded) and are **systematically lenient** (>90% of large disagreements *over*-grade). So the
  judge is calibrated against the gold set and treated as a scaled approximation, never as truth.

---

## 3. Adopt vs build

Retrieval evaluation is a mature field. We adopt proven libraries and build only the quarry-specific glue.

| Piece | Decision | Why |
|---|---|---|
| Metrics (nDCG@10, Recall@k, MRR, MAP) + significance testing | **Adopt `ranx`** (MIT, pure-Python) | Validated against canonical TREC `trec_eval`; uniquely ships built-in significance tests (paired t-test, randomization) — the decider for an A/B runner. `pytrec_eval` is the thinner fallback. |
| Relevance annotation UI (graded 0–3 → qrels) | **Adopt Label Studio** (self-host Docker) | Actively maintained, no Elasticsearch requirement; lighter and a safer long-term bet than Argilla (which went maintenance-mode in 2025). Phase 2 only. |
| LLM judge | **Build a thin rubric judge** (no framework) | LLM-judge leniency means we calibrate against gold locally; a heavyweight RAG-eval framework (RAGAS/DeepEval) buys nothing. Structure it around an explicit multi-criteria rubric (exactness / coverage / topicality / fit), not a free-form "is this relevant?" prompt. |
| Corpus fixture, config A/B runner over LanceDB, metadata-pollution metric, calibration step | **Build** | Quarry-specific; no off-the-shelf tool covers these. |

---

## 4. Staged plan

Sequencing principle: **never block measurement on labeling infrastructure.** Get real numbers cheap,
prove the loop, then invest in graded labeling.

### Phase 1 — bootstrap (no UI)

Stand up the whole skeleton on **known-item + regression** qrels:

- Assemble the fixed corpus fixture (§5).
- ~30 queries whose answer doc is known (including the regression failures).
- `ranx`-based scoring (nDCG@10 / Recall@50 / MRR) + the metadata-pollution metric.
- The config A/B runner over LanceDB: index the fixture under config X → run queries → emit a TREC
  run file → score.
- `make eval` target; committed fixtures under `tools/eval/` (or similar).

Deliverable: **baseline nDCG@10 and a working regression guard on day one**, zero labeling UI. This
proves the harness end-to-end and de-risks before we invest in labeling.

### Phase 2 — graded gold via Label Studio

- Stand up Label Studio (self-host).
- **Pool** the top-K from baseline + the round-1 levers; export the pool to Label Studio.
- Hand-grade 0–3; export back to TREC qrels. Yields the ~40-query graded gold set.

### Phase 3 — calibrate the LLM judge

- Build the thin rubric judge; run it over the same pool.
- Measure agreement against the graded gold (label agreement + system-ranking correlation); wire the
  judge into `make eval` only once it clears a documented bar. Then use it to scale query coverage.

### Then — the lever bake-offs

With the harness trusted, run the round-1 A/B (baseline vs metadata-handling vs context-aware
embeddings) from `retrieval-quality-improvements.md`, each config re-indexed over the fixture,
compared on nDCG@10 with significance. Round 2: late chunking, reranker.

---

## 5. Components

- **Corpus fixture** — a curated, committed sample of a developer knowledge base: a slice of the
  quarry/lux code, several design docs + a requirements doc, and 3–5 academic/course docs. The Z /
  course material is deliberately included so **"Predicate Logic in Z"** is a fixture. Small enough
  to index in CI, representative enough to trust. Assembled by the leader (COO).
- **Query set** — `tools/eval/queries.jsonl` (committed): id, text, bucket (natural / known-item /
  regression), and for known-item the answer doc(s).
- **Qrels** — TREC-format, committed: known-item (Phase 1) upgraded to graded (Phase 2).
- **Runner** — indexes the fixture under a named config, runs the query set, writes a TREC run file,
  hands qrels + run to `ranx`. Config = the retrieval knobs being compared (embedding strategy,
  metadata handling, fusion, reranker on/off).
- **Metrics report** — per-config nDCG@10 / Recall@50 / MRR / metadata-pollution, per-query
  win/loss, and significance vs baseline. Sample size stated next to every delta.
- **LLM judge** — rubric prompt + calibration script; off by default until calibrated.

---

## 6. Where it lives / how it runs

- `make eval` — runs the harness against the committed fixture + qrels, prints the metrics report.
- `tools/eval/` — runner, fixtures (corpus manifest, queries, qrels), judge, rubric.
- **CI gate (opt-in, later):** fail the build if nDCG@10 regresses on the fixture beyond a threshold —
  the regression guard. Off until Phase 1 is stable.
- Fixtures are committed so runs are comparable over time and reproducible by anyone on the team.

---

## 7. Open decisions (to settle during build)

- **Calibration bar for a 40-query gold set.** The ~0.90 system-ranking-correlation threshold from
  the literature is for large TREC collections; on 40 queries per-query variance dominates. `kpz`
  proposes a defensible bar (likely label-agreement + spot-check, not a hard ranking correlation).
- **Corpus fixture size / licensing** for the committed academic-paper docs (use openly-licensed
  papers/course material to keep the fixture redistributable).
- **Metadata-pollution classifier** — how a chunk is tagged "metadata" (frontmatter / changelog / TOC
  / heading). Heuristic first; refine against the fixture.

---

## 8. References

- `docs/retrieval-quality-improvements.md` — the levers this harness measures + the eval-tooling research.
- `ranx` — <https://github.com/AmenRa/ranx> (MIT). Label Studio — <https://github.com/HumanSignal/label-studio>.
- A DESIGN.md ADR will record this once Phase 1 is built and the harness is settled.
