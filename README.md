# quarry-mcp

[![PyPI](https://img.shields.io/pypi/v/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![GitHub release](https://img.shields.io/github/v/release/jmf-pobox/quarry-mcp)](https://github.com/jmf-pobox/quarry-mcp/releases)
[![Python 3.13+](https://img.shields.io/pypi/pyversions/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![Tests](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml)
[![Lint](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml)
[![codecov](https://codecov.io/gh/jmf-pobox/quarry-mcp/graph/badge.svg)](https://codecov.io/gh/jmf-pobox/quarry-mcp)

Unlock the knowledge trapped on your hard drive. Works with Claude Code and Claude Desktop.

## Quick Start

One-liner install (Python 3.10+ required):

```bash
curl -fsSL https://raw.githubusercontent.com/jmf-pobox/quarry-mcp/main/install.sh | bash
```

This installs `uv` (if needed), `quarry-mcp`, downloads the embedding model, and configures Claude Code and Claude Desktop.

Or install manually:

```bash
pip install quarry-mcp
quarry install          # downloads embedding model (~500MB), configures MCP
```

Then start using it:

```bash
quarry ingest-file notes.md      # index a file — no cloud account needed
quarry search "my topic"         # search by meaning, not keywords
```

That's it. Quarry works locally out of the box — no API keys, no cloud, no setup beyond `quarry install`.

## What You Can Do

**Index anything you have.** PDFs, scanned documents, images, spreadsheets, presentations, source code, Markdown, LaTeX, DOCX, HTML, and webpages. Quarry reads each format the way you would and extracts the knowledge inside.

**Search by meaning.** "What did the Q3 report say about margins?" finds relevant passages even if they never use the word "margins." This is semantic search — it understands what you mean, not just what you typed.

**Give your LLM access.** As an MCP server, Quarry lets Claude Code and Claude Desktop search your indexed documents directly. Ask Claude about something in your files and it pulls the relevant context automatically.

**Keep things organized.** Named databases separate work from personal. Directory sync watches your folders and re-indexes when files change. Collections group documents within a database.

## Supported Formats

| Source | What happens |
|--------|-------------|
| PDF (text pages) | Text extraction via PyMuPDF |
| PDF (image pages) | OCR (local, offline) |
| Images (PNG, JPG, TIFF, BMP, WebP) | OCR (local, offline) |
| Spreadsheets (XLSX, CSV) | Tabular serialization preserving structure |
| Presentations (PPTX) | Slide-per-chunk with tables and speaker notes |
| HTML / webpages | Boilerplate stripping, converted to Markdown |
| Text files (TXT, MD, LaTeX, DOCX) | Split by headings, sections, or paragraphs |
| Source code (30+ languages) | AST parsing into functions and classes |

## CLI Reference

```bash
# Ingest
quarry ingest-file report.pdf                  # index a file
quarry ingest-file report.pdf --overwrite      # replace existing data
quarry ingest-url https://example.com/page     # index a webpage

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

## Named Databases

Keep separate databases for different purposes:

```bash
quarry ingest-file report.pdf --db work
quarry ingest-file recipe.md --db personal
quarry search "revenue" --db work
quarry databases                               # list all databases
```

Each database is fully isolated — its own vector index and sync registry. The default database is called `default`.

You can run separate MCP servers for different databases:

```json
{
  "mcpServers": {
    "work": {
      "command": "/path/to/uvx",
      "args": ["--from", "quarry-mcp", "quarry", "mcp", "--db", "work"]
    }
  }
}
```

## MCP Setup

`quarry install` configures Claude Code and Claude Desktop automatically. To set up manually:

**Claude Code:**

```bash
claude mcp add quarry -- uvx --from quarry-mcp quarry mcp
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "quarry": {
      "command": "/path/to/uvx",
      "args": ["--from", "quarry-mcp", "quarry", "mcp"]
    }
  }
}
```

Use the absolute path to `uvx` for Desktop (e.g. `/opt/homebrew/bin/uvx`). `quarry install` resolves this automatically.

**Claude Desktop note:** Uploaded files live in a sandbox that Quarry cannot access. Use `ingest_content` (the MCP tool for inline text) for uploaded content. For files on your Mac, provide the local path to `ingest_file`.

### MCP Tools

| Tool | What it does |
|------|-------------|
| `search_documents` | Semantic search with optional filters |
| `ingest_file` | Index a file by path |
| `ingest_url` | Fetch and index a webpage |
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
| `status` | Database stats |

## Configuration

Quarry works with zero configuration. These environment variables are available for customization:

| Variable | Default | Description |
|----------|---------|-------------|
| `OCR_BACKEND` | `local` | `local` (offline, no setup) or `textract` (AWS, better for degraded scans) |
| `QUARRY_ROOT` | `~/.quarry/data` | Base directory for all databases and logs |
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

## Roadmap

- [macOS menu bar companion app](https://github.com/jmf-pobox/quarry-menubar) — native macOS search interface (in development)
- Google Drive connector
- `quarry sync --watch` for live filesystem monitoring
- PII detection and redaction

For product vision and positioning, see [PR/FAQ](prfaq.pdf).

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ tests/
uv run pytest                  # 549 tests across 25 modules
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
