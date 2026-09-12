# Why agents don't autonomously use quarry

**Date:** 2026-08-30
**Investigator:** claude (Explore subagent)
**Trigger:** Operator report — "I personally find it useful to use quarry but I literally have to tell my agent to do so."
**Status:** Diagnosis. No code changed.

## Executive summary

Quarry indexes ten repositories and captures WebFetch bodies, but AI agents in
those repositories do not reach for it unless the user names it. The cause is
not missing capability — the search, the auto-index, the WebFetch capture, and
the PreCompact transcript capture all work — it is missing *instruction*.

Models select tools by matching a situation to a tool description. Quarry's
always-loaded descriptions (MCP `instructions`, tool docstrings, SessionStart
context) answer "what is quarry" and never "when do I use it." The one file
that states the rule correctly — "before WebSearch/WebFetch, `find` first;
grep for symbols, quarry for why/how/decisions" — is the vendored guide
`.punt-labs/quarry/CLAUDE.md`, loaded only in repos that ran `quarry enable`.
Of ten registered repos, only quarry itself is enabled. The globally-loaded
copy in workspace + `~/.claude/CLAUDE.md` is the same guide with the two
proactive bullets stripped. Widest reach, weakest instruction.

Compounding the retrieval gap, the capture corpus is thin enough to punish
searching: `quarry-captures` holds one transcript from May 12, while
`~/.claude/projects/*quarry*/` holds seven live ones. An agent that tries
`/find` for prior-session knowledge and gets nothing learns not to try again.

## Root causes, ranked

1. **No trigger conditions anywhere in the always-loaded channels.** Tool
   descriptions, MCP instructions, and SessionStart context describe *what
   quarry is* and never *when to reach for it*.
2. **The one correct instruction is gated behind enablement, and enablement
   is at 10%.** `guidance.py:31-36` reaches only enabled repos. Nine of ten
   registered repos are indexed-but-mute.
3. **The globally-loaded copy is the wrong copy.** The
   `<!-- quarry:capabilities -->` block in workspace + `~/.claude/CLAUDE.md`
   is the vendored guide *minus the two proactive bullets*. No code writes
   it, so it can't drift back into sync.
4. **No per-turn reinforcement.** SessionStart context is stated once and
   decays. No UserPromptSubmit hook. No dynamic tool-description mutation
   (biff does this — its `talk`/`read_messages` descriptions gain
   `[TALK]`/`(N unread)` markers per turn). Quarry's descriptions are static
   for the whole session.
5. **The `researcher` agent — the strongest quarry-first advocate — cannot
   call quarry.** `plugin/agents/researcher.md:4` grants
   `Read, Glob, Grep, WebSearch, WebFetch` and no quarry tool. The entire
   body of the agent's prompt is unexecutable.
6. **The write side is one-directional; nothing closes the loop.**
   WebFetch captures pages and returns `{}`. PreCompact says
   "Search with /find" at the one moment the agent cannot act on it. No
   signal at read time ("you already have this indexed") that would convert
   capture into retrieval habit.
7. **The capture corpus is too thin to reward searching.** One transcript
   in `quarry-captures`, two stranded in `default-captures`, seven live
   transcripts in `~/.claude/projects/` never backfilled.
8. **Scarce instruction budget spent on formatting.** Half the MCP
   `instructions` string and an entire PostToolUse hook enforce "emit
   verbatim, no markdown tables." Correct for UX, but it consumes the one
   guaranteed-delivery slot that could carry a trigger condition — and
   `suppress-output.sh` shrinks successful `find` results to a single line,
   minimizing the reinforcement a good result would otherwise give.
9. **`remember`'s description discourages its main use.** "Use this instead
   of ingest when you have the text content directly (e.g., clipboard, API
   response, or sandbox-uploaded files in Claude Desktop)" frames the tool
   as a Claude Desktop upload path, not "persist a durable finding." An
   agent that just learned something non-obvious has no cue that `remember`
   is for that.

## Per-surface findings

### 1. Claude Code plugin (`plugin/`)

- `plugin/.claude-plugin/plugin.json` — one stdio MCP server. No `skills` key.
- `plugin/commands/` — 12 files, all user-invoked slash commands.
- `plugin/hooks/hooks.json` — SessionStart ×2, PostToolUse (WebFetch + output
  suppression), PreCompact.
- `plugin/agents/researcher.md` — one subagent, tool grant is broken (§5).
- **No `plugin/CLAUDE.md`. No `plugin/skills/`.**

