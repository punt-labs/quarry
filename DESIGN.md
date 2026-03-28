# Quarry Design Decision Log

This file is the authoritative record of design decisions, prior approaches, and their outcomes. **Every design change must be logged here before implementation.**

For system architecture, module responsibilities, configuration, and deployment, see [`docs/architecture.tex`](docs/architecture.tex). For user-facing documentation, see [README.md](README.md). For test tiers, counts, and strategy, see [`docs/architecture.tex`](docs/architecture.tex) §15.

## Rules

1. Before proposing ANY design change, consult this log for prior decisions on the same topic.
2. Do not revisit a settled decision without new evidence.
3. Log the decision, alternatives considered, and outcome.

---

## DES-001: Fire-and-Forget MCP Pattern

**Date:** 2026-02-08
**Status:** SETTLED
**Topic:** How side-effect MCP tools handle latency

### Design

Side-effect MCP tools (ingest, delete, sync, register, deregister, remember) return an optimistic response immediately and process in a bounded `ThreadPoolExecutor(max_workers=4)`.

Settings and database connections are **snapshotted at the tool boundary** and passed into background `_do_*` functions. This avoids a race condition where the mutable `_db_name` ContextVar could change between the tool call and the background thread's execution.

### Why This Design

MCP tool calls block the LLM's response stream. A 30-second ingest blocks Claude from responding. The bounded pool prevents resource exhaustion under concurrent requests. Exceptions in background threads are logged, not raised — the caller already received the optimistic response.

---

## DES-002: Protocol-Based Typing

**Date:** 2026-02-08
**Status:** SETTLED
**Topic:** Type safety for third-party libraries without stubs

### Design

Third-party libraries without type stubs (LanceDB) are typed via Protocol classes in `types.py`: `LanceDB`, `LanceTable`, `LanceQuery`, `ListTablesResult`, `OcrBackend`, `EmbeddingBackend`.

### Why This Design

Exact type checking without `Any`. Mockable interfaces for testing. No runtime cost (protocols are erased at runtime).

---

## DES-003: Named Databases

**Date:** 2026-02-10
**Status:** SETTLED
**Topic:** Work/personal separation

### Design

`resolve_db_paths(settings, name)` maps a database name to a path under `QUARRY_ROOT`. Each database is fully isolated — its own LanceDB directory, sync registry, and vector index. The `use` command (CLI) and `use` tool (MCP) switch the active database.

---

## DES-004: Embedding Model Selection

**Date:** 2026-02-08
**Status:** SETTLED
**Topic:** Which embedding model to use

### Design

snowflake-arctic-embed-m-v1.5: 768-dimensional, 512 token context, int8 quantized ONNX (~120 MB). Auto-downloads on first use.

### Why This Design

Strong retrieval quality for its size. Runs efficiently on CPU (no GPU required). Permissive license (Apache 2.0). The model is fixed — there is no configuration to swap it. Changing the model invalidates all existing embeddings, requiring full re-ingestion.

---

## DES-005: Bearer Token Authentication

**Date:** 2026-02-19
**Status:** SETTLED
**Topic:** HTTP API authentication

### Design

`quarry serve --api-key` gates all HTTP endpoints except `/health` behind `Authorization: Bearer <key>`.

- `/health` exempt — load balancers need unauthenticated health checks.
- OPTIONS exempt — CORS preflight requests carry no auth.
- `hmac.compare_digest` for token comparison — prevents timing attacks.
- Case-insensitive scheme per RFC 7235.
- Empty key = no auth — prevents accidental trivial bypass.
- No key = auth disabled — local use requires no key.
- Server refuses non-loopback binding without `--api-key`.

---

## DES-006: ASGI Server (Starlette + uvicorn)

**Date:** 2026-02-19
**Status:** SETTLED
**Topic:** HTTP server framework
**Supersedes:** Threaded Request Handling (stdlib `http.server`)

### Design

