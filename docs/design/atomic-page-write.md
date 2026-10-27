# Atomic page write: one protocol for every document the funnel stores

**Status:** PROPOSED, amended after security review (design mission; one acknowledgement outstanding, see §13)
**Bead:** quarry-70zm
**Extends:** DES-057 (decision 2's overwrite-delete gate, decision 6's create-if-absent, ownership-guarded cleanup), DES-034 (windowed insertion), DES-036 (the single funnel)
**Companion model:** `docs/design/atomic-page-write.spec.tex` (Z, fuzz-checked, ProB-checked); report beside it in `atomic-page-write.spec.report.json`

## Abstract

`ChunkStoreFunnel` stores a document as a series of LanceDB commits under the
document's real name, deleting the previous copy first and "discarding" the
partial copy on failure. Three review rounds found the same defect in that shape
from three angles; patching it again will find a fourth. This document replaces
the shape. A write stages its embedded windows in a private, anonymous spool
that no reader can see, then makes the whole document visible with **one**
LanceDB commit that carries a per-attempt *write key*, then reaps older
generations with a delete bounded by the key. The key is the instant the
content was captured, so an older capture can never replace a newer one. Every
existing reader keeps working unchanged, because the table only ever holds
complete generations. One transient state is the price: for milliseconds after
a replace (or until the next touch after a crash) the old and the new complete
copies are both present. §13 asks the leader to accept that, with the
measurements that rule out the alternatives.

## 1. Motivation

The current protocol, with the code that implements it:

- `ChunkStoreFunnel._embed_and_store` (`chunk_store_funnel.py:117-145`) calls
  `_replace_prior` (`:106-115`), which deletes every row of the name
  (`LegacyUrlNames.replace_prior`, `legacy_url_names.py:152-157`), then
  `OwnedInsert.run` (`owned_insert.py:41-50`), which calls
  `DocumentStreamer.insert` (`streaming.py:98-110`). That inserts window by
  window, each window one `table.add` (`chunk_store.py:56-73`) and so one
  LanceDB commit, under the real name.
- On failure `OwnedInsert._discard_partial` (`owned_insert.py:52-67`) deletes
  the name, but only when `url_sourced`.
- Create-if-absent is `document_exists` (`chunk_catalog.py:178-193`): any row
  under the name means held.

The defects, each reproduced in the model's mode 1 (see §6):

| # | Defect | Model witness |
|---|---|---|
| 1 | A crash, or a failed cleanup, leaves a partial copy under the real name; create-if-absent then skips it forever. | `partialVisible`, `skippedOnPartial` |
| 2 | Overwrite deletes the good copy before the new one exists; a failed embed or store leaves zero copies. | `lost`, `unsafeDelete` |
| 3 | Cleanup by name assumes one writer. Another process (local-mode CLI) can have its page deleted. | `unsafeDelete` |
| 4 | An interrupt (`BaseException`) skips the cleanup (`except Exception`). | `partialVisible` |
| 5 | A failed *label* insert (`remember`, `learn`, transcripts) keeps its partial chunks, because cleanup is gated on `url_sourced`; a later non-overwrite `remember` skips it as "exists". | `partialVisible`, `skippedOnPartial` |
| 6 | Order. FIFO order is submission order, not capture order. A backfill that read a transcript at t1, queued behind a hook capture read at t2 > t1, overwrites newer chunks with older content. | `inversion` |
| 7 | Copies already in the store (earlier builds, crashes before this ships) may be partial, and the held check trusts bare name existence. | `skippedOnPartial` |

Defects 1-4 are the original brief; 5-7 are the two addenda. One protocol covers
all of them, for every document written through the funnel, URL page or label.

## 2. Decision

**Stage, commit once, reap by key.** Described in §5. In one paragraph:

1. Resolve the write's *stamp* once (the capture instant; §7) and make a *write
   key* `"<stamp as 20-digit microseconds>-<32 hex token>"`.
2. Embed in bounded windows exactly as today, but write each window to a
   `PageSpool`: an anonymous temporary file holding an Arrow IPC stream.
   Nothing touches the `chunks` table.
3. When every window is spooled, `table.add(<reader over the spool>)`: one
   LanceDB commit. Each row carries `write_id = key` and `chunk_count = K` (the
   document's chunk total).
4. Heal: read the newest *complete* generation of the name; delete rows whose
   `write_id` is older (or NULL). A second writer, a crash, or a stale writer
   changes nothing about correctness, because the delete is bounded by a key
   that is itself complete.

The held check, create-if-absent, the superseded check, and the sweep all
read the same fact: *is there a generation of this name whose row count equals
its `chunk_count`?*

Two ownership rules bound the protocol's reach, both added after review:

- **Disjoint ownership.** Directory sync owns its names and marks its rows
  `write_id = 'sync'`. The funnel and the heal never delete a sync-marked row,
  and a funnel write to a name sync owns is refused before any embedding (§5.9).
- **Only complete copies speak.** A stored stamp, for ordering, comes only from
  a complete generation or from a legacy copy that passes the held check. A
  damaged copy contributes none, so a replay of an old capture repairs it
  instead of being "superseded" by it (§7).

## 3. LanceDB facts relied on

Verified against the installed `lancedb 0.36.0` / `pyarrow 25.0.0` on a
throwaway database under `.tmp/70zm-atomic/` (scripts `e1`-`e10`; never the
live store, never the daemon). Version deltas are `table.version` before and
after.

| # | Fact | Evidence |
|---|---|---|
| F1 | `table.add(reader)` is one commit: version +1. A reader that raises mid-stream raises out of `add`, writes no row, and leaves the version unchanged. | e1 |
| F2 | SIGKILL of the writer while its reader is mid-stream leaves no row and the version unchanged. | e2 |
| F3 | `add(reader)` memory plateaus. Peak RSS above baseline: +104 MB for 100k rows (0.3 GB of 768-dim vectors), +221 MB for 300k (1 GB), +238 MB for 600k (1.8 GB). Wall 0.3 / 0.9 / 2.3 s. I5 holds: the commit streams. | e4 |
| F4 | `merge_insert` **buffers its source**: 300k rows (1 GB) peaks at +1.2 GB. And with `when_not_matched_by_source_delete`, replacing a 12-row document in a 320k-row (1 GB) table raised RSS by 0.7-0.9 GB, with or without scalar indexes on the key columns (an insert-only merge was +74 MB). Replace-by-merge is O(table) memory. | e4, e6 |
| F5 | `merge_insert` keyed on `(collection, document_name, chunk_index)` does not collapse duplicate rows that are already there: both are updated, both stay. An empty source with a by-source delete deletes the whole document. Duplicate keys in the source are rejected. Keyed on a per-attempt `write_id`, a replace over legacy duplicates leaves one copy, and an identical retry is idempotent. | e3, e5 |
| F6 | `table.delete(predicate)` is one commit. `write_id < 'x'` compares strings and includes `''`; it does **not** match NULL rows, so a heal predicate must say `write_id IS NULL OR write_id < 'x'`. 0.04 s and +30 MB on a 620k-row table (cold). | e10 |
| F7 | A narrow scan (`select(['document_name','collection','write_id'])`) over a 620k-row table takes 0.7 s cold (0.09 s warm) and +66 MB. The daemon-start sweep reads exactly that. | e10 |
| F8 | `table.update(where, values)` is one commit that rewrites the matched rows (20k rows 0.13-0.37 s). Sound, but it only helps a design in which readers filter on a stamp (§8). | e10 |
| F9 | `add_columns({"write_id": "''", "chunk_count": "cast(0 as int)"})` yields `string` / `int32` with those defaults on existing rows. | e5, e7 |
| F10 | `add(reader)` rejects a reader whose schema lacks a column (`Append with different schema`). A list-of-dicts `add` with missing keys is **accepted and writes NULL**. So an older writer against a migrated table writes NULL `write_id`/`chunk_count`; the code must read NULL as `''`/0 and the heal must reap NULL. | e7, e10 |
| F11 | An empty table created from a schema accepts an FTS index; a following `add(reader)` works; unindexed rows are found by FTS search until the next index optimize. | e8 |
| F12 | Eight threads, each with its own connection, writing the same name with shuffled stamps while six unrelated adds run: no error, exactly one copy of K rows, the survivor is the largest committed key, unrelated documents untouched. Three runs. | e9 |
| F13 | Every operation used (`add`, `delete`, `update`) is exactly one version. A `RecordBatchReader` source from an anonymous temp file (`tempfile.TemporaryFile`, IPC stream, 1500 rows, 4.8 MB) round-trips through `add` in one commit. | e1, e7 |

Not relied on, deliberately: LanceDB has no multi-operation transaction in the
Python API; there is no atomic "swap". That absence is the reason for the
transient duplicate in §13.

## 4. Schema impact (upgrade-only)

Two columns on `chunks`, added by `SchemaManager.migrate` on every open
(`schema.py:19-23` is the mechanism), canonical in `SchemaManager.schema()`:

| Column | Type | Default on existing rows | Meaning |
|---|---|---|---|
| `write_id` | `utf8` | `''` | The write key of the attempt that wrote the row (`^\d{20}-[0-9a-f]{32}$`); the literal `sync` for rows written by directory sync (§5.9); `''` (or NULL) for rows written before this protocol. Any other value is damaged (§5.4). |
| `chunk_count` | `int32` | `0` | The document's chunk total `K`, copied onto every row (like `summary`). `0` = unknown. |

`_MIGRATION_COLUMNS` is `dict[str, str]` of SQL expressions, so
`chunk_count` is `"cast(0 as int)"` (F9). No data migration, no backfill: a
legacy row simply keeps the default, and §5.4 says how a legacy copy is judged.

Per DES-057 decision 5 this is upgrade-only. A downgrade is a schema-only
revert; an older build opening a migrated table ignores the columns on read.
An older *writer* (a stale local-mode CLI) writes NULL for both columns (F10);
every reader below treats NULL as the legacy default, and the heal reaps NULL,
so a stale writer degrades to today's behaviour for its own rows and corrupts
nothing.

`page_raw_text` is untouched: DES-057 decision 3 (the `pages` table) is a
separate delivery. §9 says what it must do to stay compatible.

## 5. Specification

Section numbers below are cited by the model's comments.

### 5.1 The write key

`WriteKey` is a frozen value: `stamp_us: int` and `token: str`. Its text form
is `f"{stamp_us:020d}-{token}"` where `token` is 32 hex digits: a class digit
(`f` for a submission-order write, `0`-`7` otherwise; see Tie-break below)
followed by 31 random hex digits from `uuid.uuid4().hex`. Lexicographic
order on the text is numeric order on the stamp, with the token as tie-break.
It is generated once per attempt. A retry is a new attempt with a new key;
the heal removes whichever of the two is older.

`WriteKey` **validates** its stamp: `0 <= stamp_us < 10**20`, else `ValueError`.
A twenty-digit field cannot hold more, and a negative or oversized stamp would
sort wrongly as text; a refused value never reaches a predicate. The text form
is parsed with `^\d{20}-[0-9a-f]{32}$` and nothing looser.

**Two thresholds, deliberately apart.** `Settings.stamp_skew_allowance_seconds`
(default **300 s**, bounded `0..3600`) defines both:

- the **write ceiling** `now + allowance`: `WriteKey.clamped(stamp_us, now_us)`
  is the only way a door builds a key, and it returns the key for
  `min(stamp_us, now_us + allowance)`;
- the **damage threshold** `now + 2 * allowance`: a *stored* key (or a legacy
  `ingestion_timestamp`) beyond it is damaged (§5.4).

The gap is the point. A stored key written an instant ago at the ceiling is
still below the damage threshold a moment later, so an honest stamp never becomes
"damaged" by the clock moving. And any stored stamp that counts is at most
`now + 2a`, so a submission write that needs `stored + 1` may exceed the write
ceiling; that case fails loudly (§7) instead of tying. Why 300: the stamp is the
capture instant on some host, and the clock error among one operator's machines
is NTP-class (milliseconds to seconds), so five minutes is two orders of margin
while bounding how far a bad stamp (a stepped clock, a corrupt `captured_at`)
can climb. A larger value widens the misordering window of §7 and the damage a
bad stamp can do; a smaller one clamps honest hook stamps more often (clamping
loses ordering precision, never data). The existing capture path already clamps
a future `captured_at` to now (`CaptureSnapshot`) and keeps doing so; the key
clamp is the general backstop for URL, backfill, and every other door.

**Tie-break.** The token's first hex digit encodes the class of the write: a
**submission-order** write (remember, learn, inline, file) draws its token with
first digit `f`; every other write draws `0`-`7`. At an equal stamp, text order
therefore puts a submission write above any other write and never below, so a
tie can only ever delete a non-user-text copy. Two submission writes with equal
stamps tie on the rest of the token. **Residual:** two submission writes to one
name at the same microsecond from two processes, with different text, are not
equivalent: the heal keeps the larger token and the other writer's text is lost.
Inside the daemon the FIFO serialises them (and the second takes `stored + 1`,
§7), so this needs two processes and a one-microsecond coincidence; it is stated
and not prevented. The text form still matches `^\d{20}-[0-9a-f]{32}$`.

### 5.2 The spool

`PageSpool` is a context manager owning `tempfile.TemporaryFile(dir=<dir>)`,
where `<dir>` is `settings.lancedb_path.parent / "spool"` (same filesystem as
the database, never tmpfs). It writes one Arrow IPC stream with the table
schema, `write(batch)` per window, and `reader()` returns a
`pa.RecordBatchReader` positioned at the start.

Directory rules, because the spool holds document text and vectors:

- Created `0o700`. An **existing** directory is verified: owned by the
  effective user, and `chmod 0o700` if its mode is wider (a directory made by an
  older tool, or by `umask`, is repaired, not trusted). A directory owned by
  another user is a refusal to spool (`OSError`), never a fallback to `/tmp`.
- The file is anonymous where the platform allows: Python 3.13 on Linux opens
  it with `O_TMPFILE`, so it never has a name. On macOS `TemporaryFile` is
  `mkstemp` then `unlink`; for that instant the file has a name inside the
  `0o700` directory and is readable only by its owner. A crash in that instant
  leaves one orphan file there. The daemon-start sweep removes a file in the
  spool directory **only if it is older than 24 hours** (`mtime`). The directory
  is **not** the daemon's alone: a local-mode CLI writing in the same store
  spools there too, and a live writer's file (named on macOS, anonymous on
  Linux) must never be removed from under it, so "any file is stale at start" is
  false. 24 hours is far above the longest write the disk-space pre-check and the
  row budget allow, and below the time an orphan could matter for disk use.
