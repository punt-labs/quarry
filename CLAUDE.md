# Quarry

Part of [Punt Labs](https://github.com/punt-labs). This repo must be checked out inside the `punt-labs/` workspace meta-repo so that org-wide configuration loads via Claude Code's ancestor directory walk:

- **`punt-labs/CLAUDE.md`** — org workflow, delegation model, beads issue tracking, tool configuration
- **`punt-labs/.claude/rules/python-*.md`** — 19 Python OO coding rules, scoped via `paths:` frontmatter (load on-demand when `.py` files are touched)
- **`punt-labs/.envrc`** — git identity, beads DB connection, API keys from platform keychain
- **`punt-kit/standards/`** — canonical reference docs

If cloned outside the workspace, these rules and configuration will not be present.

**OO Python standards adopted 2026-05-13.** The codebase does not yet fully comply. Every commit must improve OO scores (`make check-oo`), never regress. Do not match existing code patterns that violate the rules — write new code to the standard and improve touched files incrementally.

Local semantic search for AI agents and humans. Indexes 20+ document formats, embeds with a local ONNX model (snowflake-arctic-embed-m-v1.5, 768-dim), stores vectors in LanceDB, and serves via CLI, an MCP stdio server, and an HTTP API — all backed by the quarryd daemon (port 8420).

- **Package**: `punt-quarry`
- **CLI**: `quarry`
- **MCP server**: `quarry mcp` (stdio subcommand → `QuarryClient` → the quarryd REST daemon); there is no in-daemon `/mcp` WebSocket route and no `quarry-server` entry point
- **Python**: 3.13+, managed with `uv`

## Mandatory Reading

Source-of-truth documents, `@`-imported so they stay in context. Read them
before writing code.

- [`docs/WORKFLOW.md`](docs/WORKFLOW.md) is the authoritative development
  process: three nested loops (backlog → PR → mission), each with pseudocode
  for its control flow and a Z schema for its entry/exit doorway conditions.
  The [Development Loop](#development-loop) section below is a pointer to it,
  not a second copy.
- [`DESIGN.md`](DESIGN.md) is the ADR log (DES-001+); on any conflict about
  settled architecture, it wins. Read it before proposing changes.

@docs/WORKFLOW.md

## On-demand rules

Detail that used to live inline here now loads only when it applies, via
repo-local `.claude/rules/` (path-scoped, auto-loaded when a matching file is
touched) or on-read pointers. This keeps per-session context small without
losing any rule.

- [`.claude/rules/code-quality.md`](.claude/rules/code-quality.md) — module
  size limits, class/function design, the three OO/coupling/suppression
  ratchets (DES-040), the "debt amortization, not a limbo bar" philosophy, the
  database-facade and test-patching conventions. Auto-loads on any `.py`,
  `tools/`, or `Makefile` change.
- [`.claude/rules/testing.md`](.claude/rules/testing.md) — the test pyramid,
  the hermetic-and-bounded suite (DES-047), the five recurring bug classes
  (file I/O, exception boundaries, remote/local divergence, TLS, install
  scripts) as evaluator checklists, and the six testing rules. Auto-loads on
  any test, `conftest.py`, or install-script change.
- [`.claude/rules/delegation.md`](.claude/rules/delegation.md) — the
  design→implementation mission split, the worker/evaluator pairing table, and
  pipeline selection. Read before dispatching a mission.

## Architecture

### How a query works

A user (human or agent) issues a search via any surface (CLI, MCP, HTTP, plugin). Every surface goes through one `SearchService` (the `retrieval/` seam, DES-037), which runs hybrid search: (1) vector similarity (L2-normalized embeddings, cosine metric — DES-038) via the ONNX model against LanceDB, (2) BM25 full-text via Tantivy, (3) results fused via Reciprocal Rank Fusion. Agent-scoped memories apply temporal decay — recent memories rank higher. Results return as ranked chunks with source metadata.

### How ingestion works

Documents enter via `ingestion/pipeline.py`. The pipeline detects format (20+ types via `loaders/`), extracts text, splits into chunks, generates embeddings via ONNX Runtime, and writes vectors + metadata to LanceDB. Directory registration (`sync.py`) tracks which paths to re-index on change. Automatic captures (session transcripts, web fetches) are PII/secret-scrubbed at write time through a single `CaptureWriter` choke point (DES-036).

### Key architectural boundary: local vs. remote

Quarry has two operational modes. **Local mode**: direct LanceDB access via the `db/` package (`Database` facade). **Remote mode**: HTTP client → the FastAPI wire API (`api/` + `daemon/routes/`) → same database layer. The HTTP API must be a faithful proxy of every local operation — same parameters, same response fields, same behavior. Bug class 3 (remote/local divergence) documents the repeated failure mode where these paths drift. Every new query parameter or response field must exist on both paths simultaneously.

### Subsystems

- **Embedding**: ONNX Runtime with snowflake-arctic-embed-m-v1.5. int8 on CPU (default), FP16 on CUDA (auto-detected). See DES-004, DES-016.
- **Storage**: LanceDB (Rust core via PyO3). Single `chunks` table per database with vector, text, and metadata columns.
- **Search**: Hybrid — vector similarity + BM25 full-text (Tantivy) fused via RRF. Temporal decay for agent-scoped memories. See DES-017.
- **Agent memory**: `agent_handle`, `memory_type`, `summary` columns on all chunks. `memory_type` is a closed, server-validated vocabulary (`fact`/`observation`/`opinion`/`procedure`; `lesson` reserved for `learn`; unknown → 400). Identity tagging from ethos config. Schema: DES-018; decay/boost: DES-017; the write loop (`remember` → `memory-<handle>`, `quarry missions sync` for evaluator feedback, SubagentStop report distillation): DES-055.
- **Surfaces**: CLI (`quarry`), MCP server (stdio), HTTP API, Claude Code plugin.
- **User data**: `~/.punt-labs/quarry/` per filesystem standard. Per-repo config at `.punt-labs/quarry/config.md`.

### Key modules

| Module | Responsibility |
|--------|---------------|
| `ingestion/pipeline.py` | Ingestion: format detection → chunking → embedding → LanceDB write |
| `db/` (package) | LanceDB operations behind a `Database` facade (`facade.py`) — `chunk_store.py`, `chunk_search.py`, `chunk_catalog.py`, `schema.py`, `optimizer.py`, `storage.py` |
| `retrieval/` (package) | Single retrieval seam (DES-037): `SearchService`, `HybridRetriever` (vector + BM25 + RRF), `RetrievalConfig`, `reranker.py`, temporal decay |
| `embeddings.py` | ONNX provider: model loading, quantization, batch embedding |
| `scrub.py` / `capture.py` | Write-time PII/secret redaction (`Scrubber`) + the single `CaptureWriter` choke point for captures (DES-036) |
| `api/` + `daemon/routes/` | FastAPI wire API + route handlers: must mirror every local operation faithfully |
| `mcp_server.py` / `mcp_missions.py` / `mcp_guard.py` | FastMCP stdio server (reaches the quarryd REST daemon on port 8420) + the `missions_sync` tool + the shared tool-boundary guard |
| `sync.py` | Directory registration, change tracking, re-indexing |
| `doctor.py` | Health checks: model, DB, providers, registration state |
| `hooks.py` / `hooks_agent.py` | Claude Code event handlers: SessionStart, PostToolUse (WebFetch/WebSearch/Read + quarry tools), PreCompact, SessionEnd, SubagentStop |
| `mission_*.py` / `subagent_*.py` / `ethos_tree.py` | Agent-memory write loop (DES-055): `quarry missions sync` reads the ethos mission YAML trio → `memory-<worker>`; SubagentStop distills a subagent's final report → `memory-<handle>` |
| `memory_types.py` | The `MemoryType` closed vocabulary + server-side validation |
| `__main__.py` | Typer CLI: find, ingest, remember, learn, sync, missions, serve, doctor, etc. |

See `docs/architecture.tex` for the full system description. Code-quality
conventions (module size limits, the OO ratchets, the database-facade rule)
are in [`.claude/rules/code-quality.md`](.claude/rules/code-quality.md);
testing (the pyramid, the hermetic suite, the five bug classes) is in
[`.claude/rules/testing.md`](.claude/rules/testing.md).

## Ethos & Delegation

Identity: `agent: claude`, `team: quarry`, `resolution: repo-only` per `.punt-labs/ethos.yaml`. Sub-agent calls (`Agent(subagent_type=…)`) match ethos identity handles.

**The identity registry is a vendored, self-contained copy at `.punt-labs/ethos/`** — plain committed files (the `lux`/`cryptd` pattern). A clone of this repo resolves every identity from files in the repo alone: no dependency on the developer's `~/.punt-labs/ethos/`, on the `..` workspace, or on the `../team` registry. It carries the 8-member `quarry` team (jfreeman, claude, rmh, gvr, kpz, djb, mdm, adb — the roster the pairing table uses), their personalities, writing styles, talents, roles, and `teams/quarry.yaml`. `.punt-labs/ethos.yaml` pins `agent: claude`, `team: quarry`, `resolution: repo-only` — which bounds SessionStart context injection to this roster and forbids any global fallback. There is deliberately **no `.vendor.yaml`**. To refresh the roster, edit `.punt-labs/ethos/teams/quarry.yaml` and re-run the vendor+prune (`ethos vendor claude jfreeman rmh gvr kpz djb mdm adb --apply`, then prune back to the quarry-team closure: drop non-roster identities and their unreferenced attributes, keep `.punt-labs/ethos/teams/quarry.yaml`, re-run `ethos doctor`); the `../team` registry is not in this loop. The `ethos vendor` seed handles must match the roster in the team file — when you add or drop a member, change both together, or the vendor run reproduces the old set. Runtime state (`missions/`, `missions.jsonl`, `sessions/`, `.biff`) stays gitignored. **Do not add the `punt-labs/team` submodule here** — Claude Code clones plugin repos with `--recurse-submodules`, and `ethos enable` v4.15.0+ refuses submodule mounts outright (`ethos-e29s`).

The design→implementation mission split, the worker/evaluator pairing table,
and pipeline selection live in
[`.claude/rules/delegation.md`](.claude/rules/delegation.md) — read it before
dispatching a mission.

## Development Loop

**The authoritative process is [`docs/WORKFLOW.md`](docs/WORKFLOW.md)** (`@`-imported
under [Mandatory Reading](#mandatory-reading)) — three nested loops, each with
pseudocode for its control flow and a Z schema for its entry/exit doorway. This
section is a map, not a second copy; the specifics live there in full.

```text
Level 1 — Backlog loop   one iteration = one work batch     (beads)
  Level 2 — PR loop      one iteration = one pull request
    Level 3 — Mission    one iteration = one delegated mission (a do-while)
```

- **Level 1 (backlog)** keeps the bead tracker true and picks what's next:
  intake every signal → a bead or closed at the door; validate against current
  main; order automatically (security → broken journeys → active epic → debt →
  features); escalate only a genuine fork. `EnterBatch`/`ExitBatch`.
- **Level 2 (PR)** turns one throughput-sized unit into one merged,
  rollback-coherent PR: missions build it (design mission first for
  architectural units, `designRatified` before code); full-diff local review to
  a zero-findings round; the **demo gate** — build + install the wheel, restart
  the daemon, exercise the real entry point, observe real output; then PR, a
  **boolean merge gate** (CI green, Qodo and Bugbot in, every finding
  answered, zero unresolved threads), squash-merge without asking, close-out
  with the recap. `EnterPR`/`ExitPR`.
- **Level 3 (mission)** is one delegated piece of work, a do-while: the worker
  codes/tests, a *different* specialist evaluates, reflect-and-fix until a round
  is clean. The leader owns every git/GitHub operation and monitors by the
  filesystem, never by commit activity; concurrency-class work gets a djb
  adversarial pass before acceptance. `EnterMission`/`ExitMission`.

PR review follows punt-kit/standards/pr-review.md: local review (qodo review + silent-failure-hunter + code-reviewer) before the PR, then Qodo and Bugbot once at open, at most two rounds, Copilot only on request.

Two directives that govern all three and are the most-corrected failures here:
**defects flow inward, scope flows outward** — a reviewer-flagged defect in an
open PR is fixed in that PR, never laundered into a "follow-up bead"; and **PRs
need not be pure** — a docs tweak, an OO paydown, or an adjacent fix riding
along is welcome, an improvement is never held back for tidiness, and the
operator explicitly rejects rules that make it harder to improve code. Rollback
coherence is the one structural split criterion; "the diff is large" and
"separate concern" are not.

## Release

Use `/punt:auto release [version=X.Y.Z]`. Quarry is a CLI + Plugin Hybrid — releases publish to both PyPI (`punt-quarry`) and the Claude Code plugin marketplace. Dev plugin testing: `claude --plugin-dir plugin` loads `quarry-dev` alongside the installed prod plugin — the argument is the plugin root, which is `plugin/`, not the repo root (DES-050).

## Key Documents

- `DESIGN.md` — ADR log (DES-001+). Read before proposing changes to settled architecture.
- `docs/architecture.tex` → `docs/architecture.pdf` — system architecture, module responsibilities, search and retrieval, deployment
- `prfaq.tex` → `prfaq.pdf` — product direction and risk assumptions
- `docs/improving-agent-memory.md` — agent memory design rationale
- `docs/retrieval-quality-improvements.md` — retrieval-quality research + eval-harness plan (active work)
- `docs/smoke-test.md` — post-release manual smoke test
- `docs/README.md` — docs index; `docs/archive/` holds completed build-plans, reviews, and superseded designs mapped to DES-### in DESIGN.md. ONNX provider auto-detection design (formerly `docs/provider-detection-design.md`) is now in `docs/architecture.tex` + DES-016.

@.punt-labs/quarry/CLAUDE.md
@.punt-labs/vox/CLAUDE.md
@.punt-labs/ethos/CLAUDE.md
@.punt-labs/beadle/CLAUDE.md