`quarry serve` runs a Starlette ASGI app on uvicorn. REST handlers are sync functions — Starlette runs them in its threadpool. The `/mcp` WebSocket endpoint is async. Default port 8420.

### Why This Design

MCP-over-WebSocket required native async support, which stdlib `http.server` cannot provide. Thread safety invariants are unchanged: `_QuarryContext` fields set once at startup, LanceDB handles concurrent reads, ONNX Runtime sessions are thread-safe.

---

## DES-007: MCP-over-WebSocket

**Date:** 2026-02-23
**Status:** SETTLED
**Topic:** Daemon architecture for shared embedding model

### Design

The `/mcp` WebSocket endpoint enables multiple Claude Code sessions to share a single quarry daemon via mcp-proxy. Each WebSocket connection gets its own asyncio Task with a `ContextVar` for database selection. `FastMCP._mcp_server` accessed via `getattr` with runtime guard; `mcp` pinned to `<2.0.0`.

Security: Origin-based CSWSH protection, Bearer auth before WebSocket accept, session keys sanitized (control chars stripped, truncated to 64 chars, CWE-117).

---

## DES-008: Log Safety (CWE-532, CWE-117)

**Date:** 2026-02-23
**Status:** SETTLED
**Topic:** Preventing sensitive data leakage in logs
**Supersedes:** Log Redaction (query string stripping in stdlib access logs)

### Design

Uvicorn's access log is disabled entirely (`access_log=False`), eliminating query string leakage at the source. The search handler logs only result count, never the raw query. WebSocket session keys sanitized before logging.

---

## DES-009: Single Table Design

**Date:** 2026-02-08
**Status:** SETTLED
**Topic:** LanceDB table structure

### Design

All chunks live in one LanceDB table (`chunks`). Document and collection boundaries are columns, not separate tables. Simplifies cross-document search and avoids table proliferation. Filtering uses LanceDB's built-in filter predicates on vector search.

---

## DES-010: Configurable CORS Origins

**Date:** 2026-02-23
**Status:** SETTLED
**Topic:** Cross-origin access control for the HTTP API

### Design

`--cors-origin` (repeatable) controls `Access-Control-Allow-Origin`. The server reflects the request's `Origin` header only when it matches the allow list, and adds `Vary: Origin`. Defaults to `http://localhost`.

---

## DES-011: Container Deployment (Fly.io)

**Date:** 2026-02-19
**Status:** SETTLED (relocated)
**Topic:** Production deployment

### Design

