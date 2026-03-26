# punt-quarry

[![License](https://img.shields.io/github/license/punt-labs/quarry)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/punt-labs/quarry/test.yml?label=CI)](https://github.com/punt-labs/quarry/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Python](https://img.shields.io/pypi/pyversions/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Working Backwards](https://img.shields.io/badge/Working_Backwards-hypothesis-lightgrey)](./prfaq.pdf)

Local semantic search across your documents. Works with Claude Desktop, Claude Code, and the macOS menu bar.

## Quick Start

### Claude Desktop

[**Download punt-quarry.mcpb**](https://github.com/punt-labs/quarry/releases/latest/download/punt-quarry.mcpb) and double-click to install. Claude Desktop will prompt you for a data directory.

Attach a document to your conversation and ask Claude to index it:

> "Index this report"
>
> "What does it say about Q3 margins?"

That's it. Everything runs locally — no API keys, no cloud accounts. The embedding model (~500 MB) downloads automatically on first use.

### Claude Code

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/25eaa96/install.sh | sh
```

<details>
<summary>Manual install (if you already have uv)</summary>

```bash
uv tool install punt-quarry
quarry install
quarry doctor
```

</details>

<details>
<summary>Verify before running</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/25eaa96/install.sh -o install.sh
shasum -a 256 install.sh
cat install.sh
sh install.sh
```

</details>

## How It Works

Quarry runs as a **daemon** — a single background process that loads the embedding model once and serves all sessions. Claude Code and Desktop don't talk to the daemon directly; they spawn a lightweight [**mcp-proxy**](https://github.com/punt-labs/mcp-proxy) binary (~5MB, <10ms startup) that bridges MCP stdio to the daemon over WebSocket:

```text
                    stdio                      WebSocket
Claude Code ◄──────────────► mcp-proxy ◄──────────────────────► quarry serve
             MCP JSON-RPC                                       (one process)
```

Without the proxy, every Claude Code tab spawns a separate Python process, each loading the embedding model into ~200MB of RAM. With it, you get instant startup and shared state across all sessions.

`quarry install` downloads mcp-proxy automatically (SHA256-verified, correct platform) and configures MCP clients to use it.

## What You Can Do

**Index anything you have.** PDFs, scanned documents, images, spreadsheets, presentations, source code, Markdown, LaTeX, DOCX, HTML, and webpages. Quarry parses each format natively — text extraction, OCR, tabular serialization, AST parsing — and indexes the content for semantic search.

**Search by meaning.** Retrieval is by meaning, not keyword — a query about "margins" finds passages about profitability even if they never use that word.

**Give your LLM access.** As an MCP server, Quarry lets Claude Desktop and Claude Code search your indexed documents directly. Ask Claude about something in your files and it pulls the relevant context automatically.

**Keep things organized.** Named databases separate work from personal. Directory sync watches your folders and re-indexes when files change. Collections group documents within a database.

## Supported Formats

| Source | What happens |
|--------|-------------|
| PDF (text pages) | Text extraction via PyMuPDF |
| PDF (image pages) | Local OCR (RapidOCR) |
| Images (PNG, JPG, TIFF, BMP, WebP) | Local OCR (RapidOCR) |
| Spreadsheets (XLSX, CSV) | Tabular serialization preserving structure |
| Presentations (PPTX) | Slide-per-chunk with tables and speaker notes |
| HTML / webpages | Boilerplate stripping, converted to Markdown |
| Text files (TXT, MD, LaTeX, DOCX) | Split by headings, sections, or paragraphs |
| Source code (30+ languages) | AST parsing into functions and classes |

## Claude Desktop

The easiest way to install is the [**.mcpb file**](https://github.com/punt-labs/quarry/releases/latest/download/punt-quarry.mcpb) — download and double-click. Claude Desktop handles the rest. Alternatively, `quarry install` (from the CLI) configures Claude Desktop automatically.

Once installed, Claude can search, index, and manage your documents through conversation. Ask it to index a file, search your knowledge base, or crawl a documentation site — Quarry handles the rest behind the scenes.

**Note:** Uploaded files in Claude Desktop live in a sandbox that Quarry cannot access. Use `remember` for uploaded content, or provide local file paths to `ingest`.

<details>
<summary>Manual MCP setup</summary>

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "quarry": {
      "command": "/path/to/mcp-proxy",
      "args": ["ws://localhost:8420/mcp"]
    }
  }
}
```

Use the absolute path to `mcp-proxy` (e.g. `~/.local/bin/mcp-proxy`). `quarry install` resolves this automatically. Requires `quarry serve` running (either started manually, or installed as a daemon via `quarry install`).

<details>
<summary>Without mcp-proxy (not recommended)</summary>

```json
{
  "mcpServers": {
    "quarry": {
      "command": "/path/to/uvx",
      "args": ["--from", "punt-quarry", "quarry", "mcp"]
    }
  }
}
```

This spawns a full Python process per session (~200MB, ~2s startup each).

</details>
</details>

## Claude Code

`quarry install` configures Claude Code automatically, or set up manually:

```bash
claude mcp add quarry -- mcp-proxy ws://localhost:8420/mcp
```

<details>
<summary>Without mcp-proxy (not recommended)</summary>

```bash
claude mcp add quarry -- uvx --from punt-quarry quarry mcp
```

This spawns a full Python process per session.

</details>

### Ambient Knowledge (Plugin)

As an MCP server, Quarry is a tool you call — `/find`, `/ingest`, explicit commands. As a Claude Code plugin, Quarry changes how Claude Code itself works. The host becomes knowledge-aware.

**Learning.** Knowledge flows through every session and normally evaporates — web research, document reads, debugging discoveries, architectural decisions. The plugin captures this passively. Hooks detect knowledge-generating events and ingest them automatically. You work normally; the knowledge base grows.

Three hooks are shipped today:

- **Session start** — auto-registers the project directory so new files are indexed on sync.
- **Post web fetch** — every URL Claude fetches is auto-ingested into the knowledge base.
- **Pre-compact** — conversation transcript is captured before context compaction, preserving discoveries that would otherwise be lost.

Per-project hook configuration via `.claude/quarry.local.md` YAML frontmatter lets you selectively disable individual hooks. All hooks are fail-open (quarry crashing never breaks Claude Code) and non-blocking.

**Recall.** Having the knowledge isn't enough if Claude doesn't know to look there. The plugin will inject a knowledge briefing at session start and nudge Claude before web searches that overlap with locally indexed content. The second time you research something, it's instant.

**Roadmap.** The full ambient knowledge architecture adds config-driven capture levels and recall hooks:

```text
/quarry learn off   — No passive capture (default without plugin)
/quarry learn on    — Capture web research + compaction transcripts
/quarry learn all   — Also capture document reads, agent findings, session digests
```

| What happens | When | Status |
|-------------|------|--------|
| **Web pages saved** | URLs Claude fetches are auto-ingested | Shipped |
| **Conversations preserved** | Before context compaction, the transcript is captured | Shipped |
| **Project directory registered** | Session start auto-registers the project | Shipped |
| **Knowledge briefing** | Session start — Claude knows what's in your knowledge base | Planned |
| **Local-first nudge** | Before web search — suggests checking quarry for familiar topics | Planned |
| **Documents indexed** | Non-code files Claude reads (PDFs, images) are queued | Planned |
| **Agent findings saved** | Research subagent results are captured | Planned |

For the full architecture, see [research/vision.md](research/vision.md).

## Menu Bar App (macOS)

[Quarry Menu Bar](https://github.com/punt-labs/quarry-menubar) is a native macOS companion app that puts your knowledge base one click away. It sits in the menu bar and lets you search across all your indexed documents without switching apps.

- Semantic search with instant results
- Switch between named databases
- Syntax-highlighted results for code, Markdown, and prose
- Detail view with full page context

Everything you index — whether through Claude Desktop, Claude Code, or the CLI — is searchable from the menu bar. The app manages its own `quarry serve` process automatically. Requires macOS 14 (Sonoma) or later and `punt-quarry` installed.

## CLI

The CLI gives you direct control over indexing and search. Everything Claude can do through MCP tools, you can do from the terminal.

```bash
# Ingest
quarry ingest report.pdf                       # index a file
quarry ingest report.pdf --overwrite           # replace existing data
quarry ingest https://example.com/page         # index a webpage (auto-detects sitemaps)
echo "meeting notes" | quarry remember --name notes.md  # index inline text

# Search
quarry find "revenue trends"                   # semantic search
quarry find "revenue" --limit 5                # limit results
quarry find "tests" --page-type code           # only code results
quarry find "revenue" --source-format .xlsx    # only spreadsheet results
quarry find "deploy" --document README.md      # search within one document

# Manage documents
quarry list documents                          # list indexed documents
quarry list collections                        # list collections
quarry show report.pdf                         # document metadata
quarry show report.pdf --page 1               # page text
quarry delete report.pdf                       # remove a document
quarry delete math --type collection           # remove a collection

# Directory sync
quarry register ~/Documents/notes              # watch a directory
quarry sync                                    # re-index all registered directories
quarry list registrations                      # list registered directories
quarry deregister notes                        # stop watching

# System
quarry status                                  # database dashboard
quarry version                                 # show version
quarry list databases                          # list all databases
quarry doctor                                  # health check
quarry install                                 # data dir + model + MCP clients + daemon
quarry uninstall                               # remove daemon service
quarry serve                                   # start HTTP API server on :8420
quarry serve --port 9000                       # override default port
quarry serve --host 0.0.0.0 --port 8080       # bind for container deployment
quarry serve --api-key $QUARRY_API_KEY         # with Bearer token auth
quarry serve --cors-origin https://punt-labs.com  # allow specific origin
quarry serve --cors-origin https://a.com --cors-origin https://b.com  # multiple
```

### Named Databases

Keep separate databases for different purposes:

```bash
quarry use work                                # set persistent default
quarry ingest report.pdf                       # uses 'work' database
quarry ingest recipe.md --db personal          # override per-call
quarry find "revenue"                          # searches 'work' database
quarry list databases                          # list all databases
```

Each database is fully isolated — its own vector index and sync registry. The default database is called `default`.

You can point the daemon at a specific database:

```bash
quarry use work                     # set persistent default database
quarry serve                        # daemon serves the 'work' database
```

```json
{
  "mcpServers": {
    "work": {
      "command": "/path/to/mcp-proxy",
      "args": ["ws://localhost:8420/mcp"]
    }
  }
}
```

## Configuration

Quarry works with zero configuration. These environment variables are available for customization:

| Variable | Default | Description |
|----------|---------|-------------|
| `QUARRY_API_KEY` | *(none)* | Bearer token for `quarry serve`. When set, all endpoints except `/health` require `Authorization: Bearer <key>` |
| `QUARRY_ROOT` | `~/.quarry/data` | Base directory for all databases (log path configured separately via `LOG_PATH`) |
| `CHUNK_MAX_CHARS` | `1800` | Max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Overlap between consecutive chunks |

For advanced settings (embedding model, paths), see [Architecture](docs/architecture.tex) §7 Configuration.

## MCP Tools Reference

Both Claude Desktop and Claude Code access Quarry through these MCP tools. You don't call these directly — Claude uses them on your behalf.

| Tool | What it does |
|------|-------------|
| `find` | Semantic search with optional filters |
| `ingest` | Index a file or URL (returns immediately, processes in background) |
| `remember` | Index inline text (returns immediately, processes in background) |
| `show` | Show document metadata or a specific page's text |
| `list` | List documents, collections, databases, or registrations |
| `delete` | Remove a document or collection (background) |
| `register_directory` | Register a directory for sync (background) |
| `deregister_directory` | Remove a directory registration (background) |
| `sync_all_registrations` | Re-index all registered directories (background) |
| `use` | Switch to a different database |
| `status` | Database stats |

Side-effect tools (`ingest`, `remember`, `delete`, `register_directory`, `deregister_directory`, `sync_all_registrations`) return an optimistic response immediately and process in the background. This keeps Claude responsive during long-running operations like PDF ingestion or directory sync.

## Roadmap

- **Ambient knowledge** — passive learning and active recall via Claude Code plugin hooks ([vision](research/vision.md))
- `quarry sync --watch` for live filesystem monitoring
- PII detection and redaction
- Google Drive connector

For product vision and positioning, see [PR/FAQ](prfaq.pdf).

## Development

```bash
make check                     # run all quality gates (lint, type, test)
make test                      # run the test suite only
make format                    # auto-format code
```

Quarry is fully typed (`py.typed`) and can be used as a Python library. See [DESIGN.md](DESIGN.md) for architecture and design decisions, and [CONTRIBUTING.md](CONTRIBUTING.md) for setup and how to add new formats.

## Documentation

- [Design](DESIGN.md) — architecture and design decisions
- [Changelog](CHANGELOG.md)
- [mcp-proxy](https://github.com/punt-labs/mcp-proxy) — the stdio-to-WebSocket bridge that eliminates per-session startup cost
- [Architecture](docs/architecture.tex) — system architecture, configuration, search tuning, logging standards
- [PR/FAQ](prfaq.pdf) — product vision and positioning

## License

[MIT](LICENSE)
