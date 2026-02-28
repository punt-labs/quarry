# Quarry Vision: Ambient Knowledge for Claude Code

**Date:** 2026-02-28
**Status:** Active
**Builds on:** [Automagic Knowledge Capture](automagic-knowledge-capture.md), [Entire.io Hook Architecture](../../punt-kit/research/entire-io-hook-architecture.md)

## The Distinction

Quarry has four projections of the same core capability — library, CLI, MCP server, HTTP API. Each serves a different caller: Python code imports the library, humans type the CLI, AI agents call the MCP tools, the menu bar app hits the HTTP API. These projections are interfaces. They expose the capability to different contexts.

The Claude Code plugin is not a fifth projection. It is a different category of integration. The four projections let callers *reach* quarry. The plugin makes Claude Code *behave differently because quarry is present*. The host becomes knowledge-aware.

When quarry is installed as a standalone MCP server, Claude Code gains tools — `search_documents`, `ingest_file`, etc. The user still drives: `/find`, `/ingest`, explicit commands. When quarry is installed as a plugin, Claude Code gains *ambient memory* — it learns from every session, recalls relevant knowledge before you ask, and builds a persistent knowledge base from the natural flow of work.

The analogy: tts as a tool speaks when you say `/say`. Tts as a plugin listens to every tool call, accumulates session signals, detects when a task completes, and speaks a summary without being asked. Biff as a tool sends messages when you say `/write`. Biff as a plugin detects branch changes, tracks commits, warns about collisions, and nudges announcements — all without being asked.

Quarry follows the same pattern. The tool indexes and searches. The plugin learns and recalls.

## Two Loops

### The Learning Loop (Knowledge In)

Knowledge flows through every Claude Code session and evaporates:

- **Web research** — Claude fetches documentation, Stack Overflow answers, API references. Read once, gone after compaction.
- **Document reads** — User opens a PDF, Claude analyzes it, context compressed away.
- **Research agents** — Explore subagents return findings, then the summary is compressed.
- **Session reasoning** — Claude connects dots, explains trade-offs, reaches conclusions. The synthesis exists only in the conversation window.
- **Debugging discoveries** — Root causes found, workarounds identified. Locked in one session's memory.

The learning loop captures this knowledge passively. Hooks detect knowledge-generating events, write to a staging queue, and quarry's CLI processes the queue asynchronously. The user works normally; the knowledge base grows.

### The Recall Loop (Knowledge Out)

The learning loop is necessary but not sufficient. If quarry has the knowledge but Claude doesn't know to look there, the knowledge is useless. Today, Claude Code has two research reflexes: Grep/Glob for local code, WebSearch/WebFetch for external knowledge. Quarry is a third option that Claude only reaches for when explicitly told (`/find`).

The recall loop changes this. Through hooks and session context, quarry tells Claude Code: "You have a local knowledge base. Before searching the web for a topic you've researched before, check locally first."

This is not about replacing web search. It is about eliminating redundant research — the second time you look something up should be instant.

## Hook Architecture

### Learning Hooks

