# punt-quarry

> Local semantic search for AI agents and humans.

[![License](https://img.shields.io/github/license/punt-labs/quarry)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/punt-labs/quarry/test.yml?label=CI)](https://github.com/punt-labs/quarry/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Python](https://img.shields.io/pypi/pyversions/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Working Backwards](https://img.shields.io/badge/Working_Backwards-hypothesis-lightgrey)](./prfaq.pdf)

Quarry indexes documents in 20+ formats, embeds them with a local ONNX model (snowflake-arctic-embed-m-v1.5), stores the vectors in LanceDB, and serves semantic search to Claude Code, Claude Desktop, and the command line. Everything runs locally — no API keys, no cloud accounts. One `quarryd` daemon per machine loads the model once; the CLI, the MCP server, and the Claude Code hooks are thin clients over it, reachable directly too via an HTTP API.

**Platforms:** macOS (Apple Silicon), Linux

## Quick Start

Install the CLI, the daemon, the MCP server, and the Claude Code plugin:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/935c58d/install.sh | sh
```

Restart Claude Code. Your current project is auto-indexed at session start, so you can search it by meaning right away — see [What It Looks Like](#what-it-looks-like).

<details>
<summary>Manual install (if you already have uv)</summary>

Install the package:

```bash
uv tool install punt-quarry
```

Set up the daemon, TLS certificates, and MCP config:

```bash
quarry install
```

Check health:

```bash
quarry doctor
```

</details>

<details>
<summary>Homebrew (Apple Silicon macOS, Linux)</summary>

Intel macOS is not currently supported by any install path — two of quarry's dependencies (`lancedb`, `onnxruntime`) publish no Intel macOS wheel, so `uv tool install`/`pip install` fails there the same way `brew install` does.

`brew install` puts the `quarry`, `quarryd`, and `quarry-hook` binaries on `PATH`. Run `quarry install` afterward for the model download, TLS certificates, and daemon service:

```bash
brew install punt-labs/tap/quarry
quarry install
```

To add the Claude Code plugin too:

```bash
claude plugin marketplace add punt-labs/claude-plugins
claude plugin install quarry@punt-labs
```

Use one distribution channel per machine — mixing Homebrew with the `curl | sh` installer puts two copies of `quarry` on `PATH` in different locations, and whichever comes first wins. Run `which quarry` (or `command -v quarry`) to see which one that is.

</details>

<details>
<summary>CLI only (skip the Claude Code plugin)</summary>

For non-Claude harnesses (Codex, Cursor, a plain terminal) or Claude Code users whose org policy blocks marketplace/plugin installs, `--no-plugin` installs everything except the marketplace-register and plugin-install steps:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/935c58d/install.sh | sh -s -- --no-plugin
```

Where a flag cannot be passed (CI templating a bare `curl … | sh`), set `QUARRY_NO_PLUGIN=1` — honored only when exactly `1`:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/935c58d/install.sh | QUARRY_NO_PLUGIN=1 sh
```

Everything else runs unchanged. Use the CLI and the stdio `quarry mcp` server directly; both talk to the resident `quarryd`. Re-run the installer without `--no-plugin` to add the plugin later.

</details>

<details>
<summary>Verify before running</summary>

Download the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/935c58d/install.sh -o install.sh
```

Check its digest (`shasum -a 256 install.sh` on macOS):

```bash
sha256sum install.sh
```

Read it:

```bash
cat install.sh
```

Run it:

```bash
sh install.sh
```

</details>

## Features

- **20+ formats** — PDFs (with OCR for scanned pages), source code (AST-aware splitting), spreadsheets, presentations, HTML, Markdown, LaTeX, DOCX, images.
- **Semantic search** — retrieval is by meaning, not keyword. A query about "margins" finds passages about profitability even if they never use that word.
- **One daemon, thin clients** — a single `quarryd` process loads the embedding model once and serves the CLI, the MCP server, and the Claude Code hooks over a versioned REST API. Its resource use is bounded so it stays quiet in the background while you work.
- **Passive knowledge capture** — `quarry enable` sets up per-project file sync, web-fetch and session-transcript capture, and per-agent memory. Captures are PII/secret-scrubbed at write time and kept separate from the code index. See [Knowledge Capture](#knowledge-capture).
- **Named databases** — isolated LanceDB directories with independent sync registries; switch with `quarry use` for work/personal separation.
- **Remote server** — run the engine on a GPU host and connect from any Mac or Linux client over TLS. See [ADVANCED-SETUP.md](ADVANCED-SETUP.md#remote-server).

## What It Looks Like

Sync a folder:

```text
> /ingest ~/Documents/research

▶  Registering /Users/you/Documents/research as 'research' (task a1b2c3)
▶  Syncing all registrations (task d4e5f6)
```

Search by meaning:

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
| `/ingest <source>` | Ingest a URL, or register+sync a local file or directory |
| `/remember <name>` | Ingest inline text under a document name |
| `/find <query>` | Semantic search; questions get synthesized answers, keywords get raw results |
| `/explain <topic>` | Search and synthesize an explanation |
| `/source <claim>` | Find which document a claim comes from |
| `/quarry [sub]` | Manage: `status`, `sync`, `collections`, `databases`, `registrations` |

### MCP Tools

| Tool | Purpose |
|------|---------|
| `find` | Semantic search with filters |
| `show` | Document metadata or page text |
| `list` | Documents, collections, databases, registrations |
| `status` | Database statistics |
| `ingest` / `remember` | Index a URL, or inline text |
| `register_directory` / `deregister_directory` | Manage a synced directory |
| `sync_all_registrations` | Re-index all registered directories |
| `delete` | Remove a document or collection |
| `use` | Switch the active database |

### CLI

| Command | What it does |
|---------|-------------|
| `quarry find "<query>"` | Hybrid search (vector + full-text) |
| `quarry ingest <url>` | Index a webpage (local files/directories: `quarry register`) |
| `quarry remember --name <name>` | Index inline text from stdin |
| `quarry list documents` | List indexed documents |
| `quarry register <dir>` | Watch a directory for changes |
| `quarry sync` | Re-index registered directories |
| `quarry enable` / `quarry disable` | Set up / tear down project collections + captures |
| `quarry use <name>` | Switch the active database |
| `quarry status` | Database dashboard |
| `quarry doctor` | Health check |
| `quarry install` | Set up the daemon service, TLS certs, and MCP config |
| `quarry uninstall` | Remove the daemon service (its launchd/systemd unit) |
| `quarry login <host> --api-key <token>` | Connect to a remote server (TOFU pinning) |
| `quarry logout` | Disconnect, revert to the local daemon |

Agent-memory tagging is available on `ingest`/`remember`/`find` via `--agent-handle`, `--memory-type`, and `--summary`.

A registered directory isn't cron-driven — `quarryd` runs a live filesystem
watch (debounced, ~1s) that reacts to changes as they happen, backed by a
5-minute periodic safety sweep (catches anything the watch missed, self-heals
the search index). `quarry sync` triggers an immediate
one-shot pass on top of that; you don't need to run it after every edit.

The watch honors ignore rules the way git does: `.gitignore` (at every level),
a root-level `.quarryignore`, and built-in scratch/VCS defaults all prune both
what gets indexed and which directories consume OS watch resources — a giant
`node_modules` or `.venv` costs nothing. `quarry list registrations` shows each
collection's live watch state (`watched`, `degraded`, or `scan-only`); a
`scan-only` collection still stays current via the periodic sweep.

## Setup

Quarry works with zero configuration. For environment variables and running
the engine on a remote/GPU host, see [ADVANCED-SETUP.md](ADVANCED-SETUP.md).

## Claude Desktop

The `.mcpb` bundle is an on-top way to reach the **same** local index from Claude Desktop. It embeds no engine — it registers the thin `quarry mcp` client, which talks to the same `quarryd` that backs the CLI and Claude Code. It is not a standalone install: quarry must already be installed and running.

`quarry install` configures Claude Desktop automatically. To add it by hand instead, [download `punt-quarry.mcpb`](https://github.com/punt-labs/quarry/releases/latest/download/punt-quarry.mcpb) and double-click it.

Uploaded files in Claude Desktop live in a sandbox quarry cannot read — use `remember` for that content, or give `ingest` a local path.

## Knowledge Capture

As a Claude Code plugin, quarry hooks into the session lifecycle and captures
knowledge automatically, with no action from you:

| Hook | What it captures |
|------|-------------------|
| `SessionStart` | Auto-registers and syncs the current project, so it's searchable from the first prompt |
| `PostToolUse` (WebFetch) | Ingests URLs Claude fetches during research. If the URL was already captured, the hook nudges Claude to `find` it instead of re-fetching |
| `PostToolUse` (WebSearch) | Files a scrubbed digest of search results under `<repo>-captures` |
| `PostToolUse` (Read) | Opt-in (off by default): captures prose files read from outside any registered tree, gated by an in-tree/secret-path/extension/size filter |
| `PreCompact` | Captures the session transcript before context compaction discards it |
| `SessionEnd` | Captures the full session transcript on every close, even a short session that never compacts |
| `SubagentStop` | Archives a subagent's own transcript, separate from the parent session's |

Every hook fails open — a hook failure never blocks Claude Code — and each is
independently toggleable in `.punt-labs/quarry/config.md`.

Captures are scrubbed at write time (secrets, paths, emails, hostnames)
through a single choke point before they ever reach disk. The scrub is
pattern-based and best-effort, not a formal guarantee of catching every
possible secret; a failure in the scrubber itself is fail-closed (the write
is blocked, not written unscrubbed). Deliberate `ingest`/`remember` content
is not scrubbed — that's content you chose to add. See [DES-036 in
DESIGN.md](DESIGN.md).

**Extension: private capture shadow.** An opt-in per-project shadow repo
(`<repo>` → private `<repo>-quarry`) can push the scrubbed captures off the
public repo entirely, for projects where even scrubbed transcripts shouldn't
live in a public history. See [DES-039 in DESIGN.md](DESIGN.md) and
[AGENTS.md](AGENTS.md).

## Managing the Daemon

`quarry install` registers `quarryd` as a per-user service that starts at login and restarts on crash (launchd on macOS, systemd on Linux). Re-running the [Quick Start](#quick-start) installer does this for you on every upgrade — it calls `quarry install` and then force-restarts the service as a belt-and-suspenders step, so a plain `curl | sh` re-run is enough.

**After upgrading the package some other way** (`uv tool install --force`, a local wheel), restart the service yourself — a running daemon holds the old engine in memory until restarted.

macOS:

```bash
launchctl kickstart -k gui/$(id -u)/com.punt-labs.quarry
```

Linux:

```bash
systemctl --user restart quarry
```

`quarry doctor` confirms the daemon is running and ready.

### HTTP API

`quarryd` also exposes a REST API — every CLI/MCP operation is a thin client
over it. The CLI is the primary, documented way to drive quarry; the HTTP API
is there for scripting or a non-Claude integration that wants to talk to the
daemon directly. `quarry install` generates a self-signed CA for the managed
daemon, local or remote, so it's TLS even on loopback:

```bash
curl --cacert ~/.punt-labs/quarry/tls/ca.crt "https://127.0.0.1:8420/v1/search?q=Q3+revenue"
```

Local installs bind loopback-only with no auth required; a `--network`
install additionally requires a Bearer token (`QUARRY_API_KEY`) — see
[ADVANCED-SETUP.md](ADVANCED-SETUP.md#remote-server). The full endpoint list
is generated at [`docs/openapi.json`](docs/openapi.json) (`make openapi`
regenerates it).

## Documentation

[Architecture](docs/architecture.tex) |
[Advanced Setup](ADVANCED-SETUP.md) |
[Design (ADR log)](DESIGN.md) |
[Agents](AGENTS.md) |
[Changelog](CHANGELOG.md)

## Development

Quality gates, architecture notes, and the PR process are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
