# punt-quarry

[![License](https://img.shields.io/github/license/punt-labs/quarry)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/punt-labs/quarry/test.yml?label=CI)](https://github.com/punt-labs/quarry/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Python](https://img.shields.io/pypi/pyversions/punt-quarry)](https://pypi.org/project/punt-quarry/)

Unlock the knowledge trapped on your hard drive. Works with Claude Desktop, Claude Code, and the macOS menu bar.

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
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/0cbb5e3/install.sh | sh
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
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/0cbb5e3/install.sh -o install.sh
shasum -a 256 install.sh
cat install.sh
sh install.sh
```

</details>

## What You Can Do

**Index anything you have.** PDFs, scanned documents, images, spreadsheets, presentations, source code, Markdown, LaTeX, DOCX, HTML, and webpages. Quarry reads each format the way you would and extracts the knowledge inside.

**Search by meaning.** "What did the Q3 report say about margins?" finds relevant passages even if they never use the word "margins." This is semantic search — it understands what you mean, not just what you typed.

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

### Automagic Mode (Plugin)

When installed as a Claude Code plugin (`claude plugin install quarry@punt-labs`), Quarry captures knowledge automatically — no manual indexing needed:

| What happens | When |
|-------------|------|
| **Your project is indexed** | Every session starts with an incremental sync of your project directory. Claude knows what's in your codebase. |
| **Web pages are saved** | Every URL Claude fetches is auto-ingested into a `web-captures` collection for later search. |
| **Conversations are preserved** | Before context compaction, the transcript is captured into `session-notes` so decisions and discoveries survive across sessions. |

Each hook can be individually disabled per project by creating `.claude/quarry.local.md`:

```yaml
---
auto_capture:
  session_sync: false
  web_fetch: false
  compaction: false
---
```

All hooks default to enabled. Automagic mode is additive — Quarry works the same way without the plugin, you just manage ingestion manually.

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

- Google Drive connector
- `quarry sync --watch` for live filesystem monitoring
- PII detection and redaction

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
