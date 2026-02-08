# Changelog

All notable changes to quarry-mcp will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `delete_document` MCP tool and `quarry delete` CLI command to remove indexed documents
- `status` MCP tool reporting document/chunk counts, database size, and embedding model info
- `count_chunks` database function for O(1) chunk counting
- MCP server tests (`test_mcp_server.py`)
- CHANGELOG.md

## [0.1.0] - 2026-02-08

### Added
- PDF ingestion with automatic text/image page classification
- OCR via AWS Textract (async API with polling)
- Text extraction via PyMuPDF for text-based pages
- Sentence-aware chunking with configurable overlap
- Local vector embeddings using snowflake-arctic-embed-m-v1.5 (768-dim)
- LanceDB vector storage with PyArrow schema
- MCP server with `search_documents`, `ingest`, `get_documents`, `get_page` tools
- CLI with `ingest`, `search`, `list` commands and Rich progress display
- Full page text preserved alongside chunks for LLM context
- 62 tests across 9 modules
