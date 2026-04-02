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

---

## DES-019: Ethos Extension Session Context Setup

**Date:** 2026-03-29
**Status:** SETTLED
**Topic:** How `quarry install` writes memory instructions into ethos identity extension files

### Design

`quarry install` step 7/7 scans `~/.punt-labs/ethos/identities/*.ext/quarry.yaml` and appends a `session_context` YAML literal block scalar to any file that has `memory_collection` but no `session_context`. The template is parameterized by agent handle and collection name.

Key design choices:

1. **Raw file append, not YAML round-trip.** The function reads the raw text to detect existing keys, then appends the `session_context: |` block directly. `yaml.safe_load` is used only to extract the `memory_collection` value — the file is never re-serialized through `yaml.dump`.

2. **Per-identity exception handling.** The scan loop wraps each identity in `try/except (OSError, yaml.YAMLError)`. A malformed file for one identity does not abort processing of the others. Failed identities are reported in the result message.

3. **Three-way classification.** `_write_ethos_ext_session_context` returns `"updated"`, `"already_set"`, or `"no_collection"` — not a boolean. The `no_collection` case is surfaced in the install output so users know their config is incomplete.

### Why This Design

Ethos v2.4.1 removed `BuildMemorySection` (hardcoded quarry knowledge in Go code, a DES-008 violation) and replaced it with generic `BuildExtensionContext` (DES-022). Ethos now emits whatever is in the `session_context` key of any extension YAML verbatim at session start and compaction. Quarry owns the content of its own instructions — ethos just delivers them.

Without this install step, agents with existing `quarry.yaml` ext files (containing `memory_collection` but no `session_context`) silently lose their memory instructions after upgrading ethos. The install step closes this gap idempotently.

### Alternatives Considered

1. **YAML round-trip via `yaml.safe_load` + `yaml.dump`** — Rejected. Destroys comments, blank lines, and key ordering in the user's file. Silent data corruption on the happy path.
2. **Ethos writes quarry's instructions** — Rejected. Violates the one-way dependency: quarry depends on ethos for identity, but ethos has zero knowledge of quarry's internals (DES-008).
3. **Require users to manually add `session_context`** — Rejected. Silent failure with no error message. Users would not know their memory stopped working until they noticed missing recall.

---

## DES-020: TLS Everywhere with TOFU Certificate Pinning

**Date:** 2026-04-01
**Status:** SETTLED
**Topic:** How quarry secures remote connections

### Design

The installed quarry service always runs with TLS enabled — including on localhost. `quarry serve` without `--tls` is supported for local development but must not be used for production. The security model is TOFU (Trust On First Use) with self-signed CA certificate pinning.

**Certificate generation:** `quarry install` generates a self-signed EC P-256 CA and server certificate with full x509 extension set. Certs are written atomically to `~/.punt-labs/quarry/tls/` with 0600/0644 permissions. The CA cert CN is `"Quarry CA"` (not hostname-scoped). The server cert SAN includes the configured hostname, localhost, and loopback addresses.

**TOFU login flow:** `quarry login <host>` fetches the server's CA cert over HTTPS with verification disabled (bootstrap), displays the SHA256 fingerprint for out-of-band confirmation, then pins the CA cert locally at `~/.punt-labs/mcp-proxy/quarry-ca.crt`. All subsequent connections verify against the pinned CA only — system roots are excluded.

**mcp-proxy integration:** The `quarry.toml` profile includes a `ca_cert` field pointing to the pinned CA cert. mcp-proxy builds a custom TLS config with a cert pool containing only this CA, enforcing TLS 1.3 minimum.

### Why This Design

Quarry servers hold the user's entire document corpus. Plaintext connections expose both the content and the API key to any network observer. TLS on localhost adds zero user friction (handled by `quarry install`) and eliminates the "it's just localhost" exception that leads to split security models. TOFU is appropriate because there is no PKI — quarry servers are personal infrastructure, not public services.

### Alternatives Considered

1. **Let's Encrypt / ACME** — Rejected. Requires a public domain name and port 80/443 access. Personal home servers often have neither.
2. **System trust store** — Rejected. Adding a self-signed CA to the system trust store requires root and varies by OS. TOFU pinning works in userspace.
3. **Optional TLS (`--insecure` flag)** — Rejected. Two code paths, two security models. Users who start with `--insecure` never switch. One path, always encrypted.

---

## DES-021: Remote CLI Routing — No Split Horizon

**Date:** 2026-04-01
**Status:** PROPOSED
**Topic:** How CLI commands route when a remote server is configured

### Design

When a remote quarry server is configured (via `quarry login`), **every data command routes to the remote server**. There is no split horizon where some commands go remote and others silently fall back to the local database. The only local-only commands are authentication and administration:

**Local-only:** `login`, `logout`, `remote list`, `install`, `uninstall`, `serve`, `mcp`, `version`

**Everything else routes remotely:** `find`, `status`, `list` (all subcommands), `show`, `ingest`, `remember`, `delete`, `register`, `deregister`, `sync`, `use`, `doctor`

The CLI detects remote configuration via `_safe_proxy_config()` and routes through HTTP helpers with the pinned CA cert and Bearer token. Currently only `_remote_https_get()` exists; a matching `_remote_https_post()` / `_remote_https_delete()` helper is needed for write operations (tracked as `quarry-stcd`).

**Current state (v1.11.0):** Only `find` and `status` route remotely. All other commands silently hit the local database. This is tracked as epic `quarry-g0ed` with 11 tasks to complete parity.

**Target endpoint surface:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/search` | GET | Semantic search (exists) |
| `/status` | GET | Database statistics (exists) |
| `/collections` | GET | List collections (exists, CLI not wired) |
| `/documents` | GET | List documents (exists, CLI not wired) |
| `/show` | GET | Document metadata and page text (needs endpoint) |
| `/ingest` | POST | File upload or URL ingestion (needs endpoint) |
| `/remember` | POST | Inline text ingestion (needs endpoint) |
| `/documents` | DELETE | Remove a document (needs endpoint) |
| `/collections` | DELETE | Remove a collection (needs endpoint) |
| `/registrations` | GET/POST/DELETE | Directory registration CRUD (needs endpoint) |
| `/sync` | POST | Trigger incremental sync (needs endpoint) |
| `/databases` | GET | List named databases (needs endpoint) |
| `/use` | POST | Switch active database (needs endpoint) |
| `/health` | GET | Server health for doctor (exists) |

### Why This Design

When a user is connected to a remote server, they expect every command to operate on the remote data. A user who runs `quarry list collections` after `quarry login` expects to see the remote server's collections, not whatever happens to be on their local machine. Silent local fallback gives wrong data with no warning — the worst kind of bug.

The "local-only" classification applies only to commands that genuinely operate on local configuration (authentication, service management). There is no data operation that makes sense locally when the user has chosen to work with a remote server.

### Alternatives Considered

1. **Split horizon with explicit `--local`/`--remote` flags** — Rejected. Adds complexity to every command. Users shouldn't have to think about where their data lives after logging in.
2. **Only route read operations remotely** — Rejected. A user who can search remotely but can't ingest remotely has a broken workflow. Remote means remote for everything.