- Free space is checked before the first window: `shutil.disk_usage(<dir>).free`
  must exceed twice the estimated spool bytes (rows × (vector bytes + text
  bytes) from the chunk list), else the write fails before embedding with
  `OSError(ENOSPC)`; a mid-write `ENOSPC` is the same fault later (§5.6).
- Order of close: the reader handed to `add_generation` is fully consumed, or
  explicitly closed, **before** the spool's `with` exits, on every path
  (`try/finally` inside the context), so the commit never reads a closed file.

Elsewhere the file is anonymous, so a crash, a SIGKILL, or power loss leaves
**nothing** to sweep. `with` closes it on every exit path including
`BaseException` (`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`), which
closes defect 4. Memory use is one window; disk use is §12.

### 5.3 The protocol

```text
write(document, intent, stamp):          # intent: CREATE | CAPTURE_OVERWRITE | SUBMISSION_OVERWRITE
    chunks = chunk(document)                          # as today; zero chunks -> skip, unchanged
    SyncOwnership.require_funnel_owned(name, collection)   # §5.9; RefusalError, before any embed;
                                                           # a lookup that raises is a refusal too
    verdict = StoredCopy.of(name, collection)         # one narrow read; §5.4
    if intent is CREATE and verdict.held:    return Skipped("exists", verdict.name)
    if intent is CAPTURE_OVERWRITE and verdict.stamp >= stamp: return Skipped("superseded")  # §7
    if intent is SUBMISSION_OVERWRITE:                                                      # §7; never skipped
        stamp = max(stamp, verdict.stamp + 1)
        if stamp > now + allowance: raise RefusalError("stored copy is stamped too far ahead")
    key = WriteKey.clamped(stamp, now, allowance)     # min(stamp, now + allowance); validates 0 <= stamp < 10**20
    with PageSpool(...) as spool:
        for window, vectors in stream_batches(chunks):        # bounded; as today
            spool.write(rows(window, vectors, key, K=len(chunks)))   # RowBudget enforced here
        verdict = StoredCopy.of(name, collection)             # re-check; optimisation only
        if intent is CREATE and verdict.held:    return Skipped("exists")
        if intent is CAPTURE_OVERWRITE and verdict.stamp >= stamp: return Skipped("superseded")
        with store.write_lock():                              # the one in-process write lock
            SyncOwnership.require_funnel_owned(name, collection)   # re-check, atomic with the add
            store.add_generation(spool.reader())              # ONE commit
    heal(name, collection)                                    # §5.5; never fails the write
    drop_twins(...)                                           # url_sourced only, as today
```

Properties of this shape that the model checks:

- Before `add_generation` no row of the name changes. A failure, a crash, or an
  interrupt anywhere before it leaves the table exactly as it was. **No
  cleanup exists to get wrong**, and none of it deletes by name (I4).
- `add_generation` makes a generation visible only whole (F1, F2).
- The heal is the only delete, it is bounded by a complete key, and it is
  idempotent.
- The held and superseded re-checks are an optimisation (they save an add and
  a heal). The protocol is correct with them removed; the model leaves
  `BeginOverwrite` enabled in a state where `SkipSuperseded` is also enabled to
  prove that. The **ownership** re-check is not an optimisation: it is the
  model's guard on `Add`, and it runs inside the lock sync's inserts also take
  (§5.9).

`add_generation` creates the table when absent: `create_table(schema=...)` with
no rows, the FTS index, then the add (F11). That replaces the
create-with-first-batch special case in `ChunkTable.get_or_create`.

### 5.4 The held check: `StoredCopy`

One narrow read (`select write_id, chunk_count, chunk_index, page_number,
total_pages, ingestion_timestamp` where name and collection; vectors never
loaded) classifies what the store holds under a name. Rows group by
`write_id` (NULL read as `''`):

| Group | Classification |
|---|---|
| `write_id` matches `^\d{20}-[0-9a-f]{32}$`, `count == chunk_count > 0`, and `chunk_index` is `0..K-1` | **complete** generation, stamp from its key |
| `write_id` matches that pattern, anything else | **damaged** (an external delete or a mixed-version writer can make this): never held, never a stamp |
| `write_id == 'sync'` | **sync-owned**; not a funnel generation; §5.9 |
| `write_id` is `''` or NULL | **legacy**; see below |
| any other `write_id` text | **damaged** (a malformed key is data, never trusted, never parsed for a stamp) |
| a well-formed key whose stamp is **beyond `now + 2 * allowance`** | **damaged: future** (a stepped clock or a corrupt row): never complete, never held, no stamp, reapable once a complete generation exists |

`held` is true when there is a complete generation, or an acceptable legacy
copy. Never "any row exists". Predicates built from a stored `write_id` still go
through `escape_sql`, even though the pattern admits no quote: the pattern is a
classification, not a sanitiser.

