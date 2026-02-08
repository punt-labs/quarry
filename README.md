# quarry-mcp

Extract searchable knowledge from any document. Expose it to LLMs via MCP.

Quarry ingests PDFs, images, text files, and audio into a local vector database, then serves semantic search over that content through the [Model Context Protocol](https://modelcontextprotocol.io). Point Claude Code or Claude Desktop at your documents and ask questions.

## Features

- **PDF ingestion** with automatic text/image classification per page
- **OCR** via AWS Textract for scanned and image-based documents
- **Text extraction** via PyMuPDF for text-based PDF pages
- **Sentence-aware chunking** with configurable overlap
- **Local vector embeddings** using snowflake-arctic-embed-m-v1.5 (768-dim)
- **LanceDB** for fast, local vector storage (no external database)
- **MCP server** with 4 tools: `search_documents`, `ingest`, `get_documents`, `get_page`
- **CLI** for ingestion, search, and document management
- **Full page text preserved** alongside chunks for LLM reference

## Quick Start

```bash
# Clone and install
git clone https://github.com/youruser/quarry-mcp.git
cd quarry-mcp
uv sync

# Configure AWS credentials (required for OCR)
export AWS_ACCESS_KEY_ID=your-key
export AWS_SECRET_ACCESS_KEY=your-secret
export AWS_DEFAULT_REGION=us-east-1

# Ingest a PDF
uv run quarry ingest /path/to/document.pdf

# Search
uv run quarry search "revenue growth in 2024"

# List indexed documents
uv run quarry list
```

## Installation

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

For development:

```bash
uv pip install -e ".[dev]"
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

Set your S3 bucket via environment variable or `.env` file:

```bash
export S3_BUCKET=your-bucket-name
```

## Usage

### MCP Server (Claude Code)

Add to your Claude Code configuration:

```bash
claude mcp add quarry -- uv run --directory /path/to/quarry-mcp python -m ocr mcp
```

After restarting Claude Code, four tools are available:

| Tool | Description |
|------|-------------|
| `search_documents` | Semantic search across all indexed documents |
| `ingest` | OCR and index a new PDF |
| `get_documents` | List all indexed documents with metadata |
| `get_page` | Retrieve full OCR text for a specific page |

### MCP Server (Claude Desktop)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "quarry": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/quarry-mcp", "python", "-m", "ocr", "mcp"],
      "env": {
        "AWS_ACCESS_KEY_ID": "your-key",
        "AWS_SECRET_ACCESS_KEY": "your-secret",
        "AWS_DEFAULT_REGION": "us-east-1",
        "S3_BUCKET": "your-bucket"
      }
    }
  }
}
```

### CLI

```bash
# Ingest a document
uv run quarry ingest report.pdf

# Re-ingest (overwrite existing)
uv run quarry ingest report.pdf --overwrite

# Search across all documents
uv run quarry search "board governance structure"

# Search with result limit
uv run quarry search "quarterly revenue" -n 5

# List indexed documents
uv run quarry list
```

### Multiple Indices

Run separate MCP server instances with different data directories:

```json
{
  "mcpServers": {
    "legal-docs": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/quarry-mcp", "python", "-m", "ocr", "mcp"],
      "env": { "LANCEDB_PATH": "/data/legal/lancedb" }
    },
    "financial-reports": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/quarry-mcp", "python", "-m", "ocr", "mcp"],
      "env": { "LANCEDB_PATH": "/data/financial/lancedb" }
    }
  }
}
```

## Configuration

All settings are configurable via environment variables or a `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ACCESS_KEY_ID` | | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | | AWS secret key |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `S3_BUCKET` | `ocr-7f3a1b2e4c5d4e8f9a1b3c5d7e9f2a4b` | S3 bucket for Textract uploads |
| `LANCEDB_PATH` | `./data/lancedb` | Path to LanceDB storage |
| `EMBEDDING_MODEL` | `Snowflake/snowflake-arctic-embed-m-v1.5` | HuggingFace embedding model |
| `CHUNK_MAX_CHARS` | `1800` | Target max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Character overlap between consecutive chunks |
| `TEXTRACT_POLL_INTERVAL` | `5` | Seconds between Textract status checks |
| `TEXTRACT_MAX_WAIT` | `900` | Maximum seconds to wait for Textract job |

## Architecture

```
Input (PDF)
  │
  ├─ Text pages ──→ PyMuPDF text extraction
  │                        │
  └─ Image pages ─→ S3 upload → Textract async OCR → parse → S3 cleanup
                           │
                     Page contents
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
```

Each chunk stores both its text fragment and the full page raw text, so LLMs can reference surrounding context when a search result is relevant.

## Roadmap

### Epic 1: PDF Pipeline ✓

Core ingestion and search for PDF documents.

- [x] PDF page analysis (text vs image classification)
- [x] Text extraction via PyMuPDF
- [x] OCR via AWS Textract (async API with polling)
- [x] Sentence-aware chunking with configurable overlap
- [x] Local vector embeddings (snowflake-arctic-embed-m-v1.5)
- [x] LanceDB vector storage with PyArrow schema
- [x] MCP server with search, ingest, list, and page retrieval
- [x] CLI with progress display
- [x] Test suite (62 tests across 9 modules)

### Epic 2: Text Document Ingestion

Direct ingestion of text-based formats without OCR.

- [ ] Plain text files (.txt)
- [ ] Markdown (.md)
- [ ] LaTeX (.tex)
- [ ] Configurable page/section boundary detection

### Epic 3: Image Format Support

OCR for standalone image files (not wrapped in PDF).

- [ ] Common formats: PNG, JPG, TIFF, BMP, WebP
- [ ] Single-image and batch ingestion
- [ ] Image preprocessing for OCR quality (deskew, contrast)

### Epic 4: Audio Transcription

Speech-to-text ingestion for audio content.

- [ ] Audio format support: MP3, WAV, M4A, FLAC
- [ ] AWS Transcribe or Whisper integration
- [ ] Speaker diarization
- [ ] Timestamped chunks for source reference

### Epic 5: Ingestion Quality

Post-processing to improve extracted text quality.

- [ ] LLM-based OCR error correction
- [ ] Chunk quality scoring and filtering
- [ ] Duplicate and near-duplicate detection
- [ ] Table and figure extraction

### Epic 6: Multi-Index Management

First-class support for organizing documents into collections.

- [ ] Named indices with isolated storage
- [ ] Per-index configuration (embedding model, chunk size)
- [ ] Cross-index search
- [ ] Index metadata and statistics

## Development

```bash
# Run all quality gates
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ocr tests
uv run pytest

# Auto-format
uv run ruff format .
```

The project enforces strict mypy, comprehensive ruff rules, and requires all tests to pass before every commit.

## License

[MIT](LICENSE)
