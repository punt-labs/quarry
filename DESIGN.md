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
**Implemented by:** `retrieval/hybrid.py` (`HybridRetriever`) behind the DES-037
seam; scoring uses normalized embeddings + cosine (DES-038).

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
**Status:** SETTLED
**Topic:** How CLI commands route when a remote server is configured

### Design

When a remote quarry server is configured (via `quarry login`), **every data command routes to the remote server**. There is no split horizon where some commands go remote and others silently fall back to the local database. The only local-only commands are authentication and administration:

**Local-only:** `login`, `logout`, `remote list`, `install`, `uninstall`, `serve`, `mcp`, `version`

**Everything else routes remotely:** `find`, `status`, `list` (all subcommands), `show`, `ingest`, `remember`, `delete`, `register`, `deregister`, `sync`, `use`, `doctor`

The CLI detects remote configuration via `_safe_proxy_config()` and routes through `_remote_https_get()`, `_remote_https_post()`, and `_remote_https_delete()` helpers with the pinned CA cert and Bearer token.

**Current state (v1.16.0):** All data commands route remotely: `find` (/search), `show` (/show), `status` (/status), `ingest` (/ingest POST), `remember` (/remember POST), `delete document` (DELETE /documents), `delete collection` (DELETE /collections), `sync` (POST /sync), `list documents` (GET /documents), `list collections` (GET /collections), `list registrations` (GET /registrations), `list databases` (GET /databases), `register` (POST /registrations), `deregister` (DELETE /registrations). Register and deregister use fire-and-forget (return task_id, exit 0) since v1.15.0.

