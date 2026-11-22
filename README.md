# punt-quarry

> Local semantic search for AI agents and humans.

[![License](https://img.shields.io/github/license/punt-labs/quarry)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/punt-labs/quarry/test.yml?label=CI)](https://github.com/punt-labs/quarry/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Python](https://img.shields.io/pypi/pyversions/punt-quarry)](https://pypi.org/project/punt-quarry/)
[![Working Backwards](https://img.shields.io/badge/Working_Backwards-hypothesis-lightgrey)](./prfaq.pdf)

Quarry indexes documents in 20+ formats, embeds them with a local ONNX model (snowflake-arctic-embed-m-v1.5), stores the vectors in LanceDB, and serves semantic search to Claude Code, Claude Desktop, and the command line. Everything runs locally — no API keys, no cloud accounts. One `quarryd` daemon per machine loads the model once; the CLI, the MCP (Model Context Protocol) server, and the Claude Code hooks are thin clients that send requests to it. Scripts can also call its HTTP API directly.

**Platforms:** macOS (Apple Silicon), Linux

Intel macOS is not supported by any install path: two of quarry's dependencies (`lancedb` and `onnxruntime`) publish no Intel macOS wheel.

## Quick Start

Install the CLI, the daemon, the MCP server, and the Claude Code plugin:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/6a4b2c1/install.sh | sh
```

Restart Claude Code. At each session start, quarry registers your current project and starts indexing it in the background. See [What It Looks Like](#what-it-looks-like) for example searches.

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

Install the formula. It puts the `quarry`, `quarryd`, and `quarry-hook` commands on `PATH`:

```bash
brew install punt-labs/tap/quarry
```

Download the model and set up the TLS certificates and the daemon service:

```bash
quarry install
```

To add the Claude Code plugin too, add the Punt Labs marketplace:

```bash
claude plugin marketplace add punt-labs/claude-plugins
```

Then install the plugin:

```bash
claude plugin install quarry@punt-labs
```

Use one install method per machine. Mixing Homebrew with the `curl | sh` installer puts two copies of `quarry` on `PATH`, and whichever comes first wins. Run `which quarry` (or `command -v quarry`) to see which one that is.

</details>

<details>
<summary>CLI only (skip the Claude Code plugin)</summary>

For other coding agents (Codex, Cursor), a plain terminal, or Claude Code setups where policy blocks plugin installs, `--no-plugin` installs everything except the Claude Code marketplace and plugin:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/6a4b2c1/install.sh | sh -s -- --no-plugin
```

Where a flag cannot be passed (CI templating a bare `curl … | sh`), set `QUARRY_NO_PLUGIN=1` — honored only when exactly `1`:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/6a4b2c1/install.sh | QUARRY_NO_PLUGIN=1 sh
```

Everything else runs unchanged. Use the CLI, or point your tool at the `quarry mcp` server (MCP over standard input/output); both talk to the `quarryd` daemon. Re-run the installer without `--no-plugin` to add the plugin later.

</details>

<details>
<summary>Verify before running</summary>

Download the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/punt-labs/quarry/6a4b2c1/install.sh -o install.sh
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
- **One daemon, thin clients** — a single `quarryd` process loads the embedding model once and serves the CLI, the MCP server, and the Claude Code hooks over a versioned REST API. It caps its embedding and database compute threads at two each to limit CPU load.
- **Automatic knowledge capture** — `quarry enable` sets up per-project file sync, web-fetch and session-transcript capture, and per-agent memory. Secrets and personal information are scrubbed from captures before they are stored, and captures are kept separate from the code index. See [Knowledge Capture](#knowledge-capture).
- **Named databases** — separate databases, each with its own index and directory registrations, for example to keep work and personal documents apart. `quarry use <name>` sets the default database and `--db <name>` picks one for a single command. Each daemon serves one database, and the installed service serves the default; see [Managing the Daemon](#managing-the-daemon) to run another.
- **Remote server** — run the daemon on a GPU host and connect from any Mac or Linux client over TLS. See [ADVANCED-SETUP.md](ADVANCED-SETUP.md#remote-server).

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
| `/ingest <source>` | Ingest a URL, or register and sync a local file or directory |
| `/remember <name>` | Ingest inline text under a document name |
| `/learn <lesson> [as <name>]` | Save a short lesson (up to 500 characters) that ranks higher in search results |
| `/find <query>` | Semantic search; questions get answers with citations, keywords get raw results |
| `/explain <topic>` | Search and write an explanation of a document or topic |
| `/source <claim>` | Find which document a claim comes from |
| `/quarry [sub]` | Manage: `status` (default), `sync`, `collections`, `databases`, `registrations`, `use <name>`, `enable`, `disable`, `missions sync` |
| `/help` | List quarry's slash commands |

### MCP Tools

| Tool | Purpose |
|------|---------|
| `find` | Semantic search with filters |
| `show` | Document metadata or page text |
| `list` | Documents, collections, databases, registrations |
| `status` | Database statistics |
| `insights` | Local search statistics: query volume, latency, empty-result rate, and result breakdowns |
| `ingest` / `remember` | Index a URL, or inline text |
| `learn` | Save a short lesson that ranks higher in search results |
| `register_directory` / `deregister_directory` | Manage a synced directory |
| `sync_all_registrations` | Re-index all registered directories |
| `delete` | Remove a document or collection |
| `use` | Switch the active database |
| `missions_sync` | Save each completed ethos mission round to the worker agent's memory |

### CLI

| Command | What it does |
|---------|-------------|
| `quarry find "<query>"` | Hybrid search (meaning plus keywords) |
| `quarry ingest <url>` | Index a webpage (for local files and directories, use `quarry register`) |
| `quarry remember --name <name>` | Index text read from standard input |
| `quarry learn "<lesson>"` | Save a short lesson (up to 500 characters) that ranks higher in search results |
| `quarry show <document>` | Show a document's metadata, or one page's text with `--page` |
| `quarry list documents` | List indexed documents (also `collections`, `databases`, `registrations`) |
| `quarry delete <name>` | Delete a document, or a collection with `--type collection` |
| `quarry register <dir>` | Watch a directory and keep its index current |
| `quarry deregister <collection>` | Stop watching a directory and remove its data (`--keep-data` keeps it) |
| `quarry sync` | Re-index registered directories |
| `quarry enable` / `quarry disable` | Turn project indexing and capture on or off for a repo |
| `quarry missions sync` | Save each completed ethos mission round to `memory-<worker>` (`--mission`, `--dry-run`, `--force`) |
| `quarry use <name>` | Set the default database |
| `quarry status` | Show document and chunk counts, storage size, and model |
| `quarry insights` | Local search statistics: query volume, latency, empty-result rate, and result breakdowns |
| `quarry optimize` | Compact the database and rebuild its indexes |
| `quarry doctor` | Health check |
| `quarry install` | Set up the daemon service, TLS certificates, and MCP config |
| `quarry uninstall` | Remove the daemon service (its launchd or systemd unit) |
| `quarry login <host> --api-key <token>` | Connect to a remote server; the server's certificate is pinned on first connection |
| `quarry logout` | Disconnect and go back to the local daemon |
| `quarry version` | Print the installed version |

Run `quarry --help` for the full list, including `captures`, `skills`,
`backfill-sessions`, `remote`, and `mcp`, and `quarry <command> --help` for
each command's options.

[ethos](https://github.com/punt-labs/ethos) is the Punt Labs tool that gives
agents named identities and runs delegated tasks ("missions"); the
`missions sync` commands only matter if you use it.

A registered directory is indexed automatically: quarry watches it for changes
and re-indexes as you work (honoring `.gitignore` and a root-level
`.quarryignore`, so directories such as `node_modules` or `.venv` are skipped),
with a periodic sweep that catches anything the live watch misses. You rarely
need `quarry sync`. `quarry list registrations` shows each directory's watch
state (`watched`, `degraded`, or `scan-only`); a `scan-only` directory still
stays current through the sweep.

To set up a repo, run `quarry enable` in it. No other setup is needed.
`quarry enable` also lets Claude call quarry without asking each time: it adds
quarry's allow rules to that project's `.claude/settings.json` (admin and
destructive commands still prompt), and `quarry disable` removes them.
Driving quarry from an agent — memory tagging and agent handles — is covered in
[AGENTS.md](AGENTS.md).

> **Upgrading from an older version?** Earlier quarry wrote the same allow rules
> to your user-global `~/.claude/settings.json` at session start; `quarry
> disable` does not remove those. Delete them by hand if you want quarry to
> prompt again everywhere.

## Setup

Quarry works with zero configuration. For environment variables and running
the daemon on a remote or GPU host, see [ADVANCED-SETUP.md](ADVANCED-SETUP.md).

## Claude Desktop

The `.mcpb` bundle connects Claude Desktop to the same local index that the CLI and Claude Code use. It contains no search engine of its own: it registers the `quarry mcp` client, which talks to the same `quarryd` daemon. Install quarry first; the bundle does not work on its own.

`quarry install` configures Claude Desktop automatically. To add it by hand instead, [download `punt-quarry.mcpb`](https://github.com/punt-labs/quarry/releases/latest/download/punt-quarry.mcpb) and double-click it.

Uploaded files in Claude Desktop live in a sandbox quarry cannot read — use `remember` for that content, or give `ingest` a local path.

## Knowledge Capture

The Claude Code plugin runs hooks (commands Claude Code runs on session events)
that capture knowledge automatically:

| Hook | What it captures |
|------|-------------------|
| `SessionStart` | Registers the current project and starts indexing it in the background |
| `PostToolUse` (WebFetch) | Ingests URLs Claude fetches during research. If the URL was already captured, the hook tells Claude to use `find` instead of fetching it again |
| `PostToolUse` (WebSearch) | Files a scrubbed digest of search results under `<repo>-captures` |
| `PostToolUse` (Read) | Opt-in (off by default): captures prose files Claude reads from outside any registered directory, skipping likely secret files, non-prose file types, and large files |
| `PreCompact` | Captures the session transcript before context compaction discards it |
| `SessionEnd` | Captures the full session transcript on every close, even a short session that never compacts |
| `SubagentStop` | Captures a subagent's transcript separately from the parent session's. If the subagent has an ethos identity, its final report is also saved to that agent's memory (`memory-<handle>`) |

If a hook fails, Claude Code continues normally. Each hook is
independently toggleable in `.punt-labs/quarry/config.md`.

Captures are scrubbed (secrets, paths, emails, hostnames) before they are
written to disk. The scrub matches known patterns and may miss some secrets.
If the scrubber itself fails, the capture is not written. Content you add
yourself with `ingest` or `remember` is not scrubbed. See [DES-036 in
DESIGN.md](DESIGN.md).

**Private capture repo.** You can opt in to a private companion repo per
project (`<repo>` → private `<repo>-quarry`) so that scrubbed captures are
pushed there instead of living in the public repo's history. Set it up with
`quarry captures init`. See [DES-039 in DESIGN.md](DESIGN.md) and
[AGENTS.md](AGENTS.md).

**Search telemetry.** The daemon uses punt-observe to record typed search
statistics to the private local file `~/.punt-labs/quarry/telemetry.db`.
Search text, result content, and filters are not stored. Insights groups
empty queries, agents, and collections by per-install salted hashes and shows
aggregate latency, counts, and hit-age bands. The shared store retains 90 days
across all Quarry databases; the old per-database retention setting no longer
applies. Set `PUNT_TELEMETRY=off`, `QUARRY_TELEMETRY=off`, or
`TELEMETRY_ENABLED=false` in the daemon's environment and restart it to disable
capture. Quarry does not opt in to network export.

Upgrading starts a fresh recall history. Quarry never reads or migrates
the old per-database `telemetry.db` under the data root. Old files may contain
scrubbed query text and are not automatically deleted. To discard them, stop
any older Quarry daemons, then delete only the old per-database
`data/<db>/telemetry.db` and its `-wal` and `-shm` sidecars for each affected
database. If you set a custom data root, look there instead. The new
installation-wide `~/.punt-labs/quarry/telemetry.db` is separate.

## Managing the Daemon

The CLI, MCP server, hooks, daemon, and detached sync dispatcher write diagnostics
to `~/.punt-labs/quarry/logs/quarry.log`. The shared sink redacts and rotates that
private file. `QUARRY_LOG_LEVEL` adjusts terminal verbosity, not the file threshold.
If the daemon cannot open its private log and you are using launchd or
Homebrew services, stop the service and run `quarryd` in a terminal to see
the startup diagnostic.

`quarry install` registers `quarryd` as a per-user service that starts at login and restarts if it crashes (launchd on macOS, systemd on Linux).

To upgrade, re-run the [Quick Start](#quick-start) installer. It installs the tested dependency versions, runs `quarry install`, restarts the daemon, and fails if the new daemon does not start and report ready. [ADVANCED-SETUP.md](ADVANCED-SETUP.md#upgrades) lists the steps.

**After upgrading some other way** (for example `uv tool install --force`), restart the service yourself. A running daemon keeps using the old version until it restarts.

macOS:

```bash
launchctl kickstart -k gui/$(id -u)/com.punt-labs.quarry
```

Linux:

```bash
systemctl --user restart quarry
```

`quarry doctor` confirms the daemon is running and ready.

To use a second named database while the service runs, start another daemon
for it in the foreground. `--port 0` picks a free port, and `--tls` matches
the installed service:

```bash
quarryd --db work --port 0 --tls
```

Then pass the same name to each command, for example `quarry --db work find "<query>"`.

`quarryd` also serves a REST API for scripts and other integrations. See [ADVANCED-SETUP.md](ADVANCED-SETUP.md#http-api).

## Documentation

[Architecture (PDF)](docs/architecture.pdf) |
[Advanced Setup](ADVANCED-SETUP.md) |
[Design (ADR log)](DESIGN.md) |
[Agents](AGENTS.md) |
[Changelog](CHANGELOG.md) |
[Contributing](CONTRIBUTING.md)

## License

MIT
