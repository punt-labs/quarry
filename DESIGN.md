# Quarry Design

Design decisions for contributors and agents. For system architecture, module responsibilities, configuration, and deployment, see [`docs/architecture.tex`](docs/architecture.tex). For user-facing documentation, see [README.md](README.md).

## Key Design Decisions

### Fire-and-Forget MCP Pattern

Side-effect MCP tools (ingest, delete, sync, register, deregister, remember) return an optimistic response immediately and process in a bounded `ThreadPoolExecutor(max_workers=4)`. This is necessary because:

1. MCP tool calls block the LLM's response stream. A 30-second ingest blocks Claude from responding.
2. Quarry also deploys as a REST service (`quarry serve`) where request throughput matters.
3. The bounded pool prevents resource exhaustion under concurrent requests.

Settings and database connections are **snapshotted at the tool boundary** and passed into background `_do_*` functions. This avoids a race condition where the mutable `_db_name` global could change between the tool call and the background thread's execution.

Exceptions in background threads are logged via `logger.exception()`, not raised — the MCP caller already received the optimistic response.

### Protocol-Based Typing

Third-party libraries without type stubs (LanceDB) are typed via Protocol classes in `types.py`. This gives us:

- Exact type checking without `Any`
- Mockable interfaces for testing
- No runtime cost (protocols are erased at runtime)

### Named Databases

`resolve_db_paths(settings, name)` maps a database name to a path under `QUARRY_ROOT`. Each database is fully isolated — its own LanceDB directory, sync registry, and vector index. The `use` command (CLI) and `use` tool (MCP) switch the active database. This enables work/personal separation and per-project databases.

### Embedding Model

snowflake-arctic-embed-m-v1.5: 768-dimensional, 512 token context. Auto-downloads on first use (~500 MB). Chosen for:

- Strong retrieval quality for its size
- Runs efficiently on CPU (no GPU required)
- Permissive license (Apache 2.0)

### Bearer Token Authentication

`quarry serve --api-key` (or `QUARRY_API_KEY` env var) gates all HTTP endpoints except `/health` behind `Authorization: Bearer <key>`. Design choices:

- **`/health` is exempt** — load balancers and uptime monitors need unauthenticated health checks.
- **OPTIONS is exempt** — CORS preflight requests don't carry auth headers.
- **`hmac.compare_digest`** for token comparison — prevents timing attacks.
- **Case-insensitive scheme** — `Bearer`, `bearer`, `BEARER` all accepted per RFC 7235.
- **Empty key = no auth** — `QUARRY_API_KEY=""` is treated as unset, preventing accidental trivial bypass.
- **No auth when omitted** — local use (menu bar, CLI) requires no key. Auth is opt-in for production deployment.

### Container Deployment (Fly.io)

Multi-stage Dockerfile: (1) install Python deps with `uv sync`, (2) download the embedding model at build time via `hf_hub_download`, (3) slim runtime image with baked-in model. LanceDB data lives on a Fly persistent volume at `/data` (`QUARRY_ROOT=/data`). The `--host 0.0.0.0` flag binds to all interfaces for container networking. Auto-stop scales to zero when idle; cold start is ~5s with the baked-in model (no download on first request). TLS is terminated by Fly's proxy.

### Threaded Request Handling

