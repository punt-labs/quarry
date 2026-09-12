# Proactive Trigger Vocabulary — SessionStart Context and MCP Tool Descriptions

## Ratifications (2026-08-30)

Three operator rulings that override the design body where they conflict:

- **R1a — new `/v1/coverage` route: proceed.** Ship the endpoint as specified.
- **R2b — trigger trailer applies uniformly.** The design's Branch (2)
  (daemon unreachable) section proposes withholding the trailer; that is
  reversed. All three branches emit R1/R2/R3 verbatim, including Branch (2).
  Rationale: an agent that sees the rules alongside a "daemon unreachable"
  operational message can act on the diagnosis (restart quarryd, run
  `quarry doctor`) and then apply the rules. Withholding the rules assumed the
  agent was a passive receiver, which is not how this repo's agents behave.
- **R3a — `remember` docstring narrowing: proceed.** Drop the clipboard/API-
  response/sandbox-uploaded-files framing entirely; replace with R3 verbatim.

The Branch (2) template becomes:

```text
Quarry is enabled for this repo but quarryd is currently unreachable.
Once you restart it (systemctl --user restart quarry / launchctl kickstart)
the tools below become available.
Use find before WebSearch or WebFetch for research, or before answering a
why/how/what-did-we-decide question.
Prefer grep for symbol and value lookups; prefer find for meaning.
Use remember when you learn something durable — a decision, a gotcha, a
non-obvious fact, a procedure — so it survives context compaction.
```

Two surfaces load automatically for every agent session — the SessionStart
`additionalContext` and the MCP tool listing — plus one surface an agent must
actively invoke (the `recall` skill). This design makes the three carry the
same three trigger sentences, so an agent that reads any one of them gets the
same rule.

## Vocabulary alignment

Three canonical sentences. Each appears **verbatim** in every surface below —
no paraphrase, no re-ordering of clauses.

```text
R1 = "Use find before WebSearch or WebFetch for research, or before answering a why/how/what-did-we-decide question."
R2 = "Prefer grep for symbol and value lookups; prefer find for meaning."
R3 = "Use remember when you learn something durable — a decision, a gotcha, a non-obvious fact, a procedure — so it survives context compaction."
```

Derivation, so the wording is traceable rather than invented:

