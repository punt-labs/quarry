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
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/0e4e6d1/install.sh | sh
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
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/0e4e6d1/install.sh -o install.sh
shasum -a 256 install.sh
cat install.sh
sh install.sh
```

</details>

## What You Can Do

**Index anything you have.** PDFs, scanned documents, images, spreadsheets, presentations, source code, Markdown, LaTeX, DOCX, HTML, and webpages. Quarry parses each format natively — text extraction, OCR, tabular serialization, AST parsing — and indexes the content for semantic search.

**Search by meaning.** Retrieval is by meaning, not keyword — a query about "margins" finds passages about profitability even if they never use that word.

**Give your LLM access.** As an MCP server, Quarry lets Claude Desktop and Claude Code search your indexed documents directly. Ask Claude about something in your files and it pulls the relevant context automatically.

**Keep things organized.** Named databases separate work from personal. Directory sync watches your folders and re-indexes when files change. Collections group documents within a database.

## Supported Formats

| Source | What happens |
|--------|-------------|
| PDF (text pages) | Text extraction via PyMuPDF |
| PDF (image pages) | OCR (local by default; optional cloud backend) |
| Images (PNG, JPG, TIFF, BMP, WebP) | OCR (local by default; optional cloud backend) |
| Spreadsheets (XLSX, CSV) | Tabular serialization preserving structure |
| Presentations (PPTX) | Slide-per-chunk with tables and speaker notes |
| HTML / webpages | Boilerplate stripping, converted to Markdown |
| Text files (TXT, MD, LaTeX, DOCX) | Split by headings, sections, or paragraphs |
| Source code (30+ languages) | AST parsing into functions and classes |

## Claude Desktop

The easiest way to install is the [**.mcpb file**](https://github.com/punt-labs/quarry/releases/latest/download/punt-quarry.mcpb) — download and double-click. Claude Desktop handles the rest. Alternatively, `quarry install` (from the CLI) configures Claude Desktop automatically.

Once installed, Claude can search, index, and manage your documents through conversation. Ask it to index a file, search your knowledge base, or crawl a documentation site — Quarry handles the rest behind the scenes.

**Note:** Uploaded files in Claude Desktop live in a sandbox that Quarry cannot access. Use `ingest_content` for uploaded content, or provide local file paths to `ingest_file`.

<details>
<summary>Manual MCP setup</summary>

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

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

Use the absolute path to `uvx` (e.g. `/opt/homebrew/bin/uvx`). `quarry install` resolves this automatically.

</details>

## Claude Code

`quarry install` configures Claude Code automatically, or set up manually:

```bash
claude mcp add quarry -- uvx --from punt-quarry quarry mcp
```

### Ambient Knowledge (Plugin)

As an MCP server, Quarry is a tool you call — `/find`, `/ingest`, explicit commands. As a Claude Code plugin, Quarry changes how Claude Code itself works. The host becomes knowledge-aware.

**Learning.** Knowledge flows through every session and normally evaporates — web research, document reads, debugging discoveries, architectural decisions. The plugin will capture this passively. Hooks detect knowledge-generating events, write to a staging queue, and `quarry learn` processes the queue in the background. You work normally; the knowledge base grows.

**Recall.** Having the knowledge isn't enough if Claude doesn't know to look there. The plugin will inject a knowledge briefing at session start and nudge Claude before web searches that overlap with locally indexed content. The second time you research something, it's instant.

The plugin will offer three capture levels via `/quarry learn`:

```text
/quarry learn off   — No passive capture (default without plugin)
/quarry learn on    — Capture web research + compaction transcripts
/quarry learn all   — Also capture document reads, agent findings, session digests
```

| What happens | When | Mode |
|-------------|------|------|
| **Knowledge briefing** | Session start — Claude knows what's in your knowledge base | Always |
| **Local-first nudge** | Before web search — suggests checking quarry for familiar topics | Always |
| **Web pages saved** | URLs Claude fetches are queued for background ingestion | `on` |
| **Conversations preserved** | Before context compaction, the transcript is captured | `on` |
| **Documents indexed** | Non-code files Claude reads (PDFs, images) are queued | `all` |
| **Agent findings saved** | Research subagent results are captured | `all` |

All learning hooks are designed to be fail-open (quarry crashing never breaks Claude Code) and non-blocking (hooks write to a staging queue, ingestion is async). Recall hooks are read-only and always active.

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
quarry ingest-file report.pdf                  # index a file
quarry ingest-file report.pdf --overwrite      # replace existing data
quarry ingest-url https://example.com/page     # index a webpage
quarry ingest-sitemap https://docs.example.com/sitemap.xml  # crawl a sitemap
quarry ingest-sitemap URL --include '/docs/*' --exclude '/docs/v1/*' --limit 50

# Search
quarry search "revenue trends"                 # semantic search
quarry search "revenue" --limit 5              # limit results
quarry search "tests" --page-type code         # only code results
quarry search "revenue" --source-format .xlsx  # only spreadsheet results
quarry search "deploy" --document README.md    # search within one document

# Manage documents
quarry list                                    # list indexed documents
quarry delete report.pdf                       # remove a document
quarry collections                             # list collections

# Directory sync
quarry register ~/Documents/notes              # watch a directory
quarry sync                                    # re-index all registered directories
quarry registrations                           # list registered directories
quarry deregister notes                        # stop watching

# System
quarry doctor                                  # health check
quarry databases                               # list all databases with stats
quarry serve                                   # start HTTP API server
```

