# Scoping and Ranking

## The three scope flags

- **No scope** (`quarry find "query"`) — searches everything the daemon's
  database holds: this codebase, design docs, prior session transcripts,
  captured web pages, and every agent's memories. The default, and the
  right choice for "what did we decide" or "how does X work" questions.
- **`--agent-handle <you>`** — restricts to memories written under that
  handle. Use your own handle to recall what *you* previously remembered or
  learned; never another agent's handle to impersonate their memory.
- **`--collection <name>`** — restricts to one named collection (a
  directory registration, a memory collection like `memory-<handle>`, or a
  captures collection). Use when you know exactly which corpus the answer
  lives in and want to exclude noise from the rest.

The three compose: `--agent-handle` and `--collection` can be combined, and
either can be combined with `--document`, `--page-type`, `--memory-type`, or
`--source-format` for a tighter filter. Confirm the current flag set with
`quarry find --help`.

## Memory decay

Ordinary memories (`remember`) decay with roughly a 30-day half-life — a
month-old fact ranks noticeably lower than a fresh one for the same query,
so recent corrections and gotchas surface ahead of stale ones without
disappearing outright. This decay does not apply to documents, code, or
lessons: only agent-authored memory rows age out of top rank.

## Lesson boost

`learn` entries (lessons) get a retrieval-preference boost over ordinary
documents and transcripts — a relevant lesson tends to outrank a transcript
or doc mentioning the same keywords, because a lesson is a distilled,
project-endorsed rule rather than a raw record. An irrelevant lesson still
loses to a genuinely relevant result; the boost changes ranking among
otherwise-comparable hits, not relevance itself.

## When to prefer which scope

| Question shape | Scope |
|---|---|
| "What did we decide about X" | No scope — the decision could live in docs, transcripts, or a teammate's memory |
| "What did I learn about X" | `--agent-handle <you>` |
| "What's in the `<repo>` design docs about X" | `--collection <repo-collection>` |
| "Is there a project rule about X" | No scope, or `--memory-type lesson` if the boost hasn't already surfaced it first |