The plugin ships zero always-loaded agent-facing context beyond MCP tool
descriptions and hook `additionalContext`. Every high-adoption plugin in
this workspace (dataviz, claude-api, update-config) ships a skill with
trigger phrases in its `description`. Quarry has none.

The MCP server `instructions` (`src/quarry/mcp_server.py:79-86`) are
approximately half formatting policy: "All quarry tool output is
pre-formatted plain text… never reformat, never convert to markdown tables."
The one slot guaranteed to reach every agent every session is spent on
rendering etiquette.

### 2. SessionStart hook

Two hooks fire, in order:

- `plugin/hooks/session-start.sh` → `handle_session_setup`
  (`src/quarry/_stdlib.py:354-406`). Install plumbing; deploys commands
  and settings rules. First-run `additionalContext` reads "Quarry plugin
  first-run setup complete." — operational noise, not instruction.
- `plugin/hooks/session-sync.sh` → `handle_session_start`
  (`src/quarry/hooks.py:201-315`). Emits the message:

  > Quarry semantic search is active for this project.
  > Collection: "quarry" (…)
  > Captures: "quarry-captures"
  > Background sync in progress.
  > Use the quarry MCP tools (find, show, ingest, remember) to search this codebase semantically.
  > Slash commands: /find, /ingest, /remember, /explain, /source, /quarry.
  > For deep research across local docs and the web, use the researcher agent.

Four of seven lines are state reporting. The three "instruction" lines are
inventory — here are the tools, here are the commands, here's an agent. No
conditional ("when you need X"), no ordering ("before Y"), no comparative
("prefer this over Grep for Z"). An agent reads this the way it reads a
`git status` banner.

`tests/test_hooks.py:524` is named `test_context_includes_recall_hint` but
its assertion only checks `ctx.startswith("Quarry semantic search is
active")`. The recall hint the test name promises does not exist in either
the string or the assertion.

### 3. Auto-index at session start

Works, quietly. `handle_session_start` (`hooks.py:217-296`) reads `cwd`,
finds or auto-registers a covering collection (two fail-closed refusals for
safety), and fires a detached background sync (`_sync_in_background`, PID
lock at `~/.punt-labs/quarry/sync.pid`).

Live state confirms success: 10 registrations, 19 collections, 6,644
documents, 107,362 chunks. Quarry repo is indexed at 535 docs / 5,995
chunks.

The agent sees one of three canned strings: "Background sync in progress." /
"Background sync already running." / "Background sync failed to launch." No
document count, no coverage summary, no "this repo has N indexed documents
and M past sessions." The agent has no signal about *whether quarry knows
anything useful about this repo* — the exact fact that would make a `find`
call worth the tokens.

A stale PID at `~/.punt-labs/quarry/sync.pid` (2370231, no live process)
means the "already running" branch can also fire spuriously. Harmless, but
it's another silent path.

### 4. PostToolUse hooks

**WebFetch auto-ingest works.** Matcher `WebFetch` in `hooks.json:31-39` →
`handle_post_web_fetch` (`hooks.py:318-374`). Reuses the already-fetched
body, redacts userinfo/query/fragment from the stored name, POSTs to the
daemon; SSRF-gated re-fetch on the daemon side is the fallback.
`quarry-captures` holds `docs.python.org/3/library/asyncio.html` and
`pathlib.html`; `default-captures` holds four more.

**There is no Read auto-capture.** No PostToolUse matcher for `Read`, no
`post-read` handler in `_HANDLERS` (`src/quarry/_hook_entry.py:64-69`).
Bead **afg** lists `post-read` and `post-web-search` as explicitly not
committed.

**The gap that matters is write-only capture.** The WebFetch hook returns
`{}`. Nothing on the *next* fetch says "you already have this indexed."
The loop that would produce compounding value (fetch → index → next time,
`find` first) is only half-built.

**The second PostToolUse matcher shrinks quarry's presence.** The
`mcp__(plugin_quarry(-dev)?_)?quarry(-proxy)?__.*` matcher routes tool
output through `suppress-output.sh`, replacing the tool panel with the
first line and moving the body to `additionalContext`. Rational for UI
density, but it means a successful `find` leaves almost no visual trace to
reinforce the behavior.

### 5. Transcript capture (PreCompact)

