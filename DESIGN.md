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

## DES-031: Engine-First Architecture with Thin Interfaces

**Date:** 2026-05-16
**Status:** PROPOSED
**Topic:** Separation of compute/storage engine from access interfaces (CLI, MCP, Library)
**Qualifies:** DES-021 (Remote CLI Routing — No Split Horizon), Principle P1 (Library-first) in `docs/architecture.tex`

### Context

Quarry has two independent axes that have been conflated in the as-built code:

1. **Interface** — how a caller talks to quarry: CLI commands, MCP tool calls, Python library imports.
2. **Engine** — what does the work: ONNX embedding model, LanceDB I/O, hybrid search, ingestion pipeline, sync registry.

The current architecture duplicates the engine into every interface. A single host can hold five engine instances simultaneously:

| Interface | Loads ONNX? | Opens LanceDB? | Runs pipeline? |
|-----------|------------|----------------|----------------|
| CLI (local mode) | yes | yes | yes |
| CLI (remote mode) | no | no | thin HTTPS client |
| MCP stdio (`mcp_server.py`) | yes | yes | yes |
| MCP WebSocket (via daemon) | no | no | thin client |
| HTTP server (`http_server.py`) | yes | yes | yes |
| Library import (`from quarry import …`) | yes | yes | yes |
| Daemon (`quarry serve`) | yes (once) | yes | yes |

P1 ("Library-first") in `docs/architecture.tex` describes this as-built reality, not the target. It was correct for the original CLI-only design from 2024. With a supervised daemon now installed on every host (`launchd` on macOS, `systemd` on Linux), the principle is stale: "the library does the work" is true only because the library is the engine.

DES-021 added "no split horizon" for the remote case (logged-in users route every data command to the remote daemon). DES-031 completes that pattern: the daemon is always the engine, on every host, for every interface.

### Design

**One engine, many interfaces.**

```text
                              ┌─────────────────────────┐
   CLI ──────────────────────►│                         │
   MCP (stdio via mcp-proxy)  │  quarry serve (daemon)  │
   MCP (WebSocket)            │      THE ENGINE         │
   Library (QuarryClient)     │                         │
                              └─────────────────────────┘
```

The daemon (`quarry serve`) owns the ONNX model, LanceDB, pipeline, sync registry. Every data operation runs there. Interfaces are thin clients.

**Interface responsibilities:**

- **CLI** — Typer command parser → HTTPS request → response renderer. No `lancedb`, `onnxruntime`, `pyarrow`, or pipeline imports.
- **MCP** — `mcp-proxy` (Go binary) bridges stdio ↔ daemon WebSocket. The Python `mcp_server.py` stdio path is dropped; the WebSocket `/mcp` endpoint on the daemon stays.
- **Library** — `QuarryClient` Python class makes HTTPS calls to the daemon. No engine imports. Used by embedded callers (QuarryMenuBar, scripts, tests-via-fixture).

**Local-only commands (genuinely host-local, no engine dependency):**

| Command | Purpose |
|---------|---------|
| `serve` | Is the engine |
| `install` | Bootstrap: set up engine, daemon, TLS, plugin |
| `login` / `logout` | Configure which daemon to talk to |
| `doctor` | Diagnose host environment (model file, daemon health, MCP config) |
| `uninstall` | Tear down install |
| `version` | Static metadata |

**Everything else is a thin client:** `find`, `ingest`, `remember`, `delete`, `show`, `status`, `list`, `sync`, `register`, `deregister`, `use`, `optimize`, `backfill-sessions`.

**Shared request/response schemas.** Pydantic models for every endpoint, imported by both the CLI client and the HTTP server handler. Param drift becomes a type error at the import site, not a silent omission.

### Why This Design

1. **Eliminates Bug Class 3 by construction.** CLAUDE.md documents remote/local divergence as the recurring failure mode through 10 TLS review rounds: HTTP server forgetting params the CLI sends, response JSON dropping fields the CLI renders. With one path, the class disappears.

2. **The daemon already exists, is supervised, and is the universal access point on every install.** Five engine copies on one host is not a feature; it is unmanaged duplication that DES-021 already started removing.