### Named Databases

Keep separate databases for different purposes:

```bash
quarry ingest-file report.pdf --db work
quarry ingest-file recipe.md --db personal
quarry search "revenue" --db work
quarry databases                               # list all databases
```

Each database is fully isolated — its own vector index and sync registry. The default database is called `default`.

You can point MCP servers at different databases:

```json
{
  "mcpServers": {
    "work": {
      "command": "/path/to/uvx",
      "args": ["--from", "punt-quarry", "quarry", "mcp", "--db", "work"]
    }
  }
}
```

## Configuration

Quarry works with zero configuration. These environment variables are available for customization:

| Variable | Default | Description |
|----------|---------|-------------|
| `OCR_BACKEND` | `local` | `local` (offline, no setup) or `textract` (AWS, better for degraded scans) |
| `QUARRY_ROOT` | `~/.quarry/data` | Base directory for all databases (log path configured separately via `LOG_PATH`) |
| `CHUNK_MAX_CHARS` | `1800` | Max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Overlap between consecutive chunks |

For advanced settings (Textract polling, embedding model, paths), see [Advanced Configuration](docs/ADVANCED-CONFIG.md).

## Cloud Backends (Optional)

Quarry works entirely offline by default. Cloud backends are available for specialized use cases.

### AWS Textract (OCR)

Better character accuracy on degraded scans, faxes, and low-resolution images. For clean digital documents, local OCR produces equivalent search results.

```bash
export OCR_BACKEND=textract
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
export S3_BUCKET=my-bucket
```

See [docs/AWS-SETUP.md](docs/AWS-SETUP.md) for IAM policies and full setup.

### SageMaker Embedding

Cloud-accelerated embedding for large-scale batch ingestion (thousands of files). Search always uses the local model regardless of this setting.

```bash
export EMBEDDING_BACKEND=sagemaker
export SAGEMAKER_ENDPOINT_NAME=quarry-embedding
```

Deploy with `./infra/manage-stack.sh deploy`. See [docs/AWS-SETUP.md](docs/AWS-SETUP.md) for details.

## MCP Tools Reference

Both Claude Desktop and Claude Code access Quarry through these MCP tools. You don't call these directly — Claude uses them on your behalf.

| Tool | What it does |
|------|-------------|
| `search_documents` | Semantic search with optional filters |
| `ingest_file` | Index a file by path |
| `ingest_url` | Fetch and index a webpage |
| `ingest_auto` | Smart URL ingestion: auto-discovers sitemaps, bulk-crawls or single-page |
| `ingest_sitemap` | Crawl a specific sitemap URL |
| `ingest_content` | Index inline text (for uploads, clipboard, etc.) |
| `get_documents` | List indexed documents |
| `get_page` | Get raw text for a specific page |
| `delete_document` | Remove a document |
| `list_collections` | List collections |
| `delete_collection` | Remove a collection |
| `register_directory` | Register a directory for sync |
| `deregister_directory` | Remove a directory registration |
| `sync_all_registrations` | Re-index all registered directories |
| `list_registrations` | List registered directories |
| `list_databases` | List named databases |
| `use_database` | Switch to a different database |
| `status` | Database stats |

## Roadmap

- **Ambient knowledge** — passive learning and active recall via Claude Code plugin hooks ([vision](research/vision.md))
- `quarry sync --watch` for live filesystem monitoring
- PII detection and redaction
- Google Drive connector

For product vision and positioning, see [PR/FAQ](prfaq.pdf).

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ tests/
uv run pytest                  # run the test suite
```

Quarry is fully typed (`py.typed`) and can be used as a Python library. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, architecture, and how to add new formats.

## Documentation

- [Changelog](CHANGELOG.md)
- [AWS Setup Guide](docs/AWS-SETUP.md) — IAM, S3, SageMaker deployment
- [Search Quality and Tuning](docs/SEARCH-TUNING.md)
- [Backend Abstraction Design](docs/BACKEND-ABSTRACTION.md)
- [Non-Functional Design](docs/NON-FUNCTIONAL-DESIGN.md)
- [PR/FAQ](prfaq.pdf) — product vision and positioning

## License

[MIT](LICENSE)