Wired and works. `hooks.json:41-51` → `handle_pre_compact`
(`hooks.py:564-643`). Archives raw JSONL to
`~/.punt-labs/quarry/sessions/`, extracts text with an artifacts header,
reads `agent_handle` from ethos, writes a scrubbed `.md` to
`<repo>/.punt-labs/quarry/captures/`, POSTs to the daemon with a 5s cap.

Findable via `/find` — verified:
`quarry find "pre-compact transcript capture session" --collection quarry-captures`
returns `session-795c803f-20260512T025950` (78 pages, 362 chunks).

**But the corpus is nearly empty.** `quarry-captures` holds exactly one
session transcript, from May 12 — over three months stale.
`default-captures` holds two more that landed in the fallback collection
instead of the repo's. Only three files in `~/.punt-labs/quarry/sessions/`,
while `~/.claude/projects/*quarry*/` holds seven live transcripts.

This is the self-defeating loop: the agent doesn't search transcripts
because there's almost nothing there; there's almost nothing there partly
because compaction is rare in short sessions and there is no `SessionEnd`
capture. Bead **kl1y** (backfill-sessions) exists precisely to fill this.

The one place quarry speaks imperatively is the PreCompact `systemMessage`
(`:639-642`): "Capturing this session's conversation (background). Search
with /find or show to retrieve it." Fired at the single moment the agent
is least able to act on it.

### 6. Per-repo CLAUDE.md

Three different quarry blocks exist in three files and they say different
things.

**(a) The vendored guide** — `src/quarry/guidance.py:25-46`, deposited to
`<repo>/.punt-labs/quarry/CLAUDE.md`, imported via
`@.punt-labs/quarry/CLAUDE.md` appended by `ClaudeMdImport.register`.
Exact wording of the two proactive bullets:

> - Before using WebSearch or WebFetch for research, run `/find` with the
>   query first. Quarry indexes this codebase, design docs, prior session
>   transcripts, and web pages from previous research. If quarry returns
>   relevant results, use them — do not re-research what has already been
>   found.
> - Use grep for symbol lookups and value lookups; use quarry for "why",
>   "how", and "what did we decide about X" questions.

**This is exactly the right instruction. It is also the only copy of it
anywhere.**

**(b) The repo `CLAUDE.md` fenced block** — `CLAUDE.md:287-308`, between
`<!-- quarry:begin -->` / `<!-- quarry:end -->`. This is a duplicate of
(a), immediately followed at line 309 by the `@`-import of (a). The agent
gets the same twenty lines twice. No code in `src/` writes this fence —
grepped. It appears to be a legacy artifact from the pre-`@`-import model.

**(c) The workspace and global blocks** —
`~/Coding/punt-labs/CLAUDE.md:424-439` and
`~/.claude/CLAUDE.md:506-521`, fenced `<!-- quarry:capabilities -->`. These
are the *degraded* version:

> - **Slash commands**: /find, /ingest, /remember, /explain, /source, /quarry
> - **Research agent**: researcher — …
> - **Auto-behaviors**: working directory is auto-indexed at session start; …
> - **Search tip**: natural language queries work best…

The two proactive bullets are absent. This is the version an agent sees in
every punt-labs repo except quarry.

**Enablement coverage compounds this.** Six sibling repos checked — biff,
vox, lux, punt-kit, cryptd, dungeon — none has `.punt-labs/quarry/enabled`,
none has the guide, none has the import. All are registered and indexed
(biff 294 docs, vox 733, lux 1141, punt-kit 136, cryptd 183, dungeon 24)
because `handle_session_start` auto-registers without enablement. Even in
this repo, `.punt-labs/quarry/enabled` and `.punt-labs/quarry/CLAUDE.md`
are untracked (`git status` shows `??`); only `config.md` is committed, so
a fresh clone starts un-enabled.

The fourth channel — ethos `session_context`, written by
`EthosMemoryBootstrap` at enable time (`src/quarry/enable.py:135`, template
at `src/quarry/doctor_ethos.py:12-36`) — reads "To recall prior knowledge:
`/find <query>`… To persist something you learned: `/remember <content>`…".
Better than (c), still no trigger condition, and only reaches
identity-bearing agents whose `quarry.yaml` ext has a `memory_collection`.

### 7. MCP tool descriptions

All 11 tools assessed for proactivity. None open with an occasion.