> Dockerfile, fly.toml, .dockerignore, and sync-chat-db.sh relocated to [punt-labs/public-website/infra/quarry/](https://github.com/punt-labs/public-website/tree/main/infra/quarry). The Dockerfile installs `punt-quarry` from PyPI.

Multi-stage build downloads the embedding model at build time for fast cold starts (~5s). LanceDB data on Fly persistent volume at `/data`. TLS terminated by Fly's proxy.

---

## DES-012: Documentation Consolidation into LaTeX

**Date:** 2026-03-25
**Status:** SETTLED
**Topic:** Reducing docs fragmentation

### Design

Created `docs/architecture.tex` — a single LaTeX architecture document absorbing content from `ADVANCED-CONFIG.md`, `SEARCH-TUNING.md`, `NON-FUNCTIONAL-DESIGN.md`, and the architecture sections of `DESIGN.md`. The LaTeX source is readable by agents; the compiled PDF is readable by humans.

### Why This Design

Docs were fragmented across 5+ markdown files with overlapping content. LaTeX provides consistent rendering, section numbering, and the ability to include tables, diagrams, and cross-references in one document. Follows the pattern established by lux's `docs/architecture.tex`.

### Alternatives Considered

**Keep markdown files** — simpler tooling but no cross-references, inconsistent formatting, content duplication across files. Rejected.

---

## DES-013: Agent Discovery — Three-Layer Strategy

**Date:** 2026-03-26
**Status:** SETTLED
**Topic:** How agents discover quarry's capabilities

### Design

Three complementary mechanisms:

1. **Researcher agent** (`.claude-plugin/agents/researcher.md`) — subagent that combines quarry local search with web research. Auto-ingests valuable web findings.
2. **CLAUDE.md injection** — `quarry install` appends a capabilities section to `~/.claude/CLAUDE.md` (idempotent, HTML comment sentinel).
3. **SessionStart context enrichment** — hook returns `additionalContext` with tool names, slash commands, and researcher agent mention.

### Why This Design

No single mechanism is reliable across all contexts. CLAUDE.md is always loaded but generic. SessionStart is session-specific but has known delivery issues. The researcher agent is discovered via the plugin system. Layering all three provides redundancy.

---

## DES-014: Thin Hook Standard Compliance

**Date:** 2026-03-26
**Status:** SETTLED
**Topic:** session-start.sh refactoring

### Design

Refactored `session-start.sh` from 88 lines of shell business logic to a 4-line thin gate that derives `PLUGIN_ROOT` from `$0` and delegates to `quarry-hook session-setup`. Command deployment and MCP permissions logic moved to `handle_session_setup()` in `_stdlib.py` (stdlib-only imports).

### Why This Design

Follows punt-kit/standards/hooks.md: hooks are plumbing, not product. Business logic belongs in testable Python, not shell. The 4-line gate (not 3) is necessary because `session-setup` needs the plugin root to find the `commands/` directory, and `CLAUDE_PLUGIN_ROOT` is only a hooks.json template variable, not an env var.

---

## DES-015: Filesystem Standard Adoption

**Date:** 2026-03-26
**Status:** SETTLED
**Topic:** User data directory layout

### Design

Adopted punt-kit/standards/filesystem.md. User data moves from `~/.quarry/` to `~/.punt-labs/quarry/`. Per-project config moves from `.claude/quarry.local.md` to `.punt-labs/quarry/config.md`. Logs move from `data/quarry.log` to `logs/quarry.log`.

### Why This Design

One namespace, one root. All Punt Labs tools under `~/.punt-labs/<tool>/`. Per-repo config owned by quarry, not by Claude Code's `.claude/` directory. No automatic migration per the standard — clean break with manual `mv` command documented.

---

## DES-016: Execution Provider Strategy for Embedding

**Date:** 2026-03-27
**Status:** SETTLED
**Topic:** Which ONNX execution provider and model precision to use per platform

### Design

Two deployment profiles:

| Profile | Model | Provider | Throughput | Use case |
|---------|-------|----------|-----------|----------|
| **Local (CPU)** | int8 (~120 MB) | CPUExecutionProvider | 9.4 texts/s (M2 Air), 134 texts/s (AMD) | Laptops, default install |
| **Server (GPU)** | FP16 (~218 MB) | CUDAExecutionProvider | 3,042 texts/s (RTX 5080) | Central quarry, bulk ingestion |

The model is selected based on available hardware. The embedding dimension (768) is identical across these precision variants of the same model, so embeddings remain comparable while the underlying architecture, checkpoint, and tokenizer are unchanged. If any model artifact changes (even with the same dimension), all existing vectors must be re-embedded.

### Why This Design

Benchmarked 6 configurations on two machines (M2 Air, AMD + RTX 5080):

| Config | M2 Air | AMD host (w/ RTX 5080) | Notes |
|--------|--------|------------------------|-------|
| int8 + CPU | 9.4 texts/s | 134 texts/s | Production default (CPU only, GPU unused) |
| int8 + CUDA | — | 158 texts/s | 168 memcpy nodes, barely faster than CPU |
| FP32 + CUDA | — | 1,639 texts/s | Full precision, no warnings |
| FP16 + CUDA | — | 3,042 texts/s | Half precision, fastest, 9ms/batch |
| FP32 + CPU | — | 79 texts/s | Slower than int8, larger model |
| FP32 + CoreML | 0.8 texts/s | — | 99 graph partitions, 8.9 GB RAM, 12x slower |
| FP16 + CoreML | — | — | Not tested (CoreML dead end) |

### Alternatives Considered

1. **CoreML EP on Apple Silicon** — Rejected. The Neural Engine cannot efficiently run this transformer architecture. 463 of 636 nodes supported, requiring 99 partitions. Result: 0.8 texts/s (12x slower than CPU), 8.9 GB RAM.
2. **int8 on CUDA** — Rejected for GPU deployment. The int8 quantized operators require 168 CPU↔GPU memory copies per inference, negating GPU acceleration. Only 18% faster than CPU despite having the GPU.
3. **FP32 on CUDA** — Valid but suboptimal. FP16 is 1.9x faster with identical embedding quality for retrieval.
4. **AWS SageMaker** — Previously rejected (DES-004 era). Network round-trip dominated; local CPU was faster.

---

## DES-017: Hybrid Search via BM25 + Vector + RRF

**Date:** 2026-03-28
**Status:** SETTLED
**Topic:** How to retrieve agent memories and documents

### Design

Dual-channel retrieval: vector similarity (existing ANN search) + BM25 full-text search (Tantivy via LanceDB native FTS). Results fused with Reciprocal Rank Fusion: `score[id] = Σ(1/(60 + rank))` across channels, weighted by temporal decay for agent-scoped memories.

Hybrid search is used for all `find` calls regardless of whether `agent_handle` is provided. FTS failures gracefully fall back to vector-only with a WARNING log.

### Why This Design

Vector search misses exact terms (proper nouns, code identifiers, jargon). BM25 catches keyword matches that embeddings miss. RRF fuses rankings without requiring score normalization across channels (~30 lines). Temporal decay (`exp(-decay_rate * hours)`) keeps recent working memories relevant without losing stable reference material. Decay applies only to chunks with `memory_type` in {fact, observation, opinion, procedure}. Documents and seeded expertise (empty `memory_type`) are exempt — even when tagged with `agent_handle` for ownership.

### Alternatives Considered

1. **Vector-only with reranking** — Rejected. Reranking improves ordering but cannot surface documents the vector search missed entirely.
2. **BM25-only** — Rejected. Loses semantic similarity for paraphrased queries.
3. **Weighted linear combination** — Rejected. Requires normalizing scores across channels (cosine similarity vs BM25 are on different scales). RRF avoids this by using rank positions only.
4. **Graph-based retrieval (Neo4j)** — Rejected. Overkill for local-only. Entity extraction + metadata filtering gives 80% of the benefit at 20% complexity.

---

## DES-018: Agent Memory Metadata Schema

**Date:** 2026-03-28
**Status:** SETTLED
**Topic:** How to scope memories to agents and classify memory types

### Design

Three new columns on the LanceDB chunks table:

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `agent_handle` | utf8 | `""` | Which agent owns this memory (empty = unscoped) |
| `memory_type` | utf8 | `""` | fact, observation, opinion, procedure (empty = document) |
| `summary` | utf8 | `""` | One-line summary for lightweight search |

Migration via `table.add_columns()` with empty-string defaults. Idempotent `_migrate_schema()` runs on every table open. Memory type taxonomy from the Hindsight architecture (91.4% on LongMemEval).

### Why This Design

Agent memories need scoping (rmh's memories shouldn't pollute bwk's searches) and classification (a "how do I deploy?" query should prefer procedures over facts). Empty defaults preserve backwards compatibility — existing unscoped documents gain the columns with no behavior change. Per-agent collections (`memory-claude`, `bwk-books`) provide isolation; the columns enable filtering within shared collections.

### Alternatives Considered

1. **Separate tables per agent** — Rejected. Complicates cross-agent search and increases table management overhead.
2. **JSON metadata column** — Rejected. No SQL filtering, no type safety, harder to index.
3. **Full entity extraction at ingestion** — Deferred to v2. Requires LLM pass per chunk (~1s each). The current schema supports it when added later.
