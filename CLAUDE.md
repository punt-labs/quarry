# Quarry

Part of [Punt Labs](https://github.com/punt-labs). This repo must be checked out inside the `punt-labs/` workspace meta-repo so that org-wide configuration loads via Claude Code's ancestor directory walk:

- **`punt-labs/CLAUDE.md`** — org workflow, delegation model, beads issue tracking, tool configuration
- **`punt-labs/.claude/rules/python-*.md`** — 19 Python OO coding rules, scoped via `paths:` frontmatter (load on-demand when `.py` files are touched)
- **`punt-labs/.envrc`** — git identity, beads DB connection, API keys from platform keychain
- **`punt-kit/standards/`** — canonical reference docs

If cloned outside the workspace, these rules and configuration will not be present.

**OO Python standards adopted 2026-05-13.** The codebase does not yet fully comply. Every commit must improve OO scores (`make check-oo`), never regress. Do not match existing code patterns that violate the rules — write new code to the standard and improve touched files incrementally.

Local semantic search for AI agents and humans. Indexes 20+ document formats, embeds with a local ONNX model (snowflake-arctic-embed-m-v1.5, 768-dim), stores vectors in LanceDB, serves via MCP (stdio or WebSocket daemon on port 8420).

- **Package**: `punt-quarry`
- **CLI**: `quarry`
- **MCP server**: `quarry-server`
- **Python**: 3.13+, managed with `uv`

## Architecture

### How a query works

A user (human or agent) issues a search via any surface (CLI, MCP, HTTP, plugin). The query hits `search.py` which runs hybrid search: (1) vector similarity via the ONNX embedding model against LanceDB, (2) BM25 full-text via Tantivy, (3) results fused via Reciprocal Rank Fusion. Agent-scoped memories apply temporal decay — recent memories rank higher. Results return as ranked chunks with source metadata.

### How ingestion works

Documents enter via `pipeline.py`. The pipeline detects format (20+ types via `loaders/`), extracts text, splits into chunks, generates embeddings via ONNX Runtime, and writes vectors + metadata to LanceDB. Directory registration (`sync.py`) tracks which paths to re-index on change.

### Key architectural boundary: local vs. remote

Quarry has two operational modes. **Local mode**: direct LanceDB access via `database.py`. **Remote mode**: HTTP client → `http_server.py` → same database layer. The HTTP API must be a faithful proxy of every local operation — same parameters, same response fields, same behavior. Bug class 3 (remote/local divergence) documents the repeated failure mode where these paths drift. Every new query parameter or response field must exist on both paths simultaneously.

### Subsystems

- **Embedding**: ONNX Runtime with snowflake-arctic-embed-m-v1.5. int8 on CPU (default), FP16 on CUDA (auto-detected). See DES-004, DES-016.
- **Storage**: LanceDB (Rust core via PyO3). Single `chunks` table per database with vector, text, and metadata columns.
- **Search**: Hybrid — vector similarity + BM25 full-text (Tantivy) fused via RRF. Temporal decay for agent-scoped memories. See DES-017.
- **Agent memory**: `agent_handle`, `memory_type`, `summary` columns on all chunks. Identity tagging from ethos config. See DES-018.
- **Surfaces**: CLI (`quarry`), MCP server (stdio + WebSocket), HTTP API, Claude Code plugin.
- **User data**: `~/.punt-labs/quarry/` per filesystem standard. Per-repo config at `.punt-labs/quarry/config.md`.

### Key modules

| Module | Responsibility |
|--------|---------------|
| `pipeline.py` | Ingestion: format detection → chunking → embedding → LanceDB write |
| `database.py` | LanceDB operations: table creation, writes, queries, migrations |
| `search.py` | Hybrid search: vector + BM25 + RRF fusion, temporal decay |
| `embedding.py` | ONNX provider: model loading, quantization, batch embedding |
| `http_server.py` | REST API: must mirror every local operation faithfully |
| `mcp_server.py` | FastMCP server (stdio + WebSocket on port 8420) |
| `sync.py` | Directory registration, change tracking, re-indexing |
| `doctor.py` | Health checks: model, DB, providers, registration state |
| `hooks.py` | Claude Code event handlers (SessionStart, PostToolUse) |
| `__main__.py` | Typer CLI: find, ingest, remember, sync, serve, doctor, etc. |

See `docs/architecture.tex` for the full system description.

## Code Quality

**Module size limits.** No module over 500 lines without a design reason. Known violations: `__main__.py` (2,008), `pipeline.py` (1,589), `http_server.py` (1,530), `doctor.py` (1,141), `database.py` (925), `hooks.py` (868), `sync.py` (660), `mcp_server.py` (581). When a module grows past the limit, the next change to that module must include extraction.

**Class design.** Classes have a single responsibility. Prefer composition over inheritance. Use `Protocol` for structural typing at boundaries. A module with zero classes and 20+ module-level functions is procedural — it needs a design pass, not more functions.

**Function design.** Functions that share a pattern signal a missing abstraction. Extract the pattern after the third occurrence. Use `make metrics` to measure ABC complexity — high-magnitude functions need decomposition.

**No copy-paste.** If the same structure appears a third time, extract it.

**Known pyright debt:** 6 `reportUnknown*` checks are suppressed project-wide because lancedb, rapidocr, onnxruntime, fitz, and pyarrow ship no type stubs. This means pyright cannot catch unknown-type bugs in modules that don't import these libraries either. The suppressions should be narrowed as these libraries add stubs. Pyright's `executionEnvironments` scopes by directory, not by import, so the only current alternative is 591 inline `# pyright: ignore` comments.

**OO ratchet:** `make check-oo` (part of `make check`) compares current OO scores against `.oo-baseline.json`. It passes only if no metric regressed on touched files and at least one metric improved. It fails if any metric got worse or nothing improved. This is how the codebase converges to the OO standard — every commit ratchets forward.

Workflow:

1. Write code that improves OO quality on the files you touch.
2. `make check` runs `check-oo --check` automatically. If it fails, fix the regression.
3. After all checks pass, run `make update-oo` to write the new baseline.
4. Stage `.oo-baseline.json` and `.oo-audit.jsonl` with your commit — they are committed files.

Bootstrap (first time only): run `make update-oo` to create the initial baseline. After that, the ratchet is active.

**Do not negotiate with the ratchet.** Do not edit `.oo-baseline.json` by hand. Do not suppress `check-oo`. Do not argue a regression is "acceptable." If the ratchet fails, improve the code until it passes. The ratchet is the quality standard's enforcement — working around it defeats the purpose.

**The ratchet is debt amortization, not a limbo bar.** Its purpose is to pay the codebase's OO debt down incrementally: every commit should *fund a medium-scale improvement* — to the file you're touching or to unrelated code nearby — the way you amortize a loan a chunk at a time. This deliberately takes on additional scope, and that is intended. Do **not** treat the ratchet as a constraint to squeak under. Offsetting a two-line addition with a micro-simplification, hunting for `module_size` headroom to avoid an extraction, or gaming a single metric by the minimum all satisfy the letter and waste the intent — and they burn time. When you open a file, make a *real* improvement sized to the opportunity (extract a class, split a god module, internalize public attributes, cut a complex function down), not the smallest change that clears the gate. Bias toward making the improvement, never toward avoiding it.

**Org standards override review tools.** Copilot, Bugbot, and Cursor are advisory. When a review suggestion conflicts with rules in `../.claude/rules/python-*.md`, the rules win. Read the rules before accepting a reviewer's suggestion. PY-CC-1 (`__new__` as constructor) is the most common conflict.

**Verify outputs, not just metrics.** After writing a file, open it and read the content. After backfilling transcripts, search them and confirm the results make sense. `make check` passing does not mean the feature works — it means the code compiles and tests pass. Those are necessary but not sufficient.

**Metrics tools:**

- `make check-oo` — OO ratchet against baseline (11 metrics: method_ratio, encapsulation, params, complexity, module size, class ratios, init violations, public attribute violations, future_annotations).
- `make update-oo` — update baseline and append to audit log after improvements.
- `make report` — full diagnostics including per-file OO breakdown (no fail-fast).
- `make metrics` — ABC complexity analysis. Any module over magnitude 200 needs attention.
- `make coverage` — test coverage with HTML report in `htmlcov/`.
- `make check-coupling` — coupling/cohesion analysis (efferent coupling between modules, public API surface, circular import detection, LCOM class cohesion). Informational — not in the `check` chain yet.
- `make update-coupling` — update coupling baseline after improvements.

### Database facade convention

Functions in `src/quarry/ingestion/pipeline.py` and `src/quarry/ingestion/url_ingester.py` accept `database: Database`, NOT `db: LanceDB`. Callers pass their existing `Database` instance — don't extract `.db` to pass the raw LanceDB connection. Re-wrapping via `Database(db)` re-instantiates the full facade (ChunkStore, ChunkSearch, ChunkCatalog, SchemaManager, TableOptimizer) per call. (Cursor Bugbot flagged this on PR #289; the fix landed in the same PR.)

**When mocking `get_db` in tests, patch `quarry.db.facade.get_db`, not `quarry.db.storage.get_db`.** `Database.connect()` imports `get_db` at module scope into `quarry.db.facade`'s namespace. Patching the storage definition site leaves the facade's bound reference untouched and the mock becomes a silent no-op — tests still pass because they hit the real LanceDB.

### Suppression ratchet uses `tokenize`, not regex/AST heuristic

`tools/suppression_ratchet.py` counts suppressions by walking `tokenize` tokens. A line is "code" iff it carries any non-trivial token; a `COMMENT` token is the only real suppression source. Don't revert to the older AST + `_CODE_START_RE` heuristic — it had documented blind spots (`async def`, attribute assignments, tuple targets, triple-quoted single-line docstrings containing `# noqa` text). The tokenize version is the principled implementation that handles all of those correctly.

## Testing

### Pyramid

| Layer | Make target | Runs in CI | What it covers |
|-------|-------------|------------|----------------|
| Unit | `make test` | yes | DB, embedding, search, CLI, doctor, hooks, enable/disable, service, install scripts |
| Integration | `make test-integration` | no (needs real ONNX model) | Real filesystem + ONNX model end-to-end |
| Shell scripts | `make test` (via pytest) | yes | Install script ordering, shellcheck |
| HTTP API contract | `make test` | yes | Endpoint shape, params, response fields (growing) |
| Wheel install | `make test-wheel` | local pre-PR gate | Build wheel → isolated venv → serve on 8422 → smoke checks |
| MCP smoke test | `docs/smoke-test.md` | post-release manual | 38 checks (14 MCP + 17 CLI + 7 enable/disable) + install verification |

`make check-full` = `make check` + `make test-wheel`. Full test suite needs `timeout=300000` on the Bash tool (5 minutes). During development, use targeted tests: `uv run pytest tests/test_specific.py -v`.

### What good testing means in this project

Quarry has four surfaces (CLI, MCP, HTTP, plugin) backed by the same core. Every feature must work on all surfaces or explicitly document which surfaces it applies to. The recurring failure mode is surfaces drifting — a parameter added to the CLI but missing from the HTTP API, or a response field present locally but omitted remotely. The testing rules below exist because these bugs appeared repeatedly and were expensive to find.

**Never retry a command that produces no output.** Diagnose first — empty output usually means a silent exception or a missing code path, not a transient failure.

### Recurring bug classes (quarry-ccji-tls, 10 review rounds)

Ten review cycles on the TLS remote-access feature revealed five classes of bugs that appeared repeatedly. Each class points to a testing gap that must be closed with any future change in that area. These are evaluator checklists — every code review must check for these.

**Class 1 — File I/O safety.** `os.write()` is not guaranteed to write all bytes. `os.fdopen()` can raise before taking ownership of the fd, leaking it. Atomic rename must be inside the try block or the temp file leaks on failure. Permissions race: creating a file then chmoding it leaves a window.

*Required tests:* Every function that uses `os.open()`/`os.fdopen()` must have tests covering (a) successful write, (b) fd explicitly closed when `os.fdopen()` raises, (c) temp file removed on any write failure, (d) file created with correct mode from the start (not chmod after). Mock `os.fdopen` to raise and assert the fd is closed and the temp file is gone.

**Class 2 — Exception boundaries.** Functions that promise `(bool, str)` or a clean fallback can silently propagate exceptions when a dependency raises before the `try` block. `ssl_ctx.load_verify_locations()` outside the try block crashes instead of returning `(False, reason)`. `read_proxy_config()` raising `ValueError` on a malformed TOML crashes CLI commands that should fall back to local mode. Install scripts that do not gate on subprocess exit codes print success after failure.

*Required tests:* Every function returning `(bool, str)` must have a test that makes the underlying call raise and verifies the function returns `(False, <non-empty string>)` rather than propagating. Every CLI command that reads optional config must have a test with malformed config that verifies fallback (exit 0, warning printed) not crash.

**Class 3 — Remote/local divergence.** The same logical operation (e.g. `quarry find`) has two code paths: local (DB) and remote (HTTP). These paths drift: the HTTP `/search` endpoint used the vector-only `search()` while the CLI used `hybrid_search()`; the `/search` route ignored `agent_handle`, `memory_type`, `document` params that the CLI sent; the remote JSON response omitted `page_number`, `page_type`, `source_format` that the local response included.

*Required tests:* For every CLI command with a remote path, write an equivalence test: call the command twice (once mocked to local, once mocked to remote HTTP), assert the JSON output contains exactly the same field names. For every query param the CLI encodes into the URL, write an HTTP server test asserting the server reads that param and passes it to the database query. A new filter on the local path must fail a test until it is also on the remote path.

**Class 4 — TLS semantics.** IP addresses require `x509.IPAddress()`, not `x509.DNSName()` — TLS clients reject the latter per RFC 5280. `not_valid_before(now)` causes "not yet valid" rejections on clients with minor clock skew; certificates should backdate by at least 5 minutes. A new CA cert context must exclude system roots entirely (`ssl.PROTOCOL_TLS_CLIENT` + `load_verify_locations` only) — using `ssl.create_default_context()` accepts any system-trusted cert, defeating pinning. CA cert and key must be verified to match before reusing them.

*Required tests:* Cert generation tests must assert: (a) IP hostnames produce `x509.IPAddress` SANs, not `x509.DNSName`; (b) `not_valid_before` is at least 1 second in the past relative to `datetime.now(UTC)`; (c) the SSL context used for pinned-CA connections has no system roots (verify by checking `ctx.verify_mode == CERT_REQUIRED` and that `ctx.get_ca_certs()` returns only the pinned cert); (d) mismatched CA cert/key raises `ValueError` before any cert is written.

**Class 5 — Install script logic.** Shell scripts have no test coverage beyond shellcheck. Logic bugs — checking API key after a slow download, service registering on loopback while the script runs on 0.0.0.0, never creating `quarry.toml` so the plugin silently falls back — are invisible to shellcheck and only caught by manual testing or Bugbot.

*Required tests:* At minimum, every install script must pass `shellcheck -x`. For logic correctness: write integration tests that invoke the scripts with a mock `quarry` binary (a shell function that records its invocations and returns success/failure). Assert: (a) QUARRY_API_KEY is checked before any slow step; (b) the service command baked into launchd/systemd includes `--host 0.0.0.0` when `QUARRY_SERVE_HOST=0.0.0.0` is set; (c) the script exits non-zero when the daemon fails to start; (d) `quarry login localhost --yes` is called after the daemon starts.

### Testing rules

1. **No new `os.open()`/`os.fdopen()` pattern without a failure-injection test** covering fd closure and temp file cleanup.
2. **No new `(bool, str)` return function without a raises-then-returns-false test.**
3. **No new CLI filter param without a matching HTTP server test** asserting the param reaches the database query.
4. **No new remote code path without an equivalence test** asserting JSON field names match the local path.
5. **No new cert generation call without asserting** SAN type (IP vs DNS), `not_valid_before` is in the past, and pinned context excludes system roots.
6. **Shell scripts must pass `shellcheck -x` in CI.** Logic tests via mock quarry binary for any script with conditional branching on quarry subcommand results.

## Ethos & Delegation

Identity: `agent: claude` per `.punt-labs/ethos.yaml`. Sub-agent calls (`Agent(subagent_type=…)`) match ethos identity handles.

All code delegation uses ethos missions. Every non-trivial delegation has two phases: (1) **design mission** — describes the problem, constraints, and invariants but does NOT prescribe a write set; (2) **implementation mission** — uses the write set produced by the design phase. The design mission's output IS the write set — the specialist decides what to create, split, or extract. This is critical: prescribing a write set before design prevents refactoring and forces code into existing modules (which is how `__main__.py` reached 2,008 lines).

**Every implementation mission MUST direct the worker to make a real OO improvement on the files it touches** — sized to the opportunity (extract a class, split a god module, internalize public attributes, cut complexity), not minimal ratchet-clearing. The ratchet is debt amortization; every mission pays some down (see the "debt amortization" note in Code Quality). A worker that offsets a small addition with a micro-simplification, or hunts for `module_size` headroom to dodge an extraction, has missed the point. Purity is not a goal: an adjacent improvement riding along the mission's diff is welcome.

### Why these pairings

Quarry spans four technical domains that require distinct expertise: (1) **ML/numerical** — ONNX embedding, quantization, GPU dispatch, search algorithm design — owned by `kpz` because these are inference pipeline and hardware abstraction problems; (2) **data infrastructure** — LanceDB schema, migrations, chunk storage, agent memory — owned by `rmh` because these are Python data-layer problems with strict type contracts; (3) **network trust** — TLS cert generation, pinned CA contexts, HTTP API contracts — owned by `djb` because TLS semantics are security-critical and the bug class history proves subtle mistakes recur; (4) **user surface** — CLI commands, install scripts, system service lifecycle — split between `mdm` (CLI design) and `adb` (infrastructure/service).

| Task type | Worker | Evaluator |
|-----------|--------|-----------|
| Embedding pipeline / ONNX provider selection | `kpz` (Karpathy) | `rmh` (Hettinger) |
| Quantization, GPU/CPU dispatch, model loading | `kpz` | `gvr` (van Rossum) |
| Search algorithm (hybrid, RRF, temporal decay, BM25) | `kpz` | `rmh` |
| LanceDB schema / chunks table / migrations | `rmh` | `gvr` |
| Python implementation (CLI commands, library API) | `rmh` | `gvr` |
| MCP server (stdio + WebSocket on port 8420) | `rmh` | `mdm` (Pike) |
| HTTP API / `/search` endpoint / param contracts | `rmh` | `djb` (Bernstein) |
| TLS / cert generation / pinned-CA contexts | `djb` | `rmh` |
| Install scripts / launchd / systemd service | `adb` (Lovelace) | `djb` |
| Agent memory: identity tagging, summary, decay | `rmh` | `kpz` |
| Document loaders / format ingestion (20+ types) | `gvr` | `rmh` |
| CLI surface (`quarry find`, `ingest`, `remember`) | `mdm` | `rmh` |
| Performance / latency / index-build benchmarks | `kpz` | `adb` |

### Pipeline selection

Use `standard` pipeline (design → implement → test → review) for any change touching `/search`, the embedding pipeline, TLS, or work that crosses the local/remote boundary. Use `quick` (implement → review) only for documented bugfixes inside a single module that don't cross boundaries. Apply the five bug classes from the Testing section as evaluator checklists on every review. Review-cycle fix rounds (Copilot/Bugbot findings) use bare `Agent()`, not missions.

## Development Loop

Two nested loops govern all code changes. See `punt-kit/standards/pr-review.md` for the authoritative reference.

### Inner loop — one mission

Execute after every agent delegation that produces sizeable code changes.

1. **Delegate** to the right ethos specialist (see pairing table above). Do not use bare `Agent()` for implementation work.
2. **`make check`** — must pass before proceeding. Zero exceptions.
3. **`make install`** — builds wheel and installs it locally. `make check` passing is not installation.
4. **`make test`** against the installed artifact — not from source.
5. **`/feature-dev:code-reviewer`** on the mission diff.
6. **`/pr-review-toolkit:silent-failure-hunter`** on the mission diff.
7. **Fix every finding.** To dismiss one: document (a) the exact finding, (b) the specific reason it does not apply, (c) the code reference. "Pre-existing" and "by design" are not reasons.
8. **Re-run both agents.** Exit the fix loop only when both return zero findings.
9. **Exercise manually** — write expected output first, then compare actual. Cover one failure mode, one boundary condition.
10. **Commit.**

### Outer loop — one PR (one rollback-coherent unit)

After all missions for the feature complete and each has passed its inner loop:

1. **`make check`** on the full accumulated diff.
2. **Both local review agents** on the complete diff — cross-mission issues only appear at this level.
3. **Fix all findings** using the same documentation standard.
4. **Human IDE review** of the full diff.
5. **`make install`** then exercise the complete user-facing workflow end-to-end, paste actual output.
6. **Re-run agents** until clean.
7. **Open PR.** A PR opened before step 6 is clean is a procedural violation.

### PR boundaries

Split by **rollback granularity**, not size. Ask: if this broke production, what reverts together? That is one PR. "The diff is large" and "separate concern" are prohibited split reasons. Independent rollback capability and sequential dependency are valid.

**PRs do not need to be "pure," and purity is never a reason to hold back an improvement.** These PRs are agent-reviewed and squash-merged — the whole branch collapses to one commit on `main`, so the "normal fencing" (one-concern-per-PR, keep-the-diff-minimal, split-out-the-unrelated-bit) does not apply. Do not spend time policing scope: a docs tweak, an OO/complexity paydown, or an adjacent bug fix riding along with a feature PR is welcome, not a violation. **The operator explicitly rejects rules that make it harder to improve code.** If you are in a file and can make it better, do it — never revert or defer a genuine improvement to keep a PR "clean," and never open a separate PR solely for purity. The one real constraint is mechanical, not stylistic: when multiple agents share one worktree, don't let them edit the same uncommitted lines simultaneously — sequence them so no one's work is clobbered. That is about not losing work, not about scope.

## Release

Use `/punt:auto release [version=X.Y.Z]`. Quarry is a CLI + Plugin Hybrid — releases publish to both PyPI (`punt-quarry`) and the Claude Code plugin marketplace. Dev plugin testing: `claude --plugin-dir .` loads `quarry-dev` alongside the installed prod plugin.

## Key Documents

- `DESIGN.md` — ADR log (DES-001+). Read before proposing changes to settled architecture.
- `docs/architecture.tex` → `docs/architecture.pdf` — system architecture, module responsibilities, search and retrieval, deployment
- `prfaq.tex` → `prfaq.pdf` — product direction and risk assumptions
- `docs/improving-agent-memory.md` — agent memory design rationale
- `docs/retrieval-quality-improvements.md` — retrieval-quality research + eval-harness plan (active work)
- `docs/smoke-test.md` — post-release manual smoke test
- `docs/README.md` — docs index; `docs/archive/` holds completed build-plans, reviews, and superseded designs mapped to DES-### in DESIGN.md. ONNX provider auto-detection design (formerly `docs/provider-detection-design.md`) is now in `docs/architecture.tex` + DES-016.

<!-- quarry:begin -->
## Quarry

Local semantic search is available via quarry. Use it to search indexed
documents by meaning, ingest new content, and recall knowledge across sessions.

- Before using WebSearch or WebFetch for research, run `/find` with the query
  first. Quarry indexes this codebase, design docs, prior session transcripts,
  and web pages from previous research. If quarry returns relevant results,
  use them — do not re-research what has already been found.
- Use grep for symbol lookups and value lookups; use quarry for "why", "how",
  and "what did we decide about X" questions.
- **Slash commands**: `/find`, `/ingest`, `/remember`, `/explain`, `/source`,
  `/quarry`
- **Research agent**: `researcher` — combines quarry local search with web
  research. Use for deep investigation across local docs and the web.
- **Auto-behaviors**: working directory is auto-indexed at session start;
  URLs fetched via WebFetch are auto-ingested; transcripts are captured before
  context compaction.
- **Search tip**: natural language queries work best ("What were Q3 margins?"
  outperforms "Q3 margins").
<!-- quarry:end -->