Following the [Entire.io dispatcher pattern](../../punt-kit/research/entire-io-hook-architecture.md#pattern-5-cli-as-hook-dispatcher): all hooks call `quarry hooks <event>`. The CLI binary is the single entry point. Hook scripts are one-liners. All logic lives in testable Python.

Following the [tts config-driven pattern](../tts): behavior controlled by `.quarry/config.md`. Hooks gate on config state — fast skip when disabled.

| Hook Event | Matcher | What It Captures | Config Gate |
|------------|---------|------------------|-------------|
| PostToolUse | `WebFetch` | URLs Claude fetched — queue page for ingestion | `learn: on` |
| PostToolUse | `WebSearch` | Search result URLs — queue top results | `learn: on` |
| PostToolUse | `Read` | Non-code documents (PDF, PPTX, images) | `learn: all` |
| PreCompact | `auto` | Conversation transcript before compression | `learn: on` |
| SubagentStop | `Explore`, `general-purpose` | Research agent findings | `learn: all` |
| SessionEnd | — | Final session knowledge digest | `learn: all` |

**Design constraints:**

- **Fail-open.** Every learning hook returns 0 on failure. Quarry crashing never breaks Claude Code.
- **Non-blocking.** Hooks write to `.quarry/staging/` and return immediately. Ingestion is async.
- **Selective.** `learn: off` disables all passive capture. `learn: on` enables the high-signal, low-volume sources (web research + compaction). `learn: all` adds lower-signal sources (document reads, agent findings, session digests).

### Recall Hooks

| Hook Event | Matcher | What It Does |
|------------|---------|-------------|
| SessionStart | `startup` | Injects knowledge briefing into `additionalContext`: database stats, collection topics, recency. Tells Claude that local knowledge exists. |
| PreToolUse | `WebSearch` | Before Claude searches the web, checks if the query overlaps with indexed collections. If yes, injects `additionalContext` suggesting a local search first. Does not block — Claude decides. |

**The SessionStart briefing** is lightweight — it reads `quarry status` output and formats a one-paragraph summary. "You have 347 documents across 5 collections (web-captures, session-notes, docs, research, codebase). Last synced 2 hours ago. Use /find to search locally before web searching."

**The PreToolUse nudge** is the more interesting hook. It intercepts WebSearch calls and checks the query against known collection names and document titles — a fast keyword match, not a full embedding search. If there's overlap, it injects context: "Quarry has 12 documents about LanceDB indexed locally. Consider /find first." Claude can still search the web (the hook does not block), but it knows to check.

This mirrors how an experienced developer works: before Googling something, they check their notes first.

## Staging Queue

All learning hooks write to `.quarry/staging/`, never ingest directly:

```text
.quarry/staging/
  urls                  # One URL per line, appended by PostToolUse hooks
  files                 # One absolute path per line, appended by Read hook
  content/              # Markdown files from PreCompact, SubagentStop, SessionEnd
    2026-02-28T14:32-compact.md
    2026-02-28T15:10-agent-explore.md
```

Processing the queue is a CLI concern:

- `quarry learn` — Process the staging queue: ingest URLs, ingest files, ingest content. Dedup against existing documents. Report what was learned.
- `quarry sync` — Could incorporate queue processing as part of the sync cycle.
- Background: The `quarry learn` command runs via Bash `run_in_background` — the conversation is never blocked.

This is Option A from the original brainstorm: hooks are thin writers, the CLI does the heavy lifting async.

## Config

Following the tts pattern (`.tts/config.md` with YAML frontmatter):

```yaml
# .quarry/config.md
---
learn: "on"              # off | on | all
---
```

**`/quarry learn` command** (parallels `/notify y|c|n`):

```text
/quarry learn off   — No passive capture (current behavior)
/quarry learn on    — Capture web research + compaction transcripts
/quarry learn all   — Also capture document reads, agent findings, session digests
/quarry learn       — Show current setting
```

The recall hooks (SessionStart briefing, PreToolUse nudge) are always active when quarry is installed as a plugin. They are read-only and fast — there is no reason to disable them.

## Signal Accumulation

Parallel to tts's `vibe_signals`, quarry accumulates `learn_signals`:

```yaml
learn_signals: "web-fetch@14:32:lancedb.github.io,web-fetch@14:35:stackoverflow.com,read-pdf@14:40:report.pdf"
```

PostToolUse hooks append signals. At PreCompact or SessionEnd, the hook reads signals to determine what knowledge was generated. If signals are empty, no digest is written — nothing was learned.

This prevents empty session notes from accumulating ("In this session, the user asked about X and I responded" — useless filler).

## What This Unlocks

**Week 1:** Developer enables `learn on`. Works normally. Web searches auto-captured. Compaction transcripts saved.

**Week 4:** 200 web pages indexed, 30 session notes captured.

- `/find "NATS authentication patterns"` — returns Stack Overflow answers from a biff debugging session 3 weeks ago, plus the session note recording the root cause.
- Claude starts a web search for "LanceDB vector index tuning" — PreToolUse hook fires: "Quarry has 8 documents about LanceDB indexed locally." Claude runs `/find` first, gets the answer from cached docs, skips the web search.
- New team member joins, inherits the quarry database. Searches "why did we choose snowflake-arctic-embed" — gets the session note from the architecture discussion.

**Month 3:** Quarry is the team's institutional memory. Not because anyone decided to build a knowledge base — because the knowledge base built itself from the natural flow of work.

## Relationship to Existing Components

| Component | Role | Changes Needed |
|-----------|------|----------------|
| `quarry hooks` CLI subcommand | Dispatcher for all hook events | New — Python module with subcommands per event |
| `.quarry/config.md` | Per-project config (YAML frontmatter) | New — parallels `.tts/config.md` |
| `.quarry/staging/` | Queue for async ingestion | New — simple file-based queue |
| `quarry learn` CLI command | Process staging queue | New — dedup, ingest, report |
| `hooks/hooks.json` | Plugin hook registration | Expand — add learning + recall hooks |
| `hooks/session-start.sh` | Plugin initialization | Expand — add knowledge briefing |
| `commands/quarry.md` | `/quarry` command | Expand — add `learn` subcommand |
| `quarry ingest-url`, `ingest-file`, `ingest-content` | Existing ingestion | No changes — staging queue feeds these |
| `quarry search` | Existing search | No changes — recall hooks feed queries here |

## Implementation Sequence

1. **Config layer** — `.quarry/config.md`, `/quarry learn` command, `set_config` MCP tool addition.
2. **Recall hooks** — SessionStart briefing, PreToolUse WebSearch nudge. Immediate value with zero learning hooks — just makes Claude aware of existing knowledge.
3. **Learning hooks: web capture** — PostToolUse on WebFetch/WebSearch. Staging queue + `quarry learn` CLI command.
4. **Learning hooks: compaction** — PreCompact transcript capture to staging.
5. **Signal accumulation** — `learn_signals` in config, gating for PreCompact/SessionEnd digest.
6. **Learning hooks: extended** — Read (documents), SubagentStop, SessionEnd. Behind `learn: all`.

## Storage: Shadow Branch for Team Sharing

The staging queue (`.quarry/staging/`) is local and gitignored — fine for one person, invisible to teammates. For small-team sharing, captured content could live on a git shadow branch instead.

Entire.io uses this pattern: session metadata lives on `entire/checkpoints/v1`, pushed to remote via a `pre-push` git hook. The code branch stays clean. The provenance travels with the repo on a branch you never check out.

Quarry could do the same:

```text
quarry/knowledge/v1          # shadow branch, never checked out
  web-captures/
    2026-02-28-lancedb-api.md
    2026-02-28-nats-auth.md
  session-notes/
    2026-02-28T14:32-quarry-hook-fix.md   # tagged with commit hashes
    2026-02-28T16:10-biff-transport.md
```

The flow:

1. Learning hooks write raw content (markdown, URLs) to the shadow branch via `git worktree` or direct tree manipulation
2. A `pre-push` git hook pushes the shadow branch alongside the code branch (fail-open, like Entire.io)
3. Teammates pull — their quarry instances detect new content on the shadow branch
4. Each person's quarry vectorizes locally using their own embedding model
5. `quarry search` returns results from teammates' captured knowledge

This works because:

- **Git handles sync.** No server, no accounts, no platform.
- **Raw content is git-friendly.** Markdown files diff and merge cleanly.
- **Vectorization is local.** No shared embedding infrastructure. Each machine builds its own LanceDB index from the raw content.
- **The working tree stays clean.** Shadow branch content never appears in `git status` or clutters the project.
- **Provenance is natural.** Session notes tagged with commit hashes — `quarry search "why did we change the auth flow"` returns the session where those commits were made.

This would not scale to large teams (hundreds of session notes per week, merge traffic on the shadow branch) but works well for 2-5 people. The simplest possible collaboration model.

## Force Multiplier for Design Tools

Quarry's ambient knowledge isn't just for code sessions. Punt Labs' own products generate high-value design artifacts that are currently locked inside single conversations:

**PR/FAQ (`prfaq`).** The Working Backwards process produces press releases, FAQs, risk assessments, competitive analysis, and customer evidence — all in LaTeX. A `/prfaq:meeting` generates multi-persona debate transcripts with specific critiques and verdicts. Today, these artifacts live in one `.tex` file and the conversation that produced them evaporates. With quarry learning enabled, the meeting transcript, the researcher's evidence gathering, and the iterative feedback rounds all become searchable. Six months later: `quarry search "what was the value risk for quarry"` returns the exact meeting debate.

**Z Specifications (`z-spec`).** Formal specifications capture design invariants, state schemas, and operation contracts — the most precise expression of intent in the entire stack. The `/z:elaborate` skill produces narrative explanations of why each constraint exists. The `/z:partition` skill derives test cases from the spec. These are design decisions in their purest form. If quarry captures the conversations where specs are debated, refined, and validated, you get a searchable history of *why* invariants exist — not just what they are.

**The pattern.** Any tool that produces design artifacts (PRDs, specifications, architecture decisions, risk assessments) generates knowledge that's worth more over time than in the moment. The PR/FAQ you wrote six months ago is valuable when scoping the next product. The Z spec constraints you debated inform the next system's design. Quarry turns ephemeral design conversations into persistent, searchable institutional knowledge.

This also means quarry's recall hooks should be aware of these tools. When a user starts a `/prfaq` session, the SessionStart briefing should include: "You have 3 previous PR/FAQ documents indexed. Previous competitive analysis and customer evidence are searchable." The design tools become cumulative rather than starting from zero each time.

## Open Questions

1. **PreCompact data availability.** What exactly is available in the PreCompact hook's stdin? The conversation transcript? A summary? We need the raw transcript — quarry can chunk and embed it directly. If only a summary is available, that works too but with less granularity.

2. **Cross-project knowledge.** Web captures from working on quarry should be available when working on biff. A shared `web-captures` database (not per-project) would be more useful. The `--db` flag already supports this — the config could specify a shared database for auto-captured content.

3. **PreToolUse nudge precision.** The keyword-match approach for the WebSearch nudge is fast but imprecise. A better approach might be to maintain a lightweight topic index (collection names + top-N terms per collection) that the hook checks against. This needs to stay under 50ms.

4. **Dedup strategy.** The staging queue will contain duplicate URLs (same page fetched across sessions). `quarry learn` needs efficient dedup — check document name existence before ingesting. The existing `--overwrite` flag handles re-ingestion, but skipping entirely is faster.

5. **Shadow branch mechanics.** Writing to a shadow branch from a hook needs to be fast and non-blocking. Options: (a) `git worktree` for the shadow branch, write files, commit — clean but heavyweight; (b) direct `git hash-object` / `git update-index` / `git commit-tree` — fast plumbing commands, no worktree needed; (c) write to staging first, batch-commit to shadow branch during `quarry learn`. Option (c) is simplest and keeps hooks thin.

## References

- [Entire.io Hook Architecture Research](../../punt-kit/research/entire-io-hook-architecture.md) — Dual-layer capture, fail-open/fail-closed, CLI dispatcher pattern
- [Choosing the Right Projection](../../public-website/src/content/blog/choosing-the-right-projection.md) — Tool projections vs. host integration
- [tts plugin](../../tts/) — Config-driven hooks, signal accumulation, `/notify` command pattern
- [biff plugin](../../biff/) — Ambient coordination, session awareness, git hook integration
