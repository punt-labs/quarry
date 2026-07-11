# Retrieval Quality Improvements

**Status:** Research complete, direction proposed. The eval harness this doc calls
a prerequisite has since **shipped** (Phase 0 retrieval seam #343 / DES-037; Phase 1
`make eval` + ranx metrics #344) — see `eval-harness-design.md` for the authoritative,
current design. The **embedding levers** below (contextual embeddings, late chunking,
metadata handling) remain unimplemented; they are now measured against `make eval`
before adoption. Where §5–§6 restate an eval plan, `eval-harness-design.md` supersedes
it (notably: Phase 1 uses MRR/success@k, not the nDCG@10/Recall@50 sketched here).
**Date:** 2026-07-04.
**Owner:** search domain (`kpz` worker, `rmh`/`gvr` evaluator).
**Prompted by:** live queries on real data (`course-ox-*`) showing short, term-dense
chunks — document frontmatter, `CHANGELOG` lines, TOC/heading listings — outranking
substantive passages that actually answer the query.

This document captures (1) the failure, (2) what production retrieval systems do in
2025–2026, (3) the evidence-backed direction for quarry, and (4) a high-level plan for
the evaluation harness that must come first. It exists so the analysis is not lost.

---

## 1. The observed failure

Query: **"Predicate Logic in Z"** (real, on the live index). Ranked results:

1. `spivey-z-reference-manual-1992.md` p.1 — the manual's **YAML frontmatter** (title/author/description), not content about predicate logic. Scored highest (0.5548).
2. `CHANGELOG.md` p.50 — a one-line changelog entry mentioning "predicate logic, sets, and basic Z notation." Ranked *above* the substantive slide.
3. `topic02.pdf` p.57 — *"the relational calculus is effectively predicate logic and typed set theory — which is what Z is based on."* This is the actual answer, ranked 3rd.

Second symptom (same query): the list is **RRF-ordered while the printed score is cosine**,
so a higher-cosine row (`topic02 p.28` at 0.5159) can appear *below* a lower one
(`CHANGELOG` at 0.4961). Display order and printed score do not always agree.

### Root cause

Quarry ranks per-**chunk** with a **bi-encoder** (dense embedding) fused with BM25 via RRF.
A bi-encoder compresses a whole chunk into one mean-pooled vector, so it rewards **token
density**, not relevance: a short block where nearly every token is on-topic (a title, a
changelog line, a heading) produces a vector pointing straight at the query, while a
substantive passage dilutes its vector with connective prose and scores lower. Metadata
and structural chunks are short and dense, so they systematically outrank the passages that
actually answer the query.

This is **not** an over-valuing of "volume of matched content." It is the opposite: the
current ranker *under*-values substance. It is also distinct from the `quarry-gcnf` bug
(FTS-only rows showing a fake `1.00`), which is already fixed — all scores here are proper
bounded cosine.

---

## 2. Current stack (the retrieval floor)

| Layer | Quarry today |
|---|---|
| Dense | `snowflake-arctic-embed-m-v1.5`, 768-dim, **512-token max, mean-pooled**, ONNX int8 on CPU by default; FP16 on CUDA when detected (DES-016) |
| Sparse | BM25 full-text via Tantivy |
| Fusion | Reciprocal Rank Fusion, `_RRF_K = 60` |
| Store | LanceDB, one `chunks` table |
| Results | per-chunk, no document-level dedup |

---

## 3. What production retrieval systems do (2025–2026)

Sourced from a verified deep-research pass (21 claims confirmed, 4 refuted). Citations in §8.

**turbopuffer (highest-traction engine) validates our floor.** It runs BM25 and vector as
*separate* queries and fuses server-side with **RRF (k=60)** — then explicitly positions
itself as **stage-one retrieval feeding an external cross-encoder reranker** (it names
Cohere, Voyage, ZeroEntropy, MixedBread). Quarry's `BM25 + vector + RRF (k=60)` is exactly
this stage-one. **Keep RRF as-is:** weighted fusion barely beat it (nDCG 0.726 vs 0.716) and
requires score calibration RRF avoids.

**Two-stage retrieve-then-rerank is the consensus pattern.** A cross-encoder reranker is the
single largest quality lever *when the domain matches*: +17.2pp MRR@3, Recall@5 0.695→0.816
(Cohere Rerank v4 on financial docs).

**But the reranker flips from "decisive" to "risky" for quarry's profile:**

- Off-the-shelf cross-encoders **degrade** quality on technical/scientific corpora: −0.3 to
  −3.1% NDCG on the closest-matching study, −12% (Jina-v2) to −34% (BGE-v2-m3) on patents —
  while adding **560–2100 ms/query**. They are trained on web-search distribution that does
  not transfer to code, specs, and textbooks.
- The closest study to quarry — a **local BGE-small ONNX pipeline** — concludes gains come
  from **better embeddings and context-aware chunking, not post-hoc reranking**.
- "Semantically similar but logically irrelevant" is a *named, benchmarked* reranker failure
  mode (SciRerankBench) — i.e. rerankers are not guaranteed to fix the metadata problem.

**The fix that targets our failure lives at the embedding, not the reranker:**

- **Late chunking (Jina):** embed the *whole document's* tokens through a long-context model,
  *then* mean-pool per-chunk token spans, so each chunk carries document context. No LLM, no
  GPU, no retraining. On the canonical example a context-dependent chunk lacking the query
  term rose **cosine 0.7084→0.8249, with the gain largest at small chunk sizes** — precisely
  the short/metadata regime. +1.5–1.9pp nDCG@10 on BeIR.
- **Contextual embeddings (Anthropic):** prepend an LLM-written 50–100 token context before
  embedding — −35% retrieval failure alone, −49% with contextual BM25 via RRF. Needs an LLM
  at *ingest* (not query).
- Late chunking **matches** contextual embedding on relevance (0.8516 vs 0.8590 vs naive
  0.6343) **without the LLM cost**.
- Reranking and contextual chunking are **complementary, not substitutes**: Anthropic's −49%
  becomes −67% *with* a reranker; a separate study finds a final reranking step is what makes
  contextual chunking pay off consistently.

**Refuted — do not chase:** ColBERT/ColPali multi-vector late-interaction (0-3) and adaptive
per-query IDF-weighted RRF weighting (0-3) both failed adversarial verification.

---

## 4. What this means for quarry (corrected direction)

The general-purpose advice — "add a cross-encoder reranker, it's the biggest lever" — is
**right for a SaaS stack and wrong as the first move for quarry.** Quarry is local and, in its
common deployment (including the dev machine), CPU-only — FP16 on CUDA is supported and
auto-detected (DES-016), but most instances run int8 on CPU, where the reranker's added latency
bites. Its corpus (code, design docs, transcripts, textbooks, Z specs) is exactly the technical
distribution where off-the-shelf rerankers backfire. The on-target, lower-risk fix is at the
**embedding**.

Direction:

1. **Primary lever — context-aware embeddings.** Contextual embeddings (works with the
   current model; LLM at ingest) *or* late chunking (cheaper; needs a long-context model).
   Plus cheap **metadata handling** (down-weight/exclude frontmatter, changelog lines, TOC).
2. **Keep RRF k=60.** It is the validated, calibration-free floor.
3. **Reranker = optional, domain-validated add-on**, considered only *after* embeddings are
   fixed, and kept only if it is net-positive on quarry's own corpus.
4. **Skip** multi-vector/late-interaction and adaptive-RRF (refuted).

### The real fork (an open question)

Late chunking requires a **long-context, mean-pooling** embedder. `arctic-embed-m` is
**512-token** — too short to embed a whole document before per-chunk pooling — so late
chunking likely means a **model swap** (e.g. Jina-v3 or nomic-embed, 8192-token). Contextual
embeddings work with the *current* model but cost an LLM call per chunk at ingest. Re-embed
cost is not a constraint; the decision is model-swap vs ingest-time LLM. The harness (below)
decides it with data.

---

## 5. Plan (staged, quality-first)

1. **Build the eval harness first** (§6). Retrieval quality is strongly domain-dependent and
   benchmark magnitudes **do not transfer** — every lever must be measured on quarry's corpus.
2. **A/B the primary lever**: baseline vs +contextual-embeddings vs +late-chunking vs
   +metadata-handling, on the frozen harness.
3. **Keep RRF k=60.**
4. **A/B a local reranker** as an optional second stage; keep only if net-positive here.
5. **Record the outcome** as a DESIGN.md ADR once a direction wins on the numbers.

Sequencing note: steps 2 and 4 both require step 1. The harness is the gate, not a side task.

---

## 6. The evaluation harness — high-level plan

The single most important discipline this research surfaced: **you cannot trust a retrieval
number you did not measure on your own corpus.** The harness turns "this should help" into
"this moved nDCG@10 from X to Y on our data." It is done well when it is *reproducible*,
*labeled on our data*, *A/B by construction*, and *honest about small samples*.

### 6.1 Components

**a. Query set** — representative queries over the real corpus (course material, quarry code,
design docs, transcripts, agent memories). Three buckets:

- *Natural questions* — how a human/agent actually searches ("why does sync stay live during
  indexing", "predicate logic in Z", "how are chunks flushed").
- *Regression cases* — the known failures, kept as permanent guards: the cookie/ruby
  fake-`1.00` queries (already fixed) and **"Predicate Logic in Z"** (frontmatter/changelog
  outranking substance). A fix is not a fix until these regression queries pass.
- *Query-type spread* — conceptual ("why/how"), known-item lookup, and keyword-ish, so we do
  not tune for one shape and regress another.

**b. Relevance judgments (qrels)** — for each query, which chunks/documents are relevant,
graded 0–3. This is the hard, valuable part. Layered approach:

- *Human gold set* (small, high-trust): the operator judges a few dozen query→passage pairs.
  This is the calibration anchor.
- *LLM-as-judge* (scaled): grade query↔passage relevance with an LLM, **calibrated against
  the human gold set** (report judge-vs-human agreement; do not trust the judge blind).
- *Known-item anchors* (cheap): for queries whose answer doc is known ("predicate logic in Z"
  → the Spivey predicate-logic chapter / tutorial 02), label the target directly.

**c. Metrics** — report several; no single number:

- **nDCG@10** — primary. Graded, position-aware; rewards putting the *most* relevant passage
  highest, which is exactly our failure mode.
- **Recall@50** — candidate-pool health. Answers "is the good passage even retrieved before
  ranking?" A reranker cannot fix what retrieval never surfaced.
- **MRR@10** — rank of the first relevant result.
- **Metadata-pollution@10** (quarry-specific) — fraction of the top-10 that are
  frontmatter/changelog/TOC/heading chunks. A direct meter for *our* named failure; a fix
  should drive this toward zero without hurting nDCG.

**d. A/B protocol** — freeze the query set and qrels; run each configuration (baseline,
+contextual-embeddings, +late-chunking, +metadata-handling, +reranker) through the **same**
queries against the **same** judgments; re-index per configuration (cost is not a constraint).
Report **per-query win/loss**, not just averages — small query sets hide regressions in the
mean. State the sample size next to every delta.

**e. Reproducibility** — a `quarry eval` entry point: `config + query-set + qrels → metrics
report`. Deterministic. The query set and qrels are **committed artifacts** so results are
comparable across time and the regression cases run in CI.

### 6.2 How it is done well (guardrails against self-deception)

- **Label on our data, not a benchmark.** BM25 beating dense on financial docs, and rerankers
  hurting on technical corpora, are the proof that magnitudes do not transfer. Our corpus is
  the only valid test.
- **Calibrate the LLM judge against a human gold set** before trusting it at scale; report the
  agreement rate.
- **Per-query reporting + hold-out.** Never tune on the test set; watch for small-sample noise
  (the weakest studies in the research were tiny-subset, small-margin evals).
- **Regression queries are permanent.** The known failures become fixtures; the harness is
  also a regression guard, not just a one-time A/B.
- **One metric can't win alone.** A lever that lifts nDCG@10 but tanks Recall@50, or cuts
  metadata-pollution but hurts nDCG, is not a win.

### 6.3 Rough shape (for the design pass to refine)

- Phase A: assemble query set (3 buckets) + human gold qrels + known-item anchors.
- Phase B: LLM-judge scaffolding, calibrated to the gold set.
- Phase C: `quarry eval` harness computing nDCG@10 / Recall@50 / MRR@10 / metadata-pollution.
- Phase D: baseline numbers on the current stack (the reference point everything is measured
  against).
- Phase E: A/B each lever (§5 step 2, then step 4).

The detailed technical design (LLM-judge prompts, qrels schema, harness API, corpus sampling)
is delegated to the search specialist once this direction is ratified.

---

## 7. Open questions

- **arctic-embed compatibility with late chunking** — 512-token cap vs the long-context,
  mean-pooling requirement. Confirm whether document-window batching suffices or a model swap
  is required.
- **Metadata handling** — none of the verified sources tested structural/metadata filtering
  directly; it may be the cheapest win of all and should be A/B'd alongside the embedding
  levers, not assumed.
- **Contextual embeddings vs late chunking** on quarry's *self-indexing agent-memory* corpus —
  does the ~1.5–1.9pp BeIR gain hold, shrink, or grow here?
- **Local reranker net-effect** on quarry's mixed corpus — the benchmarks split by domain, so
  this needs a corpus-specific A/B before adoption.

---

## 8. Sources

Confidence and adversarial-verification votes from the research pass.

- turbopuffer hybrid search + RRF + external reranker positioning — <https://turbopuffer.com/docs/hybrid> (3-0)
- Cross-encoder largest lever when domain-matched; RRF vs weighted fusion — T2-RAGBench, <https://arxiv.org/html/2604.01733v1> (3-0)
- Cross-encoders degrade on technical corpora; local BGE-small pipeline favors embeddings+chunking over reranking — <https://arxiv.org/pdf/2604.15484> (2-1, direction well-corroborated)
- Named reranker failure modes (noisy / semantically-similar-but-irrelevant / counterfactual) — SciRerankBench, <https://arxiv.org/pdf/2508.08742> (3-0)
- Anthropic Contextual Retrieval (−35% / −49% / −67%) — <https://www.anthropic.com/news/contextual-retrieval> (3-0)
- Late chunking mechanism + cosine 0.7084→0.8249 + gain largest at small chunk sizes — Jina, <https://arxiv.org/pdf/2409.04701> (3-0)
- Reranking necessary for contextual chunking to pay off consistently — <https://arxiv.org/abs/2504.19754> (3-0)

### Caveats

Several load-bearing numbers rest on single, non-peer-reviewed 2026 arXiv preprints
(T2-RAGBench, the local BGE-small study, SciRerankBench). The domain-dependence of every
result is precisely why the eval harness (§6) leads: we validate on quarry's own corpus rather
than trusting benchmark point estimates. Reranker model versions move fast — the architectural
pattern is stable, the specific model recommendations are not.
