---
name: quarry-recall
description: >
  Retrieve what quarry already knows before researching from scratch or
  guessing. Covers three overlapping intents, one tool: understand code
  ("how does X work", "where is X defined", "what calls X"); recall project
  knowledge or a decision ("what did we decide about X", "why is it this way",
  "did we consider Y instead"); and recall memory, yours or a teammate's
  ("what did I learn about X", "any gotchas with X", "have we hit this
  before"). Backed by `quarry find`, which fuses vector similarity and
  full-text search over this codebase, design docs, prior session
  transcripts, and previously fetched web pages. Use before WebSearch or
  WebFetch, and even when the answer seems obvious — a prior decision or a
  teammate's note may contradict the assumption. Skip it for an exact
  symbol or literal-string lookup (grep is faster and exact) or for
  navigating the file already open.
---

# Quarry Recall

`quarry find` answers "has anyone already figured this out." Reach for it
before spending a WebSearch/WebFetch call, and before answering a
why/how/what-did-we-decide question from memory alone.

## Recognize the trigger

- **Understand code.** "How does X work", "where is X defined", "what calls
  X", "explain this module."
- **Recall project knowledge.** "What did we decide about X", "why is it
  built this way", "did we already try Y."
- **Recall memory.** "What did I learn about X", "any gotchas here", "has a
  teammate hit this before."

All three are the same tool with different scope — pick the flags, not a
different command.

## Run it

```sh
quarry find "<natural-language question>"
```

Natural language beats keywords ("what did we decide about retry limits"
outperforms "retry limits"). Confirm the exact flag set with `quarry find
--help` before relying on one not shown here — flags are the live contract,
this skill is not.

Scope the search to match the intent:

- Everything indexed: `quarry find "query"` (no scope flags).
- Only your own memories: `quarry find "query" --agent-handle <your-handle>`.
  Always your own handle — never guess or borrow another agent's.
- One collection: `quarry find "query" --collection <name>`.
- Narrow further: `--document`, `--page-type`, `--source-format`,
  `--memory-type` (e.g. `fact`, `procedure`, `lesson`).

## grep vs. find

| Need | Use |
|---|---|
| Exact symbol, literal string, a value you can name | `grep` — faster, exact, no embedding round-trip |
| Meaning, a paraphrase, "why"/"how"/"what did we decide" | `quarry find` |
| The file already open | The editor — don't search for what's on screen |

## Read the result honestly

- **Cite what you use**: `[document p.N]`. A claim without a citable hit is
  your own reasoning, not quarry's.
- **Empty is not "false."** No results means quarry has not indexed it —
  say so plainly. Never present an empty search as proof something didn't
  happen or isn't true.
- **Results are memory, dated.** A hit reflects the state of the world when
  it was captured or indexed, not necessarily now. Check the date before
  treating a hit as current truth.
- **You reasoned; quarry retrieved.** Don't attribute a conclusion to
  quarry ("quarry says X") — quarry supplied the evidence, you drew the
  conclusion.
- No branded headings, emoji banners, or repeated summary blocks in the
  answer — plain prose citing sources.

## When it doesn't come back clean

See `references/failure-modes.md` for the full recognize → next-action →
do-not table (daemon down, empty results, remote/local mode, a stale index,
an unknown collection, unauthorized). The one rule that covers all of them:
narrow once, then stop — never blind-retry the same query hoping for a
different answer.

## Deeper scoping and decay

See `references/scoping.md` for how `--agent-handle`/`--collection` interact,
how memory decay and the lesson boost affect ranking, and when to prefer one
scope over another.

## Do not

- Reach for `find` on an exact symbol or literal string `grep` answers
  directly and faster.
- Treat an empty result as proof of absence.
- Retry a failing or empty query more than once without changing the query,
  the scope, or the daemon's state.
