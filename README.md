# punt-quarry

> Local semantic search for AI agents and humans.

[![License](https://img.shields.io/github/license/punt-labs/quarry)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/punt-labs/quarry/test.yml?label=CI)](https://github.com/punt-labs/quarry/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Python](https://img.shields.io/pypi/pyversions/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Working Backwards](https://img.shields.io/badge/Working_Backwards-hypothesis-lightgrey)](./prfaq.pdf)

Quarry indexes documents in 20+ formats, embeds them with a local ONNX model (snowflake-arctic-embed-m-v1.5, 768-dim), stores vectors in LanceDB, and serves semantic search to Claude Code, Claude Desktop, and the CLI. Everything runs locally — no API keys, no cloud accounts. The embedding model (~500 MB) downloads once on first use.

**Platforms:** macOS, Linux

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/4bffcc0/install.sh | sh
```

Restart Claude Code, then:

```text
> /ingest report.pdf                    # index a document (runs in background)
> /quarry status                        # after a moment, confirm it's there
> /find "what does the report say about margins"   # search by meaning
```

Once installed, a plugin hook auto-indexes your current project directory on every session start — you don't need to `/ingest` your codebase manually.

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
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/4bffcc0/install.sh -o install.sh
shasum -a 256 install.sh
cat install.sh
sh install.sh
```

</details>

### Claude Desktop

[**Download punt-quarry.mcpb**](https://github.com/punt-labs/quarry/releases/latest/download/punt-quarry.mcpb) and double-click to install. Alternatively, `quarry install` configures Claude Desktop automatically.

**Note:** Uploaded files in Claude Desktop live in a sandbox that quarry cannot access. Use `remember` for uploaded content, or provide local file paths to `ingest`.

## Features

- **20+ formats** --- PDFs (with OCR for scanned pages), source code (AST-aware splitting), spreadsheets, presentations, HTML, Markdown, LaTeX, DOCX, images
- **Semantic search** --- retrieval is by meaning, not keyword. A query about "margins" finds passages about profitability even if they never use that word
- **Daemon architecture** --- one `quarry serve` process loads the embedding model once (~200 MB RAM) and serves all Claude Code sessions via [mcp-proxy](https://github.com/punt-labs/mcp-proxy) over WebSocket
- **Passive knowledge capture** --- SessionStart hook auto-indexes the working directory, PostToolUse hook auto-ingests fetched URLs, PreCompact hook captures transcripts before context compaction
- **Named databases** --- isolated LanceDB directories with independent sync registries. Switch with `use` for work/personal separation
- **Research agent** --- `researcher` subagent combines quarry local search with web research, auto-ingests valuable findings

## What It Looks Like

### Ingest a document

```text
> /ingest report.pdf

▶ Ingesting report.pdf (background)
```

### Check what's indexed

```text
> /quarry

▶ Database: default
  Documents: 47
  Chunks: 1,203
  Size: 12.4 MB
  Model: snowflake-arctic-embed-m-v1.5 (768-dim)
```

### Search by meaning

```text
> /find "what were the Q3 revenue figures"

▶ [report.pdf p.12 | text/.pdf] (similarity: 0.4521)
  Third quarter revenue reached $142M, up 18% year-over-year,
  driven primarily by expansion in the enterprise segment.
  Gross margins improved to 71% from 68% in Q2.
```

## Commands

### Slash Commands (Claude Code)

| Command | What it does |
|---------|-------------|
| `/ingest <source>` | Ingest a URL, directory, or file |
| `/remember <name>` | Ingest inline text under a document name |
| `/find <query>` | Semantic search. Questions get synthesized answers; keywords get raw results |
| `/explain <topic>` | Search and synthesize an explanation |
| `/source <claim>` | Find which document a claim comes from |
| `/quarry [sub]` | Manage: `status`, `sync`, `collections`, `databases`, `registrations` |

### MCP Tools

| Tool | Purpose | Execution |
|------|---------|-----------|
| `ingest` | Index a file or URL | Background |
| `remember` | Index inline text | Background |
| `register_directory` | Register directory for sync | Background |
| `sync_all_registrations` | Re-index all registered directories | Background |
| `find` | Semantic search with filters | Sync |
| `show` | Document metadata or page text | Sync |
| `list` | Documents, collections, databases, registrations | Sync |
| `status` | Database statistics | Sync |
| `delete` | Remove document or collection | Background |
| `deregister_directory` | Remove registration | Background |
| `use` | Switch active database | Sync |

### CLI

```bash
quarry ingest report.pdf                       # index a file
quarry ingest https://example.com              # index a webpage
echo "notes" | quarry remember --name notes.md # index inline text
quarry find "revenue trends"                   # hybrid search (vector + FTS)
quarry list documents                          # list indexed documents
quarry register ~/Documents/notes              # watch a directory
quarry sync                                    # re-index registered dirs
quarry use work                                # switch database
quarry status                                  # database dashboard
quarry doctor                                  # health check
quarry serve                                   # start daemon on :8420

# Agent memory tagging
quarry ingest notes.md --agent-handle claude --memory-type fact
quarry find "deployment steps" --agent-handle claude
echo "key insight" | quarry remember --name insight.md --agent-handle claude \
  --memory-type observation --summary "Key insight from review"
```

## Setup

Quarry works with zero configuration. These environment variables are available for customization:

| Variable | Default | Description |
|----------|---------|-------------|
| `QUARRY_API_KEY` | *(none)* | Bearer token for `quarry serve` |
| `QUARRY_ROOT` | `~/.punt-labs/quarry/data` | Base directory for all databases |
| `CHUNK_MAX_CHARS` | `1800` | Max characters per chunk (~450 tokens) |
| `CHUNK_OVERLAP_CHARS` | `200` | Overlap between consecutive chunks |

For the full configuration reference, see [Architecture](docs/architecture.tex) section 7.

## Passive Knowledge Capture

Beyond explicit `/ingest` and `/find` commands, quarry runs as a Claude Code plugin with hooks that capture knowledge automatically during your sessions:

| Hook | When it fires | What it does |
|------|--------------|-------------|
| **Session start** | On every session start | Auto-registers your project directory and syncs it in the background. Your codebase is searchable without manual ingestion. |
| **Web fetch** | After any `WebFetch` tool call | URLs Claude fetches during research are auto-ingested into a `web-captures` collection. Reuses already-retrieved content when available, falls back to URL ingest otherwise. |
| **Pre-compact** | Before context compaction | Captures the conversation transcript into a `session-notes` collection. Discoveries that would be lost when the context window shrinks are preserved as searchable chunks. |

All hooks are fail-open — failures are ignored and never block Claude Code. Each hook is individually toggleable via `.punt-labs/quarry/config.md` YAML frontmatter. See [AGENTS.md](AGENTS.md) for the full integration model.

## How It Works

Quarry runs as a daemon. Claude Code sessions connect through mcp-proxy:

```text
                    stdio                      WebSocket
Claude Code <-----------------> mcp-proxy <---------------------> quarry serve
             MCP JSON-RPC       (~5 MB Go)                        (one daemon)
```

Without the proxy, every session spawns a separate Python process, each loading the embedding model into ~200 MB of RAM. With it, startup is instant and state is shared across all sessions.

`quarry install` downloads mcp-proxy (SHA256-verified, correct platform) and configures MCP clients.

## Documentation

[Architecture](docs/architecture.tex) |
[Z Specification](docs/claude-code-quarry.tex) |
[Design](DESIGN.md) |
[Agents](AGENTS.md) |
[Changelog](CHANGELOG.md)

## Development

```bash
uv sync                        # install dependencies
make check                     # run all quality gates (lint, type, test)
make test                      # test suite only
make format                    # auto-format code
make docs                      # build LaTeX documents
```

## License

MIT
