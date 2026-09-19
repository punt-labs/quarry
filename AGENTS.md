# Quarry for Agents

Quarry gives AI agents persistent semantic memory. Index documents, search by meaning, and recall knowledge across sessions — all local, no API keys.

## What Quarry Does

You have a context window. It fills up and gets compacted. Quarry sits outside that window as durable storage: you ingest documents and conversation fragments, then search them later by meaning. A query about "margins" finds passages about profitability even if they never use that word.

Quarry handles 20+ formats natively: PDFs (with OCR for scanned pages), source code (AST-aware splitting), spreadsheets, presentations, HTML, Markdown, LaTeX, DOCX, and images.

## Integration Model

Quarry integrates with Claude Code as a **plugin** that provides three layers:

1. **MCP tools** — the core interface. Search, ingest, list, delete, manage databases.
2. **Slash commands** — user-facing shortcuts that call MCP tools with formatting logic.
3. **Hooks** — automatic background behaviors that run without user intervention.

### MCP Tools

These are the tools you call directly. Read-only tools are synchronous; write tools are fire-and-forget (return immediately, process in background).

Thirteen tools (`src/quarry/mcp_server.py` registers twelve; the thirteenth, `missions_sync`, lives in the sibling `mcp_missions.py` and is registered from the same call):

| Tool | Purpose | Execution |
|------|---------|-----------|
| `find` | Hybrid search (vector + BM25 via RRF) with optional filters (collection, document_filter, page_type, source_format, agent_handle, memory_type) | Sync |
| `show` | Retrieve document metadata or a specific page's full text | Sync |
| `list` | List documents, collections, databases, or registrations | Sync |
| `status` | Database stats: doc/chunk counts, size, model info | Sync |
| `ingest` | Ingest a URL (auto-discovers sitemaps); local files and directories go through `register_directory` + `sync_all_registrations` | Background |
| `remember` | Ingest inline text content with a document name (agent_handle routes it to `memory-<handle>`; memory_type and summary tag it) | Background |
| `learn` | Save a distilled lesson (<= 500 chars) that gets retrieval preference; the only writer of `memory_type = lesson` | Background |
| `delete` | Delete a document or collection | Background |
| `register_directory` | Register a directory for incremental sync | Background |
| `deregister_directory` | Remove a directory registration | Background |
| `sync_all_registrations` | Sync all registered directories | Background |
| `use` | Switch the active named database | Sync |
| `missions_sync` | File each frozen ethos mission round of this repo as an `observation` in `memory-<worker>` (`mission`, `dry_run`, `force`); run after `ethos mission close` | Sync |

**`memory_type` is a closed vocabulary**, validated server-side (`src/quarry/memory_types.py`): `fact`, `observation`, `opinion`, `procedure`. `lesson` is reserved for `learn`, and any other value is rejected with a 400 on `remember`, `ingest`, and the capture route alike — a mistyped `facts` is an error, never a silently stored row that neither decays nor matches a typed filter. Always pass your own `agent_handle`: the daemon cannot infer it, and a subagent's working directory resolves to the repo's leader, not to the subagent (DES-055).

**Tool name resolution**: The MCP tool names vary by which quarry install a session connects to. Two variants:

- `mcp__quarry__find` — native prod (installed quarry MCP server)
- `mcp__quarry-dev__find` — native dev twin (in-tree plugin, for development)

Use whichever variant is available in your session. They call the same backend.

### Slash Commands

Users invoke these; you execute the underlying MCP calls.

| Command | What it does |
|---------|-------------|
| `/find <query>` | Search the knowledge base. Questions get synthesized answers with citations; keywords get raw results. |
| `/ingest <source>` | Ingest a URL, directory, or file. URLs auto-discover sitemaps. Directories get registered + synced. |
| `/remember <name>` | Ingest inline text content under a document name. |
| `/learn <lesson> [as <name>]` | Save a distilled lesson that gets retrieval preference (calls `learn`). |
| `/explain <topic>` | Search and synthesize an explanation of a document or topic. |
| `/source <claim>` | Find which document(s) a claim comes from. Cite document name, page, and excerpt. |
| `/quarry [subcommand]` | Manage the knowledge base: `status` (default), `sync`, `collections`, `databases`, `registrations`, `use <name>`, `enable`, `disable`, `missions sync` (calls `missions_sync`; accepts a mission id, `--dry-run`, `--force`). |

