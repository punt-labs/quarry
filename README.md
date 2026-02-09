# quarry-mcp

[![PyPI](https://img.shields.io/pypi/v/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![GitHub release](https://img.shields.io/github/v/release/jmf-pobox/quarry-mcp)](https://github.com/jmf-pobox/quarry-mcp/releases)
[![Python 3.13+](https://img.shields.io/pypi/pyversions/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![Tests](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml)
[![Lint](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml)

Extract searchable knowledge from any document. Expose it to LLMs via MCP.

Quarry ingests PDFs, images, text files, and raw text into a local vector database, then serves semantic search over that content through the [Model Context Protocol](https://modelcontextprotocol.io). Point Claude Code or Claude Desktop at your documents and ask questions.

## Why Quarry?

If your documents are already machine-readable text (TXT, Markdown, DOCX), [mcp-local-rag](https://github.com/shinpr/mcp-local-rag) is a solid zero-config option — one `npx` command and you're searching.

Quarry exists for documents that aren't text yet:

- **Scanned PDFs** — Board packs, legal filings, archival records. No embedded text, just page images. Quarry classifies each page, routes image pages through AWS Textract OCR, and extracts text from the rest.
- **Mixed-format PDFs** — Some pages are text, some are scans. Quarry handles both in a single pipeline.
- **Images** — Photos of whiteboards, receipts, handwritten notes. PNG, JPG, TIFF (multi-page), BMP, WebP.
- **Text files** — TXT, Markdown, LaTeX, DOCX. No OCR needed, straight to chunking.
- **Raw text** — Paste content directly via `ingest_text`. Use this from Claude Desktop for uploaded files.

Quarry also preserves full page text alongside chunks, so LLMs can reference surrounding context when a search hit lands mid-page.

## Features

- **PDF ingestion** with automatic text/image classification per page
- **Image ingestion** — PNG, JPG, TIFF (multi-page), BMP, WebP via Textract OCR
- **Text file ingestion** — TXT, Markdown, LaTeX, DOCX
- **Raw text ingestion** — ingest content directly without a file on disk
- **OCR** via AWS Textract for scanned and image-based documents
- **Text extraction** via PyMuPDF for text-based PDF pages
- **Sentence-aware chunking** with configurable overlap
- **Local vector embeddings** using snowflake-arctic-embed-m-v1.5 (768-dim)
- **LanceDB** for fast, local vector storage (no external database)
- **Directory registration and incremental sync** — register directories, detect new/changed/deleted files via mtime+size, re-index in parallel
- **MCP server** with 13 tools: `search_documents`, `ingest`, `ingest_text`, `get_documents`, `get_page`, `delete_document`, `delete_collection`, `list_collections`, `register_directory`, `deregister_directory`, `sync_all_registrations`, `list_registrations`, `status`
- **CLI** for ingestion, search, document management, directory registration, and sync
- **Full page text preserved** alongside chunks for LLM reference

## Quick Start

```bash
pip install quarry-mcp

# Set up data directory, download embedding model, configure MCP clients
quarry install

# Check everything is working
quarry doctor

# Configure AWS credentials (required for OCR)
export AWS_ACCESS_KEY_ID=your-key
export AWS_SECRET_ACCESS_KEY=your-secret
export AWS_DEFAULT_REGION=us-east-1

# Ingest a PDF
quarry ingest /path/to/document.pdf

# Search
quarry search "revenue growth in 2024"

# List indexed documents
quarry list
```

## Installation

```bash
pip install quarry-mcp
quarry install
```

`quarry install` creates the data directory (`~/.quarry/data/lancedb/`), downloads the embedding model (~500MB), and configures MCP for Claude Code and Claude Desktop.

Run `quarry doctor` to verify your environment:

```
  ✓ Python version: 3.13.1
  ✓ Data directory: /Users/you/.quarry/data/lancedb
  ✓ AWS credentials: AKIA****YMUH (via shared-credentials-file)
  ✓ Embedding model: snowflake-arctic-embed-m-v1.5 cached
  ✓ Core imports: 5 modules OK
```

### AWS Setup

Quarry uses AWS Textract for OCR. Your IAM user needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "textract:DetectDocumentText",
        "textract:StartDocumentTextDetection",
        "textract:GetDocumentTextDetection"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::your-bucket/*"
    }
  ]
}
```

Set your S3 bucket:

```bash
export S3_BUCKET=your-bucket-name
```

## Usage

### MCP Server

`quarry install` configures both Claude Code and Claude Desktop automatically. To configure manually:

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

Use the absolute path to `uvx` for Desktop (e.g. `/opt/homebrew/bin/uvx`) since Desktop has a limited PATH. `quarry install` resolves this automatically.

### MCP Tools

| Tool | Description |
|------|-------------|
| `search_documents` | Semantic search across all indexed documents |
| `ingest` | OCR and index a file (PDF, image, TXT, MD, TEX, DOCX) |
| `ingest_text` | Index raw text content directly (for uploads or pasted text) |
| `get_documents` | List all indexed documents with metadata |
| `get_page` | Retrieve full text for a specific page |
| `delete_document` | Remove a document and all its chunks |
| `delete_collection` | Remove all documents in a collection |
| `list_collections` | List all collections with document and chunk counts |
| `register_directory` | Register a directory for incremental sync |
| `deregister_directory` | Remove a directory registration |
| `sync_all_registrations` | Sync all registered directories (ingest new/changed, remove deleted) |
| `list_registrations` | List all registered directories |
| `status` | Database stats: document/chunk counts, registrations, storage size, model info |

**Claude Desktop note:** Uploaded files live in a container that Quarry cannot access. For uploaded files, use `ingest_text` with the extracted content. For files on your Mac, provide the local path to `ingest`.

### CLI

```bash
# Ingest documents
quarry ingest report.pdf
quarry ingest whiteboard.jpg
quarry ingest notes.md
quarry ingest report.pdf --overwrite

# Search
quarry search "board governance structure"
quarry search "quarterly revenue" -n 5

# Manage documents
quarry list
quarry delete report.pdf
quarry collections
quarry delete-collection math

# Register directories for incremental sync
quarry register /path/to/courses/ml-101 --collection ml-101
quarry register /path/to/courses/stats-200
quarry registrations
quarry sync
quarry sync --workers 8
quarry deregister ml-101

# Environment
quarry doctor
quarry install
```

### Multiple Indices

Run separate MCP server instances with different data directories:

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

## Configuration

All settings are configurable via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ACCESS_KEY_ID` | | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | | AWS secret key |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `S3_BUCKET` | `ocr-7f3a1b2e4c5d4e8f9a1b3c5d7e9f2a4b` | S3 bucket for Textract uploads |
| `LANCEDB_PATH` | `~/.quarry/data/lancedb` | Path to LanceDB storage |
| `EMBEDDING_MODEL` | `Snowflake/snowflake-arctic-embed-m-v1.5` | HuggingFace embedding model |
| `CHUNK_MAX_CHARS` | `1800` | Target max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Character overlap between consecutive chunks |
| `TEXTRACT_POLL_INITIAL` | `5.0` | Initial seconds between Textract status checks |
| `TEXTRACT_POLL_MAX` | `30.0` | Maximum polling interval (exponential backoff, 1.5x) |
| `TEXTRACT_MAX_WAIT` | `900` | Maximum seconds to wait for Textract job |
| `REGISTRY_PATH` | `~/.quarry/data/registry.db` | Path to directory registration SQLite database |

## Architecture

```
Input
  │
  ├─ PDF ─────────┬─ Text pages ──→ PyMuPDF extraction
  │               └─ Image pages ─→ S3 → Textract async OCR → S3 cleanup
  │
  ├─ Images ──────→ Textract sync OCR (BMP/WebP converted to PNG)
  │                 TIFF multi-page → S3 → Textract async OCR
  │
  ├─ Text files ──→ Direct text extraction (TXT, MD, TEX, DOCX)
  │
  └─ Raw text ────→ ingest_text (from uploads, clipboard, etc.)
                          │
                    Sentence-aware chunking (with overlap)
                          │
                    snowflake-arctic-embed-m-v1.5
                          │
                    LanceDB (local vector store)
                          │
                 ┌────────┴────────┐
                 │                 │
             MCP Server         CLI
         (stdio transport)   (typer + rich)

Incremental Sync
  │
  Directory Registry (SQLite, WAL mode)
  │
  ├─ register → track directory + collection mapping
  ├─ sync ────→ walk directory, compare mtime+size
  │              ├─ new/changed → ThreadPoolExecutor → ingest pipeline
  │              ├─ unchanged  → skip
  │              └─ deleted    → remove from LanceDB + registry
  └─ deregister → remove tracking + optionally clean data
```

Each chunk stores both its text fragment and the full page raw text, so LLMs can reference surrounding context when a search result is relevant.

## Development

```bash
# Run all quality gates
uv run ruff check .
uv run ruff format --check .
uv run mypy src/quarry tests
uv run pytest
```

The project enforces strict mypy, comprehensive ruff rules, and requires all tests to pass before every commit.

## License

[MIT](LICENSE)