> **Superseded by [ASGI Server](#asgi-server-starlette--uvicorn).** Kept for historical context.

`QuarryHTTPServer` extends `ThreadingHTTPServer` — each request gets its own daemon thread. This prevents a slow embedding (the dominant latency) from blocking other clients. Thread safety relies on immutable shared state: `_QuarryContext` fields are set once at startup, LanceDB handles concurrent reads internally, and ONNX Runtime sessions are thread-safe for inference. Alternative considered: uvicorn/starlette (async) — rejected as unnecessary complexity for 5 synchronous endpoints with no I/O multiplexing benefit.

### ASGI Server (Starlette + uvicorn)

Supersedes [Threaded Request Handling](#threaded-request-handling). The addition of MCP-over-WebSocket required native async support, which stdlib `http.server` cannot provide. `quarry serve` now runs a Starlette ASGI app on uvicorn. REST handlers remain sync functions — Starlette auto-detects this and runs them in its threadpool, preserving the original concurrency model where a slow embedding doesn't block other clients. The `/mcp` WebSocket endpoint is async, bridging WebSocket frames into MCP's `Server.run()` via anyio memory streams. Thread safety invariants are unchanged: `_QuarryContext` fields are set once at startup, LanceDB handles concurrent reads internally, and ONNX Runtime sessions are thread-safe for inference. Default port is 8420 (`DEFAULT_PORT` in `quarry.config`), overridable with `--port`.

### MCP-over-WebSocket

The `/mcp` WebSocket endpoint enables multiple Claude Code sessions to share a single quarry daemon via mcp-proxy, avoiding duplicate embedding model loads and database connections. Each WebSocket connection gets its own asyncio Task with a `ContextVar` for database selection (`_db_name`), so `use_database("work")` in one session doesn't affect others. `Server.run()` creates a local `ServerSession` per call — safe for concurrent use without shared mutable state. The `FastMCP._mcp_server` attribute is accessed via `getattr` with a runtime guard and actionable error message; `mcp` is pinned to `<2.0.0` to protect this private API usage.

Security: Origin-based CSWSH protection (browsers always send `Origin`; non-browser clients like mcp-proxy don't), Bearer auth checked before WebSocket accept, and session keys are sanitized (control chars stripped, truncated to 64 chars) before logging (CWE-117).

### Configurable CORS Origins

`--cors-origin` (repeatable) controls `Access-Control-Allow-Origin`. The server reflects the request's `Origin` header only when it matches the allow list, and adds `Vary: Origin` to signal cacheability varies by origin. When no `Origin` header is present (e.g. non-browser clients), no CORS headers are emitted. Defaults to `http://localhost` for backward compatibility with quarry-menubar.

### Log Redaction (CWE-532)

> **Superseded by [Log Safety](#log-safety-cwe-532-cwe-117).** The query string redaction approach was specific to stdlib `http.server` access logs.

Access logs redact query strings from request lines. The `_redact_query_string` method parses HTTP request lines (`GET /path?q=secret HTTP/1.1`) to strip the query while preserving the method, path, and HTTP version. The `_handle_search` handler logs only result count, never the raw query. Error handlers use `urlparse().path` to strip queries.

### Log Safety (CWE-532, CWE-117)

Supersedes [Log Redaction](#log-redaction-cwe-532). Uvicorn's access log is disabled entirely (`access_log=False`), eliminating query string leakage at the source rather than redacting after the fact. The search handler continues to log only result count, never the raw query. WebSocket session keys are sanitized before logging: control characters stripped via `_CONTROL_CHAR_RE` and truncated to 64 characters to prevent log injection (CWE-117) and log flooding.

### Single Table Design

All chunks live in one LanceDB table (`chunks`). Document and collection boundaries are columns, not separate tables. This simplifies cross-document search and avoids table proliferation. Filtering by document/collection/page_type/source_format uses LanceDB's built-in filter predicates on the vector search.

## ADR: Convention Hints via Hook Accumulator

**Status:** Proposed
**Date:** 2026-03-09

### Context

Claude Code agents drift from project conventions during sessions — running `uv run mypy src/` instead of the full quality gate chain, staging with `git add -A` instead of specific files, using `pip` instead of `uv`. The conventions are documented in CLAUDE.md, but long documents lose influence as context grows.

The v1.1.0 hook wiring gap (quarry-g6x, quarry-dx9, quarry-5lr) demonstrated that quarry's hook integration layer is the right place for passive, fail-open interventions. The question is how to surface relevant conventions without blocking valid workflow.

### Decision

Add a **PreToolUse hook** that accumulates recent tool calls in a state file and emits soft hints (`permissionDecision: "allow"` with `additionalContext`) when command sequences violate project conventions.

### Design

#### Two-Class Rule System

**Instant rules** fire on the current command alone. No accumulated state needed. These catch unambiguous anti-patterns:

| Pattern | Hint |
|---------|------|
| `git add -A` / `git add .` | Stage specific files by name |
| `pip install` | Use `uv` for package management |
| `git push --force` | Force-push is destructive — confirm intent |
| `git commit --no-verify` | Do not skip hooks unless explicitly asked |

**Sequence rules** require temporal context from the accumulator. These detect patterns that are only wrong *in sequence*:

| Sequence | Hint |
|----------|------|
| `git commit` without a preceding full gate chain | Run quality gates before committing |
| 2+ solo gate tools (`uv run ruff`, `uv run mypy`, ...) in a row without `&&` | Use the full gate chain instead of individual tools |

#### Accumulator Design

Adapted from biff's `DisplayQueue` pattern (`biff/src/biff/server/display_queue.py`): a timestamped rolling window of recent tool calls, persisted to a JSON file so state survives across hook invocations within a session.

```text
State file: $TMPDIR/hint-state-{session_id}.json

[
  {"ts": 1741556400.0, "tool": "Bash", "command": "uv run mypy src/"},
  {"ts": 1741556410.0, "tool": "Bash", "command": "uv run ruff check ."},
  {"ts": 1741556420.0, "tool": "Bash", "command": "git commit -m \"fix: ...\""}
]
```

Properties (following biff's accumulator discipline):

- **Time-based expiry**: Events older than 5 minutes are pruned on each hook invocation. Prevents stale context from triggering false hints.
- **Session scoping**: State file is keyed by `session_id` from the hook payload. Concurrent sessions don't cross-contaminate.
- **Pure matcher**: The sequence matcher is a pure function `(events) → hint | None`. No I/O, deterministic, testable with synthetic event lists.
- **Fail-open**: Any error in the hook (state file read failure, malformed JSON, matcher crash) produces no output. The command runs unimpeded.

#### Hook Payload Interface

**Input** (PreToolUse stdin):
```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {"command": "git commit -m \"fix: ...\""},
  "session_id": "abc123",
  "cwd": "/Users/.../quarry"
}
```

**Output** (stdout, only when a hint fires):
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "additionalContext": "Reminder: run the full quality gate before committing: uv run ruff check . && ..."
  }
}
```

The `permissionDecision: "allow"` is critical — hints are advisory, never blocking. Claude sees the `additionalContext` as a system message and can decide whether it applies. This eliminates the false-positive problem: a targeted `uv run pytest tests/test_hooks.py -v` during development is fine; the hint only fires when `git commit` follows without a full gate run.

#### Matching Strategy

Regex-based for latency. The hook runs on every Bash tool call; it must complete in <50ms. No embedding search, no LLM inference, no network calls. Pattern matching against the accumulated command strings.

The quality gate rule illustrates the sequence matching logic:

```
on git commit:
  scan events[-10:] for a command containing the full gate chain
  if not found:
    emit hint with the project's gate string
```

The full gate string is project-specific. For quarry: `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ tests/ && uv run pyright && uv run pytest`. This could be read from a config file or hardcoded per-project in the hook.

#### Where This Lives

In quarry's plugin hook layer (`hooks/` + `src/quarry/hooks.py`), registered as a PreToolUse entry in `hooks.json`. The accumulator state file lives in `$TMPDIR` (session-scoped, gitignored).

Quarry is the right home because:
1. The hook infrastructure is already built and tested (v1.1.0 wiring).
2. The accumulator pattern is borrowed from biff but scoped to quarry's conventions use case.
3. Future evolution: swap regex matching for semantic search against a `conventions` collection when latency allows.

### Alternatives Considered

**Prompt-based PreToolUse hook** — Claude Code's newer API lets the hook provide instructions and Claude itself decides whether to block. More nuanced but adds LLM latency (~1-2s) to every Bash call. Rejected for the common case; may be reconsidered for complex rules that resist regex.

**UserPromptSubmit injection** — Inject convention reminders when the user submits a prompt. Lower friction but fires too early (before Claude has decided what commands to run) and can't react to specific commands.

**Hard blocks (`permissionDecision: "deny"`)** — Block the command outright. Higher enforcement but brittle with false positives. A blocked targeted test during development would be disruptive. Soft hints preserve flow while still nudging.

**Quarry semantic search on every tool call** — Search a `conventions` collection with the command as query. Higher quality matching but ~200ms latency per call (embedding + vector search). May be the evolution path when the regex approach hits its limits.

### Consequences

- **Convention adherence improves** without manual vigilance. The agent gets the right context at the right time.
- **False positives are non-blocking** — worst case, Claude sees an irrelevant reminder and ignores it.
- **State file adds complexity** — the accumulator must handle concurrent writes, corruption, and stale data. Biff's pattern (write-on-every-access, prune-on-read) is proven.
- **Project-specific rules** — the gate string and patterns are hardcoded to quarry's conventions initially. Generalizing to per-project config is a future step.
- **Latency budget** — regex matching on a small JSON file is sub-millisecond. The Python startup for `json.loads` dominates at ~30ms. Acceptable for PreToolUse.

## Test Architecture

See [`docs/architecture.tex`](docs/architecture.tex) §15 for test tiers, counts, and strategy.
