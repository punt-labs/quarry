---
paths:
  - "src/**"
  - "tests/**"
  - "tools/**"
---

# Delegation with Missions

All code delegation uses ethos missions. Every non-trivial delegation has two phases: (1) **design mission** — describes the problem, constraints, and invariants but does NOT prescribe a write set; (2) **implementation mission** — uses the write set produced by the design phase. The design mission's output IS the write set — the specialist decides what to create, split, or extract. This is critical: prescribing a write set before design prevents refactoring and forces code into existing modules (which is how `__main__.py` reached 2,008 lines).

**Every implementation mission MUST direct the worker to make a real OO improvement on the files it touches** — sized to the opportunity (extract a class, split a god module, internalize public attributes, cut complexity), not minimal ratchet-clearing. The ratchet is debt amortization; every mission pays some down (see the "debt amortization" note in [`.claude/rules/code-quality.md`](code-quality.md)). A worker that offsets a small addition with a micro-simplification, or hunts for `module_size` headroom to dodge an extraction, has missed the point. Purity is not a goal: an adjacent improvement riding along the mission's diff is welcome.

## Why these pairings

Quarry spans four technical domains that require distinct expertise: (1) **ML/numerical** — ONNX embedding, quantization, GPU dispatch, search algorithm design — owned by `kpz` because these are inference pipeline and hardware abstraction problems; (2) **data infrastructure** — LanceDB schema, migrations, chunk storage, agent memory — owned by `rmh` because these are Python data-layer problems with strict type contracts; (3) **network trust** — TLS cert generation, pinned CA contexts, HTTP API contracts — owned by `djb` because TLS semantics are security-critical and the bug class history proves subtle mistakes recur; (4) **user surface** — CLI commands, install scripts, system service lifecycle — split between `mdm` (CLI design) and `adb` (infrastructure/service).

| Task type | Worker | Evaluator |
|-----------|--------|-----------|
| Embedding pipeline / ONNX provider selection | `kpz` (Karpathy) | `rmh` (Hettinger) |
| Quantization, GPU/CPU dispatch, model loading | `kpz` | `gvr` (van Rossum) |
| Search algorithm (hybrid, RRF, temporal decay, BM25) | `kpz` | `rmh` |
| LanceDB schema / chunks table / migrations | `rmh` | `gvr` |
| Python implementation (CLI commands, library API) | `rmh` | `gvr` |
| MCP server (stdio) | `rmh` | `mdm` (Pike) |
| HTTP API / `/search` endpoint / param contracts | `rmh` | `djb` (Bernstein) |
| TLS / cert generation / pinned-CA contexts | `djb` | `rmh` |
| Install scripts / launchd / systemd service | `adb` (Lovelace) | `djb` |
| Agent memory: identity tagging, summary, decay | `rmh` | `kpz` |
| Document loaders / format ingestion (20+ types) | `gvr` | `rmh` |
| CLI surface (`quarry find`, `ingest`, `remember`) | `mdm` | `rmh` |
| Performance / latency / index-build benchmarks | `kpz` | `adb` |

## Pipeline selection

Use `standard` pipeline (design → implement → test → review) for any change touching `/search`, the embedding pipeline, TLS, or work that crosses the local/remote boundary. Use `quick` (implement → review) only for documented bugfixes inside a single module that don't cross boundaries. Apply the five bug classes from [`.claude/rules/testing.md`](testing.md) as evaluator checklists on every review. Review-cycle fix rounds (Qodo/Bugbot findings) use bare `Agent()`, not missions.
