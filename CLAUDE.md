# Quarry

Local semantic search for AI agents and humans. Indexes 20+ document formats, embeds with a local ONNX model (snowflake-arctic-embed-m-v1.5, 768-dim), stores vectors in LanceDB, serves via MCP (stdio or WebSocket daemon on port 8420).

## Architecture

- **Embedding**: ONNX Runtime with snowflake-arctic-embed-m-v1.5. int8 on CPU (default), FP16 on CUDA (auto-detected). See DES-004 and DES-016 in DESIGN.md.
- **Storage**: LanceDB (Rust core via PyO3). Single `chunks` table per database with vector, text, and metadata columns.
- **Search**: Hybrid search — vector similarity + BM25 full-text (Tantivy) fused via RRF. Temporal decay for agent-scoped memories. See DES-017 in DESIGN.md.
- **Agent memory**: `agent_handle`, `memory_type`, `summary` columns on all chunks. Identity tagging from ethos config. See DES-018 in DESIGN.md.
- **Surfaces**: CLI (`quarry`), MCP server (stdio + WebSocket), HTTP API, Claude Code plugin with slash commands.
- **User data**: `~/.punt-labs/quarry/` per filesystem standard. Per-repo config at `.punt-labs/quarry/config.md`.

## Project-Specific Conventions

- **Quality gates**: always use `make check` — never ad-hoc individual lint/type/test commands.
- `make check` = `make lint` + `make type` + `make test`
- `make docs` builds all LaTeX documents (prfaq, architecture, Z spec). PDFs are committed.
- `make metrics` — ABC complexity analysis. Any module over magnitude 200 needs attention.
- `make coverage` — test coverage with HTML report in `htmlcov/`.
- **Full test suite** needs `timeout=300000` on the Bash tool (5 minutes). During development, use targeted tests: `uv run pytest tests/test_specific.py -v`.
- **Never retry a command that produces no output.** Diagnose first.

## Code Quality Standards

**Module size limits.** No module over 500 lines without a design reason. Known violations: `__main__.py` (2,008), `pipeline.py` (1,589), `http_server.py` (1,530), `doctor.py` (1,141), `database.py` (925), `hooks.py` (868), `sync.py` (660), `mcp_server.py` (581). When a module grows past the limit, the next change to that module must include extraction.

**Class design.** Classes have a single responsibility. Prefer composition over inheritance. Use `Protocol` for structural typing at boundaries. A module with zero classes and 20+ module-level functions is procedural — it needs a design pass, not more functions.

**Function design.** Functions that share a pattern signal a missing abstraction. Extract the pattern after the third occurrence. Use `make metrics` to measure ABC complexity — high-magnitude functions need decomposition.

**No copy-paste.** If the same structure appears a third time, extract it. Three similar functions is not "better than a premature abstraction" when the pattern is proven.

**Design-first delegation.** Every non-trivial delegation has two phases: (1) design mission — describes the problem, constraints, and invariants, does NOT prescribe a write set; (2) implementation mission — uses the write set produced by the design phase. The specialist decides what to create, split, or extract based on quality standards. Never skip the design phase.

## Testing

### Pyramid (1559 tests collected, 22 deselected in CI)

| Layer | Count | Make target | Runs in CI | Coverage |
|-------|-------|-------------|------------|----------|
| Unit | ~1530 | `make test` | yes | DB, embedding, search, CLI, doctor, hooks, enable/disable, service, install scripts |
| Integration | 22 | `make test-integration` | no (needs real model) | Real filesystem + ONNX model |
| Shell scripts | ~18 | `make test` (via pytest) | yes | Install script ordering, shellcheck |
| HTTP API contract | partial | `make test` | yes | Endpoint shape/param, growing |
| Wheel install | 6 | `make test-wheel` | local pre-PR gate | Build wheel, install in isolated venv, run on port 8422 alongside prod 8420 |
| MCP smoke test | 0 automated | qae agent + `docs/smoke-test.md` | post-release manual | 35 checks: all MCP tools + CLI mirror + install verification |

**Make targets:**

- `make check` = `make lint` + `make type` + `make test` (CI gate)
- `make test-integration` = pytest with `--run-slow` (local only, needs real model)
- `make test-wheel` = build wheel → isolated venv → `quarry serve --port 8422` → smoke checks → teardown (local pre-PR gate)
- `make check-full` = `make check` + `make test-wheel` (local pre-PR gate)
- `make build` = `uv build` + `twine check` (existing)

### Recurring bug classes from code review (quarry-ccji-tls, 10 rounds)

Ten review cycles on the TLS remote-access feature revealed five classes of bugs that appeared repeatedly. Each class points to a testing gap that must be closed with any future change in that area.

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

### Rules

1. **No new `os.open()`/`os.fdopen()` pattern without a failure-injection test** covering fd closure and temp file cleanup.
2. **No new `(bool, str)` return function without a raises-then-returns-false test.**
3. **No new CLI filter param without a matching HTTP server test** asserting the param reaches the database query.
4. **No new remote code path without an equivalence test** asserting JSON field names match the local path.
5. **No new cert generation call without asserting** SAN type (IP vs DNS), `not_valid_before` is in the past, and pinned context excludes system roots.
6. **Shell scripts must pass `shellcheck -x` in CI.** Logic tests via mock quarry binary for any script with conditional branching on quarry subcommand results.

## Ethos & Delegation

Identity: `agent: claude` per `.punt-labs/ethos.yaml`. Sub-agent calls (`Agent(subagent_type=…)`) match ethos identity handles.

Quarry is Python with a heavy ML core (ONNX embeddings, LanceDB vectors), a hybrid search algorithm (vector + BM25 + RRF + temporal decay), a multi-surface API (CLI, MCP stdio + WebSocket, HTTP), and a TLS remote-access feature with a long history of subtle bug classes (see Testing section). Every domain has a clear specialist pair. Within each row, the worker and evaluator must be distinct handles. Claude is the leader, never the evaluator.

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

Apply the five recurring bug classes from the Testing section as evaluator checklists — file I/O safety, exception boundaries, remote/local divergence, TLS semantics, install-script logic. Use the `standard` pipeline for any change touching `/search`, the embedding pipeline, or TLS. Use `quick` only for documented bugfixes inside a single module that doesn't cross the local/remote boundary.

## Key Design Documents

- `DESIGN.md` — ADR log (DES-001 through DES-029)
- `docs/architecture.tex` → `docs/architecture.pdf` — system architecture, module responsibilities, search and retrieval, deployment
- `prfaq.tex` → `prfaq.pdf` — product direction and risk assumptions
- `docs/improving-agent-memory.md` — agent memory design rationale
- `docs/provider-detection-design.md` — ONNX provider auto-detection design

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