**Only some copies contribute a stamp.** A stored stamp (for ordering, §7) comes
from a complete generation or from a legacy copy that passes `held`, **and
only if it is not beyond `now + 2 * allowance`**. A damaged generation, a
future-key generation, a legacy copy judged damaged below, and a legacy copy
whose newest `ingestion_timestamp` is beyond `now + 2 * allowance` contribute
**none**; with no contributing copy the stored stamp is absent and nothing can
supersede the write. This closes the far-future hole: a stored key of
`10**20 - 1` can neither supersede a later capture nor push a submission-order
stamp up (§7), and it is deleted by the heal of the next write that completes. A
stored key between `now + allowance` and `now + 2 * allowance` is *valid*: it
counts as a stamp, and it is the case §7's loud refusal exists for.

**Legacy copies.** A copy with no `chunk_count` cannot be proven complete: a
prefix of windows, committed in order, is contiguous and looks exactly like a
shorter document. The design says so rather than pretend. Policy:

- *Provably damaged* (not held): `chunk_index` has a gap (the chunker emits a
  contiguous, document-global sequence, DocumentStreamer docstring); or the
  fetched-page copy fails the **page-coverage test**: its last chunk's
  `page_number < total_pages`. Coverage is necessary, not sufficient (a window
  boundary inside the last page passes), and is applied **only to URL-sourced
  names**, whose remedy (re-fetch and replace) never destroys unrecoverable
  text.
- *Unverified* (held): everything else. Labels (`remember`, `learn`) get no
  coverage test: replacing a label because a heuristic doubted it would destroy
  the only copy of the user's text. A legacy duplicate (a chunk index twice)
  is complete-looking and held; the next overwrite collapses it (F5).
- A new write supersedes any legacy generation (`''` is older than every real
  key; the NULL case likewise). The one comparison that matters for ordering,
  against a legacy copy's `ingestion_timestamp`, is §7.

Callers of "is it stored" all go through `StoredCopy`: `ExistingPage.page`,
`.label`, `.capture`, `LegacyUrlNames.stored_as`, `ChunkCatalog.document_exists`,
`StoredSessions` (backfill's currency test), `daemon/routes/captures.py`'s
lookup, and the bulk dedup (`SafeEntrySelector`). A held name whose copy is
legacy-unverified is reported as held, as today; the doctor (PR4) lists the
unverified population.

### 5.5 Heal

```text
heal(name, collection):
    if SyncOwnership.owns(name, collection): return      # §5.9 (a): first step, every caller;
                                                         # a lookup that raises -> log, return
    verdict = StoredCopy.of(name, collection)
    if verdict has no complete generation: return        # never delete when nothing is complete
    newest = max key among complete generations
    verdict = StoredCopy.of(name, collection)            # re-verify newest, immediately before the delete
    if newest is no longer complete: return
    delete where name and collection and write_id <> 'sync' and
          (write_id IS NULL OR write_id < newest             # older generations and legacy
           OR (write_id <> newest and write_id is a damaged generation))   # damaged, any key
```

The damaged clause is expressed as the exact set of damaged `write_id` values
the read just classified (`write_id IN (...)`, each value escaped), not as a
pattern: the delete removes what was observed damaged (partial, malformed, or
future-key) and nothing newer.

- Complete-bounded: the delete cannot remove a copy that has no complete
  successor. A legacy copy with no complete newer key stays (I2). Damaged
  generations are removed **only** when a complete generation exists, and the
  newest complete generation is never in the delete set.
- Never sync: every delete carries `write_id <> 'sync'`. Sync-owned rows are
  outside the funnel's reach by predicate, not by luck (§5.9; model:
  `syncTouched`).
- Race-safe without a lock: `newest` was complete when observed and only a
  heal with a larger complete key can remove it. **Residual:** the re-verify and
  the delete are two LanceDB calls. If an external fault damages `newest`
  inside that millisecond, the delete still removes the older complete copy and
  no complete copy remains. The first model run found exactly this, with a
  separate observe and delete step; with re-verify modelled as one step with
  the delete the property holds, and this double fault (an external delete
  *during* a heal) is stated, not claimed away. The doctor reports a name with
  no complete generation.
- Idempotent and cheap (two small queries, 0.04 s delete on 620k rows). On a
  Lance commit conflict (a concurrent `optimize` compaction, another heal) the
  heal retries **once**; a second conflict is logged and left for the next
  touch.
- Runs for the writer after its `add`, on any held check that finds more than
  one complete generation or a damaged one, and in the sweep (§5.8).
- A failed heal (any `Exception`) is logged with class and frames, never the
  message (a store's message quotes a predicate), and does not fail the write:
  the page is stored; the duplicate is reaped on the next touch. The same
  boundary rule `LegacyTwins` follows.

### 5.6 Failure matrix

| Step | Fault | State after | Recovery |
|---|---|---|---|
| chunk / embed / RowBudget | exception or interrupt | table unchanged; spool closed | none needed; error propagates |
| spool write | `ENOSPC`, `OSError` | table unchanged; spool closed | none; error propagates |
| `add_generation` | exception | table unchanged (F1); orphan fragment files unreferenced | existing `optimize` cleanup reclaims them |
| process dies before the commit | SIGKILL, power | table unchanged (F2); anonymous spool gone | none |
| process dies after the commit, before the heal | SIGKILL, power | new generation visible beside the old one | next touch or the start sweep heals |
| heal delete | exception | stored; old copy still visible beside it | logged; next touch heals |
| two writers, one name | any interleaving | newest key survives (F12; model `inversion` unreachable) | the later heal reaps |
| name owned by sync | funnel write | `RefusalError` before embedding; table unchanged | none; fixed reason |
| a row of a generation deleted externally | any time | generation damaged, never held, contributes no stamp | the next heal with a complete generation removes it; a write replaces it |
| external damage to the newest copy *during* a heal | millisecond window | residual double fault (§5.5): possibly no complete copy | doctor reports it; re-ingest |

### 5.7 Search and list visibility

In-flight rows live in the spool and are visible to no reader: there is no
partial state in `chunks` to hide, so **no reader changes** (I1 by
construction; the table holds only complete generations). `search`, `list`,
`show`, `count`, the held check, and FTS/vector queries see a document the
instant its `add` commits.

The transient is the duplicate: between a replace's `add` and its heal (two
queries, milliseconds), and after a crash in that gap until the next touch, a
name has two complete generations, identical in schema and complete in content.
Search can return a chunk of both. This is the one place the protocol is
weaker than "exactly one copy" (I1 in the brief). §13 D1.

### 5.8 Crash-recovery sweep timing

`GenerationSweep` heals every name that has more than one generation. It runs:

1. **On touch**, always: after every write (5.5), and when a held check
   (5.4) sees a second generation or a damaged one.
2. **At daemon start**, once, in the background after the table opens and
   migrates: one narrow scan (F7, 0.7 s per 620k rows), group by
   `(collection, document_name, write_id)` in Python, heal each name that
   has several generations. It deletes only what has a complete newer key.
3. **Never** a timer. A crash cannot make the table inconsistent, only
   duplicated, and a duplicate heals on first touch. There is no
   "grace period" to tune, because there is no staged state in the table.

