# quarry-mcp

[![PyPI](https://img.shields.io/pypi/v/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![GitHub release](https://img.shields.io/github/v/release/jmf-pobox/quarry-mcp)](https://github.com/jmf-pobox/quarry-mcp/releases)
[![Python 3.13+](https://img.shields.io/pypi/pyversions/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![Tests](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml)
[![Lint](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml)
[![codecov](https://codecov.io/gh/jmf-pobox/quarry-mcp/graph/badge.svg)](https://codecov.io/gh/jmf-pobox/quarry-mcp)

Unlock the knowledge trapped on your hard drive. Works with Claude Code and Claude Desktop.

## Quick Start

```bash
pip install quarry-mcp
quarry install          # downloads embedding model (~500MB), configures MCP
quarry ingest-file notes.md  # index a file — no cloud account needed
quarry search "my topic"
```

That's it. Quarry works locally out of the box.

## What It Does

You have years of knowledge buried in PDFs, scanned documents, notes, spreadsheets, and source code. Quarry extracts that knowledge, makes it searchable by meaning, and gives your LLM access to it.

This is not media search — Quarry doesn't find images or match audio. It reads every document the way you would, extracts the text and structure, and indexes the *knowledge inside*. A scanned whiteboard becomes searchable prose. A spreadsheet becomes structured data an LLM can reason about. Source code becomes semantic units an LLM can reference.

**Supported formats:** PDF, images (PNG, JPG, TIFF, BMP, WebP), spreadsheets (XLSX, CSV), text files (TXT, Markdown, LaTeX, DOCX), and source code (30+ languages).

**How each format is processed:**

| Source | What happens | Result |
|--------|-------------|--------|
| PDF (text pages) | Text extraction via PyMuPDF | Prose chunks |
| PDF (image pages) | OCR (local or cloud) | Prose chunks |
| Images | OCR (local or cloud) | Prose chunks |
| Spreadsheets | LaTeX tabular serialization via openpyxl | Tabular chunks |
| Text files | Split by headings / sections / paragraphs | Section chunks |
| Source code | Tree-sitter AST parsing (functions, classes) | Code chunks |

Every format is converted to text optimized for LLM consumption. Structured formats like spreadsheets are serialized to LaTeX to preserve tabular relationships while remaining token-efficient. The goal is always the same: turn your files into knowledge an LLM can use.

## Installation

```bash
pip install quarry-mcp
quarry install
```

`quarry install` creates `~/.quarry/data/`, downloads the embedding model, and writes MCP config for Claude Code and Claude Desktop.

Verify with `quarry doctor`:

```
  ✓ Python version: 3.13.1
  ✓ Data directory: /Users/you/.quarry/data/lancedb
  ✓ Local OCR: RapidOCR engine OK
  ○ AWS credentials: Not configured (optional — needed for OCR_BACKEND=textract)
  ✓ Embedding model: snowflake-arctic-embed-m-v1.5 cached
  ✓ Core imports: 8 modules OK
```

## Usage

### CLI

```bash
# Ingest
quarry ingest-file report.pdf
quarry ingest-file whiteboard.jpg
quarry ingest-file src/main.py
quarry ingest-file report.pdf --overwrite

# Search
quarry search "authentication logic"
quarry search "quarterly revenue" -n 5

# Manage
quarry list
quarry delete report.pdf
quarry collections
quarry delete-collection math

# Directory sync — register a folder, then sync to pick up changes
quarry register /path/to/docs --collection my-docs
quarry sync
quarry registrations
quarry deregister my-docs
```

### MCP Server

`quarry install` configures Claude Code and Claude Desktop automatically. Manual setup:

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

**Available tools:**

| Tool | Description |
|------|-------------|
| **Search** | |
| `search_documents` | Semantic search across indexed documents |
| **Ingestion** | |
| `ingest_file` | Ingest a file (PDF, image, text, source code) |
| `ingest_content` | Ingest inline text content directly |
| **Documents** | |
| `get_documents` | List indexed documents with metadata |
| `get_page` | Retrieve full text for a specific page |
| `delete_document` | Remove a document and its chunks |
| **Collections** | |
| `list_collections` | List collections with document/chunk counts |
| `delete_collection` | Remove all documents in a collection |
| **Directory sync** | |
| `register_directory` | Register a directory for sync |
| `deregister_directory` | Remove a directory registration |
| `sync_all_registrations` | Sync all registered directories |
| `list_registrations` | List registered directories |
| **System** | |
| `status` | Database stats: counts, storage size, model info |

**Claude Desktop note:** Uploaded files live in a sandbox that Quarry cannot access. Use `ingest_content` with extracted content for uploads. For files on your Mac, provide the local path to `ingest_file`.

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OCR_BACKEND` | `local` | `local` (RapidOCR, offline) or `textract` (AWS) |
| `LANCEDB_PATH` | `~/.quarry/data/default/lancedb` | Vector database location (overrides `--db`) |
| `CHUNK_MAX_CHARS` | `1800` | Target max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Overlap between consecutive chunks |

### OCR Backends

Quarry ships with two OCR backends:

| Backend | Speed | Quality | Setup |
|---------|-------|---------|-------|
| **local** (default) | ~7-8s/page | Good for semantic search | None |
| **textract** | ~2-3s/page | Excellent character accuracy | AWS credentials + S3 bucket |

The local backend uses RapidOCR (PaddleOCR models via ONNX Runtime, CPU-only, ~214 MB). No cloud account needed.

### AWS Textract Setup

Only needed if you want cloud OCR. Set these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ACCESS_KEY_ID` | | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | | AWS secret key |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `S3_BUCKET` | | S3 bucket for Textract uploads |

Your IAM user needs `textract:DetectDocumentText`, `textract:StartDocumentTextDetection`, `textract:GetDocumentTextDetection`, and `s3:PutObject/GetObject/DeleteObject` on your bucket.

### Named Databases

Use `--db` to keep separate databases for different projects:

```bash
quarry ingest-file report.pdf --db work
quarry ingest-file paper.pdf --db personal
quarry search "revenue" --db work
quarry databases  # list all databases with stats
```

Each database resolves to `~/.quarry/data/<name>/lancedb` with its own registry. Start an MCP server against a named database:

```json
{
  "mcpServers": {
    "work": {
      "command": "/path/to/uvx",
      "args": ["--from", "quarry-mcp", "quarry", "mcp", "--db", "work"]
    },
    "personal": {
      "command": "/path/to/uvx",
      "args": ["--from", "quarry-mcp", "quarry", "mcp", "--db", "personal"]
    }
  }
}
```

`LANCEDB_PATH` still works as an override for edge cases.

## Advanced Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TEXTRACT_POLL_INITIAL` | `5.0` | Initial Textract polling interval (seconds) |
| `TEXTRACT_POLL_MAX` | `30.0` | Max polling interval (1.5x exponential backoff) |
| `TEXTRACT_MAX_WAIT` | `900` | Max wait for Textract job (seconds) |
| `REGISTRY_PATH` | `~/.quarry/data/default/registry.db` | Directory sync SQLite database |

## Architecture

```
Connectors                Formats              Transformations
  │                         │                        │
  ├─ Local filesystem       ├─ PDF ──────┬─ text ──→ PyMuPDF extraction
  │   (register + sync)     │            └─ image ─→ OCR (local or Textract)
  │                         │
  └─ Google Drive           ├─ Images ─────────────→ OCR (local or Textract)
     (planned)              │
                            ├─ Text files ─────────→ Section-aware splitting
                            │
                            ├─ Source code ─────────→ Tree-sitter AST splitting
                            │
                            └─ Raw text ───────────→ Direct chunking
                                                         │
                                                  Indexing
                                                    │
                                                    ├─ Sentence-aware chunking
                                                    ├─ Chunk metadata (page_type, source_format)
                                                    ├─ Vector embeddings (768-dim)
                                                    └─ LanceDB storage
                                                         │
                                                  Query
                                                    │
                                                    ├─ Semantic search
                                                    └─ Collection filtering
                                                         │
                                                  Interface
                                                    │
                                                    ├─ MCP Server (stdio)
                                                    └─ CLI (typer + rich)
```

## Library API

Quarry is fully typed (`py.typed`) and can be used as a Python library:

```python
from pathlib import Path
from quarry import Settings, get_db, ingest_content, ingest_document, search
from quarry.backends import get_embedding_backend

# Load settings from environment variables
settings = Settings()
db = get_db(settings.lancedb_path)

# Ingest a file
result = ingest_document(Path("report.pdf"), db, settings, collection="work")

# Ingest inline content
result = ingest_content("Quarterly revenue was $4.2M.", "notes.txt", db, settings)

# Search
backend = get_embedding_backend(settings)
vector = backend.embed_query("revenue figures")
results = search(db, vector, limit=5, collection_filter="work")
for r in results:
    print(r["text"], r["_distance"])
```

The public API surface is in `quarry/__init__.py`. Pipeline functions accept a `progress_callback: Callable[[str], None]` for status updates during ingestion.

## Roadmap

- Spreadsheets (XLSX, CSV) via LaTeX tabular serialization
- Presentations (PPTX) with speaker notes
- HTML with structure-aware splitting
- macOS menu bar companion app
- Google Drive connector

For product vision and positioning, see [PR/FAQ](prfaq.pdf).

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ tests/
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, architecture, and how to add new formats.

## Documentation

- [Changelog](CHANGELOG.md)
- [Search Quality and Tuning](docs/SEARCH-TUNING.md)
- [Backend Abstraction Design](docs/BACKEND-ABSTRACTION.md)
- [Non-Functional Design](docs/NON-FUNCTIONAL-DESIGN.md)
- [PR/FAQ](prfaq.pdf) -- product vision and positioning

## License

[MIT](LICENSE)
