# Automagic Knowledge Capture — Quarry as Ambient Memory

**Date:** 2026-02-24
**Status:** Proposal
**Depends on:** [Entire.io Hook Architecture Research](../../punt-kit/research/entire-io-hook-architecture.md)

## Problem

Knowledge evaporates between Claude Code sessions. A developer spends 30 minutes reading API docs, debugging a library quirk, or understanding a codebase pattern — then the session ends and that knowledge is gone. The next session (or the next developer) starts from zero.

Quarry already indexes documents on demand (`/ingest`). The question is: can it index knowledge *passively* from the natural flow of work?

## Observation: Where Knowledge Appears in a Session

| Source | Hook Event | Signal Quality | Volume |
|--------|-----------|---------------|--------|
| URLs Claude fetches | PostToolUse `WebFetch` | High — you fetch docs because you need them | Low (5-20/session) |
| URLs the user pastes | UserPromptSubmit | High — explicit user interest | Very low |
| Session summaries at compaction | PreCompact | High — distilled reasoning | 1-2/session |
| Error resolutions | Stop (after debug sequences) | Medium — noisy | Variable |
| External file reads | PostToolUse `Read` | Low — most are transient | High (noisy) |
| Agent conversation text | Stop | Low — too much noise | Very high |

The high-signal, low-volume sources are: **fetched URLs** and **compaction summaries**. These are the 80/20.

## Proposed Architecture

### Layer 1: URL Auto-Capture (PostToolUse on WebFetch)

When Claude fetches a URL via `WebFetch`, a PostToolUse hook sends it to quarry for background ingestion.

```
User asks about LanceDB API
  → Claude calls WebFetch("https://lancedb.github.io/lancedb/python/...")
  → PostToolUse hook fires
  → Hook extracts URL from tool input
  → Calls `quarry ingest-url <url> --collection web-captures --quiet`
  → Next session: `/find "LanceDB vector search API"` returns the page
```

**Hook implementation:**

```json
{
  "matcher": "WebFetch",
  "hooks": [{
    "type": "command",
    "command": "quarry hooks post-web-fetch"
  }]
}
```

The `quarry hooks post-web-fetch` command:
1. Reads tool input from stdin (JSON with `url` field)
2. Checks if URL is already indexed (dedup by document name)
3. If new, queues for background ingestion into `web-captures` collection
4. Returns immediately (fail-open, non-blocking)

**Why this works well:**
- Zero user effort — knowledge accrues by working normally
- High signal — you only fetch URLs you actually need
- Low volume — won't overwhelm the index
- Dedup is natural — same URL won't be re-ingested
- USP handles sitemaps if a doc site is fetched

**Concerns:**
- Some fetched URLs are throwaway (error pages, redirect chains)
- Need a way to exclude patterns (e.g., `*.json`, API endpoints)
- Collection size grows unbounded — need TTL or manual pruning

### Layer 2: Compaction Knowledge Extraction (PreCompact)

Before context compaction, Claude generates a summary of the session. This summary contains distilled knowledge — architectural decisions, debugging insights, discovered patterns. A PreCompact hook could capture this.

```
Session hits context limit
  → PreCompact hook fires
  → Hook receives the compaction summary text
  → Ingests it as a document: `session-2026-02-24-quarry-sitemap.md`
  → Collection: `session-notes`
  → Next session: `/find "why did we choose USP for sitemaps?"` returns the context
```

**Hook implementation:**

```json
{
  "matcher": "auto",
  "hooks": [{
    "type": "command",
    "command": "quarry hooks pre-compact"
  }]
}
```

**Why this works well:**
- Compaction summaries are high-quality distilled knowledge
- 1-2 per session — very low volume
- Captures the "why" that git commits don't
- Searchable across sessions — "how did we solve X last week?"

**Concerns:**
- PreCompact output format may not include the summary text (needs verification)
- Session summaries may contain sensitive context (API keys in error output, etc.)
- Need to verify what data is available in the hook's stdin

### Layer 3: User-Pasted URLs (UserPromptSubmit)

When a user pastes a URL in their prompt, it signals explicit interest. A UserPromptSubmit hook could detect URLs and queue them for ingestion.

```
User: "Look at https://docs.pydantic.dev/latest/concepts/models/ and tell me..."
  → UserPromptSubmit hook fires
  → Hook extracts URL(s) from prompt text
  → Queues for background ingestion
```

**Why this is lower priority than Layer 1:**
- If Claude fetches the URL, Layer 1 already captures it
- Adds complexity to UserPromptSubmit (already has hooks from other plugins)
- URL extraction from free text is imprecise

### Layer 0: Codebase Indexing (SessionStart)

The highest-leverage capture is the one closest to home: the current repository. If quarry auto-indexes the working codebase, Claude can `/find` relevant files and code patterns instead of doing multiple rounds of grep/glob exploration. This changes the development loop:

```
Session starts
  → SessionStart hook fires
  → Hook checks if current repo is registered with quarry
  → If not registered: register_directory + sync
  → If registered: sync (picks up changes since last session)
  → SessionStart additionalContext tells Claude:
    "This codebase is indexed in quarry. Use /find or search_documents
     to locate relevant code before using Grep/Glob."
```

**Hook implementation:**

```json
{
  "matcher": "",
  "hooks": [{
    "type": "command",
    "command": "quarry hooks session-start"
  }]
}
```

The `quarry hooks session-start` command:
1. Detects current git repo root
2. Checks if it's registered via `list_registrations`
3. If unregistered, calls `register_directory` for the repo root
4. Runs `sync_all_registrations` to pick up new/changed files
5. Outputs `additionalContext` telling Claude that quarry is available for codebase search

**Why this is Layer 0 (ship first):**
- Immediate value — every session benefits from indexed codebase
- Uses existing infrastructure (`register_directory`, `sync`)
- Reduces grep/glob thrashing that wastes context window
- The `/find` command already exists — just needs the index populated

**Concerns:**
- Large repos may take time to sync on first registration
- Need to handle repos already registered by user (don't double-register)
- Sync on every SessionStart adds latency — may need to be async or throttled

## What NOT to Capture

Following Entire.io's pattern of deliberate restraint:

| Source | Why Not |
|--------|---------|
| Every file Read | Too noisy — most reads are transient navigation |
| Conversation text | Too much volume, low signal-to-noise, privacy risk |
| Tool outputs (Bash, Grep) | Ephemeral — relevant to one task, not reusable knowledge |
| Code diffs | Git already captures this better |

## Collection Strategy

| Collection | Source | Retention | Expected Size |
|-----------|--------|-----------|---------------|
| `web-captures` | Auto-ingested URLs from WebFetch | Keep indefinitely, user can prune | 50-200 docs/month |
| `session-notes` | Compaction summaries | Keep indefinitely | 30-60 docs/month |
| (existing) | User's explicit `/ingest` commands | User-managed | Varies |

## Configuration

Users should be able to opt in/out and tune the behavior. Following Entire.io's `settings.json` / `settings.local.json` pattern:

```yaml
# .claude/quarry.local.md frontmatter
---
auto_capture:
  web_fetch: true          # Layer 1: auto-ingest fetched URLs
  compaction: true         # Layer 2: capture compaction summaries
  user_urls: false         # Layer 3: extract URLs from prompts
  exclude_urls:            # URL patterns to skip
    - "*.json"
    - "*/api/v*"
    - "github.com/*/raw/*"
  database: "default"      # which quarry database to use
---
```

## Privacy: Deny Self-Reference

Following Entire.io's Pattern 4, quarry should NOT let Claude search its auto-captured collections during normal work. The auto-captured knowledge is for the *user* to search explicitly via `/find`. If Claude auto-searched its own session notes, it could create feedback loops or context pollution.

This is naturally handled: quarry's `/find` command is user-initiated, not auto-triggered. But if we ever add a "search before answering" hook, we should exclude `session-notes` from auto-search.

## Implementation Sequence

All hooks follow Entire.io's dispatcher pattern: `quarry hooks <event>`. The CLI binary is the single entry point — hook scripts are one-liners, all logic lives in testable Python.

0. **`quarry hooks` subcommand** — CLI dispatcher with subcommands for each event. Thin shell, delegates to internal functions.
1. **Layer 0: `quarry hooks session-start`** — Auto-register and sync current repo. Inject context telling Claude to use `/find`. Highest leverage — every session benefits.
2. **Layer 1: `quarry hooks post-web-fetch`** — Auto-ingest fetched URLs. High signal, low volume.
3. **Configuration** — Add opt-in/out via plugin settings.
4. **Layer 2: `quarry hooks pre-compact`** — Capture compaction summaries. Requires verifying what data PreCompact exposes.
5. **Layer 3** — Only if Layers 0-2 prove insufficient.

## Open Questions

1. **What data does PreCompact expose?** Need to test what's available in hook stdin for PreCompact events. If the summary text isn't accessible, Layer 2 may need a different approach (e.g., SessionEnd with transcript analysis).

2. **Background ingestion queue.** WebFetch hooks must return fast (fail-open). Need a lightweight queue — fire-and-forget subprocess? Write URL to a file and batch-process? The `quarry ingest-url` call takes 2-5 seconds per URL.

3. **Cross-project knowledge.** If someone reads LanceDB docs while working on quarry, should those docs be available when working on langlearn? Currently quarry databases are per-project. A shared `web-captures` database would be more useful.

4. **Overlap with Entire.io.** Entire captures the "why" (prompts, reasoning). Quarry captures the "what" (documents, knowledge). These are complementary — Entire is session provenance, quarry is searchable knowledge. No conflict, but worth being explicit about the boundary.