3. **Matches the standard pattern for systems with shared state.** Docker has `dockerd` + thin `docker` CLI. Postgres has the server + `psql` client. Redis has `redis-server` + `redis-cli`. The "library" in all three is a client API, not the engine. Quarry's shared state (the LanceDB corpus, the loaded model) makes it the same shape.

4. **Reduces `__main__.py` substantially.** Every data command currently has a 30–60 line `if proxy_config: ... else: ...` dispatch. Thin-client form is ~5 lines per command. The Phase 4–7 OO refactoring on `oo/phase-4-services` becomes easier, not harder, because most of the file goes away.

5. **Aligns with adopted principles.** P3 ("One daemon, many clients") and DES-021 ("No split horizon") both point in this direction. DES-031 names the target and finishes the transition.

### Alternatives Considered

1. **Keep dual paths, fix divergence with stricter testing.** Rejected. The testing rules in CLAUDE.md (CLI/HTTP equivalence tests for every param) are valuable but reactive — they catch drift after it happens. The drift keeps happening because the structure permits it. Eliminating the structure eliminates the class.

2. **Drop the daemon, use direct library access everywhere (CLI-only).** Rejected. P3 (shared ONNX model across MCP sessions) requires a daemon. Five Claude Code tabs cannot each load 200 MB of model into RAM.

3. **Hybrid: data ops daemon-mediated, admin ops local.** This is the design (see "Local-only commands" above). The split is principled — anything that touches documents goes through the engine; anything that configures the host stays local. The rejected version is per-command discretion ("`status` is cheap, run it locally") which is exactly the split horizon DES-021 forbade.

4. **Make the library API the canonical surface and add a thin daemon for MCP.** Rejected. This is the current architecture inverted; it preserves "library = engine" and leaves every CLI invocation responsible for model loading and DB locking. The daemon already supervises both; making it canonical removes the duplicate engine code, not the daemon.

### Scope and Sequencing

Six PR-sized slices, each independently revertable. Implementation order matters because the HTTP API must be complete before any client can drop its local engine path.

| PR | Scope |
|----|-------|
| 1  | DES-031 ADR + `docs/architecture.tex` revisions (P1, §Daemon Model, §CLI Independence, §Deployment, concurrency matrix) + README diagram |
| 2  | HTTP API completeness: every CLI param has an endpoint; shared Pydantic schemas for requests and responses |
| 3  | CLI refactor: every data command becomes a thin client; `__main__.py` dispatch removed |
| 4  | `mcp_server.py` stdio path: collapse to thin proxy or drop in favor of `mcp-proxy` |
| 5  | Library API: introduce `QuarryClient` (HTTPS client class); embedded callers migrate |
| 6  | Install/test fixtures: install verifies daemon is up before exiting 0; pytest fixture starts an in-process daemon per session |

PR 1 is doc-only and sets the contract. PRs 2–5 are implementation slices that can land in dependency order. PR 6 closes the loop on bootstrap and tests.

### Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Test suite slows down from per-test daemon startup | Session-scoped pytest fixture: one daemon per test run, not per test |
| Install bootstrap fails if daemon doesn't start | Install script verifies `quarry serve` is listening via `/health` before exiting 0 (already in scope for Phase 4.8–4.12 OO refactor) |
| First-install commands need an engine before daemon exists | Only `install`, `serve`, `doctor`, `login`, `version` run pre-daemon; they don't need engine access |
| Embedded callers (QuarryMenuBar) break | `QuarryClient` shipped in PR 5 before old library API is removed; deprecation cycle of one release |
| Fly.io container ops that run without a daemon | Container entrypoint always starts the daemon; ops invoke the CLI which talks to the local daemon — same pattern as developer machines |

### Documents Made Misleading by This Change

- `docs/architecture.tex` §Overview P1 ("Library-first")
- `docs/architecture.tex` §Daemon Model → §CLI Independence ("The CLI does not use the daemon")
- `docs/architecture.tex` §Deployment Topology diagrams
- `docs/architecture.tex` §Operation Concurrency Model (the "CLI (local)" column collapses)
- `README.md` "How it works" section
- `prfaq.tex` risk register (Bug Class 3 reduction)

All revisions ship in PR 1.

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
