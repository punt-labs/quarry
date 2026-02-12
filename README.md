# quarry-mcp

[![PyPI](https://img.shields.io/pypi/v/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![GitHub release](https://img.shields.io/github/v/release/jmf-pobox/quarry-mcp)](https://github.com/jmf-pobox/quarry-mcp/releases)
[![Python 3.13+](https://img.shields.io/pypi/pyversions/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![Tests](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml)
[![Lint](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml)

Index any document. Search with natural language. Works with Claude Code and Claude Desktop.

## Quick Start

```bash
pip install quarry-mcp
quarry install          # downloads embedding model (~500MB), configures MCP
quarry ingest notes.md  # index a file — no cloud account needed
quarry search "my topic"
```

That's it. Quarry works locally out of the box.

## What It Does

Quarry turns documents into searchable knowledge for LLMs. You feed it files, it chunks and embeds them into a local vector database, and exposes semantic search via MCP tools or a CLI.

**Supported formats:** PDF, images (PNG, JPG, TIFF, BMP, WebP), text files (TXT, Markdown, LaTeX, DOCX), and source code (30+ languages).

**How each format is processed:**

| Source | What happens | Result |
|--------|-------------|--------|
| PDF (text pages) | Text extraction via PyMuPDF | Prose chunks |
| PDF (image pages) | OCR (local or cloud) | Prose chunks |
| Images | OCR (local or cloud) | Prose chunks |
| Text files | Split by headings / sections / paragraphs | Section chunks |
| Source code | Tree-sitter AST parsing (functions, classes) | Code chunks |

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
quarry ingest report.pdf
quarry ingest whiteboard.jpg
quarry ingest src/main.py
quarry ingest report.pdf --overwrite

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
| `search_documents` | Semantic search across indexed documents |
| `ingest` | Ingest a file (PDF, image, text, source code) |
| `ingest_text` | Index raw text content directly |
| `get_documents` | List indexed documents with metadata |
| `get_page` | Retrieve full text for a specific page |
| `delete_document` | Remove a document and its chunks |
| `delete_collection` | Remove all documents in a collection |
| `list_collections` | List collections with document/chunk counts |
| `register_directory` | Register a directory for sync |
| `deregister_directory` | Remove a directory registration |
| `sync_all_registrations` | Sync all registered directories |
| `list_registrations` | List registered directories |
| `status` | Database stats: counts, storage size, model info |

**Claude Desktop note:** Uploaded files live in a sandbox that Quarry cannot access. Use `ingest_text` with extracted content for uploads. For files on your Mac, provide the local path to `ingest`.

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OCR_BACKEND` | `local` | `local` (RapidOCR, offline) or `textract` (AWS) |
| `LANCEDB_PATH` | `~/.quarry/data/lancedb` | Vector database location |
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
| `S3_BUCKET` | `ocr-7f3a1b2e4c5d4e8f9a1b3c5d7e9f2a4b` | S3 bucket for Textract uploads |

Your IAM user needs `textract:DetectDocumentText`, `textract:StartDocumentTextDetection`, `textract:GetDocumentTextDetection`, and `s3:PutObject/GetObject/DeleteObject` on your bucket.

### Multiple Indices

Run separate MCP instances with different data directories:

```json
{
  "mcpServers": {
    "legal-docs": {
      "command": "/path/to/uvx",
      "args": ["--from", "quarry-mcp", "quarry", "mcp"],
      "env": { "LANCEDB_PATH": "/data/legal/lancedb" }
    },
    "financial-reports": {
      "command": "/path/to/uvx",
      "args": ["--from", "quarry-mcp", "quarry", "mcp"],
      "env": { "LANCEDB_PATH": "/data/financial/lancedb" }
    }
  }
}
```

## Advanced Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TEXTRACT_POLL_INITIAL` | `5.0` | Initial Textract polling interval (seconds) |
| `TEXTRACT_POLL_MAX` | `30.0` | Max polling interval (1.5x exponential backoff) |
| `TEXTRACT_MAX_WAIT` | `900` | Max wait for Textract job (seconds) |
| `REGISTRY_PATH` | `~/.quarry/data/registry.db` | Directory sync SQLite database |

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

## Roadmap

- Spreadsheets (XLSX, CSV) via tabular serialization
- Presentations (PPTX) with speaker notes
- HTML with structure-aware splitting
- Search filters by content type and file format
- Google Drive connector

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ tests/
uv run pytest
```

## License

[MIT](LICENSE)