| Tool | Opening line | Proactive? |
|---|---|---|
| `find` | "Search indexed documents using hybrid semantic + keyword search." + RRF internals | No — describes mechanism |
| `remember` | "Remember inline text content: chunk, embed, and index for search." + "Use this instead of ingest when you have the text content directly" | **No, and actively narrowing** |
| `ingest` | "Ingest an HTTP(S) URL into the knowledge base." + why local files aren't supported | No |
| `show` | "Show document metadata or retrieve a specific page's text." | No |
| `list` | "List documents, collections, databases, or registrations." | No |
| `status` | "Get database status: document/chunk counts, storage size, and model info." | No |
| `delete` | "Delete indexed data for a document or collection." | No |
| `use` | "Switch to a different named database…" + 2 paragraphs of remote-target caveats | No |

Every description is passive reference-style. Contrast Context7's server
instruction visible in this session: "Use this server to fetch current
documentation whenever the user asks about a library, framework, SDK…
**Use even when you think you know the answer** — your training data may
not reflect recent changes. **Prefer this over web search** for library
docs." Three trigger conditions, one anti-rationalization clause, one
priority ordering. Quarry's `find` has none of the four.

The docstrings lean on implementation vocabulary — RRF, BM25, "the daemon
202s", "fire-and-forget", "the daemon owns the filesystem." Excellent code
documentation, poor tool-selection signal. Tells a model how quarry is
built, not when quarry is the answer.

### 8. Discoverability signals

Searched every channel for a "before you WebSearch, `find` first" style
directive:

| Channel | Has a proactive trigger? |
|---|---|
| MCP server `instructions` | No — 2 sentences of scope + 1 of formatting policy |
| Tool descriptions | No |
| SessionStart `additionalContext` | No — capability inventory |
| session-setup `additionalContext` | No — install log |
| PostToolUse WebFetch | Returns `{}` |
| PostToolUse suppress-output | Formatting only |
| PreCompact `systemMessage` | Weakly — fired at compaction |
| Vendored guide | **Yes** — but only in enabled repos (1 of 10) |
| Workspace/global CLAUDE.md | **No** — stripped copy |
| ethos `session_context` | Partial — no trigger |
| Skills | None ship |
| UserPromptSubmit hook | None exists |

**Verdict: quarry is a tool the agent has to already know to ask for.** In
the single repo where enablement landed, one file states the rule
correctly, buried at line 309 of a 310-line CLAUDE.md, duplicated by a
legacy fence directly above it. Everywhere else, the rule does not exist.

The `researcher` agent is the sharpest instance of the pattern. Its
description promises "Searches quarry first (local, fast, curated), then
web for gaps. Auto-ingests valuable web findings so research compounds
across sessions." Its body has a five-section strategy built on quarry
`find`/`show`/`remember`. Its `tools:` line grants **Read, Glob, Grep,
WebSearch, WebFetch** — and no quarry tool. Both places that advertise the
agent (`guidance.py:39-40`, `hooks.py:310-311`) point users at it.

By contrast, 7 of 28 repo agents in `.claude/agents/` (e.g. `mcg.md:11-15`)
explicitly list `mcp__plugin_quarry_quarry__find|remember|show|ingest|use`
— hand-wired, per-agent, outside the plugin.

## Recommended interventions

### Existing beads

| Bead | Verdict |
|---|---|
| **nmev** — "'use quarry' prompt hint" | **Highest-leverage existing bead — re-scope up.** As written it targets Context7's magic phrase. Context7's adoption comes from its *server instruction*, not the phrase. Re-scope to: rewrite MCP `instructions` string + `find`/`remember` descriptions with explicit trigger conditions. Keep the phrase as a bonus. Attacks causes 1 and 9. |
| **afg** — "Hook dispatcher + learning/recall hooks" | **Rename half already done** — `_hook_entry.py` dispatches `quarry-hook <event>`. Remaining scope: `session-end` capture (closes empty-corpus loop), `post-read`, `post-web-search`. Blocked on **b6p**. Causes 6, 7. |
| **b6p** — "learn CLI + config layer" | **Config layer partly shipped** — `auto_capture.{session_sync,web_fetch,compaction}` in `.punt-labs/quarry/config.md`, read by `load_hook_config`. Remaining scope: `learn` verb + `set_config` MCP tool. Re-scope to what's actually left, then unblock afg. |
| **kl1y** — "backfill-sessions" | **Directly fixes cause 7.** `~/.claude/projects/*quarry*/` has 7 transcripts; `quarry-captures` has 1. Backfill makes prior-session search return something. `quarry backfill-sessions` CLI referenced in `hooks.py:627-635`; verify what's actually left. |
| **ppv** — "/capture command" | User-initiated → does not fix autonomy. Value is *distillation* — Context/Findings/Sources docs search better than raw transcripts. Keep, not critical path. |
| **fmm** — session artifact extraction | **Likely already implemented.** `hooks.py:592-599` imports and calls `extract_artifacts` and `format_artifacts_header` from `quarry.artifacts`. Verify and close. |
| **bpm** — git artifact query at compaction | Improves capture *quality*, not agent *initiative*. Downstream. Defer. |
| **ih3l** — email memory via beadle | New capture source. Widens the write/read gap before retrieval is fixed. Defer. |