The sweep skips every name that holds any sync-marked row or is registry-owned
(§5.9) and, per name, uses the heal of §5.5, including its single retry on a
commit conflict. The spool needs no sweep on Linux (anonymous); at daemon start
the sweep also removes spool files older than 24 hours (the macOS `mkstemp`
instant, §5.2), and no younger file. Orphan fragment files from an interrupted `add` are
unreferenced and are reclaimed by the existing `optimize` cleanup (DES-057
decision 4's finalize).

### 5.9 Ownership: the funnel and directory sync never share a name

Directory sync writes the same table through `SingleFileFlushTarget` with the
registry watermark and `delete_document_tail`. Names are **not** disjoint by
construction: a funnel file ingest names a document by its path-derived name,
which can equal a sync relative-path name. Nothing enforces separation today,
and the heal's "delete older" would delete a sync file's rows if a funnel
generation ever appeared beside them. So ownership is an explicit relation with
four rules, all enforced in code and all in the model (`reg`, `g0sync`, rows of
class 4, `Register`, `Purge`, `SyncInsert`):

- **Owned.** `SyncOwnership.owns(collection, name)` is true when the registry's
  `files` table lists the name, or the registry's `retained_documents` table
  does (rule d), or any row of the name has `write_id = 'sync'`.
- **Marker.** Every row sync writes carries `write_id = 'sync'` and
  `chunk_count = 0`. `'sync'` is not a write key, sorts above every key
  (`'s' > '9'`), and every heal delete carries `write_id <> 'sync'` anyway.
  Rows sync wrote earlier stay `''` and are protected by the lookup, not by the
  marker (see Migration below).
- **(a) Every funnel entry point asks first.** `heal()` itself calls
  `SyncOwnership.owns` as its first step, so the writer's own heal, a heal
  triggered by a held check that found a second generation, and the sweep all
  pass through it. Owned, or the lookup raised: **no delete**.
- **(b) A funnel write to an owned name is refused**, whatever its intent
  (create, capture overwrite, submission overwrite): `RefusalError` with the
  fixed reason `document is managed by directory sync` (`document belongs to a
  retained directory registration` when the only claim is the retained record,
  see (d)), before any embedding
  (top of §5.3) and again at the commit, inside the one in-process write lock
  that sync's `insert_records` also takes. The reason carries no caller text
  (DES-057 decision 6 layer C): the name travels in structured fields only.
  This is the rule that covers the file-ingest collision.
- **(c) Order reversal: sync takes the name over, but only a file it can
  prove is the same file.** A funnel copy exists and the directory is registered
  later. Sync's **first** write of a name (a file with no `files` row yet) looks
  at the rows under the name. If **every** row is file-sourced from this same
  source path (`document_path` equals the file's absolute path: a funnel
  `ingest file` of that very file), sync calls `delete_document(name)` and
  proceeds, with one INFO line per name ("directory sync took over a
  document"), the name redacted as everywhere. If the rows are anything else (a
  label from `remember` or `learn`, a capture, a URL page: `document_path`
  empty, a URL, or another path) it is a **conflict**: sync **skips the file**,
  writes no `files` row (so the name stays unowned and the funnel can still
  write it), records the conflict once in a registry table
  `sync_conflicts(collection, document_name, path, recorded_at)`, logs one
  WARNING with the name redacted, and `quarry doctor` **lists the conflicts with
  the names shown** (the doctor is the one place a name is displayed). The
  funnel copy stays. Sync retries the file only when the file or the copy
  changes. *This rule is the coordinator's interim ruling; the operator may
  override it at ratification.* Why: sync deleting a user's `remember` text
  because a directory happened to hold a file of the same name is user-text loss;
  "the registered directory is authoritative" is true of files, not of notes.
  Model: `SyncInsert` is enabled over funnel rows only when `fsrc` holds,
  otherwise `SyncConflict`; `labelLost` (a takeover over a non-file copy) is
  unreachable in mode 0 and is reached in mode 1 by the unconditional takeover
  the earlier draft had.
- **(d) Deregister with the data kept: the names stay sync-owned.** The
  registry already records a retained collection (`retained_collections`,
  `sync_schema.py`); that is collection-level and says nothing about which
  documents in it sync wrote. **Chosen: a `retained_documents(collection,
  document_name)` table in the same `registry.db`**, populated from the `files`
  rows in the **same transaction** that retains the collection, so the names are
  recorded before the rows that name them are dropped. `owns` consults it. A
  name leaves it when it is re-adopted (a `files` row for it exists again), when
  it is deleted explicitly (`quarry delete`, or a deregister that purges), or
  when the collection is purged.
  - **Backfill for collections already retained by earlier builds.** Those
    have no `retained_documents` rows. A one-time step, `RetainedBackfill`, run
    once at daemon start after the registry and the table are both open and
    recorded as done in the registry, reads each `retained_collections` row and
    inserts, with `INSERT OR IGNORE` (so a re-run, or an interrupted run, changes
    nothing twice), the distinct `document_name` of the Lance rows of that
    collection that pass the rule below. Marked rows (`'sync'`) need nothing: the
    marker covers them. It reads one narrow scan per retained collection (F7) and
    writes only the registry.
    - **A row qualifies only if** its `write_id` is `''` or NULL **and** its
      `document_path` is an absolute filesystem path: it starts with `/` (or the
      platform's absolute form), has no URL scheme (`scheme://`), and contains no
      NUL. A URL, a relative path, and an empty path (a label) never qualify.
    - **Containment is by path component, not by string prefix.** Both the
      `document_path` and the collection's `original_directory` are normalised
      (`os.path.normpath` on absolute paths; no symlink resolution, because the
      directory may no longer exist) and compared as component tuples: the path
      is under the directory when the directory's components are a prefix of the
      path's components. `/data/proj` does not contain `/data/project/a.md`.
    - **When `original_directory` is NULL**, a name is claimed **only if every
      row of that name** has a qualifying absolute filesystem `document_path`
      (and `write_id` `''`/NULL). A name with any URL, relative, or empty
      `document_path` row (a URL page, a capture, a label) is **not claimed**: it
      is recorded for the doctor to list, with the reason, instead.
    - **Over-claiming is conservative.** A funnel file-ingest copy under the
      directory (an `ingest file` of a file that lives there) is indistinguishable
      from a sync row and is claimed. The consequence is a **refusal, never a
      delete**: the funnel will not write it, and nothing removes it. Two ways out:
      `quarry delete` the name and re-ingest it, or re-register the directory,
      whose sync first-write finds the rows file-sourced from the same path and
      takes the name over (rule c), which resolves it.
    - **The refusal text is accurate for this case.** A name owned only through
      `retained_documents` raises `RefusalError` with the fixed reason `document
      belongs to a retained directory registration`; a name owned through `files`
      or the `'sync'` marker keeps `document is managed by directory sync`. The
      doctor shows, per refused name, which of the three records owns it, so the
      remedy (`quarry delete`, then re-ingest; or re-register the directory) is
      one command away. Neither text carries a name.
    - **Test.** A store with a retained collection of: unmarked sync rows under
      the directory (claimed), a sibling directory whose name is a string prefix
      of the directory's (`/data/proj` and `/data/project`: not claimed), a
      funnel label, a funnel URL page, and a capture. After the step the sync
      names refuse the funnel; the sibling, label, URL page, and capture do not.
      The same collection with `original_directory` NULL: the URL page and the
      capture stay unowned and are listed for the doctor, a name whose every row
      has an absolute path is claimed, a name with one absolute row and one URL
      row is not. A second run adds no row. The refusal reason for a
      retained-only name is the retained text; for a `files` name the sync text.
  - **Delete order.** `quarry delete` (and any delete of a document by name)
    clears `retained_documents` **before** the Lance delete. A crash between them
    leaves rows of a name that is no longer owned, which the retried delete
    removes; the other order leaves a name that is owned but empty and refuses
    the funnel until the doctor intervenes. The doctor lists owned-empty names
    anyway.
  - **Crash consistency of deregister.** The keep-data deregister is one SQLite
    transaction (retain the collection, insert `retained_documents` from
    `files`, delete the `files` rows): a crash leaves it registered or fully
    retained, never between, and Lance is not touched. A purging deregister marks
    `pending_purge_collections` first (existing), runs the Lance delete, and
    removes the registry rows in the transaction that completes the purge; an
    interrupted purge is finished by the existing reconciler and the names stay
    owned until it does.
  - Why a table and not the marker: the marker cannot cover rows sync wrote
    before this change (they are `''`, and rewriting them is the migration this
    design declines), and `files` rows are exactly what deregister deletes. Why
    not "refuse the whole retained collection": it would also refuse funnel
    documents that live in that collection and were never sync's. The model
    collapses `files`, retained, and marker into `reg` and rows of class 4;
    `Purge` is the exit.
- **Lookup failure fails closed; an absent registry is not a failure.** If
  `registry.db` does not exist there are no registrations (a store never
  synced, local mode on a fresh database): the registry part of `owns` is
  **false**, and marked sync rows are still caught from Lance by the marker. If
  it exists but is locked, corrupt, unreadable, or lacks a table `owns` needs,
  `owns` raises: the funnel write is **refused** (the same refusal path, logged
  with class and frames, never the message), and `heal()` and the sweep **skip
  the name** and log the same way; nothing is deleted on a lookup failure. A
  failed lookup is never read as "not owned". (Model: `LookupFails` is the only
  transition for a failed lookup; it changes no row. The model cannot show an
  implementation does not fail open: test 11 does.)
- **Sync keeps its own rules.** `delete_document` and `delete_document_tail` are
  unchanged: sync owns its names and may delete them by name.
- **The model's sync is stronger than the code.** `SyncInsert` decides the
  takeover and writes in one step; the code reads the rows, deletes, and inserts
  as separate calls. A crash between the delete and the first window leaves the
  name with no funnel rows and no sync rows and no `files` row (nothing lost that
  was not already the file's to replace); a crash between windows leaves a
  partial sync document, which the registry watermark resumes. The model does not
  show either.
- **Migration: choose lookup, not rewrite.** Existing sync rows carry `''`
  today. A one-time `table.update(write_id='sync')` at migrate would rewrite
  every sync row (most of a multi-GB table; 0.13-0.37 s per 20k rows in e10, so
  minutes to tens of minutes), hold the write lock, and risk a half-migrated
  table if interrupted. The lookup needs nothing from the table, so it is the
  choice: rows sync writes from now on carry the marker, and the unmarked
  remainder is protected by `files` and `retained_documents` (and the backfill
  above for collections retained before this change). Model: `g0sync` rows are
  protected by the heal-start check, and `syncTouched` covers them.
- **Residual: registry change between the check and the commit.** The registry
  read is not in the Lance commit. A directory whose file first writes after a
  funnel write passed its check and before its `add` lets one funnel generation
  land on a name that is then owned; sync's first write of the name takes it over
  only if the copy is file-sourced from the same path, otherwise it records a
  conflict and the funnel copy stays unowned. A heal that already passed its
  ownership check deletes only funnel rows that existed at that time. The doctor
  flags a name that holds both sync-marked and funnel-keyed rows.

## 6. Model-check results

`docs/design/atomic-page-write.spec.tex`; run with `z-spec check` (fuzz) and
`probcli` on the same file. **Scope: every claim below is about this model and
nothing wider.** The model has one document name and these actors: two funnel
writers; the directory-sync writer (window-by-window marked rows; its first
write of a name, which is a takeover only over funnel rows file-sourced from the
same path and a skipped conflict otherwise; a delete by name); the operator
(`Purge`, a deregister that deletes the data; registering claims no name until
sync writes the file, and a keep-data deregister leaves the retained record, so
neither is an operation); and an environment that deletes one row of a funnel
generation. `K = 2` chunks. Row classes: `0` legacy funnel rows, or, while
`g0sync`, the **unmarked sync rows** an earlier build wrote; `1` and `2` ordinary
keys, key `2` doubling as the **write ceiling**; `3` a **future-key** generation
(beyond twice the allowance, always damaged); `4` sync-marked rows. State
carries `reg` (the registry or retained set lists the name) and `fsrc` (funnel
rows, if any, are file-sourced from sync's path). Writers may begin with any
ordinary key regardless of begin order, fail, fail closed on a lookup
(`LookupFails`), be refused (`RefuseSync`, and `RefuseCeiling` for a submission
write above a copy at the ceiling); the heal's observe and its
re-verify-then-delete are separate steps; the heal starts only on an unowned
name. The initial states are every combination of ownership, unmarked sync rows,
file-sourced or not, a legacy copy, a future-key copy, and marked sync rows
(108 states).

What the model does **not** claim, in addition to the spec's §1 list: row
content and memory (I5); legacy clean-prefix partials; the legacy
`ingestion_timestamp` comparison; skew *within* the allowance; the token class
digit (keys are distinct in the model, so equal-stamp ties are argued in §5.1
and §7 and tested, not model-checked); that the code's check-delete-insert
takeover is as atomic as `SyncInsert`; the registry read and the commit as one
atomic step across processes; an older-build writer; that the registry lookup
fails closed (the model has one transition for a failed lookup and it changes no
row, but an implementation that failed open would not be caught: test 11 does
that); that the delete predicate in the implementation equals the one the model
writes (tests 5 and 5a do that); and liveness beyond a possibility claim.

**What the model found, in two rounds.** (1) The first sync-aware run reached
`lost`: a healer observed key 2 complete, the environment removed one row of key
2, and the healer's delete then removed the older complete copy too. The heal
now re-verifies the observed generation immediately before the delete. (2) The
ownership round added the registry, unmarked sync rows, and `Register`/`Purge`;
an unguarded heal is exactly what `syncTouched` would catch (mode 1 reaches it),
and the guards in mode 0 (ownership checked at heal start and at `Add`, never
class 4, never `g0sync` rows) leave it unreachable.

**Mode 0 (this design), full exploration** (`-p MAX_INITIALISATIONS 400`):

| Check | Result |
|---|---|
| `z-spec check` (fuzz) | OK |
| Reachable states / transitions | 233,893 / 2,523,482, all visited, no warning |
| Deadlock in reachable states | none |
| Operations covered | all but the mode-1 operations (`RefuseCeiling`, `SyncConflict`, and every mode-0 operation fire) |
| Goal `partialVisible ∨ lost ∨ unsafeDelete ∨ skippedOnPartial ∨ inversion ∨ syncTouched ∨ syncClash ∨ labelLost = ztrue` | **never reached** (`No counter example found. ALL states visited.`) |
| Convergence, `AG(EF(quiescent ∧ at most one complete generation ∧ (one complete ⇒ no damaged funnel generation and no future-key generation)))` | **TRUE** (CTL, 549 s). A *possibility* claim: a clean state is reachable from every reachable state; it does not say every execution gets there |
| Transient duplicate (two complete generations at once) | reachable, as stated in §5.7 |
| Stale writer's generation next to a newer complete one | reachable; the heal that follows deletes keys below the newest, which removes the stale one |

The witnesses: `partialVisible` (I1: a protocol step produces a partial
generation), `lost` (I2), `unsafeDelete` (I4), `skippedOnPartial` (create over a
partial copy), `inversion` (I6: the newest complete key goes backwards),
`syncTouched` (a funnel, heal, or sweep step changed marked sync rows, or the
unmarked ones while `g0sync`) and `syncClash` (a funnel step added a generation
to an owned name) and `labelLost` (sync's takeover deleted funnel rows that were
not file-sourced from its path). The ownership witnesses are checked against `Add`'s guard,
the heal-start check, and the heal's exclusion of class 4; they show those rules
sufficient *as modelled*, not that the SQL predicate is right.

**Mode 1 (the funnel as it was), each witness reached in at most ten steps
(`-nodead`; the traces for the first seven are from the previous round, the
operations being unchanged apart from frames, and the combined goal was re-run
and is reached in one step; `labelLost` is new):**

| Witness | Property | What the ProB trace shows |
|---|---|---|
| `partialVisible` | I1 | `OldBeginOverwrite`, `OldInsert(wb)`: a half-written document under the real name |
| `lost` | I2 | `OldBeginOverwrite(2, wa)` over a complete legacy copy: the only complete copy is deleted before the new one exists |
| `unsafeDelete` | I4 | `OldBeginOverwrite(1, wb)`: a delete that nothing supersedes |
| `skippedOnPartial` | create-if-absent | `OldSkipHeld(wb)` from a start with a partial copy, one step |
| `inversion` | I6 | `OldSkipHeld`, `Purge`, `OldBeginOverwrite(2, wb)`: an overwrite removes the newest complete copy and compares nothing |
| `syncTouched` | ownership | `OldBeginOverwrite(1, wa)` from a start where sync's unmarked rows are present: delete-by-name removes them |
| `syncClash` | ownership | `OldBeginCreate`, `OldFail`, ..., `OldInsert(wb)` into a registered name: the funnel writes into a name sync owns |
| `labelLost` | ownership | `OldSkipHeld`, `SyncDelete`, `OldSyncTakeover`: the earlier unconditional takeover deletes a copy that is not file-sourced from sync's path (mode-1 operation `OldSyncTakeover`; the mode-0 `SyncInsert` is enabled over funnel rows only when `fsrc`) |

`z-spec test` cannot be the saved report: it stops probcli at 600 s, and this
model needs about 900 s and `-p MAX_INITIALISATIONS 400` (probcli's default of 4
silently truncates the first transition set; one run of an earlier version
printed that warning and was discarded). `atomic-page-write.spec.report.json` is
written by hand from the direct `probcli` runs, with the commands in its `note`.
Two items in it are expected, not defects: the mode-1 operations never fire in
mode 0 (coverage), and the constraint-based deadlock probe returns a degenerate
state with every function empty, which is not reachable; the reachable-state
search found no deadlock.

## 7. Ordering and the compare-and-swap (I6)

Two kinds of write, with different ordering rules. The distinction is the
**intent**, not the door.

| Intent | Writes | Key stamp | May be "superseded" |
|---|---|---|---|
| **Capture-stamped** | hook captures (WebFetch, transcripts), `backfill-sessions`, URL ingest, bulk and sitemap | the instant the content was captured: `captured_at` (snapshot), the backfill's own read instant, the instant the fetch returned | yes |
| **Submission-order** | `remember`, `learn`, inline-content ingest, file ingest | `max(now, stored_stamp + 1)`, where `stored_stamp` is the newest stamp §5.4 allows (so never beyond `now + 2 * allowance`); **refused** if that exceeds `now + allowance` | never |

**Capture-stamped.** The key's leading component is the capture instant, so
**key order is capture order** and the heal's "newest complete key wins" is
last-writer-wins by capture time. That, not the check, is what makes I6 hold: a
stale writer that does *not* check still has its generation reaped by its own
heal (model: `inversion` is false with `BeginOverwrite` enabled over newer
data). The explicit compare-and-swap is the early exit and the report: an
overwrite whose stamp is **older than or equal to** the stored stamp is skipped,
before embedding and again before the commit, and reported as
`{"chunks": 0, "skipped": "superseded"}` (`IngestResult.skipped`,
`results.py:34-35`, gains this value beside `"exists"`). An equal stamp is the
same capture again, and skipping it removes the one tie in which a random token
could delete a stored copy; a replay that must **repair** a damaged copy is not
affected, because a damaged copy supplies no stamp (below). `RefusalAwareTerminal` already counts any non-empty
`skipped` as not-a-failure (`refusal_aware_terminal.py:66`); a test pins that
for `"superseded"`.

**Submission-order.** These writes carry no instant that means "the content as
of". The caller's intent is "this, now". They are **never** skipped as
superseded: the compare-and-swap does not apply, and `skipped: "superseded"` is
unreachable for them. To keep a later request winning despite a backward clock
step or a stored stamp ahead of the clock, the key stamp is
`max(now, stored_stamp + 1)`: strictly above the stored stamp, so there is no
tie, and order is monotonic regardless of the wall clock. Because a stored stamp
that counts is at most `now + 2 * allowance` (§5.4) and a write may not exceed
`now + allowance` (§5.1), `stored_stamp + 1` can exceed the ceiling. In that
case the write **fails loudly**: `RefusalError` with the fixed reason `stored
copy is stamped too far ahead`, nothing is written, the stored copy survives. The
alternative, clamping to the ceiling, would tie with or fall below the stored
stamp and could lose to it, or be deleted by it; a random tie-break that can
delete user text is not acceptable, so the write refuses and the operator
resolves it (wait for the clock, or delete the entry). **Equal-stamp ties
between a submission write and any other copy resolve in favour of the
submission write**, by the token's class digit (§5.1: `f` for a submission
write, `0`-`7` otherwise). (Model: `BeginSubmission` picks a key above every
complete key; when the complete copy sits at the ceiling key, `RefuseCeiling` is
the only move: no write, no delete, and `lost` and `inversion` stay false.)
**`RefuseCeiling` is transient.** As `now` advances the write ceiling
`now + allowance` rises past the stored stamp, so the refusal clears within at
most the allowance (default 300 s) and a retry then succeeds. The model abstracts
time away, so in it a copy at the ceiling key stays at the ceiling and the
refusal looks permanent; that is an abstraction of the model, not a liveness
claim, and no property here depends on it.
Inside the daemon the FIFO's submission order is processing order, so two
requests for one name serialise in the order made; across processes the stamp
does.

**Which copy supplies `stored_stamp`.** Only a complete new-protocol generation
(its key's stamp), or a legacy copy that **passes `held`** (§5.4; its stamp is
the newest `ingestion_timestamp` among its rows, which every build stored). A
damaged generation, and a legacy copy judged damaged, supply none. Consequence,
the case the security review named: a capture replayed from the job spool or the
dead-letter queue after an old-build partial copy was left behind carries a
`captured_at` earlier than that partial's rows were written. The partial is not
held, so it contributes no stamp, so the replay is **not** superseded; it
writes a new complete generation and the heal removes the partial. The model encodes the rule: `SkipSuperseded` is enabled only over a *complete*
generation, never over a partial or damaged one, and the heal removes a damaged
generation of any key. Test 7 pins it at the I/O level.

**Residual: mixed-version writers.** An older build still running (a local-mode
CLI, a not-yet-restarted process) writes windows under the real name with NULL
`write_id`. If it overlaps a new-build write of the same name, the new build's
heal can delete the older build's earlier windows (NULL sorts older), and the
older build can delete the new generation by name (`delete_document`). Neither
corrupts the protocol's own invariants (§5.5 reaps NULL; a deleted new
generation is an I2 loss caused by a writer that does not follow the protocol),
but the loss is real for that window. Upgrade-only (DES-057 decision 5) is the
mitigation: the window is the time between migrating the store and the last
older-build writer exiting. The model's `ExternalDelete` stands for this fault,
not an older-build actor.

**Remote-hook skew bound.** A hook running on another host stamps captures with
its own clock. The daemon clamps a future `captured_at` to now and rejects one
older than its floor (`CaptureSnapshot`), but cannot correct skew inside that
range. Two writes to one name within `s` of each other, where `s` is the skew
between hook host and daemon host (and `s` no larger than the allowance, §5.1),
may order wrongly: a hook clock *behind* by
`s` can lose a capture-stamped write to a local one made up to `s` earlier. The
**misorder window is the skew, bounded above by the allowance** (default 300 s):
skew *within* the allowance can misorder two writes; a stamp beyond it is
clamped at write time, and one found stored beyond twice the allowance is
treated as damaged, so it can never dominate. A skew larger than the allowance is a configuration fault the doctor
should surface, not something the protocol can repair.

**`ingestion_timestamp` is not the key.** The column keeps the meaning DES-057
decision 6 gave it: a capture's `captured_at`, otherwise the instant of
chunking. The write key is for ordering only, and a bumped submission-order
stamp does **not** change the column. The consumers of the column, and why none
changes meaning:

| Consumer | Reads | Changes? |
|---|---|---|
| Temporal decay and boost (`retrieval/`, agent-scoped memories) | row `ingestion_timestamp` | no: same column, same values |
| `quarry doctor` last-ingest | newest `ingestion_timestamp` | no |
| `StoredSessions.is_current` (backfill) | the document's `ingestion_timestamp` | the held verdict decides whether a document exists; the timestamp comes from the held generation, same value |
| Insights recency bands (`insights_builder`) | document's ingestion time | no |
| Sync staleness | the registry's own `ingested_at` (a different store) | no |
| `list_documents` / `show` | a per-document timestamp | no; while two generations coexist (milliseconds) the newer is shown |

Create-if-absent (`overwrite=False`) does not use the stamp: held means skip,
whatever the age. At the commit-time re-check a creator that lost a race to
another creator skips; across processes the residual race (check, then add)
ends with both complete and the newer key surviving, a duplicate that heals.

`WriteKey` raises `ValueError` for a stamp outside `0 <= stamp < 10**20`
and every door builds it through `WriteKey.clamped` (§5.1). A capture's `captured_at` has already passed range checks
(`CaptureSnapshot`, `capture_snapshot.py`: not before 2000, clamped to now); the key's check is the
backstop for any other door.

## 8. Rejected alternatives

| Alternative | Why not |
|---|---|
| **A. Stamp column, windows written into `chunks`, finalize by `update`, readers filter unfinalized** | One update commit does finalize (F8), but every reader (about 30 query sites across `chunk_catalog`, `chunk_search`, `retrieval/hybrid`, the connection and name-prefix helpers, the routes, the sync reconciler, `document_exists`) would need the filter; one missed site is an I1 violation, found in review rather than by construction. Costs a double write of every row, and an in-flight window is a visible row until finalized. Largest reader-side complexity of the candidates. |
| **B. Staging name, renamed by one `update`** | Same reader problem (a staging name is a document name that search and list return), same rewrite cost, plus a name-collision and display-name concern. Strictly worse than A. |
| **C. Staging table, single atomic `add` into `chunks`** | The right idea; the wrong container. A Lance staging table pays a version and fragment set per window, needs a sweep for orphans, and its final copy into `chunks` is an `add(reader)` anyway. The anonymous spool is the same staging with no residue and no sweep. This design **is** C with a file. |
| **D. `merge_insert` replace in one commit** | Atomic, and the heal vanishes. But it buffers the whole document (F4: +1.2 GB for 1 GB) and, with by-source delete, reads O(table) (F4: +0.7-0.9 GB against a 1 GB table to replace 12 rows). On the operator's multi-GB store each ingest would transiently map the table. I5 forbids it. |
| **E. Write-ahead roll-forward: delete old, add from a durable spool, recover on restart** | Gives exactly one copy at all times that anything is visible, at the cost of a window with **zero** visible copies (search misses the document) and a durable, recoverable spool: a manifest, a replay state machine, and two crash points to model. Strictly more machinery for a weaker availability story than a transient duplicate. |
| **F. Buffer the whole document and `add` once** | Violates I5; RowBudget bounds a row, not a row count (DES-057 decision 2), so the bound is not provable. |
| **G. Keep windowed commits, fix cleanup (djb-1/2/5 patches)** | The loop that has not converged: cleanup is the problem, and no cleanup is the cure. |
| **H. Dedupe at read time (keep only the newest key per `(name, chunk_index)` in results)** | Would hide the transient duplicate. It is a post-filter in one or two result builders and could be added later without changing the protocol; not needed for I1 (both copies are complete) and adds a comparison to every search. Not taken. |

## 9. Interactions

- **Legacy twins** (`LegacyTwins`, `LegacyUrlNames`). `drop_twins` stays after
  the commit and keeps its current fault handling. `stored_as` must go through
  `StoredCopy`, so a twin that is damaged is not "the page the store holds".
  `replace_prior` is deleted: the delete-before-insert is what this change
  removes. The twin deletes are by different names and cannot touch the new
  generation.
- **Create-if-absent skip** (`ExistingPage`). The skip is now "a complete
  generation or an acceptable legacy copy exists", never "a row exists" (§5.4).
  The skip result and its `document_name` of the held (possibly legacy-spelled)
  page are unchanged.
- **RowBudget** (DES-057 decision 2). `ChunkTable.build_records` remains the
  single record builder and now runs while spooling, so a row, chunk, or label
  over budget fails the write **before the commit**; the whole-document budget
  check before any write is unchanged and no longer has an overwrite-delete to
  protect. The commit reads the spool as a stream (F3), so memory stays at one
  window plus Lance's bounded buffer; disk is the document's row bytes.
- **Collection FIFO.** Correctness no longer depends on it. It still
  serialises embedding (`EMBED_CONCURRENCY_CEILING == 1`, the embed gate), which
  protects throughput and RAM, not the data. A local-mode CLI writing the same
  name beside the daemon is now safe (F12). `_write_lock` (`chunk_store.py:27`)
  is held across the commit only (one streaming add; milliseconds for a typical
  document, about a second per gigabyte).
- **Backfill routed through the FIFO.** Unaffected by queue order (§7). It must
  pass its read instant as the stamp and treat `"superseded"` like `"exists"` in
  its tallies (`BackfillResult.skipped_existing`, `backfill.py:53`), and
  `StoredSessions.is_current` (`stored_sessions.py:45-67`) must read the
  copy's stamp through `StoredCopy` so a partial transcript document is not
  "current".
- **Sync path** (`SingleFileFlushTarget`, `ProgressiveIndexer`, registry
  watermark, `delete_document_tail`, DES-034 section 5.3). Its protocol is
  unchanged; its **ownership** is now explicit (section 5.9). Its rows are
  written with `write_id = 'sync'` and `chunk_count = 0`
  (`ChunkTable.build_records` supplies both); the funnel refuses a name sync
  owns, and the heal and sweep never delete a sync-marked row. Rows sync wrote
  before this change stay `''` and are protected by the registry lookup, not by
  a rewrite.
- **DES-057 decision 3 (pages table), pending.** The pages table must carry the
  same `write_id`; a writer adds its pages *before* the chunk commit and the
  heal deletes superseded pages after the chunks. A crash then leaves only
  orphan pages of a never-committed key, invisible to readers and swept by the
  24 h rule decision 3 already states.
- **`optimize`.** Gains orphan fragment files from an interrupted `add` (F1/F2),
  which its existing cleanup already reclaims.
- **Surfaces** (CLI, MCP, HTTP, plugin). The new `skipped: "superseded"` is a
  value of an existing field, but it is a new observable on every surface; the
  equivalence test of the five bug classes (Class 3) covers it.

## 10. Implementation write-set

New (each ≤ 500 lines, one concept, `__new__`, `@final`, slots):

| File | Content |
|---|---|
| `src/quarry/db/write_key.py` | `WriteKey` (frozen; validates `0 <= stamp < 10**20`; `clamped(stamp, now, allowance)` is the only door constructor; `text`; `parse` against `^\d{20}-[0-9a-f]{32}$`; `is_future(now, allowance)`; ordering) |
| `src/quarry/db/stored_copy.py` | `StoredCopy` (verdict from one narrow read: complete / damaged / legacy; `held`, `newest_key`, `stamp`) and `DocumentGenerations` (the grouping) |
| `src/quarry/db/generation_heal.py` | `GenerationHeal` (observe newest complete, bounded delete, fault boundary) and `GenerationSweep` (startup scan) |
| `src/quarry/ingestion/page_spool.py` | `PageSpool` (anonymous IPC spool; directory ownership and mode check; free-space pre-check; context manager) |
| `src/quarry/ingestion/sync_ownership.py` | `SyncOwnership` (`owns`, `require_funnel_owned`: the registry lookup or any sync-marked row; raises `RefusalError` with the fixed reason; a lookup that raises is also a refusal, and `heal` and the sweep treat it as owned) |
| `src/quarry/ingestion/atomic_page_write.py` | `AtomicPageWrite` (§5.3; replaces `OwnedInsert`) and a `WriteOutcome` (`Stored`, `SkippedExists`, `SkippedSuperseded`) |

Changed:

| File | Change |
|---|---|
| `src/quarry/db/schema.py` | add `write_id`, `chunk_count` to `schema()` and `_MIGRATION_COLUMNS` |
| `src/quarry/db/chunk_table.py` | `build_records` takes the key and `K`; the sync path supplies `'sync'`/`0`; `get_or_create` becomes create-empty + FTS + add |
| `src/quarry/sync_ingest.py`, `src/quarry/ingestion/file_flush_target.py`, `plain_flush_target.py` | sync's flush targets write the `'sync'` marker; sync's own deletes unchanged |
| `src/quarry/db/chunk_store.py` | `add_generation(reader)` (under `_write_lock`, with the ownership re-check), `delete_generations(ref, keys, newest)` (every predicate carries `write_id <> 'sync'`); `insert_records` unchanged for the sync path |
| `src/quarry/db/connection.py`, `src/quarry/types.py` | `RecyclingTable.add` and the `LanceTable` protocol accept a `RecordBatchReader`; `create_table` without rows |
| `src/quarry/db/chunk_catalog.py` | `document_exists` delegates to `StoredCopy.held` |
| `src/quarry/ingestion/chunk_store_funnel.py` | `_embed_and_store` uses `AtomicPageWrite`; `_replace_prior` deleted; the stamp resolved once and given to the chunk labels |
| `src/quarry/ingestion/existing_page.py`, `legacy_url_names.py` | held checks through `StoredCopy`; `replace_prior` deleted |
| `src/quarry/ingestion/owned_insert.py` | deleted (and its tests moved) |
| `src/quarry/ingestion/streaming.py`, `plain_flush_target.py` | `DocumentStreamer.insert` and `PlainFlushTarget` deleted if no caller remains (the funnel was the only one) |
| `src/quarry/ingestion/ingest_context.py` | a `WriteIntent` (`CREATE`, `CAPTURE_OVERWRITE`, `SUBMISSION_OVERWRITE`); `ingested_at` keeps its DES-057 meaning; URL doors set it after the fetch |
| `src/quarry/results.py` | `skipped` documents `"superseded"` |
| `src/quarry/stored_sessions.py`, `src/quarry/backfill.py` | currency through `StoredCopy`; `"superseded"` tallied |
| `src/quarry/daemon/routes/captures.py` | lookup through `StoredCopy` |
| `src/quarry/sync_schema.py`, `src/quarry/sync_registry.py`, `src/quarry/registrar.py`, `src/quarry/registrations.py` | `retained_documents(collection, document_name)` and `sync_conflicts(collection, document_name, path, recorded_at)` tables and their migrations; a registry flag recording the one-time retained-documents backfill; `quarry delete` clears `retained_documents` before the Lance delete; populated from the `files` rows in the transaction that retains a collection (`deregister --keep-data`); cleared on re-adoption, explicit delete, and purge; `owns` reads `files` and `retained_documents` |
| `src/quarry/ingestion/file_indexer.py`, `settled_file.py` (sync's first write of a name) | takeover only when every row of the name has `document_path` equal to the file's path (delete the name, one INFO line); otherwise skip the file, record the conflict, one WARNING |
| `src/quarry/retained_backfill.py` (new) | `RetainedBackfill`: the one-time, idempotent fill of `retained_documents` from unmarked absolute-path Lance rows under each retained collection's `original_directory` (path-component containment, URL and relative paths rejected; a NULL directory claims only all-absolute names and lists the rest for the doctor), run at daemon start; `SyncOwnership` reports which record owns a name so the refusal text is accurate |
| `src/quarry/doctor*.py` | list sync conflicts (names shown) and owned-empty names |
| `src/quarry/config.py` | `Settings.stamp_skew_allowance_seconds` (default 300, bounded 0..3600) |
| `src/quarry/daemon/server.py` (startup) | schedule `GenerationSweep` once, and remove spool files older than 24 hours |
| `DESIGN.md` | an ADR (leader-authored); DES-057 decision 2/6 text on overwrite-delete and cleanup amended |

OO note for the contract: `AtomicPageWrite` replaces a class whose job was
cleanup; `StoredCopy` replaces a free-standing `document_exists`; the move
from procedure to value classes (`WriteKey`, `StoredCopy`, `PageSpool`) is the
real improvement to ask the worker for. The ratchet is debt amortisation:
`chunk_store_funnel.py` and `chunk_table.py` lose methods in this change.

## 11. Test plan

Tests lead; every item fails on the current code first.

1. **Atomicity (real LanceDB, tmp path).** Embed raises on window 2 of 3:
   `chunks` holds exactly the prior rows, spool closed. `add_generation`
   raises: same. `KeyboardInterrupt` in the embedder: same. Run for a URL page
   **and** a label (defect 5).
2. **Crash (subprocess + SIGKILL).** Kill the writer between the last spooled
   window and the commit: zero rows of the new key, prior copy intact, no spool
   file on disk. Kill between commit and heal: two complete generations; the
   next held check heals to one (I3).
3. **Overwrite never loses (I2).** An overwrite whose embed fails leaves the old
   copy and its count identical; a successful one leaves exactly one copy with
   the new content.
4. **Create-if-absent (defect 1, 7).** A new-protocol partial (hand-built:
   `chunk_count = 5`, 3 rows) is not held and is replaced; a legacy copy with a
   chunk-index gap is not held; a legacy URL copy failing page coverage is not
   held; a legacy label with the same shape **is** held; a legacy duplicate is
   held and collapses on overwrite.
5. **Heal safety (I4, F6).** Heal never deletes the only complete generation;
   NULL `write_id` rows (written by a dict-`add`, F10) are reaped; rows of other
   names and collections are untouched; a damaged generation of **any** key
   (including one newer than the newest complete) is deleted only when a
   complete generation exists and never the newest complete one; every delete
   predicate contains `write_id <> 'sync'`.
5a. **Ownership.**
   - *Refusal.* A funnel write (create, capture overwrite, and submission
     overwrite) to a name that holds `'sync'` rows, is listed in the registry's
     `files` with legacy `''` rows, or is in `retained_documents`, raises
     `RefusalError("document is managed by directory sync")` with the embedder
     never called and the table unchanged, on every surface. The text contains no
     name.
   - *File-ingest collision.* A funnel `ingest file` whose path-derived name
     equals a registered sync relative-path name is refused (this is the case
     "disjoint by construction" wrongly excluded).
   - *Heal and sweep.* `heal()` itself consults ownership first: with a name
     owned (marked, unmarked-but-registered, or retained) it deletes nothing, on
     the writer's heal, on a held-check-triggered heal, and on the sweep; every
     delete predicate contains `write_id <> 'sync'`; heal and sweep leave every
     `'sync'` row and every row of an owned name alone.
   - *Takeover.* A funnel copy exists, then the directory is registered: the
     funnel refuses the name at once; sync's first write of the name deletes the
     funnel generations by name and logs one INFO line, with no name text;
     searching before that first write still returns the funnel rows.
   - *Deregister, keep data.* The retained names are recorded in
     `retained_documents` in the same transaction as `retained_collections`; the
     funnel refuses them afterwards; re-adoption (a `files` row again), an
     explicit delete, and a purging deregister each remove the name; sync rows
     written after the change carry `'sync'`.
   - *Backfill.* The one-time step for already-retained collections (§5.9 d):
     idempotent (a second run adds no row), recorded as done, owns exactly the
     unmarked absolute-path rows under `original_directory` by path component
     (a string-prefix sibling is not owned), leaves a funnel label, a URL page and
     a capture unowned, and in a NULL-`original_directory` collection claims only
     names whose every row is an absolute filesystem path, listing the rest for
     the doctor; the refusal text differs for retained-only and for `files` or
     marker ownership.
   - *Takeover and conflict.* A funnel `ingest file` copy whose `document_path`
     equals the sync file's path is taken over (deleted, one INFO line); a
     `remember` label, a capture, and a URL page of the same name are **not**:
     sync skips the file, no `files` row, one `sync_conflicts` row, the funnel
     copy intact and still writable, and the doctor lists the conflict with the
     name.
   - *Delete order.* `quarry delete` clears `retained_documents` before the Lance
     delete (kill between the two: the name is unowned and the retried delete
     finishes); a keep-data deregister is atomic (kill mid-transaction:
     registered or fully retained).
   - *Sync's own deletes.* `delete_document` still deletes sync's rows.
5b. **StoredCopy parsing.** A `write_id` not matching `^\d{20}-[0-9a-f]{32}$`,
   `'sync'`, `''`, or NULL is damaged (never parsed for a stamp); a value
   containing a quote or `%` is escaped in every predicate built from it.
5c. **WriteKey.** Stamps `-1`, `10**20`, and a non-int raise `ValueError`;
   `0` and `10**20 - 1` are accepted and sort numerically as text.
   `WriteKey.clamped` returns `min(stamp, now + allowance)` for every door (the
   write ceiling; the damage threshold is `now + 2 * allowance`); a
   test per door (hook capture, backfill, URL fetch, remember, learn, inline,
   file) shows no key leaves it above `now + allowance`. The token class digit:
   a submission token starts `f`, every other starts `0`-`7`, and both match
   `^\d{20}-[0-9a-f]{32}$`.
5d. **Far-future stamp.** A stored complete generation whose key is
   `10**20 - 1`, and separately a legacy copy whose `ingestion_timestamp` is
   beyond `now + 2 * allowance`, contributes no stamp, is not held (a
   create-if-absent proceeds), and is deleted by the heal that follows the next
   complete write. Then a capture-stamped overwrite and a submission-order write
   each **replace** it: neither is `"superseded"`, and the submission key is
   `max(now, stored + 1)` with `stored` ignored when future.
5e. **Ceiling.** A stored complete generation whose key is exactly
   `now + allowance` (the write ceiling), then `remember` and `learn` of the same
   name: each raises `RefusalError("stored copy is stamped too far ahead")`, the
   embedder is never called, and the stored copy is byte-identical afterwards. A
   stored key at least two microseconds below the ceiling: the submission key is
   `stored + 1`, within the ceiling, and it **replaces** the copy. A stored key
   at `now + allowance + 1 us` (valid, beyond the write ceiling) behaves as the
   ceiling case; at `now + 2 * allowance + 1 us` it is damaged and replaced.
   **Ties:** a submission write and a capture-stamped write with equal stamps:
   the submission generation survives the heal in both arrival orders; a
   capture-stamped overwrite whose stamp equals the stored stamp is skipped as
   superseded. Skew inside the allowance: two captures within it may order wrongly
   and the test pins the documented outcome.
6. **Concurrency (F12).** Eight writers on separate connections, same name,
   shuffled stamps, plus unrelated adds: one copy, the newest key, no error.
   Repeat N times (name the race if it ever fails; never retry).
7. **Ordering (I6).** A capture-stamped overwrite with an older `captured_at`
   than a *complete* stored copy is skipped with `skipped: "superseded"`, before
   embedding (assert the embedder was not called) and again at the commit
   re-check; with the checks mocked out the stale generation is still reaped. A
   newer stamp replaces. An equal stamp is skipped as superseded (§7). A held legacy copy's
   `ingestion_timestamp` is the stored stamp. Backfill queued behind a newer hook
   capture does not replace it. **Damaged copies contribute no stamp:** an
   old-build partial (rows written after the replayed `captured_at`) followed by
   a replay of that capture through the job spool and the dead-letter queue
   **repairs** it (complete generation, partial gone), never `"superseded"`.
   **Submission-order** writes (`remember`, `learn`, inline, file) are never
   `"superseded"`, including with the clock mocked backwards past the stored
   stamp; their key stamp is `max(now, stored + 1)`, or the write is refused at the ceiling (test 5e).
8. **Row budget.** An over-budget chunk or label fails before the commit with
   the table unchanged.
9. **Bounded memory (I5), `slow` marker.** A 200k-row stream through the real
   funnel: peak RSS growth stays under a stated ceiling, and the spool file is
   gone afterwards.
10. **Class 1 (file I/O) failure injection.** `TemporaryFile` creation raising,
    `write_batch` raising `ENOSPC`, close raising: no fd leaked (count fds
    before and after), nothing left in the spool directory, the original error
    propagates.
11. **Class 2.** `StoredCopy`, `GenerationHeal`, `GenerationSweep`, and
    `SyncOwnership` boundaries: make the underlying query raise and assert the
    documented result (held check raises, heal logs class and frames only, sweep
    continues to the next name). **Fail closed:** with `registry.db` locked, with
    a corrupt file, with it unreadable (permissions), and with a table missing, a
    funnel write is refused (the embedder never called, the table unchanged), a
    heal and a sweep delete nothing for that name, and the log line carries the
    exception class and frames, not its message. A lookup failure is never read
    as "not owned". **Absent registry:** with `registry.db` missing (a store
    never synced, local mode on a fresh database) the registry part of `owns` is
    false, the funnel write proceeds, and a name carrying `'sync'`-marked Lance
    rows is still refused.
12. **Class 3 (surfaces).** The `"superseded"` result is byte-identical in local
    and daemon-routed mode for the CLI, HTTP, MCP, and plugin; field names match.
13. **Schema.** Migration of a legacy table adds both columns with `''`/`0`
    (int32); an old-shape dict `add` writes NULL and is read as legacy.
14. **Stress.** Heal against heal (two healers, one name, interleaved
    observe and delete, many rounds) and heal against `optimize` compaction (a
    commit conflict is retried once, the second is logged, the table never loses
    its last complete generation); the sweep, run twice concurrently, converges.
    Name any race that fails; never retry a test.
15. **Spool.** A pre-existing `0o755` spool directory is `chmod`ed to `0o700`
    (a directory owned by another user refuses); the free-space pre-check fails
    before embedding with `ENOSPC` when `disk_usage` is mocked low; a spool file
    older than 24 hours is removed by the start sweep and a younger one is not
    (including one held open by a second process); the reader is closed
    before the spool in every path, including a commit that raises mid-stream.
16. **`ingestion_timestamp` consumers.** Decay ranking, doctor's last-ingest,
    `StoredSessions.is_current`, and the insights bands return the same values
    for the same inputs before and after the change (golden values).
17. **Model conformance.** A table-driven test replays the model's mode-0
    interesting traces (stale after newer; crash after add; two writers) against
    the real store.

## 12. Backwards compatibility

- **Readers:** none change. Rows written by earlier builds read as legacy.
- **Stored data:** no migration beyond two defaulted columns. Existing copies
  keep today's semantics; they are *unverified* (§5.4), never deleted by this
  change, and a new write supersedes them.
- **Older writers:** after migration an older local-mode build still writes
  (NULL columns, F10); its rows are legacy and are reaped by the next
  generation. An older *reader* ignores the new columns.
- **API surface:** `skipped` gains `"superseded"`. A client that matched
  `skipped == "exists"` exactly sees a new string; one that tests truthiness
  (`RefusalAwareTerminal`) is unaffected.
- **Behaviour:** overwrite no longer empties the name while it embeds (an
  improvement); the document is not searchable until its commit (it was
  progressively searchable window by window before; DES-034 §2's "each flushed
  window searchable" is deliberately given up for single-document ingest, since
  a visible prefix *is* the defect).
- **Disk.** Peak disk during a write is about **twice the document's row
  bytes**: the spool (R), plus the new fragments the commit writes (R), while
  the old generation is still present until the heal (and its files until the
  next `optimize` cleanup). Hence the pre-check in §5.2: free space on the
  spool filesystem must exceed twice the estimated spool bytes before the first
  window is embedded. A store whose data and spool share a nearly full disk now
  fails early, with `ENOSPC`, rather than mid-write.
- **Sync.** `write_id = 'sync'` on newly synced rows; earlier sync rows keep
  `''` and are protected by the registry lookup (§5.9). A funnel write to a
  sync-owned name now fails with a refusal where it used to succeed.

## 13. For the leader

**D1 (needs your acknowledgement). Transient duplicate, not "exactly one".**
The brief's I1 reads "exactly one complete copy". LanceDB offers no atomic swap
(§3), and the one operation that replaces in a single commit, `merge_insert`,
costs O(table) memory (F4). This design guarantees: *never a partial copy;
always at least one complete copy; an older complete copy may sit beside the
newest for milliseconds, or after a crash until the next touch; it is then
reaped without operator action.* The model shows that over its state space (§6; I3
as a possibility claim in CTL). If you want literally-exactly-one, the only cost-bounded route is
alternative E (a window with zero visible copies and a recoverable durable
spool); I recommend against it. **Recommendation: accept D1.**

**D2 (informational). Legacy partials cannot all be found.** A legacy copy with
no `chunk_count` that is a clean prefix is indistinguishable from a shorter
complete document. The design detects gaps, and for URL pages a coverage
shortfall, and treats the rest as held. Labels are never judged partial by a
heuristic, because the remedy would destroy the only copy. Going forward every
write is verifiable. Nothing here needs a ruling; it should be in the ADR.

**D3 (informational). Surfaces.** `skipped: "superseded"` is user-visible, and
the sync-owned refusal is a new refusal on every surface.

**Resolved by ruling (security review).** Sync ownership with the `'sync'`
marker, refusal, and lookup-not-rewrite migration (§5.9); stamps only from
complete or held copies (§5.4, §7); submission-order writes never superseded
(§7); strict key and `write_id` parsing (§5.1, §5.4); damaged generations
reaped (§5.5); spool hardening and disk (§5.2, §12); scoped model claims (§6).

**D4 (operator may override at ratification). Takeover only of the same file.**
Interim ruling: sync takes over a name only when the existing funnel copy is
file-sourced from the same source path; labels, captures, and URL pages are
never taken over: sync skips that file, records a conflict the doctor lists, and
the funnel copy stays (§5.9 c). The cost is that such a file is not indexed by
sync until the conflict is resolved by hand. The alternative (unconditional
takeover, the earlier draft) loses user text.

**Also resolved this round.** Retained-documents backfill for collections
retained by earlier builds (§5.9 d); stored-stamp damage threshold at twice the
write ceiling, and a submission write that cannot fit refuses loudly, with
equal-stamp ties resolved for the submission write by the token's class digit
(§5.1, §7); an absent registry means no registrations, a present-but-unreadable
one fails closed (§5.9, test 11); `quarry delete` clears the retained record
first; deregister is one transaction.

**Residuals stated, none needing a ruling.** Mixed-version writers (§7),
external damage inside a heal's re-verify-to-delete millisecond (§5.5),
cross-process name collision with sync (§5.9), remote-hook clock skew (§7),
legacy clean-prefix partials (D2).

No other operator-level question arose.
