# quarry-mcp

[![PyPI](https://img.shields.io/pypi/v/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![GitHub release](https://img.shields.io/github/v/release/jmf-pobox/quarry-mcp)](https://github.com/jmf-pobox/quarry-mcp/releases)
[![Python 3.13+](https://img.shields.io/pypi/pyversions/quarry-mcp)](https://pypi.org/project/quarry-mcp/)
[![Tests](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/test.yml)
[![Lint](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml/badge.svg)](https://github.com/jmf-pobox/quarry-mcp/actions/workflows/lint.yml)

A document intelligence pipeline for LLMs. Ingest anything, search everything.

Quarry transforms documents into searchable knowledge through format-aware ingestion, intelligent content transformations, local vector indexing, and semantic search — exposed as both an MCP server and a CLI.

## Why Quarry?

Most RAG tools handle plain text. Quarry handles the full spectrum:

| Capability | What Quarry does | Typical RAG tool |
|---|---|---|
| **Formats** | PDF, images, code, text, spreadsheets (planned) | Text files only |
| **Transformations** | OCR, tree-sitter AST splitting, LaTeX tabular (planned) | None — expects pre-processed text |
| **Indexing** | Vector embeddings, incremental sync, collections | Basic embedding |
| **Query** | Semantic search, format filters (planned), full page context | Vector similarity only |
| **Interface** | MCP server + CLI | Usually one or the other |
| **OCR** | Cloud (AWS Textract) today, local (Tesseract) planned | None |

Quarry's vision: support both local and cloud backends for each capability. Users with GPUs run everything locally. Others use cloud services. Anyone can mix and match.

## Capabilities

### Formats

Quarry ingests these document types today:

- **PDF** — automatic text/image classification per page. Text pages use PyMuPDF; image pages route through OCR.
- **Images** — PNG, JPG, TIFF (multi-page), BMP, WebP.
- **Text files** — TXT, Markdown, LaTeX, DOCX. Section-aware splitting by headings and structure.
- **Source code** — 30+ languages via tree-sitter AST splitting. Functions, classes, and imports become semantic sections.
- **Raw text** — paste content directly via `ingest_text` for uploads or clipboard.

### Transformations

Each format goes through a content-specific transformation before indexing:

| Source | Transformation | Output |
|---|---|---|
| PDF (text pages) | PyMuPDF extraction | Prose chunks |
| PDF (image pages) | AWS Textract OCR | Prose chunks |
| Images | AWS Textract OCR | Prose chunks |
| Text files | Section-aware splitting (headings, `\section{}`, paragraphs) | Section chunks |
| Source code | Tree-sitter AST parsing (functions, classes, imports) | Code chunks |
| Spreadsheets (planned) | pandas → LaTeX tabular | Tabular chunks |
| Presentations (planned) | Slide + speaker notes extraction | Slide chunks |

### Connectors

- **Local filesystem** — ingest individual files or register directories for incremental sync. Detects new, changed, and deleted files via mtime+size comparison. Parallel ingestion via ThreadPoolExecutor.
- **Google Drive** (planned) — cloud document source.

### Indexing

- **Vector embeddings** — snowflake-arctic-embed-m-v1.5 (768-dim), runs locally.
- **Sentence-aware chunking** — 1800-char target with 200-char overlap. Preserves sentence boundaries.
- **Incremental sync** — register directories, sync on demand. Only re-indexes changed files.
- **Collections** — organize documents by project, topic, or source.
- **Full page context** — each chunk retains the complete page text for LLM reference.

### Query

- **Semantic search** — vector similarity across all indexed documents.
- **Collection filtering** — scope searches to specific collections.
- **Content type and format filters** (planned) — filter by `page_type` (code, text, spreadsheet) or `source_format` (.pdf, .py, .xlsx).
- **Hybrid search** (planned) — combine vector similarity with document-level ranking.

### Interface

- **MCP server** — 13 tools for ingestion, search, sync, and document management. Works with Claude Code and Claude Desktop.
- **CLI** — same capabilities via `quarry` command with Rich progress display.

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
| `ingest` | Ingest a file (PDF, image, text, source code) |
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
Connectors                Formats              Transformations
  │                         │                        │
  ├─ Local filesystem       ├─ PDF ──────┬─ text ──→ PyMuPDF extraction
  │   (register + sync)     │            └─ image ─→ Textract OCR
  │                         │
  └─ Google Drive           ├─ Images ─────────────→ Textract OCR
     (planned)              │
                            ├─ Text files ─────────→ Section-aware splitting
                            │
                            ├─ Source code ─────────→ Tree-sitter AST splitting
                            │
                            ├─ Spreadsheets ───────→ LaTeX tabular (planned)
                            │
                            └─ Raw text ───────────→ Direct chunking
                                                         │
                                                  Indexing
                                                    │
                                                    ├─ Sentence-aware chunking
                                                    ├─ Vector embeddings
                                                    └─ LanceDB storage
                                                         │
                                                  Query
                                                    │
                                                    ├─ Semantic search
                                                    ├─ Collection filtering
                                                    └─ Format filters (planned)
                                                         │
                                                  Interface
                                                    │
                                                    ├─ MCP Server (stdio)
                                                    └─ CLI (typer + rich)
```

## Roadmap

### Formats
- **Spreadsheets** — XLSX, XLS, CSV ingestion via LaTeX tabular serialization
- **Presentations** — PPTX slide extraction with speaker notes
- **HTML** — web page ingestion with structure-aware splitting
- **Email** — EML/MBOX with header, body, and attachment extraction

### Transformations
- **Local OCR** — Tesseract backend for offline/air-gapped environments
- **PII detection** — identify and redact sensitive information before indexing

### Connectors
- **Google Drive** — cloud document source with incremental sync

### Query
- **Search filters** — filter by content type and file format for targeted retrieval
- **Hybrid search** — combine vector similarity with document-level ranking

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