### New beads

1. **Ship a `plugin/skills/` entry with trigger phrases** — description
   carries "use when the user asks why/how/what-did-we-decide, before
   WebSearch or WebFetch for research, when recalling a prior session or
   design decision". Every high-adoption plugin here has one; quarry has
   zero. **Highest-impact new item.** Causes 1, 4.

2. **Fix the `researcher` agent's tool grant** — add
   `mcp__plugin_quarry_quarry__{find,show,remember}` to
   `plugin/agents/researcher.md:4`. The agent's entire prompt is currently
   unexecutable. Cause 5.

3. **Reconcile the three CLAUDE.md variants under one owner** —
   `guidance.py` as source of truth; delete the `<!-- quarry:begin -->`
   fence at `CLAUDE.md:287-308` (duplicate of the very next line's import);
   regenerate the `<!-- quarry:capabilities -->` blocks in workspace and
   global from the vendored guide. Causes 2, 3.

4. **Enrich SessionStart context into a directive with evidence** — replace
   `hooks.py:301-312` with coverage facts plus the rule: "This repo has N
   indexed documents and M prior session transcripts in `<repo>` /
   `<repo>-captures`. Before WebSearch/WebFetch, or before answering a
   why/how/what-did-we-decide question, call `find` first. Use Grep for
   symbols and literals." Fix `tests/test_hooks.py:524` while there.
   Causes 1, 3, 4.

5. **Auto-enable-or-nudge on auto-register** — `handle_session_start`
   auto-registers unregistered `cwd` but never deposits the guide or the
   `@`-import, so nine repos are indexed-but-mute. Options: (a) run
   `Enablement(directory).enable()` on auto-register, (b) emit a one-line
   nudge in `additionalContext`. Design call: (a) writes into git-tracked
   space silently, may be unwelcome. Nudge is the safe default. Cause 2.
   **Second-highest-impact new item.**

6. **Close the WebFetch loop** — PostToolUse WebFetch hook returns
   `additionalContext` when the URL (or a near-neighbor) is already
   indexed: "already captured — `find` it instead of re-fetching." Turns
   a write-only hook into a retrieval trigger. Cause 6.

7. **Rewrite `remember`'s description around durable knowledge** —
   "When you learn something durable — a decision, a gotcha, a procedure,
   a non-obvious fact — persist it here so it survives compaction." Demote
   the Claude-Desktop upload case to a secondary note. Cause 9. Fold into
   nmev if that bead is re-scoped per above.

## Cheap wins surfaced along the way

- **Stale `~/.punt-labs/quarry/sync.pid`** — leaked lock file, PID 2370231,
  no live process. Silent-path bug.
- **`test_context_includes_recall_hint` at `tests/test_hooks.py:524`** —
  name promises a hint the assertion doesn't check.
- **`fmm` appears already implemented** — verify and close.
- **`b6p` config layer already shipped** — re-scope bead to remaining work.
- **`researcher` agent tool grant** — small enough to fix inline rather
  than file a bead.

## Verification notes

Every claim in this document that was checked against running state:

- 10 registrations, 19 collections, 6,644 documents, 107,362 chunks — via
  `quarry status`.
- Enablement coverage — checked 6 sibling repos, none has the marker.
- WebFetch capture — `quarry-captures` holds two `docs.python.org` pages;
  four URLs in `default-captures`.
- Transcript capture — `quarry find "pre-compact transcript capture
  session" --collection quarry-captures` returns
  `session-795c803f-20260512T025950`.
- SessionStart hook output — ran the hook live; the message reproduced
  above is the actual `additionalContext`.
- Researcher agent tool grant — read from `plugin/agents/researcher.md:4`.

No code changed during the investigation.