### Hooks (Automatic Behaviors)

These fire without user action (`plugin/hooks/hooks.json`). All are fail-open — errors are logged, never block the session — and each is independently toggleable under `auto_capture` in `.punt-labs/quarry/config.md`. Every capture goes to the daemon, which derives the `<collection>-captures` collection from the cwd's directory registration (created by `quarry enable`, session-start auto-register, or manual `quarry register`) and falls back to `default-captures`; content is PII/secret-scrubbed server-side before it is stored (DES-036, DES-041).

| Hook | Event | What it does |
|------|-------|-------------|
| **Session start** | `SessionStart` | Auto-registers the working directory and launches a background sync so the codebase starts getting indexed shortly after session start. |
| **Web fetch capture** | `PostToolUse` on `WebFetch` | URLs you fetch during research are auto-ingested from the already-fetched content (no re-fetch; a JS-rendered page that extracts to nothing is re-fetched by the daemon through its SSRF gate). If the URL was already captured, the hook nudges you to `find` it instead of re-fetching. |
| **Web search capture** | `PostToolUse` on `WebSearch` | Files a scrubbed digest of the search results under the document name `search: <query>`. |
| **Read capture** | `PostToolUse` on `Read` | Opt-in (off by default): captures prose files read from outside any registered tree, gated by an in-tree / secret-path / extension / size filter. |
| **Pre-compact capture** | `PreCompact` | Before context compaction, captures the conversation transcript (user/assistant text, tool-use blocks skipped, capped at 500K chars), attributed to the repo's ethos pin. |
| **Session end capture** | `SessionEnd` | Captures the full transcript on every close, so a short session that never compacts still yields a durable capture. |
| **Subagent stop capture** | `SubagentStop` | Archives the subagent's own transcript (separate from the parent's) and — when `agent_type` names a registered ethos identity — files the subagent's final report as an `observation` in `memory-<handle>`; a bare `Agent()` (`general-purpose`) is filed unattributed, never under the leader's pin (DES-055). A blocking hook: it returns `{}` on every path. |
| **Output suppression** | `PostToolUse` on quarry tools | Formats quarry tool output for display. |

### Subagents

| Agent | What it does |
|-------|-------------|
| `researcher` | Deep research combining quarry local search with web search. Searches quarry first, web for gaps, auto-ingests valuable findings. Use for investigation tasks that span local documents and external sources. |

## Architecture You Should Know

### One Daemon, Many Sessions

Quarry runs as a single background daemon (`quarryd`, supervised by launchd/systemd) that loads the embedding model once (~200 MB RAM, ~250ms load). Every Claude Code session runs `quarry mcp`, a FastMCP stdio server whose tool bodies are `QuarryClient` calls over the daemon's `/v1` REST API — it imports no engine and loads no model. The CLI (`quarry find`, `quarry ingest`) is the same kind of thin client; nothing but `quarryd` opens LanceDB or ONNX (DES-031).

### Named Databases

Databases are fully isolated: separate LanceDB directories, sync registries, and vector indexes. Switch with the `use` tool, which overrides the database for the rest of your session's `quarry mcp` process only — each session gets isolated database state, so `use("work")` in one session doesn't affect others, and nothing is written to disk. The persistent default is set only by the CLI `quarry use <name>`. Common pattern: `default` for general use, project-specific databases for scoped work.

### Fire-and-Forget

Background MCP tools (ingest, remember, learn, delete, sync, register, deregister) post to the daemon and return immediately with a `task_id` (HTTP 202). The work runs on the daemon's serialized per-collection ingest queue (DES-042); poll `GET /v1/tasks/{task_id}` or ignore the outcome. This prevents blocking your response stream during long ingestions.