**Remaining endpoint surface:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/use` | POST | Switch active database (needs endpoint) |

### Why This Design

When a user is connected to a remote server, they expect every command to operate on the remote data. A user who runs `quarry list collections` after `quarry login` expects to see the remote server's collections, not whatever happens to be on their local machine. Silent local fallback gives wrong data with no warning — the worst kind of bug.

The "local-only" classification applies only to commands that genuinely operate on local configuration (authentication, service management). There is no data operation that makes sense locally when the user has chosen to work with a remote server.

### Alternatives Considered

1. **Split horizon with explicit `--local`/`--remote` flags** — Rejected. Adds complexity to every command. Users shouldn't have to think about where their data lives after logging in.
2. **Only route read operations remotely** — Rejected. A user who can search remotely but can't ingest remotely has a broken workflow. Remote means remote for everything.

---

## DES-022: Single Install Script with --network Flag

**Date:** 2026-04-11
**Status:** SETTLED
**Topic:** How the installer handles local-only vs network-accessible daemon modes

### Design

One `install.sh` with a single optional flag: `--network`. Default (no flags) installs everything with the daemon on localhost. `--network` binds the daemon to `0.0.0.0` and requires `QUARRY_API_KEY`. Plugin installation is opportunistic — if the `claude` CLI exists on PATH, the plugin and marketplace are installed; if not, they're skipped with a warning.

All install steps always run in both modes: CLI, model download, GPU swap, daemon registration, TLS certificates, local `quarry login localhost`. The only difference is the bind address.

### Why This Design

The prior four-script split (`install.sh`, `install-server.sh`, `install-client.sh`, `install-both.sh`) caused the same drift bug three times in one session: VERSION constants not bumped across all scripts (quarry-tu0w), GPU swap block deleted from split scripts (quarry-e4c2), and step numbering divergence. Every release that didn't update all four scripts created a broken install path for some users.

The original `--server`/`--client` split was based on a false premise — that "server" and "client" were different install types. In practice every machine needs the full install (CLI, model, daemon, GPU swap). The only real axis is whether the daemon should be reachable from the network.

### Alternatives Considered

1. **Four separate scripts** — Rejected. Caused three drift bugs in one day. Shared code across four files with no enforcement mechanism.
2. **Three modes: default, --server, --client** — Implemented first (PR #220), then simplified. `--client` skipped model download and daemon, but clients that happen to have GPUs still want local quarry operations. The mode distinction added complexity without matching real use cases.
3. **Shared sourced fragment** (`.bin/install-gpu-swap.sh`) — Rejected. The `curl | sh` flow has no `$script_dir` to resolve sourced files. Would require a two-file download or inline the fragment via heredoc.
4. **Environment variable instead of flag** (`QUARRY_INSTALL_MODE=network`) — Rejected. `sh -s -- --network` is POSIX and more discoverable than env vars in documentation.

---

## DES-023: FTS Index Rebuild After Optimize

**Date:** 2026-04-11
**Status:** SETTLED
**Topic:** Keeping the Tantivy full-text index consistent after LanceDB compaction

### Design

`optimize_table()` now calls `table.create_fts_index("text", replace=True)` after `table.optimize()`. This rebuilds the Tantivy FTS index against the new fragment layout. The call also passes `cleanup_older_than=timedelta(days=7)` to prune old manifest versions during compaction.

### Why This Design

`table.optimize()` compacts data fragments — merging small fragments and removing deleted rows. The Tantivy FTS index stores row references by fragment ID. After compaction, those fragment IDs no longer exist. Every subsequent FTS query hit `RuntimeError: lance error: ... fragment id N but this fragment does not exist` and fell back to vector-only search via the existing exception handler in the FTS search path (now `db/chunk_search.py`, after `database.py` was decomposed into the `db/` package).

This meant the BM25 leg of hybrid search (DES-017) was dead after every `sync_all` cycle since the feature shipped. The RRF fusion that hybrid search was designed to provide — catching keyword matches that vector search misses — never worked in production. The fallback masked the failure: search returned results, but only from the vector channel.

The rebuild is O(n) in table size but only runs after bulk sync operations (via `sync_all` → `optimize_table`), not on every query. On a 33GB dataset with 59K fragments, the rebuild adds seconds, not minutes.

The 7-day version pruning addresses a secondary issue: 118K manifest files consuming 11GB in `_versions/`. LanceDB's `optimize()` merges fragments but doesn't prune old manifests by default.

### Alternatives Considered

1. **Rebuild FTS on every query** — Rejected. O(n) per query is unacceptable.
2. **Rebuild FTS on daemon startup** — Rejected. Doesn't help when the daemon runs for days between restarts. The stale index reappears after the first sync.
3. **Use `replace=False` and rely on LanceDB incremental FTS updates** — This is what was in place. LanceDB does not incrementally update the FTS index after `optimize()` changes fragment IDs. The index must be fully rebuilt.
4. **Catch the RuntimeError and retry with a fresh table handle** — Rejected. Treats the symptom. The FTS index is structurally stale after compaction; retrying with the same stale index doesn't help.

---

## DES-024: File-Based Claude Code Plugin Check in Doctor

**Date:** 2026-04-11
**Status:** SETTLED
**Topic:** How `quarry doctor` verifies the Claude Code MCP plugin is configured

### Design

`_check_claude_code_mcp()` reads `~/.claude/plugins/installed_plugins.json` directly and checks for the `quarry@punt-labs` key. It validates the install path exists and the plugin manifest contains an `mcpServers.quarry` entry. No subprocess calls. Exception handler catches `JSONDecodeError`, `OSError`, `KeyError`, `TypeError`, and `AttributeError` for graceful degradation on corrupted or changed registry formats.

### Why This Design

The prior implementation shelled out to `claude mcp list` with a 10-second timeout. That command spawns every configured MCP server for health checks — sequentially. With 10 plugins installed (quarry, biff, lux, vox, beadle, ethos, dungeon, z-spec, plus others), the total exceeded 15 seconds. The doctor check timed out on every run, on both Linux and macOS.

The check only needs to know whether quarry is configured, not whether every MCP server is healthy. Reading the JSON registry file answers that question in <1ms with zero side effects.

The Claude Desktop check (`_check_claude_desktop_mcp`) already used this file-based pattern. The Claude Code check now matches.

### Alternatives Considered

1. **Raise the timeout to 30s or 60s** — Rejected. Moves the goalpost. Adding more plugins would hit the new timeout. Users shouldn't wait 30 seconds for a doctor check.
2. **Background the probe and report results asynchronously** — Rejected. Doctor checks are synchronous by design — the output is a pass/fail table. An async probe that reports "pending" defeats the purpose.
3. **Skip the check when running under a Claude Code parent process** — Rejected. Detection is fragile (check `CLAUDE_SESSION_ID` env var?) and doesn't help when running `quarry doctor` from a plain terminal.
4. **Read the MCP config that `claude mcp add` writes to** — Considered as an alternative data source. `_configure_claude_code()` writes via `claude mcp add`, which uses a different store than the plugin registry. In practice both stores have the quarry entry because quarry is always installed as a plugin. Noted as a known limitation in a code comment.

---

## DES-025: Agent Tool Access — disallowedTools vs tools

**Date:** 2026-04-12
**Status:** SETTLED
**Topic:** How to grant sub-agents MCP tool access in agent definitions

### Design

Agent definitions in `.claude/agents/*.md` use `disallowedTools` (denylist) instead of `tools` (allowlist) when the agent needs access to MCP tools. Claude Code sub-agents inherit all tools from the main session by default, including MCP tools. The `tools` field is an allowlist — specifying it restricts the agent to exactly those tools and nothing else. Since MCP tool names are dynamic (e.g., `mcp__plugin_quarry_quarry__find`) and vary across projects and sessions, an allowlist cannot enumerate them portably.

**Prior pattern (all existing agents):**

```yaml
tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
  - Glob
```

This gives the agent 6 tools. All MCP tools are excluded. The agent cannot call `mcp__plugin_quarry_quarry__find`, `mcp__plugin_biff_tty__write`, or any other MCP tool.

**New pattern (agents that need MCP access):**

```yaml
disallowedTools:
  - Write
  - Edit
```

This gives the agent everything the main session has — all internal tools plus all MCP tools — except Write and Edit. The agent can call quarry MCP tools, biff MCP tools, etc.

### Why This Design

The QA agent (`qae`) needs to run smoke tests that exercise quarry's MCP tools (`find`, `remember`, `ingest`, `delete`, `status`, `list`, `show`, `use`). With the `tools` allowlist, these are excluded. With `disallowedTools`, they're inherited. The QA agent is restricted from Write and Edit because smoke tests should be read-only — test data is created via CLI (`quarry remember`) and MCP tools, not by writing files.

This pattern applies to any agent that needs to interact with MCP servers: QA agents testing MCP tools, agents that need to search quarry, agents that need to send biff messages, agents that need to display via lux.

### When to Use Which

| Pattern | When |
|---------|------|
| `tools: [Read, Write, Edit, Bash, Grep, Glob]` | Agent only needs file operations and shell. Most implementation agents (rmh, bwk, adb, etc.). |
| `disallowedTools: [Write, Edit]` | Agent needs MCP tools but shouldn't modify files. QA agents, research agents. |
| `disallowedTools: []` (or omit both fields) | Agent needs full access to everything. Use sparingly. |
| `tools: [Read, Grep, Glob, Bash]` + specific MCP tools | Agent needs a specific subset. Fragile — MCP tool names change across projects. |

### Alternatives Considered

1. **Add MCP tool names to the allowlist** — Rejected. MCP tool names include plugin and server prefixes (`mcp__plugin_quarry_quarry__find`) that are project-specific. An allowlist that works in quarry won't work in biff or lux. Not portable.
2. **Use wildcard patterns in tools** — Not supported. The `tools` field takes exact tool names only.
3. **Give all agents full access** — Rejected. Implementation agents should not have MCP tool access by default. A sub-agent that accidentally calls `mcp__plugin_quarry_quarry__delete` during a code fix is a data loss risk. Least privilege applies.

## DES-026: Sync Concurrency Control and Batch Writes

**Date:** 2026-04-18
**Status:** SETTLED
**Topic:** Preventing LanceDB compaction death spiral from concurrent sync

### Problem

The quarry serve process accumulated 133K LanceDB fragments (83 GB)
and burned 13 CPU cores sustained for 5 days. Root cause: every
SessionStart hook spawns `quarry sync`, which routes through the
HTTP `/sync` endpoint to run `sync_all()` inside the serve process.
No concurrency guard existed, so multiple syncs ran simultaneously.
Each sync creates 2 LanceDB transactions per document (delete + add),
each creating a new fragment. Compaction (`optimize_table()`) could
not keep up, creating a self-reinforcing death spiral.

### Design

Five changes:

1. **Server-side sync lock** — `SyncTaskState` on `_QuarryContext`
   tracks whether a sync is running. `POST /sync` returns 409 when
   one is already in progress. Simpler than `asyncio.Lock` because
   the state check is synchronous in the async handler.

2. **Registration subsumption** — `register_directory()` enforces
   that parent directories subsume children (deregisters them) and
   child directories are rejected when a parent is already registered.
   Prevents duplicate scanning.

3. **Batch LanceDB writes** — **SUPERSEDED by DES-034.** `prepare_document()`
   chunked and embedded a whole document without writing to LanceDB;
   `sync_collection()` accumulated every document's chunks and did a single
   `batch_insert_chunks()` at the end. This reduced write transactions to 1
   per collection sync but made nothing searchable until the sync finished, lost
   all embedding work on a crash, and scaled peak memory with data size (a single
   large file materialized all its vectors before any write). DES-034 replaces it
   with bounded progressive commit: a streaming embed producer plus a
   `ProgressiveIndexer` that flushes every `sync_flush_mb` and commits one
   registry transaction per flush. `prepare_document()` and `batch_insert` are
   removed.

4. **optimize_table() guard** — Skips when fragment count exceeds
   10,000 to prevent the death spiral. `quarry optimize --force`
   CLI command for manual recovery.

5. **Async sync endpoint** — `POST /sync` returns 202 with task_id.
   `sync_all()` runs as a background `asyncio.Task`. `GET /sync/{task_id}`
   returns status. CLI becomes fire-and-forget.

### Alternatives Considered

1. **Queue concurrent sync requests** — Rejected. Queuing hides the
   problem. The caller should know sync is already running.
2. **Batch deletes via IN clause** — Rejected. LanceDB's delete
   predicate may not handle large IN clauses efficiently.
3. **Partial compaction** — LanceDB 0.30 does not support it.
4. **WebSocket streaming of sync progress** — Over-engineered for a
   background batch operation. Polling is sufficient.

### Amendment 2026-07-01 — Deregister is not async (quarry-noiw, quarry-xsz3)

Change #5 (async endpoint, 202 + task_id) targets `/sync`: long, multi-file
embedding that would block the event loop. `DELETE /registrations` does not
share that profile — its existence check and registry-row mutation are trivial
metadata operations that must complete before the response so the caller gets
correct feedback. Deregister is therefore split:

- **Synchronous (before the HTTP response):** validate the collection exists
  and delete its registry rows, executed via `run_in_threadpool` so the event
  loop is never blocked. An unknown collection returns **404**
  (`No registration found for '<collection>'`), which the CLI maps to exit 1 —
  parity with the local path (`__main__.py`). A registry failure returns 500.
- **Asynchronous (202 + task_id):** only the chunk purge (`delete_document`
  over the removed documents), which has real latency. `GET /tasks/<id>`
  reports its terminal status; the CLI polls after the 202 and maps a `failed`
  status to a non-zero exit with the server's error text. The CLI never reports
  success for an operation that then dies.

`SyncRegistry` now sets `PRAGMA busy_timeout=5000` on every connection, so a
concurrent sync writer causes a bounded wait instead of an instant
`database is locked` (the quarry-xsz3 mechanism). The async model of change #5
is otherwise retained for `/sync`.

The **MCP** surface (`deregister_directory`) is now synchronous end-to-end as
well — existence check, registry delete, and chunk purge run before the tool
returns. MCP has no poll channel to expose to agents, so full synchronous
execution is the correct shape there; an unknown collection returns
`No registration found for '<collection>'` and a registry or purge failure
surfaces as an error rather than a false success. All three surfaces (CLI,
HTTP, MCP) report the same fields — `collection`, `removed`, `deleted_chunks`.

Adding the CLI deregister poll grew `__main__.py` past its OO `module_size`
baseline, so the remote HTTP client machinery moved out of the CLI entrypoint
into a new module, `remote_client.py`. `RemoteClient` (a frozen dataclass bound
to one `[quarry]` config) owns the transport — `request`, `get`, `find`, and
the `await_task` poller — and `RemoteError` carries the HTTP status. This is a
behavior-preserving extraction: the CLI constructs `RemoteClient(config)` at
each remote branch and the local/remote routing (`_safe_proxy_config`) stays in
`__main__.py` where it decides which path a command takes. `remote_client.py`
is CLI-tier (it may raise `typer.Exit` and print to stderr for poll
failures/timeouts); it deliberately does **not** live in `remote.py`, which owns
a different concern — proxy config and TLS (`read/write_proxy_config`,
`fetch_ca_cert`, `validate_connection`).

## DES-027: jemalloc Memory Tuning for LanceDB Arrow Buffers

**Date:** 2026-04-18
**Status:** SETTLED
**Topic:** Daemon RSS growth from jemalloc arena retention

### Problem

The quarry daemon's RSS grew monotonically — +178 MB per sync cycle,
reaching 5.4 GB after one day of operation. `gc.collect()` did not
reclaim the memory. `tracemalloc` (Python-only) showed low Python
allocation growth. The anonymous heap (4.35 GB) was not file-backed
(not LanceDB mmap) and not attributable to the ONNX model (~110 MB).

### Root Cause

LanceDB's Rust core links jemalloc as its global allocator.
`batch_insert_chunks` calls `table.add(records)`, which creates Arrow
RecordBatches in Rust. After the write completes and the RecordBatch
is dropped, jemalloc retains the freed memory arenas for potential
reuse rather than returning them to the OS. With the default decay
settings (effectively infinite on Linux), freed pages accumulate
indefinitely.

### Evidence

Profiling the running daemon (PID 2370838, 5.4 GB RSS):

| Metric | Value |
|--------|-------|
| VmRSS | 5,548 MB |
| Pss_Anon | 4,349 MB (not mmap) |
| ONNX model | ~110 MB |
| Database on disk | 2.7 GB (62K chunks) |
| Unexplained heap | ~4.2 GB |

Empirical testing across 4 MALLOC_CONF variants (62K chunks, 3K docs):

| Config | Post sync 1 | Post sync 2 | Growth/sync |
|--------|-------------|-------------|-------------|
| No MALLOC_CONF | 5,400 MB (1 day) | cumulative | +178 MB/cycle |
| decay:1000 | 2,543 MB | 2,543 MB | stable |
| + narenas:1 | 1,760 MB | 2,292 MB | +532 MB |
| + tcache:false | 1,144 MB | 1,323 MB | +179 MB |
| + muzzy:0 | 1,147 MB | 1,356 MB | +209 MB |

### Design

Set `MALLOC_CONF=dirty_decay_ms:1000,muzzy_decay_ms:0,narenas:1,tcache:false`
in the daemon's environment:

- **Linux**: Written to `~/.punt-labs/quarry/quarry.env` by
  `_write_env_file()` in `service.py`. Systemd reads it via
  `EnvironmentFile=`.
- **macOS**: Set in the launchd plist `EnvironmentVariables` dict.

The four settings:

- `dirty_decay_ms:1000` — return dirty pages (freed, not yet
  returned to OS) within 1 second. Amortizes madvise syscall cost.
- `muzzy_decay_ms:0` — return muzzy pages (lazily purged) immediately.
  These are already MADV_FREE'd; the zero cost makes immediate
  return optimal.
- `narenas:1` — single allocation arena. Quarry's daemon does bulk
  writes in batches, not high-concurrency per-request allocation.
  Fewer arenas = less fragmentation = less retained memory.
- `tcache:false` — disable thread-local caching. Each thread's tcache
  holds freed objects for fast reallocation, but with batch workloads
  these caches hoard memory. No sync performance regression observed
  (4.92s vs 6.62s baseline).

### Rejected Alternatives

1. **`dirty_decay_ms:0`** — Immediate return for dirty pages too.
   Rejected: causes excessive `madvise` syscalls during bulk Arrow
   writes. 1-second decay amortizes the cost.
2. **Replace jemalloc with glibc malloc** — Not feasible. LanceDB's
   Rust binary links jemalloc at compile time. We don't control the
   LanceDB build.
3. **Use Arrow-native API instead of Python dicts** — Would reduce
   intermediate allocation pressure but doesn't address jemalloc
   retention of Arrow's own buffers. A complementary optimization,
   not a replacement.
4. **Periodic daemon restart** — Operational workaround, not a fix.
5. **decay:1000 only (without narenas/tcache)** — Tested: reduces to
   2.5 GB but still retains ~1.4 GB of fragmented arena memory.
   narenas:1 + tcache:false eliminate the fragmentation.
   The daemon should be able to run indefinitely.

## DES-028: CLI --verbose Maps to INFO, Not DEBUG

**Date:** 2026-04-18
**Status:** SETTLED
**Topic:** --verbose stderr level deviates from org CLI standard

### Problem

The org CLI standard (`punt-kit/standards/cli.md` §Global Flags) says
`--verbose` enables "debug logging." The logging standard
(`punt-kit/standards/logging.md` §Levels) specifies
`configure_logging(stderr_level="DEBUG")` for `--verbose`.

Quarry maps `--verbose` to INFO instead of DEBUG.

### Rationale

40 `logger.debug()` calls across 12 modules, many in hot loops:
database.py (11 per-query RRF stats), hooks.py (12 hook lifecycle),
embeddings.py (2 per-batch timing), text_extractor.py (2 per-page).
For a 100-file sync with 500 pages, DEBUG produces 1000+ lines
dominated by per-query fusion stats and per-page character counts.
INFO (80 calls across 16 modules) contains the operational messages
users want: sync plans, embedding throughput, batch-insert timing.

### Design

- `--verbose` sets `stderr_level="INFO"` in `configure_logging`
- `QUARRY_LOG_LEVEL=DEBUG` env var provides the developer escape hatch
- Third-party loggers (lancedb, onnxruntime, httpx) pinned at WARNING
  in the dictConfig `loggers` block to prevent noise at any level

### Rejected Alternative

Map `--verbose` to DEBUG per org standard. Rejected: 1000+ lines of
per-query RRF fusion stats and per-page char counts overwhelm the
terminal. The org standard's "debug logging" description fits tools
with fewer, coarser DEBUG calls — quarry's DEBUG is developer-trace
granularity.

---

## DES-029: quarry enable — Unified Project Activation

**Date:** 2026-05-11
**Status:** SETTLED
**Topic:** How projects activate all three knowledge capture types

### Design

`quarry enable <directory>` sets up three scoped collections in one command:

1. **File sync** — registers the directory for incremental sync (existing mechanism). Collection name derived from directory basename or `--collection` override.
2. **Passive captures** — web fetches and session transcripts route to `<name>-captures` (separate from the file-sync collection). Falls back to global `web-captures` / `session-notes` when no registration covers the cwd.
3. **Agent memory** — bootstraps ethos identity extensions (`quarry.yaml` with `memory_collection: memory-<handle>`) for all agent identities in `~/.punt-labs/ethos/identities/`.

`quarry disable <directory>` reverses: deregisters, optionally deletes indexed data (`--keep-data` to preserve), removes `.punt-labs/quarry/config.md`.

Session-start hook uses walk-up matching (child directories inherit the parent's collection) with a descendant guard (won't auto-register a parent that would subsume existing child registrations).

### Why This Design

Before `quarry enable`, the three capture types were configured through disconnected mechanisms: SessionStart auto-registration (crashed on child directories), hardcoded fallback collections (mixed content across projects), and manual ethos extension setup (nobody did it). One command replaces all three.

The captures separation (`<name>-captures` vs `<name>`) prevents web research and session transcripts from polluting the file-sync collection. Users searching their codebase get code, not browser tabs.

### Alternatives Considered

1. **Separate commands for each capture type** — Rejected. Three commands to set up what is conceptually one thing. Users forget the ethos step.
2. **Auto-enable on first session** — Rejected. CEO constraint: auto-registration stays for basic functionality, but `quarry enable` is for explicit configuration with captures separation and ethos bootstrap.
3. **Per-project config file only (no CLI)** — Rejected. The config file is written by `quarry enable`, not hand-authored. The command is the interface.

## DES-030: Session Transcript Lifecycle and Capture Strategy

**Status**: SETTLED (May 2026)

### Context

Claude Code stores session transcripts as `<session-uuid>.jsonl` files under `~/.claude/projects/<encoded-project-dir>/`. These are the primary source material for quarry's knowledge capture — each transcript contains the full conversation: research, debugging, design decisions, tool use.

### The Cleanup Problem

Claude Code deletes transcript `.jsonl` files older than `cleanupPeriodDays` (default: **30 days**) at startup. This is non-configurable per-project — it's a global setting in `~/.claude/settings.json`. The cleanup also covers `tool-results/`, `plans/`, `debug/`, and other session artifacts.

Known issues (as of May 2026):

- Auto-updates have silently deleted `.jsonl` files ([#41591](https://github.com/anthropics/claude-code/issues/41591))
- `--setting-sources local` ignores the setting and uses the 30-day default ([#45903](https://github.com/anthropics/claude-code/issues/45903))
- No warning or notification before deletion ([#46175](https://github.com/anthropics/claude-code/issues/46175))

### Decision

**PreCompact hook is the primary capture mechanism.** The hook fires before context compaction, while the transcript still exists in memory. It extracts conversation text and ingests it into quarry before Claude Code can clean it up. This is reliable regardless of `cleanupPeriodDays`.

**`quarry backfill-sessions` is a secondary, bounded recovery tool.** It ingests `.jsonl` files that still exist on disk — typically only sessions from the last 30 days. It cannot recover transcripts that have already been cleaned up. Its value is: (a) capturing the recent window when quarry is first installed, and (b) re-ingesting after a quarry database reset.

**The two mechanisms are complementary, not redundant.** PreCompact captures going forward; backfill captures the recent past. Neither can capture sessions older than `cleanupPeriodDays` unless the user has extended that setting.

### Filesystem Structure

```text
~/.claude/projects/<encoded-project-dir>/
    <session-uuid>.jsonl          # Main transcript (cleaned up after 30 days)
    <session-uuid>/
        subagents/                # Subagent transcripts
            agent-<id>.jsonl
        tool-results/             # Tool output artifacts
```

The encoded project dir replaces `/` with `-` and preserves the leading dash (e.g., `/Users/jm/code` → `-Users-jm-code`).

### Implications

1. Users who want full history should set `cleanupPeriodDays` to a high value (e.g., 365) in `~/.claude/settings.json` before sessions are lost.
2. `quarry enable` should advise users about the cleanup window.
3. Subagent transcripts (`subagents/agent-<id>.jsonl`) are not currently ingested by either mechanism — they are a future opportunity but not the primary knowledge source.

---

## DES-031: Daemon-First Architecture — One Engine, Formal Wire Protocol, Pure Clients

**Date:** 2026-05-16 (v1 PROPOSED); revised and **ACCEPTED** 2026-07-12 (v2.1); amended 2026-07-14 (v2.2)
**Status:** ACCEPTED
**Topic:** Separation of the compute/storage engine from access interfaces (CLI, MCP, Library) behind a formal wire protocol
**Supersedes:** the v1 "Engine-First Architecture with Thin Interfaces" framing (this entry replaces it). Full design + the 5-agent audit that grounds it: `docs/des-031v2-daemon-first.md`. The v2.2 amendment (below) is backed by `docs/des-client-architecture.md`. Epic `quarry-ma6f` / `quarry-ynvs`.

### Revision v2.2 (2026-07-14) — MCP is a client concern; `quarryd` binary; loopback `serve.token`

The operator ratified a reversal of two v2.1 decisions plus two additions.
Backing design: `docs/des-client-architecture.md`. The v1/v2.1 body below
otherwise stands; where they conflict, v2.2 governs.

- **Reverses §3.5 + §4.2 (MCP served BY the daemon).** MCP is a **client**
  concern, modeled on vox (`vox mcp` → `server.py` → `VoxClientSync`). The daemon
  serves the **REST API only** — the in-daemon `/mcp` route is deleted (landed in
  PR-1, #356). `quarry mcp` becomes a FastMCP **stdio subcommand** whose tools
  reach the daemon through `QuarryClient`; the Claude Code plugin runs
  `quarry mcp` directly. The "MCP via the daemon over `/v1/mcp`" wording in the
  §3.3 table and §3.5/§3.6 no longer applies.
- **DES-021 routing change (the tool is unaffected).** Quarry's plugin no longer
  routes its MCP path through **mcp-proxy** (`plugin.json` runs `quarry mcp`
  directly; the auth/reconnect the proxy handled for quarry moves into
  `QuarryClient`). mcp-proxy itself remains a live, supported tool used by other
  consumers — this is a change to quarry's own routing, not a supersession or
  retirement of the tool.
- **Adds `quarryd`** — a dedicated daemon entry point (like `voxd`/`luxd`) that is
  the sole engine process. This makes I1 a hard **process** boundary and removes
  the v2.1 §3.1 lazy-import carve-out for `serve`/`mcp`: no `quarry` /
  `quarry mcp` / `quarry-hook` process imports the engine; only `quarryd` (via
  `quarry/daemon/`) does.
- **Adds loopback `serve.token`.** The daemon writes a mode-0600 token beside
  `serve.port`; a `ClientConfig` resolves port+token (env → remote-login config →
  loopback files), so loopback requests are authenticated — closing the
  multi-user-host exposure §7 only aspired to. Vox-parity with `voxd`'s
  `serve.port`/`serve.token`.
- **Confirms** the §3.4 seam (`QuarryClient` + injectable REST transport; no
  vox-style gateway Protocol and no biff-style commands layer) and §3.1 hook
  routing (hooks call `QuarryClient`, not `Database` — faster than the cold
  in-process load and I1-correct).
- **Defers the richer error envelope.** The wire error stays `{"error": ...}` in
  v2-2 (a faithful contract *extraction*, not a wire redesign — `ErrorBody` is a
  superset via `extra="allow"`, preserving bug-class-3 parity). The
  `{code, message, detail}` envelope from §3.3 is a **breaking** wire change,
  deferred to land WITH `QuarryClient`'s typed `QuarryError` hierarchy in v2-4 so
  the codes and the client that consumes them move together (`RemoteClient` is
  already gone by then).

Sequencing under v2.2 (epic `quarry-ynvs`): PR-1 (#356) removed daemon MCP and
added pyright to CI; PR-2 introduces `quarry/api` + FastAPI + `/v1` (+ the missing
`/v1/optimize`, `/v1/backfill-sessions`); later PRs add `quarryd` / supervision /
`serve.token`, `QuarryClient` + the CLI thin-client (deleting `RemoteClient`), and
`quarry mcp`-as-client.

**PR-6 (`quarry-7ftj`) locked the boundary — the epic (`quarry-ma6f`/`quarry-ynvs`)
is complete.** An **import-linter** contract (`.importlinter`, run by
`make check-imports` in the `make check` chain and in CI) forbids every client
module (`quarry.__main__`, `quarry.hooks`, `quarry.mcp_server`, `quarry.client`,
`quarry.api`) from importing any engine package (`quarry.db`, `quarry.embeddings`,
`quarry.ingestion`, `quarry.retrieval`, `quarry.sync`, `quarry.daemon`) along any
*transitive* chain — a violating import fails CI, not review. A **full-client-surface
runtime sabotage test** (`tests/test_init.py`, each client module imported in a
subprocess with `lancedb`/`onnxruntime`/`pyarrow` poisoned) is the companion guard
that no engine reaches a client module's *module* scope. A hermetic **in-process
ASGI fixture** (`httpx.ASGITransport` over `build_app`) lets daemon-dependent tests
hit the real handlers without a socket, and one **real-loopback-TLS smoke** (slow
tier, out of the fast CI suite) covers the pinned-CA wire contract end-to-end;
**install health-gates** on `/health` `ready` before exit 0. Net: one engine in
`quarryd`, four thin client surfaces (CLI, hooks, library, MCP), the boundary
enforced structurally so it cannot regress.

**Correction to §3.1 enforcement-#2 of `docs/des-031v2-daemon-first.md`.** That
text assumed import-linter *cannot see* function-body lazy imports, so the
host-local engine-owning commands would be invisible to the static graph. With the
current toolchain (grimp 3.15) that is **false** — grimp resolves lazy imports.
The boundary therefore does **not** rest on lazy-import invisibility: the engine's
only entry point (`quarry.daemon.launcher`, the `quarryd` script) lives *inside*
`quarry.daemon` (engine-side, never a contract source), and the one sanctioned
exception — the host-admin diagnostics (`doctor`/`install`/`uninstall`) that probe
the local engine environment to report *why* a daemon is unhealthy — is an
**explicit, enumerated `ignore_imports` list** of the host-admin diagnostic lazy
edges (`doctor`/`doctor_captures`), not an invisibility trick. A new engine import from any other client-reachable module
still fails the contract; the sabotage test proves those diagnostics stay off the
module-scope hot path. (`quarry mcp` is no longer in the exception at all — PR-4
made it a pure client.)

#### PR-3a as landed (`quarryd` + `serve.token` + `ClientConfig`)

PR-3a implements the daemon/supervision/loopback-auth half of v2.2. What shipped:

- **`quarryd` entry point** (`quarry.daemon.launcher:entrypoint`) — the sole
  engine process. A `DaemonLauncher` refuses a remote-reachable bind that has no
  operator key, mints a 256-bit loopback token when none is given, and hands a
  `ServeConfig` to `DaemonServer`. The `quarry serve` subcommand is **deleted**
  (PL-PP-1, no shim); the supervised unit execs `quarryd` (launchd `KeepAlive` /
  systemd `Restart=always`). Because only `quarryd` (via `quarry/daemon/`)
  imports the engine, the v2.1 §3.1 lazy-import carve-out for `serve`/`mcp`
  is gone: I1 is now a hard **process** boundary, not a within-process rule.
- **`serve.token` (mode-0600).** `DaemonServer` writes the token beside
  `serve.port` (shared `RunDir`) **after a successful bind** and removes only
  what this instance wrote (guarded by a bound flag). Writing post-bind is
  what keeps a second `quarryd` that fails to bind (port in use) from
  clobbering a running peer's live token on the shared per-db path — a failed
  bind never writes or removes it. The token lands microseconds after the
  serve loop starts accepting; a client that races that window fails closed
  and retries, an acceptable trade for not nuking a live daemon. The write is
  atomic (`os.open(0o600)` + tmp-rename) so no world-readable or partial-file
  window exists. The token is written whenever
  the daemon has an effective key — **including** when the operator sets
  `--api-key` (the file holds that key), so a local client on a `--network`
  server can authenticate on loopback without re-typing it. This closes the
  multi-user-host exposure: before it, any local UID could reach the
  unauthenticated daemon on `127.0.0.1`.
- **`LoopbackPolicy`** classifies a host with `ipaddress`, and splits into two
  gates so one predicate never governs both a bind and a secret. **Bind gate**
  (`is_loopback`, daemon/installer): name-tolerant —
  `localhost`/`::1`/`127.0.0.0/8`/`::ffff:127.x` are loopback; `0.0.0.0`/`::` and
  any unresolved name are remote (fail closed — require a key). **Token-
  presentation gate** (`is_literal_loopback`, client): a LITERAL loopback IP
  only — a NAME is never a presentation target (see `ClientConfig`).
- **`ClientConfig`** (`quarry/client`) resolves a login config into (URL, pinned
  CA, bearer). The live `serve.token` is presented **only to a LITERAL loopback
  IP** (`is_literal_loopback`), never to a name: a name like `localhost` is
  resolver-controlled and on a dual-stack host can resolve to a co-tenant's
  `::1`, so presenting the secret to it would leak the token in transit. The
  managed path canonicalizes `quarry login localhost` → `127.0.0.1` at write
  time (a policy mapping to the IPv4 literal the daemon binds, not an OS-resolver
  lookup), so the normal case pins the un-hijackable `127.0.0.1:8420`. For a
  remote target the stored bearer is kept. It fails closed (`OSError` → typed
  `ClientConfigError`) rather than sending an empty bearer. **Residual:** a
  manual `quarry login ::1` while a co-tenant squats `[::1]:8420` and the real
  daemon is IPv4-only could still present the token to the squatter — non-default
  and operator-initiated; the managed install never stores `::1`. An
  endpoint-bound token (record the bind host beside `serve.port`, present on
  exact match) would erase it and is held pending an operator decision.
  The 13 `RemoteClient` construction sites route through `ClientConfig`, so any
  loopback CLI session now presents the token. `ClientConfig` resolves the
  token from the run dir of the process's **active database** (a
  `Settings.active_db` the CLI records from `--db`, else the persistent
  default), since `serve.token` lives under the daemon's startup-db run dir.
  **Limitation:** a loopback client cannot learn a non-default daemon's
  database from the URL, so loopback `--db` auth requires the operator to run
  a matching `--db` on both `quarry` and `quarryd`; the default,
  service-managed case (default database) is unaffected.
- **install `/health` gate** requires `state=="ready"`, not a bare HTTP 200 (a
  warming daemon returns 200 with `state=="starting"`).
- **Deferred (PR-4) — install success misconfigures the plugin's MCP transport
  (`quarry-lejv`, P1).** PR-1 removed the daemon's `/mcp` WebSocket, so the
  interim MCP transport is stdio `quarry mcp`, which loads the engine in-process
  until PR-4's mcp-as-client lands. But `install.sh`'s successful `quarry login`
  still writes `quarry.toml`, and `plugin.json` prefers `mcp-proxy` →
  `wss://…/mcp` — a now-dead route — so a fresh or re-install misconfigures
  Claude Code's MCP on the **success** path (a *failed* login instead leaves the
  working stdio `quarry mcp` fallback in place). PR-3a's loopback login also
  stores no bearer. Repointing the install MCP transport is PR-4's domain
  (mcp-as-client; `mcp-proxy` out of quarry's path), so it is deferred here
  rather than fixed in PR-3a, tracked as `quarry-lejv`. **Re-install guard until
  PR-4:** after re-installing, keep (or point) the Claude Code plugin at stdio
  `quarry mcp`; do **not** rely on the written `mcp-proxy` `quarry.toml` config.

**Rejected alternatives (operator/leader rulings):**

- **Route the local in-process CLI onto the daemon now** — rejected. It would
  break CLI usage when no daemon is running; that cutover (and its daemon-down
  nudge/fallback handling) belongs to `QuarryClient` in v2-3. PR-3a re-sources
  only sessions already in remote mode; the no-login local path is unchanged.
- **Trust a stored token for a loopback target** — rejected. `quarryd` mints a
  fresh token every restart (respawned under KeepAlive/`Restart=always`), so a
  stored token is stale immediately; loopback bearers are read live.
- **Auto-generate a token for a non-loopback bind** — rejected as false
  security: an auto-token in a local 0600 file is unreadable by the remote
  clients that need it. A non-loopback bind still requires an operator-set key.
- **Place the token-injecting construction seam in `__main__` (a free helper) or
  on `RemoteClient` (a classmethod)** — rejected: the first regresses the CLI
  god module, the second grows `remote_client.py` (deleted in v2-3). The seam
  lives in the new, absolute-gated `client` tier (`ClientConfig.remote_client`)
  so each CLI change is a call-site swap.
- **Defer the loopback gate-flip to a later PR** (keep loopback unauthenticated
  for now) — rejected. The gate flips in v2-3a; the interim `ClientConfig`
  wiring keeps every loopback client (the only clients that hit the daemon
  today) authenticated, so nothing is locked out and the exposure closes now
  rather than one PR later.
- **Write `serve.token` only when the token is auto-generated** (skip the file
  when `--api-key` is set) — rejected. Then a local client on a `--network`
  server would have no token to read on loopback and would fail closed even
  though a valid key exists. The file always holds the effective key.

### Context (audited reality)

A 5-agent audit (2026-07-12) established ground truth and corrected the v1 framing:

- **"Five engines per host" is a latent ceiling, not steady state.** The steady state is **one** resident engine — the `quarry serve` daemon (~1.6 GB RSS). `mcp-proxy` (~5 MB) and the menubar load no engine. The five ONNX-loading rows in the v1 table are per-invocation *possibilities*, not coexisting processes. The real driver is not duplication avoidance but **always-on incremental indexing**, which a per-invocation process cannot provide.
- **The transition v1 named never happened** — only the v1 ADR shipped. The CLI still builds the engine in-process for 18 data commands (a routing seam to `RemoteClient` exists from DES-021, but the local-engine branch was never removed). The library's public names (`Database`, `get_db`, `ingest_*`) *are* the engine. `mcp_server.py`'s stdio path loads the engine; `RemoteClient` is a de-facto thin client but CLI-coupled (`typer.Exit`/`SystemExit`). The HTTP surface has ~20 routes, **zero** Pydantic schemas, and no endpoint for `optimize`/`backfill-sessions`.
- **DES-037 already unified the *search* path** across surfaces via a shared `SearchService` library seam — bug-class-3 drift is closed for search, but every non-search command still has divergent dual paths. This design reuses `SearchService` as the daemon's internal `/v1/search` impl; it does not re-solve it. `mcp-proxy` (DES-021) already provides a thin MCP transport.

### The three invariants (operator-set)

- **I1 — Hard client/engine boundary.** CLI, MCP, and the library are **pure clients**; none may import or construct the engine (`Database`, `embeddings`, `ingestion.pipeline`, `retrieval`, `SyncRegistry`) in-process. The engine lives only in `quarry/daemon/`.
- **I2 — Daemon assumed always present.** One supervised, always-on engine per machine; clients assume it is there. Rationale: the daemon is the **one resident indexing engine** every trigger reuses instead of cold-starting ~1.6 GB per run — the precondition for always-on incremental indexing.
- **I3 — Well-specified wire protocol.** A formal, versioned REST contract is the single source of truth: shared Pydantic models generating an OpenAPI document, one `QuarryClient` conforming to it, used by CLI, MCP-thin, and library.

### Design

**Boundary (I1).** Three layers, enforced by *package membership*, not convention: `quarry/api` (Pydantic models + errors; zero heavy deps) ← `quarry/client` (`QuarryClient` + typed errors) ← client processes (`__main__`, hooks); `quarry/daemon` holds the engine and is never imported by a client. Enforced by an **import-linter** CI contract plus a **runtime-sabotage test** (import the client packages with `lancedb`/`onnxruntime` poisoned; assert success). The two host-local commands that legitimately need the engine (`serve`; `mcp` until v2-4 drops it) pull it in via a **lazy in-body import**, guarded by the sabotage test rather than import-linter. `disable` is a *hybrid*: its hook-config removal is host-local, but its chunk purge becomes a daemon call.

**Daemon-assumed (I2).** The daemon is the single engine, supervised (launchd `KeepAlive` / systemd `Restart=always`), loopback by default. It owns `SyncRegistry` and exposes `/v1/sync`; register/deregister/sync are thin mutations. `/health` reports `warming` vs `ready`. When the daemon is not running, the client **nudges the service manager** (kickstart / `systemctl --user restart`), polls `/health` under a ~30 s warm budget, then **fails fast** with a typed error distinguishing *not-installed* (→ `quarry install`) from *installed-but-down* (→ `quarry doctor`). There is **no in-process fallback** — a fallback engine would resurrect the dual-path drift I2 exists to remove. The always-on indexing *triggers* are decoupled to separate beads: interim scheduled sync (`quarry-tqdq`/`quarry-uae`) now, the event-driven watch/index loop with a serialized per-collection queue (`quarry-lxrk`) later.

**Wire protocol (I3) — FastAPI.** The daemon adopts **FastAPI** (Starlette-compatible: the WS `/mcp` route, the asyncio task system, lifespan startup, and bearer-auth middleware pass through unchanged), so request validation *and* the OpenAPI document derive from the same Pydantic models — no hand-rolled emitter or route registry. `quarry/api` holds one request/response model per operation (drift → import-time type error, the structural kill of bug-class-3 for non-search commands) and a uniform `ErrorBody` envelope via a FastAPI exception handler. All engine operations move under **`/v1`** (health/openapi unversioned); `/health` advertises `api_version`; a major mismatch raises `QuarryVersionError`. Long operations return `202 + TaskAccepted`, polled at `/v1/tasks/{id}`. The two missing operations (`/v1/optimize`, `/v1/backfill-sessions`) are added, closing split-horizon. `IngestRequest` is dual-mode (daemon-local *path* vs uploaded *bytes*, chosen by transport) so remote ingest cannot silently diverge.

**QuarryClient (I3).** One library-safe client (`quarry/client`): typed `QuarryError` hierarchy, Pydantic responses, **no `typer`/`SystemExit`/console**. `RemoteClient` is deleted; the CLI re-adds exit/print via a single boundary decorator at `__main__`. quarry-menubar (`../quarry-menubar`) is prior-art proof: a Swift `QuarryClient` over `URLSession` with pinned-CA TLS, zero Python-engine coupling — the OpenAPI doc is the shared contract both the Python and Swift clients conform to.

**MCP.** The in-process `quarry mcp` engine path is **dropped**; MCP is served by the daemon over `/v1/mcp` via `mcp-proxy`, reusing the warmed `ctx.embedder`/`ctx.database` (fixing an incidental third-ONNX-session defect). `SearchService` (DES-037) stays as the daemon's internal `/v1/search` implementation.

### Key decisions and rejected alternatives

- **FastAPI over Starlette + a hand-rolled OpenAPI emitter** (operator ruling: correctness over migration-avoidance). FastAPI *is* Starlette underneath, so the "risky migration" premise was false; the hand-rolled registry/emitter were gold-plating.
- **No in-process fallback** when the daemon is down — it would reintroduce bug-class-3.
- **MCP via `mcp-proxy` → daemon**, not an embedded thin MCP server (reuse over rebuild).
- **Watcher decoupled** from this epic into `quarry-lxrk`/`quarry-tqdq`.
- Keeping dual paths, dropping the daemon, or keeping `RemoteClient` CLI-coupled were all rejected (see `docs/des-031v2-daemon-first.md` §4).

### Scope and sequencing (supersedes the v1 6-PR table)

| PR | Bead | Scope |
|----|------|-------|
| v2-1 | `quarry-p8dq` | This ADR + `architecture.tex`/`CLAUDE.md`/README doc corrections (doc-only) |
| v2-2 | `quarry-qyrm` | FastAPI + `quarry/api` contract: schemas, `/v1`, OpenAPI, `optimize`/`backfill` endpoints, alias-route removal, menubar lockstep |
| v2-3a | `quarry-ufjt` | `quarryd` entry point + `DaemonLauncher`; `serve.token` (mode-0600) + `LoopbackPolicy` detection fix; `ClientConfig` loopback-token resolver + re-source the `RemoteClient` sites; supervised units exec `quarryd` (`Restart=always`); delete `quarry serve`; install `/health` `state==ready` gate |
| v2-3 | `quarry-veb0` | `QuarryClient` + typed errors; CLI thin-client (delete `RemoteClient` + engine branches); `disable` chunk-purge → daemon |
| v2-4 | `quarry-ydz5` | MCP over `/v1/mcp`; drop `quarry mcp`; reuse warmed `ctx` |
| v2-5 | `quarry-5e5t` | Library API = `QuarryClient` (pure in-repo removal of engine exports) |
| v2-6 | `quarry-7ftj` | Install `/health` gate + in-process ASGI fixture + import-linter boundary + one real-loopback-TLS smoke test |

The contract (v2-2) precedes clients dropping their engine paths; supervision (v2-3a) precedes fallback removal. No in-code shims (PL-PP-1): `RemoteClient`, `quarry mcp`, and the engine library exports are deleted, not aliased. The only cross-repo touch is the Swift client's `/v1` bump, coordinated in v2-2.

### Risks (summary; full table in the design doc)

Framework swap breaking the WS/task/auth path (mitigated: FastAPI inherits Starlette; the ASGI fixture exercises them); contract rewrite regressing a route (OpenAPI diff + structural equivalence); cold-start/wedged daemon (warm-budget poll + `restart` + typed not-installed-vs-down errors); remote-ingest divergence (dual-mode `IngestRequest`); the ASGI fixture not exercising TLS (one real-loopback-TLS smoke test rides the wheel gate). Default binding is loopback; remote is opt-in over the existing TLS + pinned-CA path; nothing weakens the trust model.

### Supersession

This ACCEPTED v2.1 replaces the v1 PROPOSED "Engine-First Architecture" ADR: it drops the "five engines" framing, names always-on indexing as the driver, reflects DES-037 and `mcp-proxy`, adopts FastAPI, decouples the watcher, and folds in the audited incidental defects (the `/mcp` third-ONNX-session, the stale `architecture.tex`/`CLAUDE.md` docs, the `/ca.crt` naming + task alias routes, the hardcoded `await_task` "Deregister failed" bug). Full design and audit: `docs/des-031v2-daemon-first.md`.

## DES-032: Daemon Resource Management — Thread Limits and OS Scheduling

**Date:** 2026-05-27 (revised 2026-05-29)
**Status:** SETTLED
**Topic:** Auto-detecting thread counts and OS scheduling hints for the quarry daemon
**Supersedes:** Original DES-032 (ONNX Thread Limits only)

### Problem

Three concurrent quarry processes (serve + ingest-background + CLI) each spin up ncpu ONNX threads + ncpu rayon threads + ncpu OMP threads. On 8 cores: 3×(8+8+8) ≈ 48-72 runnable threads → system load 148. Stack samples show rayon thread-pool workers and ONNX `RunInParallel` as hot frames.

Additionally, macOS App Nap throttles the windowless quarry daemon 5-10x without `ProcessType=Interactive`, because launchd classifies it as a background process eligible for power-saving throttling.

DES-027 sets `MALLOC_CONF=narenas:1,tcache:false` in the daemon environment. More threads × single arena = worse contention. Under sustained load the arena lock serialises all ONNX threads and throughput drops from 44 texts/s to 0.1 texts/s (440x slower).

### Evidence

| Scenario | Throughput |
|----------|-----------|
| Clean process, no MALLOC_CONF | 67.4 texts/s |
| Clean process, with MALLOC_CONF | 23.5 texts/s |
| Daemon after 20 min sync | 0.1 texts/s |

### Design

Auto-detect and cap all thread pools at construction time. Zero user configuration required.

| Parameter | CPU provider | GPU provider | Set where |
|---|---|---|---|
| `intra_op_num_threads` | `min(2, ncpu)` | `1` | `OnnxEmbeddingBackend.__new__` |
| `inter_op_num_threads` | `1` | `1` | `OnnxEmbeddingBackend.__new__` |
| `TOKENIZERS_PARALLELISM` | `false` | `false` | `os.environ.setdefault` |
| `OMP_NUM_THREADS` | `min(2, ncpu)` | `min(2, ncpu)` | `os.environ.setdefault` |
| `MALLOC_CONF` | DES-027 value | DES-027 value | plist/systemd env |
| macOS QoS | `ProcessType=Interactive` | `ProcessType=Interactive` | plist |
| Linux QoS | `Nice=-5` | `Nice=-5` | systemd unit |

Why each parameter has exactly one right answer:

- **`intra_op_num_threads`**: GPU offloads GEMM to CUDA so one CPU feeder thread suffices; CPU needs parallelism but more than 2 threads causes arena lock serialisation under `narenas:1`.
- **`inter_op_num_threads`**: Quarry runs a single model with sequential ops — no inter-op parallelism to exploit.
- **`TOKENIZERS_PARALLELISM`**: Disables rayon thread pool inside HuggingFace tokenizers — redundant when texts are already batched by `embed_texts`.
- **`OMP_NUM_THREADS`**: Caps OpenMP threads to match ONNX intra-op limit — without this, OpenMP spawns ncpu threads independently.
- **`ProcessType=Interactive`**: Asks macOS to exempt the windowless daemon from App Nap throttling (5-10x).
- **`Nice=-5`**: Asks systemd to keep the daemon's CPU priority high under load — search latency is user-facing.

**The OS scheduling hints are best-effort, not guarantees.** A negative `Nice` value requires privilege (`CAP_SYS_NICE` / `RLIMIT_NICE`). Under `systemctl --user`, the per-user manager generally cannot grant a negative nice, so systemd silently clamps the request to `0` — the daemon then runs at default priority and DES-032's latency guarantee does not strictly hold. `ProcessType=Interactive` is likewise an advisory QoS hint that the kernel may or may not honour. These hints improve latency where the platform permits and are harmless where it does not; the correctness of the daemon never depends on them. The thread-pool caps above are the load-bearing mitigation and are unconditional.

Note that DES-027's `MALLOC_CONF` interacts with thread count: more threads × `narenas:1` = worse contention. The thread limits here are specifically tuned for `narenas:1`.

**Session isolation** — the HTTP server's `_QuarryContext.embedder` creates a dedicated `OnnxEmbeddingBackend()` instance instead of sharing the `get_embedding_backend()` singleton with the sync pipeline. ONNX `session.run()` serialises callers via an internal mutex. With a shared session, a search query must wait for the sync's current embedding batch to complete — up to 70 seconds under `narenas:1,tcache:false`. Separate sessions eliminate this blocking. Memory cost: one extra ONNX model (~120 MB).

### Dependency

This decision depends on DES-027's `narenas:1,tcache:false`. If `MALLOC_CONF` changes to use multiple arenas or re-enables tcache, the thread limits should be revisited.

### Rejected Alternatives

1. **Increase `narenas` in MALLOC_CONF** — Tested with `narenas:4`. Reduced ONNX contention but increased jemalloc memory retention. With 4 arenas, RSS grows faster than DES-027's target. Also insufficient: queries still took 10+ seconds because session mutex contention is the primary bottleneck, not arena contention.
2. **Remove `tcache:false`** — Tested with decay-only MALLOC_CONF. RSS was 785 MB after 1 hour (DES-027 showed 2.5 GB/day without narenas+tcache). Unacceptable memory growth for a daemon that runs indefinitely.
3. **Async search route** — Making `_search_route` async and using explicit `run_in_threadpool` for blocking calls. Did not help: the bottleneck is the ONNX session mutex, not Starlette's thread dispatch.
4. **Process isolation** — Separate ONNX worker process. Eliminates all contention but adds IPC complexity. Disproportionate when session isolation achieves the same result in-process.
5. **User-configurable thread count** — Adding an `embedding_threads` field to `Settings`. Rejected for this PR because auto-detection covers all known hardware configurations correctly. Future work (DES-033) if edge cases emerge.

## DES-033: User-Configurable Embedding Thread Count — RESERVED

**Status:** RESERVED (not adopted)

Reserved by DES-032 (rejected alternative #5) for a possible `embedding_threads`
`Settings` knob. Not written: ONNX thread auto-detection covers all known
hardware. Open this entry only if a hardware edge case forces a manual override.
The number is held so DES cross-references stay stable — do not reuse it.

## DES-034: Bounded Progressive Commit for Sync Ingestion

**Date:** 2026-07-03
**Status:** SETTLED
**Supersedes:** DES-026 change #3 (batch-write-at-end)
**Topic:** Making ingestion incremental, crash-resilient, and memory-bounded

### Problem

DES-026 change #3 accumulated every `(chunks, vectors)` for a collection in
memory and did one `batch_insert` at the end. Consequences: nothing searchable
until the whole sync finished; a crash lost all embedding work; peak memory
scaled with collection size *and* with a single large file's chunk count
(`prepare_document` embedded a whole file at once). LanceDB (append-only, MVCC)
is fully capable of concurrent read/write and large data — the bottleneck was
quarry's batch-at-end usage, not the engine. Very large single files are a
primary use case, so the whole-file embed path was the load-bearing failure.

### Design

1. **Streaming producer** — a `DocumentStreamer` chunks a document once
   (assigning a document-global, contiguous `chunk_index`) and embeds it in
   bounded windows (`embed_window_chunks`, default 512), yielding
   `(chunks, vectors)` sub-batches so a single large document never materializes
   all its vectors. The window size is an embedding-throughput tuning seam
   (kpz per the CLAUDE.md pairing table).

2. **Bounded progressive commit** — a `ProgressiveIndexer` buffers windows and
   flushes to LanceDB when the buffered vector bytes reach `sync_flush_mb`
   (default 32 MB, **can fire mid-file**) and at end-of-collection. Each flush is
   Lance-add → **one** registry transaction covering every file the flush
   touched: completion rows for files whose final window is durable *and*
   advanced watermarks for files still mid-flight. All registry mutations for one
   flush commit atomically (single `conn.commit()`), so the registry can never be
   partially consistent with the one durable Lance version. Peak resident vectors
   are bounded by `N + queue_capacity × window`, independent of file or
   collection size. Writes serialize because exactly one consumer thread performs
   them (not `_table_lock`, which guards only `create_table`); a `_write_lock`
   additionally serializes producer overwrite-deletes against the consumer's adds.

3. **Fragment budget** — flush count is `O(total_vectors / N)`, independent of
   file count, keeping fragments two-plus orders of magnitude below the 10K guard
   so the single post-sync `optimize()` runs and compacts. A *file completion* is
   a registry checkpoint, not necessarily a Lance flush — small files coalesce
   into a shared flush, so a many-tiny-files collection cannot reopen the DES-026
   spiral.

4. **MVCC progressive visibility** — each flush commits a new manifest version;
   readers `open_table` per query (`chunk_search.py`) and snapshot-isolate from
   the writer, so concurrent search returns partial results without blocking. The
   vector channel sees fresh chunks immediately; FTS coverage lags until the
   post-sync FTS rebuild (graceful vector-only fallback). LanceDB MVCC provides
   blue/green visibility for incremental updates for free — no manual 2× shadow
   swap; a shadow swap is reserved for a full re-index (embedding-model change),
   scoped separately.

5. **Crash-resume (within-file, v1)** — committed flushes are durable. The
   `files` table carries a `chunks_committed` watermark and a `partial_hash`
   column; each flush advances the watermark for files it touched. On resume, a
   file with a partial watermark `w` re-enters ingestion and resumes *within* the
   file, re-embedding only `[w, total)`. Two rules keep this correct:
   - **Delete-tail-on-resume (required).** Before re-embedding, delete every
     chunk with `chunk_index >= w`. A crash between a mid-file `add` and the
     watermark commit can leave durable chunks `[w, K)` in Lance with the
     watermark unadvanced; without delete-tail, resume would re-add `[w, end)` and
     duplicate `[w, K)`. Delete-tail makes the document's Lance contents exactly
     `[0, w)` before re-embedding, so resume is idempotent under repeated crashes.
   - **Determinism precondition + fallback.** The watermark is valid only when
     re-chunking the same bytes yields byte-identical boundaries (holds for the
     deterministic text/PDF loaders). If `partial_hash != content_hash` (file
     changed) or the loader is non-deterministic (OCR/rapidocr), the watermark is
     discarded and the file is fully overwrite-deleted and re-embedded from
     `chunk_index 0`.

### Relationship to DES-027 / DES-032

Progressive flush bounds the resident **vector** working set to
`N + queue_capacity × window` in-process (source-text residency stays O(file)
until the loaders stream — a separate follow-on) and produces a bounded sawtooth
that DES-027's `dirty_decay_ms:1000` reclaims between flushes. Because the vector
working set is bounded in-process and DES-032's thread caps + session isolation
already protect query latency, **process isolation of the ingest worker is
deferred** (revisit only if benchmarks show query p99 regressing during large
syncs; the future path is an ingest subprocess writing Lance directly, read via
MVCC — candidate DES-035).

### Rejected / deferred

1. **Literal per-file flush** — reopens the fragment spiral on >10K-file
   collections (fragments decouple from data size). Rejected in favor of
   size-gated flush + per-file registry checkpoint.
2. **Add-without-delete-tail on resume** — not idempotent; a crash between a
   mid-file `add` and the watermark commit would duplicate the post-watermark
   chunks. Delete-tail-on-resume is required.
3. **Trusting the watermark under non-deterministic extraction** — an OCR re-run
   can produce different boundaries, so the watermark is discarded when
   `partial_hash` mismatches or the loader is non-deterministic.
4. **Streaming the loaders (bound source-text residency)** — deferred to a
   follow-on bead; v1 bounds the vector working set, not the extracted-text
   residency (O(file) via `_extract_pages`).
5. **`pyarrow.RecordBatch` build (drop the ~6× `.tolist()` transient)** — deferred
   to a follow-on bead (DES-027 rejected-alt #3).
6. **Process isolation** — deferred (see above).

## DES-035: Ingest-Worker Process Isolation — RESERVED (DEFERRED)

**Status:** RESERVED (deferred)

Reserved by DES-034 for a possible ingest subprocess that writes Lance directly
and is read via MVCC. Deferred: DES-034's in-process progressive commit already
bounds the vector working set, and DES-032's thread caps + session isolation
protect query latency, so a separate process is unnecessary. Revisit only if
benchmarks show query p99 regressing during large syncs. The number is held so
DES cross-references stay stable — do not reuse it.

## DES-036: Capture PII Redaction — Placeholder Emails, Bounded Local Hostname, Single Write Choke Point

**Date:** 2026-07-11
**Status:** SETTLED
**Topic:** Write-time PII redaction of captures (paths, emails, hostname, URL metadata)
**Relates:** DES-030 (capture lifecycle); quarry-ow3k (private shadow-repo sync, depends on this)

Captures (session transcripts and WebFetch auto-captures) previously scrubbed
secrets and profanity at write time but not PII. vox reported ~598
`/Users/<user>/` path findings in its captures. Because captures are the input
to DES-030's lifecycle and to the planned private shadow-repo sync (quarry-ow3k),
un-redacted PII in a capture is a leak into a git-tracked (and soon pushed)
surface. This ADR records the settled redaction invariant that ow3k depends on.

### Decision

Three write-time PII passes were added to `scrub.py`, composed as a `Scrubber`
class (secrets → paths → emails → hostname → profanity), and a single
`CaptureWriter` choke point (`capture.py`) now serves both `.md` producers
(PreCompact via `hooks.py`, backfill via `backfill.py`), replacing two duplicated
writers. WebFetch DB-ingest is scrubbed via an opt-in `content_scrubber`
parameter on `ingest_url`.

1. **Emails → `[REDACTED:email]` placeholder, not ethos-handle mapping.**
   Redaction is a security property: completeness (no false negatives) beats
   attribution. A placeholder redacts every address — team, third-party, pasted
   git-log authors — while handle-mapping would leak unknown emails and pull
   ethos identity resolution (YAML + filesystem walks) into a leaf text
   transform. Idempotent and zero-coupling.

2. **Hostnames → bounded to the local machine name** (`socket.gethostname()` +
   `.local` + short leaf ≥4, case-insensitive), not arbitrary dotted-token
   detection. Arbitrary detection has a catastrophic false-positive rate
   (`github.com`, `config.yaml`, `quarry.db.facade`, version strings). The actual
   PII is the operator's machine name; `gethostname()` targets exactly that.

3. **Ordering: email before hostname (hard constraint).** A hostname inside an
   email domain must be subsumed by the whole-email redaction; if hostname ran
   first it would produce `jim@[REDACTED:hostname]`, which the email regex then
   fails to match, leaking the local part `jim`.

4. **Redaction is at write time and fail-closed.** Scrub runs to completion
   before any atomic write; on scrub failure no file is written. Every pass emits
   a marker no pass can re-match, so `scrub(scrub(x)) == scrub(x)` (backfill may
   re-run). Both WebFetch ingress branches (primary + `ingest_url` re-fetch
   fallback) scrub before content reaches the pushable `web-captures` collection.

5. **URL metadata redaction via `CaptureUrl` (`capture_url.py`), not just the
   body.** The page body scrub (decisions 1–4) does not protect the URL itself,
   which is persisted as a capture's `document_name`/`document_path`. A fetched
   URL like `…/reset?email=a@b.com&token=xyz` would leak PII/secrets into the
   pushable `web-captures` collection even with the body scrubbed. `CaptureUrl`
   strips userinfo, query, and fragment and runs the bare `scheme://host/path`
   through the scrubber before it is stored, on **both** WebFetch branches
   (primary `hooks.py`, fallback `pipeline.py::ingest_url`) and for the dedup key.
   IPv6 host literals keep their `[...]` brackets so the netloc stays valid
   (`CaptureUrl._bracketed`). This is defence-in-depth distinct from body
   scrubbing, and it is the specific property `quarry-ow3k` relies on.

### Path pass scope

The path pass rewrites `/Users|/home/<user>/` → `~/` at a genuine path boundary,
including `file:///Users/…` and protocol-relative `//Users/…`, but a lookbehind
(`(?<!:/)`) excludes a URL scheme authority so `http://home/dashboard`,
`https://example.com/home/…`, and nested `/var/home/…` are left intact (they are
not a user home directory). This distinguishes a real home path from a hostname
or URL path segment.

### Scope boundary

`content_scrubber` on `ingest_url` defaults to `None` (byte-unchanged), so
user-initiated `quarry ingest <url>`, sitemap/bulk, and directory sync are NOT
scrubbed — only the WebFetch auto-capture path opts in. Redaction is a captures
concern, not a general-ingestion concern; deliberately-ingested documents keep
their content searchable.

### Rejected / deferred

1. **Ethos-handle email mapping** — false negatives on unknown addresses +
   layering/coupling cost; rejected for a security property (operator-ratified).
2. **Arbitrary hostname detection** — catastrophic false positives corrupt
   capture usefulness and code snippets; rejected (operator-ratified).
3. **Scrubbing globally in `_chunk_embed_store`** — would corrupt user-initiated
   ingests; rejected in favor of the opt-in WebFetch-only parameter.
4. **Git-history scrub of already-committed captures** — separate concern
   (quarry-mr0l). vox's public history verified clean (0 committed captures);
   forward redaction (this ADR) + shadow-repo sync (ow3k) are the go-forward fix.
5. **Unicode/IDN emails and SSH-remote (`git@host`) matching** — documented
   accepted limits of the ASCII email regex (over-match on SSH remotes is
   over-redaction, not a leak; IDN under-match is low-frequency).

## DES-037: Retrieval Seam — `RetrievalConfig` + `SearchService` for Single-Path, Reproducible Search

**Date:** 2026-07-05
**Status:** SETTLED
**Topic:** One production retrieval path shared by every surface and by the eval harness
**Relates:** DES-017 (hybrid algorithm), DES-031 (engine-first single path)

### Problem

Hybrid search lived in `search.py` and was invoked directly by each surface
(CLI, HTTP, MCP). This is the origin of bug class 3 (remote/local divergence):
the HTTP `/search` route once ran vector-only while the CLI ran hybrid, and query
params drifted between paths. There was also no seam to hold retrieval tunables
constant while measuring quality, which the eval work (DES tie-in below) requires.

### Decision

Introduce a `src/quarry/retrieval/` package that makes retrieval one object with
one entry point:

- **`RetrievalConfig`** (`config.py`) — a frozen config carrying the committed
  production baseline: `rrf_k=60`, `fetch_multiplier=3` (3× over-fetch),
  `metric="cosine"`, `embedding_strategy="baseline"`. This is the single place a
  tunable is defined, so an eval run can vary one lever and hold the rest fixed.
- **`SearchService`** (`service.py`) — the one `retrieve()` path. All three
  surfaces (`http_server.py`, `__main__.py`, `mcp_server.py`) now construct a
  `SearchService` and call it; none reach into the ranking internals. This kills
  the divergence bug class *by construction* — there is no second path to drift.
- **`HybridRetriever`** (`hybrid.py`), RRF **`fusion.py`**, **`reranker.py`**,
  and structural **`protocols.py`** (`Retriever`/`Reranker`) — the algorithm
  (DES-017) extracted out of `search.py` behind protocols so a lever
  (reranker, embedding strategy) can be swapped without touching callers.

The same seam is what the eval harness drives: `make eval` (PR #344) constructs a
`SearchService` from a `RetrievalConfig` and measures ranx metrics (per-bucket
MRR/success@k + a metadata-pollution diagnostic) against a known-item baseline.
Production and evaluation therefore exercise identical retrieval code.

### Rejected / deferred

1. **Leaving hybrid in `search.py` and adding an eval shim** — the shim would be
   a second path, reintroducing the divergence risk; rejected in favour of one
   `SearchService` both production and eval share.
2. **Baking lever choices in now** — `embedding_strategy` and reranker are behind
   protocols but the production default stays `"baseline"` until a lever wins on
   the numbers (a lever bake-off is future eval work; see
   `docs/eval-harness-design.md`).

## DES-038: Normalized Embeddings + Cosine Metric, with Honest FTS-Only Scores

**Date:** 2026-07-04
**Status:** SETTLED
**Topic:** Bounded, comparable similarity scores across both retrieval channels
**Relates:** DES-017 (hybrid), DES-037 (retrieval seam)

### Problem

Vector search ran LanceDB's default (unbounded) distance and reported
`similarity` without normalization, so scores were not bounded to `[0, 1]` and
were not comparable across queries. Separately, FTS-only result rows (matched by
BM25 but absent from the vector channel) reported a synthetic `1.00` similarity —
a fake perfect score that mis-ranked keyword-only hits and polluted any
score-based diagnostics.

### Decision

- **L2-normalize embeddings and search with the cosine metric** (PR #336). Every
  vector is unit-normalized at embed time and vector search uses
  `metric("cosine")` (`db/chunk_search.py`); `RetrievalConfig.metric` defaults to
  `"cosine"` (DES-037). Similarity is now bounded and comparable — a precondition
  for meaningful RRF fusion and for eval metrics.
- **FTS-only rows report their true cosine** (PR #339), computed against the
  query embedding, instead of the fake `1.00`. A `SearchResult` value type
  carries the real score so keyword-only hits rank honestly.

### Rejected / deferred

1. **Keeping unbounded distance and post-hoc rescaling** — rescaling a
   per-query, unbounded distance is not stable across queries; normalization +
   cosine is bounded by construction.
2. **Dropping FTS-only rows that lack a vector score** — they are legitimate
   keyword hits; computing their true cosine keeps them and ranks them correctly.

## DES-039: Private Capture Shadow-Repo Sync — `<repo>` → `<repo>-quarry`

**Date:** 2026-07-11
**Status:** SETTLED
**Topic:** Move redacted captures off the public repo into a per-project private
shadow; commit + push from `quarry sync`
**Relates:** DES-030 (capture lifecycle), DES-036 (write-time PII redaction — the
dependency this builds on)

### Problem

Redacted session captures land as `.md` files in the project's captures dir. The
public repo gitignores that dir, but the files have no durable home — they live
only on the operator's disk and are lost on a clean checkout. DES-036 redacts
only *new* captures at write time; captures already on disk predate it and are
un-redacted for PII. A naive "push whatever is there" first sync would leak PII
to a git remote on day one.

### Decision

1. **Per-repo private shadow `<repo>-quarry`** (not one org-wide captures repo).
   The gitignored captures dir is a standalone nested git working tree, never a
   submodule of the public repo, with a fail-closed **allowlist** `.gitignore`
   (`*` / `!.gitignore` / `!session-*.md`) so only quarry's own capture files can
   be staged — a stray non-`.md` file can never bypass the `.md`-scoped
   re-scrubber. Two fail-closed bootstrap gates: refuse if the captures dir is
   not gitignored by the parent, and refuse if the parent public repo already
   **tracks** any capture file (`git ls-files`). `.gitignore` does not untrack, so
   already-committed captures must be `git rm --cached`'d first; and an
   already-**pushed** capture additionally needs a history purge (`git
   filter-repo`/BFG + force-push, coordinated with the repo owner) since
   `rm --cached` does not rewrite history.
2. **Opt-in `shadow:` block** in `.punt-labs/quarry/config.md` (default
   `enabled: false`); remote derived as `<origin>-quarry` when unset.
3. **Push runs at the end of `quarry sync`** (fail-open) and via explicit `quarry
   captures push`; auth reuses the user's git credentials (no new secret storage).
4. **SECURITY (re-scrub only + trust boundary):** before each **COMMIT** (not
   just the push — `git push` ships all unpushed commits, so the gate is at
   commit time), re-scrub the staged `.md` bytes with the DES-036 `Scrubber`
   (idempotent: redacted files are no-ops; pre-fpc5 files get redacted), then run
   an I/O-race guard asserting the staged bytes are a `scrub` fixed point and
   ABORT-before-commit on any mismatch. `push` is never in a `finally`. The guard
   is **not** an independent PII oracle — it shares the scrubber's rules, so it
   catches stage/commit races, not scrubber blind spots. Residual PII classes the
   scrubber cannot catch (IDN/unicode email, non-`/Users`/`/home` paths,
   **cross-host** hostnames) are backstopped **solely** by the private-remote
   trust boundary — so visibility enforcement is **load-bearing**: verifiably
   public remotes are refused, and unverifiable visibility (no `gh`) requires an
   explicit `acknowledge_unverified`.
5. **Offline/failure:** git's local commit log is the durable, resumable queue;
   the next sync pushes accumulated commits.

### Rejected / deferred

1. **One org-wide captures repo** — breaks per-repo access control.
2. **Push on capture write** — chatty, risks blocking sessions; the push runs at
   sync time instead.
3. **A second, independent PII detector beyond the shared `Scrubber`** — it would
   only reproduce the scrubber's blind spots at double maintenance cost; the
   operator ruled re-scrub-only + trust boundary instead.
4. **Gating the push instead of the commit** — a poisoned commit leaks on the
   next push, so the gate must protect the commit.
5. **quarry auto-creating the private repo by default** — leak risk on a wrong
   owner or an accidental `--public`; configure-only by default, opt-in
   `--create` with `gh` private-verification.

---

## DES-040: Merge-Base OO/Coupling/Suppression Ratchet (adopt vox's tooling)

**Status:** Accepted (2026-07-12) · **Bead:** quarry-05mb

**Context.** quarry's ratchet was a 1,238-line `tools/oo_score.py` that compared
touched files against the **in-tree** `.oo-baseline.json` (`git diff HEAD~1..HEAD`
for the touched set). That model drifts and invites rebaseline-gaming: an author
can hand-edit the baseline inside a PR to lower the comparison floor. The sibling
repo `vox` rebuilt the ratchet as merge-base-scored packages, which closes that
gap; the operator ruled vox authoritative and directed adoption before further
DES-031 v2 work.

**Decision.** Adopt vox's tooling **verbatim** — `tools/oo_ratchet/` (13 modules),
`tools/coupling/` (17), `tools/suppression/` (9) — reducing the three old monoliths
to thin shims. Scoring compares the working tree against the baseline **committed
at `git merge-base origin/main HEAD`** (read via `git show <base>:<baseline>`), not
the in-tree file. `check-coupling` and `check-suppressions` join `check-oo` in the
`make check` chain; CI enforces with `--base-ref <merge-base> --require-base` and
`fetch-depth: 0` on PRs plus a `HEAD~1` push tripwire. This is a **hybrid** model:
the committed baselines stay (per-commit integrity lock + `--relax` audit anchor),
but the comparison floor is the immutable base-commit blob, so gaming is blocked at
every merge hop. Metric thresholds are unchanged (vox's tables were byte-identical
to quarry's); `.oo-baseline.json` values are byte-identical after cutover (zero
positional-only params in `src/quarry/`, so vox's PEP-570-aware `_avg_params`
yields the same numbers).

**The one quarry-origin divergence.** `tools/suppression/patterns.py` retains
quarry's `tokenize`-based suppression counter instead of vox's regex + AST
heuristic (which has documented blind spots: `# noqa` after `async def`, after
`obj.attr =`, on tuple targets, and single-line docstrings containing `noqa`). It
keeps vox's public interface so the rest of `tools/suppression/` stays verbatim.
This fix is to be upstreamed back into vox (quarry-njmr).

**Rejected / deferred.**

1. **Keep quarry's monolith** — retains the drift/gaming problem the move is meant
   to fix.
2. **Full-delete / recompute-from-source baseline** — kills the per-commit
   "commit `.oo-baseline.json`" chore, but it is an untested re-architecture that
   drops the audit log and vox's fail-closed ladder; net ratchet-strength
   regression. Operator ruled hybrid.
3. **Carry forward quarry's four baseline-era features** (`--verify` phantom guard,
   `--correct/--reason`, ratio-tolerance band, asymmetric `module_size` headroom) —
   operator directed taking vox's stricter behavior verbatim; only the tokenize
   suppression counter is retained.

**Consequence.** In-flight branches (esp. `qyrm` and the rest of the DES-031 v2
chain) must rebase onto post-adoption main and reseat their per-file baselines;
once `check-coupling` is a gate, a branch whose base predates adoption fails coupling
with "base commit predates baseline adoption — rebase onto current main" (correct
fail-closed behavior). Vox's stricter gate (no ratio tolerance, no asymmetric
`module_size` headroom) may block growth that previously passed.

---

## DES-041: One scrubbing content-ingest path — remember and capture collapse; the capture hook goes thin

**Status:** Accepted (2026-07-19) · **Bead:** quarry-en68 · **Full design:** `docs/des-capture-ingest.md`

**Context.** Two accidental gaps shared one fix. (1) The inline-content DB write did
not scrub: `RememberJob` called `ingest_content` with no scrubber, and the
PreCompact transcript capture rode the same path — so `memory-<agent>` and
`<repo>-captures` collections stored secrets/PII in cleartext. Only the git-tracked
`.md` capture file was scrubbed (DES-036 covered that surface only). (2) The
capture/PreCompact hook was the last fat client of the DES-031 daemon-first cutover:
it spawned a detached subprocess that built a full ~1.6 GB engine per compaction.
With no concurrency cap, ~14 cold engines oversubscribed an 8-core host to load
77-97 (quarry-lnog). An earlier draft assumed `remember` was *deliberately*
unscrubbed and proposed a separate scrubbing capture endpoint; the operator
established that assumption was false — remember simply never scrubbed, and should.

**Decision.** One scrubbing inline-content core, shared by two correctly-named entry
points, with the collection as a parameter (reuse the logic, never the name):
`remember` (`POST /v1/remember` → `memory-<agent>`) and `capture`
(`POST /v1/capture` → `<repo>-captures`, one door for both the transcript and
web-fetch triggers). Both build one `ScrubbedIngestJob` that always scrubs
server-side, on the worker thread, **before** embed/store (fail-closed: a raised
scrub writes zero chunks and — after DES-041's fix — deletes nothing on overwrite).
The capture hook becomes thin: it writes its durable local `.md` + raw archive, then
POSTs content to the resident daemon via `QuarryClient` fire-and-forget (202), and
returns — no per-hook engine (kills quarry-lnog), no engine import (unblocks the PR-6
boundary, gated by a runtime engine-sabotage test, not import-linter, since the
hook's imports were already lazy). Directory `sync` fills the `<repo>` core
collection from files on disk and stays **unscrubbed** (scrubbing source corrupts
it); an unregistered cwd falls back to `default-captures` via the standard
`<repo>-captures` pattern (the one-off `session-notes`/`web-captures` names are
retired). Captures are kept out of the core collection **structurally** —
`.punt-labs/quarry/captures/` is added to sync-discovery's `_DEFAULT_IGNORE_PATTERNS`
rather than relying on an ambient `.gitignore` line. As a security completion under
"ship means secure," a non-loopback bind now requires TLS (a key authenticates but
does not encrypt), and the client resolver refuses a plaintext non-loopback target
regardless of token presence — so raw pre-scrub content only ever crosses loopback
or TLS. Legacy cleartext already in the DB is **not** swept (operator-ruled
forward-only; a future full purge covers it).

**Rejected alternatives.** A dedicated capture endpoint justified by scrub semantics
(the false "remember must stay raw"); reusing the `/remember` name/`RememberJob` for
captures (overloads the name — a reader can't tell a capture from a remember); a
`scrub: bool` flag (a mode a caller sets wrong silently corrupts); a discriminated
`IngestRequest{url|content}` (revives the scoped-out user file-ingest surface); a
daemon path-read mode (arbitrary-local-file-read surface — content-over-path avoids
it); scrubbing source on sync (corrupts fixtures/paths/changelogs); relying on
`.gitignore` to keep captures out of `<repo>` (per-repo, fragile); scrubbing on the
event loop (blocks every other request); a one-time legacy-cleartext sweep
(forward-only ruling).

**Consequence.** `pipeline.py` decomposed 1,475→~1,090 (`ImagePreparer` consolidated
to one implementation, `TextLikeFormat`); `background_ingest.py` and `BackgroundIngest`
deleted (PL-PP-1, no
shim). Scoped ratchet relaxations were taken only for irreducible wire-contract /
security-gate growth on pure-schema/re-export files that carry no offsettable metric
(real principal paid on `pipeline.py`/`ingestion`), each justified in the audit logs
and verified against the merge-base. quarry-lxrk (the serialized per-collection queue)
remains the follow-on that bounds bursty concurrent captures; quarry-czf3 decides
whether an unregistered cwd should nudge-to-register rather than fall back at all.

## DES-042: Daemon-owned serialized capture/index queue

**Status:** Accepted (2026-07-20) · **Bead:** quarry-lxrk (queue portion) · **Full design:** `docs/des-capture-queue.md` (PR #371)

**Context.** DES-041 made captures fire-and-forget to the daemon, but bursty
concurrent captures (a compaction and a web-fetch landing together, several
sessions compacting at once) fired unbounded `asyncio.create_task` ingests that
oversubscribed the box — the load-90 starvation (quarry-lnog). The per-collection
LanceDB writer assumes a single caller; concurrent overwrite delete-then-adds
interleave so both chunk sets survive (lost update).

**Decision.** A daemon-owned serialized queue: one resident FIFO worker per
collection — one in-flight `progressive_insert` per LanceDB table, the
daemon-scope generalization of DES-034's single-writer invariant (not a fork;
`_write_lock`/`ProgressiveIndexer` untouched) — plus a global embed `Semaphore`
hard-clamped to 1 (DES-032: >1 buys no matmul parallelism behind the shared ONNX
session mutex and re-adds arena contention), and non-blocking bounded admission
(→ 503 on full, never a silent drop). The worker map is reaped when idle and
hard-capped so client-controlled collection keys can't accrue resident workers.
Drain-on-shutdown is bounded; an aborted un-durable job (remember/ingest) is
spooled to a private `0600` file with a truthful task status. No wire change.

**Rejected alternatives.** Reusing sync's `CollectionIngestor` (per-sync lifetime and
registry-coupled); a coroutine-level per-job timeout (inert against the
non-cancellable `run_in_threadpool` ingest — the hang is instead bounded at the
fetch's own socket timeout); `abandon_on_cancel=True` (detaches a thread that can
re-open the delete-then-add lost-update).

**Consequence.** Bursty-capture starvation is structurally closed. Follow-ons
filed as beads: remember-durability under drain-abort, total-deadline fetch
hardening, plain-ingest collection-key precision, abandon-safe interruptible
timeout, ingest-queue asyncio cleanup. The always-on watch/index loop
(quarry-lxrk residual) remains.

## DES-043: Fetch-safety — the SSRF gate runs on every fetch hop (redirects + sitemap crawl)

**Status:** Accepted (2026-07-21) · **Bead:** quarry-5pg1 · **Full design:** this entry

**Context.** The SSRF guard (`UrlSafetyCheck.reject_reason`) ran only on the
INITIAL source at the route boundary (`/v1/ingest`, `/v1/capture`) and was never
re-run downstream, so two categories of URLs reached the fetcher ungated.
(1) **Redirect targets** — `WebFetcher` delegated redirect-following to urllib's
auto-redirect, and the only post-redirect check (`_reject_non_html`) validated
the final URL's *scheme*, not its host/address, so a caller-controlled public
server could `302` to `169.254.169.254`, loopback, or a private range.
(2) **Sitemap-crawl URLs** — the crawl runs through `ultimate-sitemap-parser`
(USP), which fetched every sitemap-index, `robots.txt`, and sub-sitemap
server-side with its own `requests` client, recursing (to depth ~11) *before*
quarry saw any leaf entry; gating only the flattened leaf output missed all of
it. Both vectors are authenticated-only (a valid API key) and reachable
identically via CLI or MCP ingest — the daemon fetch is the shared choke point.
Distinct from, and a superset of, the DNS-rebind TOCTOU beads
(quarry-ljym/quarry-kmzo): entire URL categories never reached the gate, no DNS
trickery required.

**Decision.** Gate every fetch hop against the RESOLVED address, at the fetch
boundary, fail-closed — never after the fetch. `UrlSafetyCheck` relocates
daemon→core (`src/quarry/url_safety.py`) so the daemon route and the ingestion
fetch layer share one classifier without a package cycle. Two guarded seams,
both over a single module-level `GUARDED_OPENER`:

- **Redirects** — `SsrfGuardedRedirectHandler` (`ingestion/ssrf_redirect.py`)
  re-runs the gate on every 30x `Location` before opening it; `WebFetcher`'s
  final-URL check now validates the resolved host/address, not just the scheme.
- **Sitemap crawl** — `GatedSitemapWebClient` (`sitemap_web_client.py`)
  implements USP's `AbstractWebClient` and runs the gate on the initial URL and
  every redirect hop *before the socket opens*, threaded into BOTH USP entry
  points (`sitemap_tree_for_homepage`, `SitemapFetcher`), so index recursion,
  `robots.txt` `Sitemap:` lines, and nested sub-sitemaps are gated at every
  depth; a blocked target returns a non-retryable error so USP skips it and
  never connects. Discovered leaf entries are re-gated (`reject_unsafe`) as
  defense-in-depth.

The classifier rejects link-local, loopback, RFC-1918, CGNAT, unspecified,
IPv4-mapped-IPv6 variants (normalized to the embedded IPv4), and the NAT64
well-known prefix; it loops all `getaddrinfo` records (multi-record DNS → any
internal record rejects) and fails closed on a resolution error. No
TLS/connection-layer change.

**Rejected alternatives.** Gating only the leaf sitemap output (USP has already
fetched the indexes/robots/redirects server-side — the trap a mocked test hid);
IP-pinning the resolved address here (that is the DNS-rebind TOCTOU fix,
quarry-kmzo/ljym — a connection-layer change; gate-every-hop is complementary and
closes the redirect/sitemap gap without it); trusting the scheme-only
`_reject_non_html` check as sufficient.

**Consequence.** The redirect and sitemap SSRF vectors are closed at the fetch
boundary for both CLI and MCP callers, with dedicated unit coverage for the
classifier and both guarded seams and sitemap tests that drive USP's real
recursion (no wholesale mock). A residual DNS-rebind TOCTOU (the gate resolves,
the socket re-resolves) and network-specific NAT64 prefixes remain the tracked
pinning follow-up (quarry-kmzo/ljym), to be unified as validate-then-pin every
hop.

## DES-044: Fetch IP-pinning — connect to a connect-time-validated address (DNS-rebind)

**Status:** Accepted (2026-07-22) · **Bead:** quarry-kmzo + quarry-ljym · **Full design:** `docs` design note (`.tmp/design/dns-rebind-pinning.md`) + this entry

**Context.** DES-043 gates every fetch hop against the resolved address, but the
gate (`UrlSafetyCheck.reject_reason`) and the socket connect resolved DNS
*independently*: `reject_reason` ran `getaddrinfo` and validated, then threw the
result away, and `http.client`'s `connect` re-resolved the hostname at socket
time. A party controlling both an attacker-reachable URL (an `/ingest` source, a
capture `source_url`, a sitemap `loc`, a redirect `Location`) and an
authoritative DNS server could return a safe public IP at gate time and a
blocked internal/metadata IP at connect time (classic DNS rebinding). The window
was documented at `url_safety.py:44-48`; the metadata-name denylist did not close
the address-rebind.

**Decision.** Pin AND re-validate as **one** connect, so the change is
*impossible by construction* rather than merely mitigated. `PinnedHTTPConnection`
/ `PinnedHTTPSConnection` (`src/quarry/ingestion/pinned_connection.py`) perform
exactly one `getaddrinfo` on the safety path — inside `connect`, via
`UrlSafetyCheck.validated_addresses(host)` — and connect the socket to a
validated IP **literal** drawn from that same result set. There is no second,
independent resolution. The seam is the stdlib's own instance attribute
`HTTPConnection._create_connection` (set to `socket.create_connection` and called
by `connect`); the pinned classes override `connect()` to rebind it to a
validating connector, then call `super().connect()` — no `__init__` override
(PY-CC-1 clean), and via the MRO `PinnedHTTPSConnection(HTTPSConnection,
PinnedHTTPConnection)` the HTTPS `connect` still runs
`wrap_socket(server_hostname=self.host)` after the pinned TCP connect. One shared
`GUARDED_OPENER` composes the per-hop `SsrfGuardedRedirectHandler` + the pinned
HTTP(S) handlers + `ProxyHandler({})`; both attacker-reachable surfaces
(`WebFetcher.fetch`, `GatedSitemapWebClient.get`/USP crawl) use it, and each
redirect hop opens a fresh pinned connection that re-resolves-and-re-validates
the new host. `UrlSafetyCheck` gains `validated_addresses` (raises
`UrlRejectedError`, returns the validated address set, fail-closed,
all-records-reject-if-any-blocked) — the single seam the pinned connection calls
inside `connect`; `reject_reason` is retained as the None-means-safe
route-admission wrapper (it calls `validated_addresses` directly and is now pure
defense-in-depth whose divergence from the connect-time result is irrelevant to
safety). The block predicates (metadata denylist, CGNAT, IPv4-mapped
normalization) are one policy shared by both the admission and connect paths.

**Trust-domain invariant.** The pin narrows the *address*, not the *trust*.
`self.host` is never mutated → SNI = hostname, cert verified against the hostname
(never the pinned IP), `Host` header = hostname. The public-fetch context stays
`ssl.create_default_context()` (system trust store, `check_hostname=True`,
`CERT_REQUIRED`) — deliberately **not** the daemon-RPC pinned-CA context in
`client/`/`tls.py`, which is untouched.

**Rejected alternatives.** `reject_reason` returning `(reason, addrs)` and
rebuilding a per-fetch opener with those pinned addresses (P2) — reintroduces two
resolutions that must be proven equal and adds per-request plumbing to a
module-global opener for no security gain; switching the fetch path to `httpx`
with a custom resolver (the fetch path is entirely `urllib`; httpx lives only in
the daemon-RPC client — the switch would rewrite the redirect gate, size cap,
wall-clock deadline, and the USP `AbstractWebClient` adapter for nothing);
overriding `__init__` to rebind the seam (the `connect()` rebind is PY-CC-1
clean and needs no `__init__`); deleting `reject_reason` (churns route admission
and the final-URL checks for no safety gain).

**Consequence.** The DNS-rebind TOCTOU is eliminated on every attacker-reachable
fetch path (ingest, capture, sitemap crawl, redirect hops) with no TLS-trust
weakening, closing quarry-kmzo + quarry-ljym and completing the "validate-then-
pin every hop" mechanism DES-043 deferred. Covered by a headline rebinding
simulation (safe-at-gate / blocked-at-connect → `socket.create_connection` never
reached with a blocked literal) plus the five recurring bug classes (TLS
semantics, socket-leak failure injection, remote/local equivalence, exception
boundaries). `_create_connection` is a semi-private stdlib seam; a pin-target
assertion test fails loudly if a future CPython refactor bypasses it. `proxy.py`
(hardcoded GitHub hosts, install-time) is out of scope — not attacker-supplied.

## DES-045: Always-on filesystem watch/index loop (daemon-owned, all databases)

**Status:** Accepted (2026-07-22) · **Bead:** quarry-lxrk (watch-loop residual; the
DES-042 queue portion merged earlier) · **Full design:**
`.tmp/design/watch-index-loop.md` + this entry

**Context.** DES-031 named always-on incremental indexing as *the* first-class
rationale for the daemon: a per-invocation CLI cannot keep an index fresh as
files change. DES-042 landed the concurrency-critical half — the serialized
per-collection queue — but the daemon still only indexed on an explicit
`quarry sync`. The residual was the watch loop itself, and a prior draft was
non-ratifiable because it never reconciled its serialization against the
existing DES-042 / DES-034 / DES-026 layers.

**Decision.** The daemon runs one `WatchLoop` that is a **producer only**: it
watches the registered directories of **every** database in the on-disk roster,
debounces edit bursts, and submits `IngestUnit`s to the **existing DES-042
queue**. It invents no second queue and writes no LanceDB table directly, so it
inherits the whole stack unchanged — DES-042 per-table FIFO → DES-034
`ProgressiveIndexer`/`_write_lock` → DES-026 WAL + `busy_timeout`. The queue's
routing key becomes `(database, collection)` (`RouteKey`), extending
single-writer-per-table across the entire roster. Three producers (initial scan
on start, live watch, explicit `quarry sync`) all feed the one queue and thus
cannot race or double-write.

The fragment-budget-vs-fairness reconciliation (the crux the draft missed): a
small delta re-indexes as per-file `FileIndexJob`/`DocumentDeleteJob` (fragment
cost negligible, embed gate released between files for cross-collection
fairness); a burst above `watch_bulk_threshold` (default 50) for one collection
collapses to a single `CollectionSyncJob` running the unchanged
`CollectionIngestor` (DES-034's size-gated flush preserves the fragment budget).
The per-file DES-034 core is extracted as `SingleFileIndexer`, shared by both the
bulk path and `FileIndexJob` (a real paydown: `sync_ingest.py` module_size
285→237, efferent 8→5).

**FTS-rebuild coalescing is a hard resource constraint, not an option.** All
index writes use the daemon's persistent LanceDB connection, and
`create_fts_index(replace=True)` pins deleted-file readers the LanceDB Rust core
never evicts — so a per-file FTS rebuild would reopen the quarry-0dss fd leak.
`FileIndexJob` therefore never rebuilds FTS; a lone `CollectionFinalizeJob`
(`SyncFinalizer`) runs post-quiescence per `(database, collection)`, FIFO behind
the file jobs. `test_watch_session_does_not_leak_descriptors` proves the fd count
plateaus over hundreds of edits across two databases — the 0dss shape.

**All-databases (operator ruling, 2026-07-22).** The operator chose to watch
every registered database, not only the startup-active one. Each database gets
its own persistent connection + observer set (macOS FSEvents stream / Linux
inotify with a `PollingObserver` fallback on `ENOSPC`); the fd-plateau invariant
holds across all of them. **Trust invariant:** the only path to opening a
non-active database is enumerating the on-disk roster (`quarry_root` subdirs
holding a `registry.db`); no network/registry-request field can steer a DB-root
open — verified, no remotely-steerable path. This retires `quarry-uae`'s
rationale (continuous freshness without a timer) for every roster database.

**Rejected alternatives.** A parallel `asyncio.Queue` for the same tables (the
draft's sin — two writers, race hazard); a resident cross-job `ProgressiveIndexer`
per collection (a `delete_document` on a document whose windows are still
buffered resurrects stale chunks — needs a drain-before-foreign-job coupling not
worth it); one coarse `CollectionSyncJob` per fs-event (full rescan per keystroke,
holds the embed gate, breaks fairness); native inotify/FSEvents hand-rolling (two
platform paths watchdog already solves); polling-only as the primary (poll
latency + CPU walking large trees — the interim behavior being retired).

**Consequences.** Continuous freshness with no human action, bounded by
construction: embed-gate=1 (CPU), DES-034 windows (memory), coalesced FTS (fds),
per-`(database,collection)` FIFO (single writer). The explicit-sync 409 is
dropped in favor of transparent enqueue (202 + poll; the 409 stays for
`optimize`/`backfill`). `watch_enabled` defaults on (installing `quarryd` is
already the opt-in to a background engine; reverses the prior uae opt-in
posture). **`quarry-uae` fully retired:** a periodic roster reconcile
(`watch_safety_scan_s`, default 300 s) re-enumerates the roster each interval —
it begins watching databases created after `start()` (the mid-run-creation edge)
and re-submits any bulk scan the queue shed under load, so no registered
directory is left unindexed and no timer-based periodic sync is needed. The
reconcile runs synchronously on the loop between intervals (no interleave with
register/deregister) and is the single backstop for both the new-database and
shed-scan cases.
