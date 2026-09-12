---
name: recall
description: >
  Use find before WebSearch or WebFetch for research, or before answering a
  why/how/what-did-we-decide question. Quarry indexes this codebase, design
  docs, prior session transcripts, and previously fetched web pages — it often
  already has the answer. Prefer grep for symbol and value lookups; prefer find
  for meaning. Four capture verbs, each with distinct semantics: remember = a
  specific durable fact, ingest = a URL, learn = a distilled lesson that gets
  retrieval preference, and find retrieves them all. Use even when you think
  you already know the answer; a prior decision or a teammate's note may
  contradict your assumption. Do not reach for find on mechanical string
  searches or navigation within the current file — grep and the editor already
  do that well.
---

# Quarry — Local Semantic Search

Quarry indexes documents by meaning and answers natural-language questions
against them: this repo's source and docs, prior session transcripts, and web
pages fetched during earlier research. Reach for it before spending a
WebSearch or WebFetch call re-discovering something already found.

## When to use it

- Use find before WebSearch or WebFetch for research, or before answering a
  why/how/what-did-we-decide question. Quarry indexes this codebase, design
  docs, prior session transcripts, and previously fetched web pages — it often
  already has the answer.
- Prefer grep for symbol and value lookups; prefer find for meaning.
- Pick the capture verb by the shape of what you're saving. The four verbs
  are distinct on purpose:
  - `remember` — a specific durable fact (a URL, an ID, an address, a
    version pin). Small, factual, retrievable by its literal content.
  - `ingest` — a URL. Fetches the page with smart sitemap discovery and
    single-page fallback. For local files or directories, use
    `register_directory` + `sync_all_registrations` instead.
  - `learn` — a distilled lesson: the rule you'd tell a teammate ("when X,
    do Y, because Z"). Lessons route to the repo's `-lessons` collection
    and get retrieval preference over transcripts and general docs, so a
    lesson typically ranks above a session transcript that mentions the
    same keywords.
  - `find` — the retrieval verb; searches everything the other three wrote.

## When not to use it

- Exact symbol or value lookups (a function name, a literal string) — grep is
  faster and precise.
- Navigating the file currently open — use the editor, not search.
- For architecture decisions inside this repo, read DESIGN.md directly —
  quarry find complements it, does not replace it. DESIGN.md is the
  authoritative ADR log; find shines when the question spans prior session
  transcripts, web-fetched research, or docs outside the current tree.

## Tools

- `/find <query>` — search the knowledge base; natural language beats keywords
  ("What did we decide about retry limits?" beats "retry limits").
- `/remember <name>` — persist inline text as a named memory (a durable fact).
- `/learn <lesson>` — save a distilled lesson that gets retrieval preference
  over general docs and transcripts.
- `/ingest <url>` — fetch and index a URL (sitemap discovery with single-page
  fallback). For local files or directories, use `register_directory` +
  `sync_all_registrations` instead.
- `/explain <document or topic>` — search and synthesize an explanation.
- `/source <claim or text>` — find which document a claim came from.
- MCP tools (same operations, callable directly): `find`, `remember`, `learn`,
  `ingest`, `register_directory`, `sync_all_registrations`, `show`, `delete`,
  `list`, `status`, `use`. Prefer `/quarry:quarry use <db>` as the interactive
  entry for database switching; the `use` MCP tool is also available.

## Worked examples

- "Why did we choose LanceDB over pgvector?" → read `DESIGN.md` first (the
  in-repo ADR log wins for repo-internal architecture); reach for `/find` if
  the answer spans prior sessions or web-fetched research.
- "What broke in the last TLS review round?" → `/find "TLS review findings"`
  surfaces the prior session transcript and design doc, not a fresh grep.
- Learned a non-obvious root cause while debugging → `/remember` the exact
  fact (the offending path, the missing flag) so the next session can look it
  up verbatim.
- Distilled a debugging insight into a general rule ("when a pyright unknown
  suppression appears, narrow it to the one call — never widen it project-
  wide") → `/learn` it. Lessons rank above transcripts on later queries, so
  the rule surfaces first even when a transcript mentions the same keywords.