### Search Tips

- **Natural language works best.** "What were Q3 revenue figures?" outperforms "Q3 revenue" — the embedding model is trained on question-passage pairs.
- **Filter by collection** to scope results: `find` with `collection` parameter.
- **Use `show`** to get full page text after finding a relevant chunk.
- **Chunks overlap** by 200 characters. If a result seems cut off, the continuation is in the next chunk.

## Integrating Quarry into Another Plugin

If you're building a Claude Code plugin that needs semantic search:

### Option 1: Call Quarry's MCP Tools

The simplest path. Your plugin's hooks or commands call quarry's MCP tools directly. No dependency management needed — quarry just needs to be installed.

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "quarry-hook session-start"
      }]
    }]
  }
}
```

`quarry-hook <event>` (`src/quarry/_hook_entry.py`) reads the hook payload from stdin and dispatches without the full CLI import chain; the events are `session-setup`, `session-start`, `post-web-fetch`, `post-web-search`, `post-read`, `pre-compact`, `session-end`, and `subagent-stop`.

### Option 2: Use Quarry as a Library

For deeper integration, import quarry's Python client. The library holds no engine — it is the same thin client the CLI and MCP server use, and it never pulls LanceDB or ONNX into your process:

```python
import quarry
from quarry.api import RememberRequest, SearchRequest

client = quarry.TargetResolver.connect()  # resolves the local daemon (or a logged-in remote)

# Remember
accepted = client.remember(
    RememberRequest(
        name="notes.md",
        content="The API rate limit is 100 req/s.",
        agent_handle="my-plugin",
        memory_type="fact",
    )
)
print(accepted.task_id)

# Search
resp = client.search(SearchRequest(query="what is the rate limit", limit=5))
for hit in resp.results:
    print(hit.document_name, hit.text)
```

Request and response models live in `quarry.api`; the client, its error hierarchy, and `TargetResolver` are re-exported from `quarry`.

### Option 3: HTTP API

For non-Python integrations, use the REST API under `/v1` (the full contract is `docs/openapi.json`):

```text
GET  /v1/search?q=margins&limit=10
GET  /v1/documents
GET  /v1/collections
GET  /v1/status
POST /v1/remember          (202 + task_id)
POST /v1/learn             (202 + task_id)
GET  /v1/tasks/{task_id}
GET  /health               (unversioned, unauthenticated)
```

There is no WebSocket endpoint — the daemon serves REST only. Every `/v1` request carries `Authorization: Bearer <token>`: on loopback the token is the daemon's per-restart `serve.token`, which the client reads live; a remote daemon uses the `QUARRY_API_KEY` it was started with. Installed daemons serve TLS with a pinned self-signed CA (`~/.punt-labs/quarry/tls/ca.crt`).

## Format Support

| Source | What happens |
|--------|-------------|
| PDF (text) | Text extraction via PyMuPDF |
| PDF (scanned) | Local OCR (RapidOCR) |
| Images (PNG, JPG, TIFF, BMP, WebP) | Local OCR |
| Spreadsheets (XLSX, CSV) | Tabular serialization preserving structure |
| Presentations (PPTX) | Slide-per-chunk with tables and speaker notes |
| HTML / webpages | Boilerplate stripping, Markdown conversion |
| Text (TXT, MD, LaTeX, DOCX) | Section-aware splitting |
| Source code (30+ languages) | AST parsing into functions and classes |

## Further Reading

- [`docs/architecture.tex`](docs/architecture.tex) — full system architecture (LaTeX)
- [`docs/claude-code-quarry.tex`](docs/claude-code-quarry.tex) — Z specification of the plugin state machine
- [`docs/design/agent-memory-loop.md`](docs/design/agent-memory-loop.md) — the agent-memory write loop: persist habit, `missions sync`, SubagentStop distillation (DES-055)
- [`DESIGN.md`](DESIGN.md) — architectural decision records
- [`README.md`](README.md) — user-facing documentation
