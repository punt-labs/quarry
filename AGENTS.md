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

| Tool | Purpose | Execution |
|------|---------|-----------|
| `find` | Semantic search with optional filters (collection, document_filter, page_type, source_format) | Sync |
| `show` | Retrieve document metadata or a specific page's full text | Sync |
| `list` | List documents, collections, databases, or registrations | Sync |
| `status` | Database stats: doc/chunk counts, size, model info | Sync |
| `ingest` | Ingest a file or URL (auto-discovers sitemaps) | Background |
| `remember` | Ingest inline text content with a document name | Background |
| `delete` | Delete a document or collection | Background |
| `register_directory` | Register a directory for incremental sync | Background |
| `deregister_directory` | Remove a directory registration | Background |
| `sync_all_registrations` | Sync all registered directories | Background |
| `use` | Switch the active named database | Sync |

**Tool name resolution**: The MCP tool names vary depending on how quarry is configured. Common patterns:

- `mcp__quarry__find` (direct MCP)
- `mcp__plugin_quarry_quarry__find` (Claude Code plugin)
- `mcp__quarry-proxy__find` (via mcp-proxy)

Use whichever variant is available in your session. They all call the same backend.

### Slash Commands

Users invoke these; you execute the underlying MCP calls.

| Command | What it does |
|---------|-------------|
| `/find <query>` | Search the knowledge base. Questions get synthesized answers with citations; keywords get raw results. |
| `/ingest <source>` | Ingest a URL, directory, or file. URLs auto-discover sitemaps. Directories get registered + synced. |
| `/remember <name>` | Ingest inline text content under a document name. |
| `/explain <topic>` | Search and synthesize an explanation of a document or topic. |
| `/source <claim>` | Find which document(s) a claim comes from. Cite document name, page, and excerpt. |
| `/quarry [subcommand]` | Manage the knowledge base: `status` (default), `sync`, `collections`, `databases`, `registrations`. |

### Hooks (Automatic Behaviors)

These fire without user action. All are fail-open — errors are logged, never block the session.

| Hook | Event | What it does |
|------|-------|-------------|
| **Session start** | `SessionStart` | Auto-registers the working directory and launches a background sync so the codebase starts getting indexed shortly after session start. |
| **Web fetch capture** | `PostToolUse` on `WebFetch` | URLs you fetch during research are auto-ingested into the `web-captures` collection. Deduplicates by document name. Uses the already-fetched content (no re-fetch). |
| **Pre-compact capture** | `PreCompact` | Before context compaction, captures the conversation transcript into the `session-notes` collection. User/assistant messages are extracted, tool-use blocks skipped, capped at 500K chars. |
| **Output suppression** | `PostToolUse` on quarry tools | Formats quarry tool output for display. |

### Subagents

| Agent | What it does |
|-------|-------------|
| `researcher` | Deep research combining quarry local search with web search. Searches quarry first, web for gaps, auto-ingests valuable findings. Use for investigation tasks that span local documents and external sources. |

## Architecture You Should Know

### One Daemon, Many Sessions

Quarry runs as a single background daemon (`quarry serve`) that loads the embedding model once (~200 MB RAM, ~250ms load). Claude Code sessions connect via `mcp-proxy` over WebSocket. Each session gets isolated database state — `use("work")` in one session doesn't affect others.

The CLI (`quarry find`, `quarry ingest`) is independent — it loads the model in-process and doesn't use the daemon.

### Named Databases

Databases are fully isolated: separate LanceDB directories, sync registries, and vector indexes. Switch with the `use` tool. Common pattern: `default` for general use, project-specific databases for scoped work.

### Fire-and-Forget

Background MCP tools (ingest, delete, sync, register, deregister, remember) return immediately with an optimistic response. The work happens in a bounded thread pool (4 workers). This prevents blocking your response stream during long ingestions.

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
        "command": "quarry hooks session-start < /dev/stdin"
      }]
    }]
  }
}
```

### Option 2: Use Quarry as a Library

For deeper integration, import quarry's Python API:

```python
from quarry.pipeline import ingest_document, ingest_content
from quarry.database import search, get_db
from quarry.config import Settings, resolve_db_paths

settings = Settings()
settings = resolve_db_paths(settings, "my-plugin-db")
db = get_db(settings)

# Ingest
ingest_document("/path/to/file.pdf", db, settings)

# Search
results = search(db, "query text", settings, limit=5)
```

### Option 3: HTTP API

For non-Python integrations, use the REST API:

```
GET /search?q=margins&limit=10
GET /documents
GET /collections
GET /status
GET /health
WS  /mcp          (MCP JSON-RPC over WebSocket)
```

All endpoints except `/health` require `Authorization: Bearer <key>` when `QUARRY_API_KEY` is set.

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

- [`docs/architecture.tex`](docs/architecture.tex) — full system architecture (13 pages, LaTeX)
- [`docs/claude-code-quarry.tex`](docs/claude-code-quarry.tex) — Z specification of the plugin state machine
- [`DESIGN.md`](DESIGN.md) — architectural decision records
- [`README.md`](README.md) — user-facing documentation