- R1 merges `src/quarry/data/repo-guide.md:6-9` ("Before using WebSearch or
  WebFetch for research, run `/find`...") with the nmev bead's own drafted
  `find` opener ("Use before WebSearch or WebFetch for research... Use for
  why/how/what-did-we-decide questions"). Anchors on the MCP tool name
  (`find`), not the slash command, since two of the three target surfaces
  (`additionalContext`, tool docstrings) address the tool directly.
- R2 is `repo-guide.md:10-11` ("Use grep for symbol lookups and value
  lookups; use quarry for...") with "quarry" narrowed to "find" — the
  specific callable — and the why/how clause dropped (R1 already states it,
  avoiding repetition across two adjacent bullets).
- R3 is the nmev bead's drafted `remember` opener verbatim ("Use when you
  learn something durable — a decision, a gotcha, a non-obvious fact, a
  procedure — so it survives context compaction"), with "remember" (the tool
  name) prefixed so the sentence stands alone outside its own docstring.

### Current wording per surface, and the gap

| Surface | Current wording | Gap |
|---|---|---|
| `src/quarry/data/repo-guide.md:6-11` | Prose paraphrase of R1/R2 | Close in substance, not verbatim. Out of this mission's write-set (not listed in the contract); note only. |
| `plugin/skills/recall/SKILL.md:3-15,25-34` | Frontmatter `description` and body "When to use it" bullets, longer prose, no R3 sentence in that exact shape | Diverges in wording from R1/R2; R3's substance is present ("when you learn something durable...") but phrased differently. **Needs updating** — see write-set. |
| `src/quarry/hooks.py:301-312` | Zero trigger rules — pure inventory | Full replacement — see below. |
| `src/quarry/mcp_server.py:77-87,132-256` | Zero trigger rules in `instructions` or in `find`/`remember` docstrings | Full replacement — see below. |

## SessionStart `additionalContext` (hooks.py)

### Current state

`handle_session_start` (`src/quarry/hooks.py:201-316`) has **three** distinct
return points that carry a context string, not one:

1. `src/quarry/hooks.py:252-256` — child registrations exist under this
   directory; auto-register skipped to prevent subsumption.
2. `src/quarry/hooks.py:277-282` — `quarryd` unreachable; auto-registration
   deferred (fail-closed, per the docstring at `hooks.py:166-176`).
3. `src/quarry/hooks.py:301-312` — the normal path (the string named in the
   bead as the one to replace).

The bead's cited line numbers (`hooks.py:297-300`) do not point at a
daemon-reachability branch in the current file — they land on the
`_sync_line` dict (`hooks.py:296-300`), which reports the **background sync
subprocess's** launch status (`launched`/`running`/`failed`), a local
`subprocess.Popen` outcome unrelated to whether `quarryd`'s HTTP API is up.
The real daemon-unreachable branch is (2) above. This drift is exactly what
the mission warned about; the design below cites current line numbers only.

### Which branches get the rule trailer, and which don't

Branch (2) is the one case where the rule trailer must be **omitted**. If
`quarryd` is unreachable, every MCP tool (`find`, `remember`, ...) fails at
the `_guard` boundary (`mcp_server.py:55-74`) with `Error: ... unreachable`.
Telling the agent to "use find before WebSearch" when `find` is provably
broken this session is actively wrong. Branch (2) keeps its existing
operational message unchanged — go fix `quarryd`, not "use these tools."

Branch (1) does get the trailer: a nested-registration collision (some other
directory already owns the covering collection) says nothing about whether
`quarryd` itself is up, and in the common case it still is — the agent should
still reach for `find`/`remember` against whatever collection already
covers this tree.

Branch (3), the normal path, gets the full coverage line + trailer. It also
splits into a reachable and an unreachable sub-case (see below) — this is a
**new** failure mode this design introduces (the coverage query can fail
even when registration itself, a purely local SQLite write, succeeds).

### The coverage query: a new daemon route

`document_count`/`chunk_count` already exist in two places, neither of which
fits a per-session hot path:

- `ChunkCatalog.list_collections()` (`src/quarry/db/chunk_catalog.py:89-121`)
  — **no filter parameter**. It scans and groups the whole `chunks` table
  across *every* collection in the shared database, then the caller (e.g.
  `/status`, `src/quarry/daemon/routes/meta.py:78-108`) picks out one row.
  Cost is proportional to the *entire* shared database, not this repo's
  slice of it — wrong shape for something that fires on every SessionStart.
- `ChunkCatalog.list_documents(collection_filter)`
  (`src/quarry/db/chunk_catalog.py:33-87`) — pushes a `WHERE collection = …`
  predicate, bounding the scan to one collection. Closer, but it selects
  `document_name`/`document_path`/`total_pages`/`page_number`/
  `ingestion_timestamp` — no `agent_handle`/`memory_type`, so it cannot
  separate "documents indexed" from "memories saved" (both are chunks in the
  same `<repo>` collection; only the memory columns
  (`src/quarry/db/schema.py:20-22,55-57`) distinguish them).

Neither existing route is right. Add one:

**New route**: `GET /v1/coverage?collection=<repo>` (existing convention: a
GET query-param route, siblings `/documents`, `/collections`, `/status`,
registered in `src/quarry/daemon/route_table.py:135-232` order, right after
`/status`). Handler on `MetaRoutes`
(`src/quarry/daemon/routes/meta.py`, alongside `status()`), backed by one
new `ChunkCatalog` method:

```python
# src/quarry/db/chunk_catalog.py — new method on ChunkCatalog
def coverage(self, collection: str, captures_collection: str) -> CoverageCounts:
    """Count documents, transcripts, and memories for one repo's two collections."""
    if TABLE_NAME not in self._db.list_tables().tables:
        return {"documents_indexed": 0, "transcripts_captured": 0, "memories_saved": 0}
    table = self._db.open_table(TABLE_NAME)
    predicate = (
        f"collection IN ('{escape_sql(collection)}', "
        f"'{escape_sql(captures_collection)}')"
    )
    rows = (
        table.search()
        .where(predicate)
        .limit(_FULL_SCAN_LIMIT)
        .select(["collection", "document_name", "agent_handle", "memory_type"])
        .to_list()
    )
    documents: set[str] = set()
    memories: set[str] = set()
    transcripts: set[str] = set()
    for row in rows:
        name = str(row["document_name"])
        if str(row["collection"]) == captures_collection:
            # api/capture_ingest.py:13-15 — transcripts are named
            # "session-<id[:8]>"; a fetched URL capture is not.
            if name.startswith("session-"):
                transcripts.add(name)
            continue
        if row["agent_handle"] or row["memory_type"]:
            memories.add(name)
        else:
            documents.add(name)
    return {
        "documents_indexed": len(documents),
        "transcripts_captured": len(transcripts),
        "memories_saved": len(memories),
    }
```

`CoverageCounts` is a new `TypedDict` in `src/quarry/results.py`, alongside
`DocumentSummary`/`CollectionSummary` (`results.py:142-161`), same shape.
The wire model is `CoverageResponse` in `src/quarry/api/meta.py` next to
`StatusResponse` (`api/meta.py:40-51`), and `QuarryClient.coverage(req)` in
`src/quarry/client/client.py` beside `status()` (`client.py:180-182`),
following the existing one-model-per-route contract (bug-class-3 field
parity: the model is the only place the wire shape is spelled twice).

**Why this is cheap enough for a hot path**: the `WHERE collection IN (...)`
predicate is pushed to the LanceDB scan exactly like the existing
`list_documents(collection_filter=...)` path already does today — the design
is not introducing a new class of query, it is bounding an existing one to
two collection names instead of one. The response never leaves this repo's
slice of the shared table, unlike `/status`, which already pays the
unfiltered `list_collections()` scan on every manual `quarry status`
invocation — this route pays less than that existing, accepted cost, and
pays it automatically instead of on request. If a scalar index on
`collection` proves necessary under real multi-repo load, that is an
orthogonal follow-on (LanceDB supports BTREE scalar indexes); it does not
change this route's contract.

### Hook-side helper

Mirror the existing `_daemon_chunk_collections()` pattern
(`hooks.py:166-188`): translate client exceptions into one boundary-neutral
signal, `None` for "unavailable," rather than leaking `quarry.client`'s
exception hierarchy into the caller.

```python
# src/quarry/hooks.py — new helper, same shape as _daemon_chunk_collections
def _session_coverage(collection: str, captures_collection: str) -> CoverageCounts | None:
    from quarry.client import ClientConfigError, QuarryError, TargetResolver  # noqa: PLC0415
    try:
        return TargetResolver.connect().coverage(collection, captures_collection)
    except (ClientConfigError, QuarryError):
        return None
```

### The rewritten templates

Shared trailer (module-level constant, used by branches (1) and (3)):

```python
_TRIGGER_RULES = (
    "Use find before WebSearch or WebFetch for research, or before "
    "answering a why/how/what-did-we-decide question.",
    "Prefer grep for symbol and value lookups; prefer find for meaning.",
    "Use remember when you learn something durable — a decision, a gotcha, "
    "a non-obvious fact, a procedure — so it survives context compaction.",
)
```

Branch (3), coverage query reachable:

```text
Quarry: {n} documents indexed, {m} transcripts captured, {k} memories saved
in "{collection}" (captures: "{captures_collection}").
Use find before WebSearch or WebFetch for research, or before answering a
why/how/what-did-we-decide question.
Prefer grep for symbol and value lookups; prefer find for meaning.
Use remember when you learn something durable — a decision, a gotcha, a
non-obvious fact, a procedure — so it survives context compaction.
{sync_line}
Slash commands: /find, /ingest, /remember, /explain, /source, /quarry. For
deep research across local docs and the web, use the researcher agent.
```

(`{sync_line}` is the existing `launched`/`running`/`failed` line,
`hooks.py:296-300` — demoted below the rules, not dropped: it is genuinely
useful troubleshooting signal, just not a trigger.)

Branch (3), coverage query unreachable (new sub-case — registration and sync
can both succeed locally while the daemon's HTTP API is down for this one
call):

```text
Quarry is active for "{collection}" (captures: "{captures_collection}");
coverage counts unavailable (quarryd unreachable).
Use find before WebSearch or WebFetch for research, or before answering a
why/how/what-did-we-decide question.
Prefer grep for symbol and value lookups; prefer find for meaning.
Use remember when you learn something durable — a decision, a gotcha, a
non-obvious fact, a procedure — so it survives context compaction.
{sync_line}
Slash commands: ...
```

Branch (1), child registrations exist (`hooks.py:252-256`) — existing
message plus the same three-line trailer appended (no coverage numbers: no
collection was chosen for *this* directory).

Branch (2), daemon unreachable at registration time (`hooks.py:277-282`) —
**unchanged**. Per the reasoning above, the trailer is deliberately withheld
here.

## MCP surface (`mcp_server.py`)

### Server `instructions` (`mcp_server.py:77-87`)

Current: two sentences, both formatting policy, zero trigger content.

New — leads with R1 + R2 verbatim (satisfies "identical wording" for the
tool-description surface as a whole, not only the two tool docstrings that
also carry them), then the anti-rationalization clause and the negative
rule the nmev bead asks for, then the existing formatting-policy paragraph,
demoted:

```python
mcp = FastMCP(
    "punt-quarry",
    instructions=(
        "Use find before WebSearch or WebFetch for research, or before "
        "answering a why/how/what-did-we-decide question. Prefer grep for "
        "symbol and value lookups; prefer find for meaning. Use even when "
        "you think you already know the answer — a prior decision or a "
        "teammate's note may contradict your assumption. Do not use for "
        "mechanical string searches or navigating the file already open — "
        "grep and the editor do that well.\n\n"
        "All quarry tool output is pre-formatted plain text using unicode "
        "characters for alignment. Always emit quarry output verbatim — "
        "never reformat, never convert to markdown tables, never wrap "
        "in code fences or boxes."
    ),
)
```

### Tool docstrings

Each opens with the occasion (a canonical rule where one applies, a
situational sentence otherwise), current mechanism text follows, demoted
but not deleted — the `Args:` block is unchanged throughout.

| Tool | Current opener (`mcp_server.py:`) | New opener |
|---|---|---|
| `find` | "Search indexed documents using hybrid semantic + keyword search." (143) | R1 + R2, then "Combines vector similarity and BM25..." demoted one line down. |
| `remember` | "Remember inline text content: chunk, embed, and index for search." (219) | R3, then "The daemon scrubs secrets/PII before indexing." The existing "clipboard, API response, sandbox-uploaded files" framing (222) is dropped — it narrows the tool to an upload workflow, which is exactly the gap the bead names. |
| `ingest` | "Ingest an HTTP(S) URL into the knowledge base." (182) | "Use when you have a URL to add to the knowledge base — a doc, an article, a spec." |
| `list_resources` (`list`) | "List documents, collections, databases, or registrations." (260) | "Use to see what's already indexed before ingesting it again." |
| `show` | "Show document metadata or retrieve a specific page's text." (287) | "Use to read a specific page, or to check whether a document is already indexed." |
| `delete` | "Delete indexed data for a document or collection." (331) | "Use to remove stale or wrong content before re-ingesting it." |
| `register_directory` | "Register a directory for incremental sync." (357) | "Use to track a local directory so future changes sync automatically." |
| `deregister_directory` | "Remove a directory registration." (379) | "Use to stop tracking a directory — keep its indexed data with `keep_data=True`, or purge it." |
| `sync_all_registrations` | "Sync all registered directories: ingest new/changed, remove deleted." (401) | "Use after registering a new directory, or when tracked files changed outside quarry's own writes." |
| `status` | "Get database status: document/chunk counts, storage size, and model info." (410) | "Use to check how much is indexed before deciding whether to search or ingest." |
| `use_database` | "Switch to a different named database for subsequent operations." (415) | "Use to point every other tool at a different named database." (mechanism paragraphs below unchanged.) |

`find` and `remember` are the two tools that carry the shared canonical
sentences verbatim; the rest get situational openers per the nmev bead's
"opens with an occasion, not a mechanism" criterion — there is no shared
three-rule vocabulary that applies to `ingest`/`show`/`delete`/etc., so
inventing a fourth "canonical" sentence for them would be manufacturing
consistency that doesn't exist in the source material.

## `plugin/skills/recall/SKILL.md`

**Needs updating** — its current wording (frontmatter `description`,
`SKILL.md:3-15`, and the "When to use it" bullets, `SKILL.md:25-34`) covers
the same three triggers but in different prose, not the canonical
sentences. Splice R1/R2/R3 in verbatim as the first line of each
corresponding bullet, keep the explanatory sentence that follows each one
(the "why" — e.g. "quarry usually has the design doc..." at `SKILL.md:30`)
as supporting text, not a replacement:

```markdown
## When to use it

- Use find before WebSearch or WebFetch for research, or before answering a
  why/how/what-did-we-decide question. Quarry indexes this codebase, design
  docs, prior session transcripts, and previously fetched web pages — it
  often already has the answer.
- Prefer grep for symbol and value lookups; prefer find for meaning.
- Use remember when you learn something durable — a decision, a gotcha, a
  non-obvious fact, a procedure — so it survives context compaction.
```

The frontmatter `description` (`SKILL.md:3-15`) is what Claude Code
surfaces when deciding whether to invoke the skill at all — it should also
lead with R1, for the same reason the server `instructions` does: it is the
one line most likely to be read before the rest of the file is.

## Write set for the implementation mission

- `src/quarry/hooks.py` — `_TRIGGER_RULES` constant, `_session_coverage()`
  helper, the three-branch template rewrite in `handle_session_start`.
- `src/quarry/results.py` — `CoverageCounts` TypedDict.
- `src/quarry/api/meta.py` — `CoverageResponse` model.
- `src/quarry/db/chunk_catalog.py` — `ChunkCatalog.coverage()` method.
- `src/quarry/daemon/routes/meta.py` — `MetaRoutes.coverage()` handler.
- `src/quarry/daemon/route_table.py` — register `GET /v1/coverage`.
- `src/quarry/client/client.py` — `QuarryClient.coverage()`.
- `src/quarry/mcp_server.py` — server `instructions`, all eleven tool
  docstrings.
- `tests/test_hooks.py` — rewrite `test_context_includes_recall_hint`
  (`tests/test_hooks.py:524-541`) to assert each of the three rule strings
  via exact `in` checks (not `startswith`); add a coverage-line test (fake
  `_session_coverage` returning fixed counts, assert the numbers and both
  collection names appear); add a coverage-unreachable test (`
  _session_coverage` returning `None`, assert the fallback string still
  contains all three rule strings); add a test for branch (1) carrying the
  trailer and branch (2) *not* carrying it.
- `tests/test_mcp_server.py` — assert R1/R2 appear in `find`'s docstring and
  the server `instructions`; assert R3 appears in `remember`'s docstring;
  assert every other tool's docstring opens with a non-mechanism sentence
  (a simple "does not start with a verb naming the underlying operation"
  check, or an explicit per-tool string match against the table above).
- New test module for the coverage route/catalog method — `tests/
  test_chunk_catalog.py` (coverage counts split correctly across documents/
  transcripts/memories, including the `session-` prefix boundary and the
  empty-table case) and an HTTP contract test asserting `/v1/coverage`
  reads `collection` from the query string and reaches
  `ChunkCatalog.coverage` (bug-class-3: the CLI/hook caller and the route
  must agree on the parameter name).
- `plugin/skills/recall/SKILL.md` — splice R1/R2/R3 into the frontmatter
  description and the "When to use it" bullets, per the section above.

Not in the write set: `src/quarry/data/repo-guide.md` — its wording is
close in substance to R1/R2 but the mission contract does not list it, and
it is a separately-vendored artifact (baked into `CLAUDE.md` at repo setup)
rather than a live SessionStart/MCP surface. Worth a follow-up alignment
pass, not this one.
