# `quarry learn` — a fourth capture verb with retrieval preference

## Ratifications (2026-08-31, via claude:tty9)

Three operator rulings bind this design; where the body below made a call,
the ruling is the one that governs.

- **C1 — atomic operation.** `learn` writes the lesson-tagged chunk AND
  registers its retrieval preference in one call, on every surface. No
  `learn`-then-`set_config` two-step, ever — a caller that crashes between
  the two calls would leave a lesson that never surfaces, and the two-call
  shape freezes solid the moment a client exists. `set_config` may exist
  elsewhere as a general tool; `learn` must never depend on it.
- **C2 — same name, same shape, everywhere.** The verb is `learn` on CLI,
  MCP, slash, and the Python client. Lesson text is the primary positional;
  options trail:
  - CLI: `quarry learn "<lesson>" [--topic] [--name]`
  - MCP: `{lesson, topic?, name?}`
  - Slash: `/quarry:learn <lesson text> [as <name>]` (mirrors `/remember`'s
    hint grammar)
  - Client: `client.learn(lesson, topic=None, name=None)`
- **C3 — slash is a thin door.** `/quarry:learn` parses arguments, calls the
  MCP tool, reports. No logic in the command `.md`. The `-dev` twin is a
  byte-for-byte copy except the tool-name prefix, per this repo's existing
  `remember.md`/`remember-dev.md` pair.

**One resolved deviation from C2's literal text.** The ratified client
signature spells its optional params `topic=None, name=None`. Every existing
wire-adjacent model in this area (`RememberRequest`, `IngestRequest`) uses the
`""` empty-string sentinel, never `None` — no `Optional`/`| None` field
exists anywhere in `quarry.api` today, and PY-TS-14 requires a justification
comment on every one that would. `None` here has no such justification: it
would be the only `Optional` parameter in the ingestion surface, for no
different reason than the two adjacent methods that already use `""`. This
design reads C2 as specifying the *shape* (three params, one positional, two
optional) and implements the *type* per the codebase's settled convention:
`def learn(self, lesson: str, topic: str = "", name: str = "") -> TaskAccepted`.
Flagging this for the leader's review per the standard pre-implementation
escalation step — it is a mechanical, low-risk call, not a scope change.

## Verified state (2026-08-31)

- `src/quarry/api/ingestion.py:8-24` — `RememberRequest` already carries
  `agent_handle`, `memory_type`, `summary` (landed in quarry-8kdo, PR #483,
  commit `c821f0a`). `daemon/routes/ingestion.py:75-117` — `_remember_job`
  resolves collection: explicit `collection` wins, else `agent_handle` routes
  to `memory-<handle>`, else `"default"`. This is the *agent-scoped* routing
  rule — `learn` deliberately does not reuse it (see D2).
- `src/quarry/retrieval/fusion.py:16-18,73-89` — `_DECAYABLE_TYPES =
  {fact, observation, opinion, procedure}`; `_contribution` decays a row only
  when `decay_rate > 0 AND memory_type in _DECAYABLE_TYPES AND
  row.get("agent_handle")` — **both** the type and a non-empty handle are
  required, already landed (8kdo D1a). A row with empty `agent_handle` never
  decays today, regardless of `memory_type`. This is the exact property
  `learn` needs and gets for free (see D3).
- `src/quarry/config.py:96-100` — `Settings.retrieval_decay_rate` is threaded
  into `RetrievalConfig` at `daemon/routes/search.py:41`, then into
  `RrfFusion` at `retrieval/hybrid.py:44`: `RrfFusion(config.rrf_k,
  config.decay_rate)`. This is the exact threading path a `lesson_boost` knob
  reuses (D4).
- `src/quarry/captures_collection.py` — `CapturesCollection`: a `<repo>` base
  collection yields `<repo>-captures`; unregistered → `default-captures`;
  `for_registry_path(cwd, registry_path)` opens the sync registry server-side
  and resolves against the client-sent `cwd`. This is the exact shape `learn`
  needs for project-scoped (not agent-scoped) routing — see D1.
- `src/quarry/client/client.py:61-64` — `QuarryClient` is documented as
  "pure transport... one method per route," raising the bar for any method
  that does more than marshal. `learn()` is the one deliberate exception (see
  D5) because C2's ratified signature has no `cwd` parameter on any surface,
  and cwd-derived project scoping cannot happen anywhere else without
  breaking "same shape everywhere."
- `plugin/.claude-plugin/plugin.json:9-17` — the `quarry` MCP server is a
  bare `stdio` subprocess (`command: quarry, args: [mcp]`) with no explicit
  `cwd` override, so it inherits Claude Code's session working directory —
  confirmed by precedent: no MCP tool in `mcp_server.py` accepts a `cwd`
  parameter today, yet `register_directory` (`mcp_server.py:373-388`)
  resolves relative paths and reasons about "the cwd" in its own comment,
  meaning "the MCP server's process cwd is the repo" is already a load-
  bearing assumption elsewhere in this codebase, not a new one.
- `src/quarry/ethos_handle.py` — `EthosConfig.agent_handle_at(cwd)` is the
  ancestor-walking reader of `.punt-labs/ethos/config.yaml`. **Not used by
  `learn`** — see D2's rejection of agent-scoping — cited here only to record
  that it exists and was considered.
- `src/quarry/retrieval/fusion.py:92-99` — `_row_key = (document_name,
  chunk_index, page_number)` is the RRF dedup key; `scores[key] +=
  contribution` and `all_rows[key] = row` (last write wins for *display*,
  scores still sum). **Two `learn` calls that resolve to the same
  `document_name`, `chunk_index=0`, `page_number=0` (the common case for a
  short, single-chunk lesson) collide on this key** — their scores merge and
  only one body displays. This is a genuine, pre-existing collision surface
  that any caller-supplied, non-unique document name would hit; it forces the
  document-naming rule in D6 below (a discovered constraint, not a style
  preference).
- `src/quarry/doctor_memory.py:19-23,106-130` — `_MEMORY_TYPES = {fact,
  observation, opinion, procedure}` (deliberately mirrors
  `fusion._DECAYABLE_TYPES`, not imported from it — two independently
  maintained copies, out of this mission's scope to unify). `_tally` gates
  the *type* count on `if handle:` — **a lesson row (empty `agent_handle` by
  design, D2) is invisible to `quarry doctor`'s memory corpus report today**,
  both because the type tally requires a handle and because `"lesson"` isn't
  in `_MEMORY_TYPES` regardless. This is a real gap this design closes (D7),
  found by reading the code, not assumed.
- `src/quarry/daemon/ingest_jobs.py:27-97` — `ScrubbedIngestJob` already
  carries every field `learn` needs (`name, content, collection,
  format_hint, overwrite, scrub_label, agent_handle, memory_type, summary`).
  No new job type — `learn` composes one exactly like `remember` does (D1,
  D8).
- `src/quarry/formatting.py:167-190` — `format_search_results` does not
  render `SearchResult.summary` in the compact CLI/MCP view today. Storing
  `topic` in `summary` (D5) is therefore invisible in the default `find`
  output — visible via `--json`, `quarry show <doc>`, and any future
  formatting change — not a regression, since `summary` already has this
  property for `remember`.
- `plugin/hooks/hooks.json:22-24` — the `PostToolUse` suppress-output matcher
  is `mcp__(plugin_quarry(-dev)?_)?quarry(-proxy)?__.*` — a wildcard that
  already covers any new tool name, including `learn`. `plugin/hooks/
  suppress-output.sh`'s fallback branch (final `jq` block) renders any
  unrecognized tool's result verbatim in the panel. **No `hooks.json` or
  `suppress-output.sh` change is needed.**
- `plugin/commands/remember.md` / `remember-dev.md` — identical files except
  the tool-name prefix (`mcp__plugin_quarry_quarry__remember` vs.
  `mcp__plugin_quarry-dev_quarry__remember`). No generation script exists;
  the `-dev` twin is a maintained-by-hand duplicate. `scripts/restore-dev-
  plugin.sh` only restores dev state at release time via git checkout — it
  does not generate new twins. C3's "regenerate the twin" means: hand-copy.

## Decisions

### D1 — Collection: project-scoped `<repo>-lessons`, not agent-scoped

**Decision.** A new `LessonsCollection` class, structurally identical to
`CapturesCollection` (D-derivation above), maps a lesson's `cwd` to
`<repo>-lessons` (registered directory) or `default-lessons` (unregistered).
`agent_handle` plays no role in `learn`'s collection routing.

**Reasoning.** C2's ratified wire shape for all four surfaces —
`{lesson, topic?, name?}` — has no `agent_handle` field anywhere. That
omission is a deliberate signal, not a gap to patch around: a distilled
lesson is *project* knowledge ("always run `uv sync --dev` this way in this
repo"), not *personal* memory the way a `remember`-captured fact can be
("rmh prefers tabs"). Scoping by project, the same axis `<repo>-captures`
already uses for transcripts, keeps lessons from one project bleeding into
another project's default search results while never requiring an identity
parameter the ratified shape doesn't have room for.

**Rejected alternatives.**

- *(a) Route via `agent_handle` like `remember`.* Requires resolving an
  ethos handle somewhere and smuggling it onto the wire — but C2's schema has
  no field for it on any of the four surfaces. Retrofitting one would
  violate C2 outright.
- *(b) Fixed global `"lessons"` collection, no per-project scoping.* Simpler
  (no `cwd`, no registry lookup), but collapses lessons from every project
  sharing one daemon's database into one bucket — a lesson about this
  repo's release process would surface when searching an unrelated repo's
  default collection. Rejected: quarry's existing shared-corpus model
  (`docs/architecture.tex`) already solves this exact problem for captures
  via per-repo suffixed collections; reinventing a global bucket for lessons
  contradicts that settled pattern for no benefit.

### D2 — Retrieval preference: a fusion-time rank multiplier, keyed on `memory_type`

**Decision.** `RrfFusion._contribution` gains a multiplicative boost applied
whenever a row's `memory_type == "lesson"`, independent of `agent_handle`,
`decay_rate`, or collection:

```python
# src/quarry/retrieval/fusion.py
_LESSON_TYPE = "lesson"

class RrfFusion:
    __slots__ = ("_decay_rate", "_rrf_k", "_lesson_boost")

    def __new__(cls, rrf_k: int, decay_rate: float, lesson_boost: float = 1.0) -> Self:
        self = super().__new__(cls)
        self._rrf_k = rrf_k
        self._decay_rate = decay_rate
        self._lesson_boost = lesson_boost
        return self

    def _contribution(self, row: dict[str, object], rank: int, now_ts: float) -> float:
        memory_type = str(row.get("memory_type") or "")
        weight = 1.0
        if (
            self._decay_rate > 0
            and memory_type in _DECAYABLE_TYPES
            and row.get("agent_handle")
        ):
            ts = row.get("ingestion_timestamp", "")
            weight = self.temporal_weight(ts, now_ts, self._decay_rate)
        boost = self._lesson_boost if memory_type == _LESSON_TYPE else 1.0
        return (1.0 / (self._rrf_k + rank)) * weight * boost
```

This is why C1's atomicity is trivial rather than engineered: "retrieval
preference" is not a second stored fact — it is a *derived* property of the
one field (`memory_type`) the single write already sets. There is no
preference table, no second row, no second collection to keep in sync. One
`ScrubbedIngestJob`, one insert, one column value, and the retrieval layer
already reads that column on every query.

**Why a multiplier and not a filter or a promotion.** A hard filter/promotion
("always put lesson rows first") would let an irrelevant lesson outrank a
highly relevant plain result — wrong, and exactly the "capture verbs
blurring" the boundary sentence exists to prevent. A multiplier scales with
the row's own RRF rank, so a lesson that is a poor match for the query stays
buried; only a lesson that is *already* a reasonably good match gets pulled
above equivalently-ranked plain content.

**Boost value: 1.5.** RRF's per-channel term is `1/(k+rank)` with `k=60`
(unchanged production default). A lesson at fused rank `r_l` (0-indexed,
before boost) outranks a plain hit at rank `r_p` when
`boost / (60 + r_l) > 1 / (60 + r_p)`, i.e. `r_l < boost·(60 + r_p) − 60`.
For the best-case plain competitor (`r_p = 0`, boost `B = 1.5`):
`r_l < 1.5·60 − 60 = 30`. So a lesson ranked anywhere in the top 30 of its
own channel(s) will outrank even the single best non-lesson hit; a lesson
ranked 40th or worse — genuinely poor relevance — still loses to a
relevant plain result. This mirrors DES's decay-rate derivation style
(quarry-8kdo D1): a concrete, checkable threshold, not a tuned-by-feel
number. `1.5` is conservative enough that irrelevant lessons never dominate,
and material enough that a moderately-relevant lesson reliably surfaces
above unrelated content — the entire point of "retrieval preference."

**Threading.** `RetrievalConfig.lesson_boost: float = 1.0` (dataclass
default — neutral, so `RetrievalConfig()` still reproduces today's
production behavior bit-for-bit, per its existing docstring contract).
`Settings.retrieval_lesson_boost: float = 1.5, ge=1.0` (production value,
mirrors `retrieval_decay_rate`'s `Field(...)` shape at `config.py:96-100`;
`ge=1.0` fails loud on a value that would *suppress* lessons instead of
boosting them — `1.0` is how an operator disables the effect, not `0.0`,
because `0.0` would zero out the row's entire RRF term). `daemon/routes/
search.py:41` gains `lesson_boost=self.ctx.settings.retrieval_lesson_boost`
on the same `RetrievalConfig(...)` call; `retrieval/hybrid.py:44` becomes
`RrfFusion(config.rrf_k, config.decay_rate, config.lesson_boost)`.

**Rejected alternatives.**

- *(a) A separate preference/config table row per lesson.* This is the
  `learn`-then-`set_config` shape C1 explicitly forbids — two writes, two
  places to drift, and a crash window between them.
- *(b) Boost scoped by `topic`* (only boost a lesson when the query "matches"
  its topic). Requires the query itself to carry a `topic` parameter that
  `find`/`SearchRequest`/`SearchFilter` do not have today, and inventing one
  is a much larger surface-parity change (new field on every surface of
  `find`, per bug class 3) that neither C1–C3 nor the mission's write-set
  scope asks for. Rejected as over-engineered for v1; a real, data-backed
  need for topic-scoped boosting is a separate bead.

### D3 — Decay: lessons are pinned by construction, no new gate needed

**Decision.** No change to the decay condition itself. Because `learn` never
sets `agent_handle` (D1), every lesson row already fails the existing
`row.get("agent_handle")` truthiness check in `_contribution` — it is exempt
from decay for the same reason a plain knowledge chunk is exempt today.

**Belt-and-suspenders documentation.** `_DECAYABLE_TYPES` is explicitly
**not** extended to include `"lesson"`. This is redundant given the
`agent_handle` gate alone already exempts every lesson, but it guards
against a *future* change that adds `agent_handle` to `learn`'s wire shape
(e.g., if per-agent lesson scoping is ever ratified) silently causing
lessons to start decaying as an accidental side effect of that unrelated
change, rather than a deliberate decision made and reviewed on its own
terms.

**Rejected alternative.** Add a `pinned: bool` column or a `memory_type ==
"lesson"` special case inside the decay gate itself. Both are unneeded
complexity — the existing `agent_handle` gate already produces the exact
behavior wanted, for free, as a consequence of D1's routing decision. Adding
a second, redundant mechanism to enforce the same invariant is exactly the
kind of premature abstraction to avoid.

### D4 — `topic` and `name` semantics

- **`name`** — when given, seeds the human-relatable slug of the generated
  document name (D6). "User-visible slug for later reference" is honored:
  a lesson learned with `name="auth-gotcha"` is filed as
  `lesson-auth-gotcha-<8hex>`, so a human scanning `quarry show`/`quarry
  list` output recognizes it by that prefix. It is **not** used as the raw,
  literal document key the way `remember`'s `document_name` is — see D6 for
  why a bare, collision-prone key is unacceptable here.
- **`topic`** — a short, free-text domain tag ("testing", "release-process"),
  stored verbatim in the existing `summary` column (no new schema field).
  `summary`'s existing docstring ("One-line summary of the content") already
  fits a short categorical tag well enough that inventing a fifth memory
  column for a data point summary already carries is not justified — reuse
  before invention. `topic` does **not** feed content, embeddings, or the
  boost calculation (D2) — it is purely a human/organizational aid today.
  If a `--topic` search filter proves genuinely wanted later (real usage
  data, not speculation), it is a small, isolated follow-up: add
  `SearchFilter.summary` (or a dedicated `topic` column) plus the matching
  CLI/MCP/HTTP parameter, per bug class 3. Not in this write-set.

**Rejected alternative.** Embed `topic` into `content` as a markdown header
(`# Topic: {topic}\n\n{lesson}`). Rejected: it pollutes the lesson text
itself (the exact thing a human or agent later reads verbatim), duplicates
what `summary` already exists to hold, and would make `_MAX_LESSON_CHARS`
(D8) count header bytes that aren't part of the actual lesson.

### D5 — The atomic write: a new `/v1/learn` route, not client-side composition

**Decision.** A new wire model `LearnRequest{lesson, topic="", name="",
cwd=""}` and daemon route `POST /v1/learn`, structurally sibling to
`/v1/remember`. The **daemon** composes the document name (D6), forces
`memory_type="lesson"`, forces `agent_handle=""`, resolves the `<repo>-
lessons` collection (D1), and enforces the length cap (D8) — all in one
`IngestionRoutes._learn_job` chokepoint, mirroring `_remember_job`'s
existing "collection routing is a single server-side rule so no surface can
drift" pattern (`daemon/routes/ingestion.py:80-83`).

**Why not compose the `RememberRequest` client-side in each of CLI/MCP/
client instead of adding a route.** That would require the document-naming,
collection-resolution, and memory-type-forcing logic to be either (a)
duplicated three times (CLI, MCP tool, Python client) — a direct bug-class-3
risk, since a fourth surface or a maintenance edit could silently drift one
copy from the others — or (b) centralized in a shared library helper that
CLI, MCP, and the client all import, which is *harder* to get right than one
server route, because `LessonsCollection.for_registry_path` (D1) needs the
sync registry, which none of the three client-side surfaces can read without
importing the daemon's own engine (`CapturesCollection.for_registry_path`'s
own docstring: "the capture client cannot do this itself without importing
the engine"). A new route is the smaller, more correct surface.

**Why `QuarryClient.learn()` breaks "pure transport" by resolving `cwd`
itself.** `client.learn(lesson, topic="", name="")` per the ratified C2
signature has no `cwd` parameter, yet `LearnRequest.cwd` must be populated
for D1's routing to work. Every other `QuarryClient` method either needs
nothing beyond its request model's own fields, or already does more than
pure marshaling (`await_task` polls). `learn()` becomes the one route method
that resolves `Path.cwd()` internally before building its request — a
deliberate, single, documented exception, not a return to per-surface
duplication, because CLI and MCP both delegate straight through to this one
client method with no `cwd` handling of their own:

```python
# src/quarry/client/client.py
def learn(self, lesson: str, topic: str = "", name: str = "") -> TaskAccepted:
    """Save a distilled lesson as a 202 background task.

    remember = a specific durable fact, ingest = a URL, learn = a distilled
    lesson that gets retrieval preference.

    Resolves the caller's cwd to scope the lesson to this project's
    ``<repo>-lessons`` collection -- the one deliberate exception to this
    client's pure-transport contract: ``LearnRequest`` carries no ``cwd``
    parameter on any surface, so a lesson's project scope has to come from
    somewhere the caller is not asked to state twice.
    """
    req = LearnRequest(lesson=lesson, topic=topic, name=name, cwd=str(Path.cwd()))
    return self._post("/learn", TaskAccepted, req)
```

`Path` joins `client.py`'s existing top-level imports (stdlib, no lazy-import
justification needed per PY-CS-10).

**Rejected alternative.** Give `QuarryClient` an explicit `cwd` constructor
parameter used by every ingestion method. Touches the client's construction
contract for a need only `learn` has today (`remember`/`ingest` already
receive `agent_handle` explicitly from their callers, unlike `learn`) —
broader surface change than this mission's write-set justifies.

### D6 — Document naming: always generated, never the bare caller-supplied name

**Decision.** The daemon always computes a unique document name:

```python
# src/quarry/lesson.py
@final
class LessonComposer:
    """Compose a collision-proof document name for a distilled lesson."""

    __slots__ = ()

    _FALLBACK_SLUG = "note"
    _MAX_SLUG_LEN = 40

    @classmethod
    def document_name(cls, name: str, topic: str) -> str:
        """Return ``lesson-<slug>-<8 hex>``.

        The slug is human-relatable (from *name*, else *topic*, else a
        fallback); the hex suffix guarantees uniqueness across repeated
        calls with the same slug. Two distinct lessons must never collide on
        RRF's ``(document_name, chunk_index, page_number)`` dedup key
        (fusion.py) -- colliding would silently merge two unrelated lessons'
        scores and drop one lesson's text from display (see Verified state).
        """
        base = name or topic or cls._FALLBACK_SLUG
        return f"lesson-{cls._slugify(base)}-{uuid4().hex[:8]}"

    @classmethod
    def _slugify(cls, text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug[: cls._MAX_SLUG_LEN].strip("-") or cls._FALLBACK_SLUG
```

**Reasoning.** This was not a style choice — it was forced by reading
`fusion.py`'s dedup key (Verified state above). A short, single-page,
single-chunk lesson (the common case) always lands at `chunk_index=0,
page_number=0`; if two `learn` calls shared a literal document name (e.g.
both given `--name auth-gotcha` for two different lessons learned weeks
apart), their RRF row keys would collide, their scores would sum into one
bucket, and only the more-recently-inserted row's text would display —
silently losing the first lesson's content from search results while the
data itself sits harmlessly in the table. `remember` does not have this
problem in practice because its documents are typically named uniquely per
distinct piece of content and re-learning under a literal identical name
with `overwrite=False` is a rarer, more deliberate act; `learn`'s whole
premise — quick, low-ceremony capture of a rule of thumb — makes accidental
name reuse the *expected* case, so the uniqueness guarantee has to be
unconditional, not caller-opt-in.

`overwrite` is therefore never exposed as a `learn` parameter (consistent
with C2's shape, which has none) and is always `False` internally — moot,
since the generated name is always new.

**Rejected alternative.** Let `name`, when given, be the literal document
key (mirroring `remember`), with `overwrite=True` so repeated learns under
the same name replace rather than collide. Rejected: this is legitimate for
`remember`, where the caller consciously names a document to keep it
current, but `learn`'s ratified shape has no `overwrite` parameter, and
silently defaulting to "replace" on a re-used name is a surprising, lossy
behavior that a quick, no-ceremony capture tool must not have as its
default.

### D7 — `memory_type == "lesson"` is reserved: `remember`/`ingest` reject it

**Decision.** `IngestionRoutes._remember_job` and `_ingest_job` both reject a
request whose `memory_type` field equals `"lesson"` with `400`:

```python
_RESERVED_MEMORY_TYPE = "lesson"
...
memory_type = self._str_field(body, "memory_type", "")
if memory_type == _RESERVED_MEMORY_TYPE:
    return JSONResponse(
        {"error": "memory_type 'lesson' is reserved for quarry learn"},
        status_code=400,
    )
```

**Reasoning.** `fusion.py`'s boost (D2) keys purely on the `memory_type`
column value, independent of which route wrote it. Without this guard, a
caller could call `remember(memory_type="lesson")` directly, bypass the
document-naming (D6), topic/summary (D4), and length-cap (D8) rules `learn`
enforces, and still receive the exact same retrieval boost — defeating C1's
atomicity guarantee (the boost would no longer imply the write went through
`learn`'s composition) and reintroducing precisely the "capture verbs
blurring" the boundary sentence exists to prevent. Reserving the value at
the one chokepoint both routes already share (`_str_field`) closes this for
both `remember` and `ingest` in the same small change.

### D8 — Length cap: 500 characters, enforced server-side

**Decision.** `IngestionRoutes._learn_job` rejects a lesson longer than
`_MAX_LESSON_CHARS = 500` with `400`, pointing the caller at `remember`:

```python
if len(lesson) > _MAX_LESSON_CHARS:
    return JSONResponse(
        {
            "error": (
                f"lesson exceeds {_MAX_LESSON_CHARS} chars -- "
                "use remember for full documents"
            )
        },
        status_code=400,
    )
```

**Reasoning.** "Distilled" is the operative word in the boundary sentence.
500 characters is roughly 80-100 words — two to three sentences, enough for
a rule of thumb or a gotcha, not enough for a document. Without a real
enforced boundary, `learn` would inevitably accept the same content
`remember` does, and the two verbs blur exactly as the mission warns against.
No client-side pre-check duplicates this — matching how `remember`'s own
blank-content/blank-name checks are daemon-only (`_require_text`), the
daemon's `400` message surfaces verbatim through `HttpError` to the CLI's
error output and the MCP `_guard`'s error string, so a second, client-side
copy of the same check would be true duplication for no benefit.

### D9 — `quarry doctor`'s memory corpus check gains lesson visibility

**Decision.** `MemoryDiagnostics._tally` (`doctor_memory.py`) gains a fourth,
handle-independent count and `_summarize` reports it as a new segment:

```python
# src/quarry/doctor_memory.py
def _tally(rows):
    handles: Counter[str] = Counter()
    types: Counter[str] = Counter()
    collections: Counter[str] = Counter()
    lessons = 0
    for row in rows:
        handle = str(row.get("agent_handle") or "")
        collection = str(row.get("collection") or "")
        memory_type = str(row.get("memory_type") or "")
        if handle:
            handles[handle] += 1
            if memory_type in _MEMORY_TYPES:
                types[memory_type] += 1
        if memory_type == _LESSON_TYPE:
            lessons += 1
        if collection:
            collections[collection] += 1
    return handles, types, collections, lessons
```

`_summarize` appends `f"lessons={lessons}"` as its own segment (only when
`lessons > 0`, matching every other segment's "omit if empty" convention),
distinct from `types` (which stays agent-scoped, per 8kdo's original
reasoning that a knowledge chunk's incidental `memory_type` tag should not
inflate a *memory* breakdown). `_LESSON_TYPE = "lesson"` is a new
module-level constant in `doctor_memory.py`, matching `_MEMORY_TYPES`'
existing convention of a locally-owned copy rather than an import from
`fusion.py` (that duplication is pre-existing and out of this mission's
scope to unify).

**Reasoning.** Verified state above found this gap by reading the current
`_tally` implementation: it excludes any empty-`agent_handle` row from both
the handle tally (correctly) and the *type* tally (which, for `learn`'s
rows, silently makes every lesson invisible to the one diagnostic surface
built to answer "how much distilled knowledge exists here"). This is a
genuine, discovered defect in the interaction between this design and
existing code, not a speculative nice-to-have, and per this repo's
no-pre-existing-issues rule it is fixed in this write-set rather than filed
separately.

## Wire contract

```python
# src/quarry/api/ingestion.py -- alongside RememberRequest, IngestRequest
class LearnRequest(BaseModel):
    """Body for saving a distilled lesson with retrieval preference.

    ``cwd`` is never a caller-facing parameter on any surface (CLI, MCP,
    slash, client) -- ``QuarryClient.learn()`` resolves it via ``Path.cwd()``
    before this model is built, exactly the way ``CaptureIngestRequest.cwd``
    is populated by the caller's own working directory, not the user.
    """

    lesson: str
    topic: str = ""
    name: str = ""
    cwd: str = ""
```

Exported from `quarry.api.__init__` and `__all__` alongside `IngestRequest`/
`RememberRequest`.

## Daemon route

```python
# src/quarry/daemon/routes/ingestion.py -- new method on IngestionRoutes
MAX_LEARN_BODY_BYTES = 64 * 1024
_MAX_LESSON_CHARS = 500
_RESERVED_MEMORY_TYPE = "lesson"

async def learn(self, request: Request) -> JSONResponse:
    """Save a distilled lesson as a background task.

    remember = a specific durable fact, ingest = a URL, learn = a distilled
    lesson that gets retrieval preference. Body: {lesson, topic?, name?,
    cwd?}. Always writes memory_type="lesson" with no agent_handle -- a
    lesson is project-scoped, deliberately-curated knowledge, never a
    personal, decaying memory (fusion.py's decay gate exempts empty-handle
    rows already). Returns 202 with a task_id.
    """
    body = await self._authorized_body(request, MAX_LEARN_BODY_BYTES)
    if isinstance(body, JSONResponse):
        return body
    job = await self._learn_job(body)
    if isinstance(job, JSONResponse):
        return job
    state = self.ctx.tasks.begin("learn")
    return self.submit(job, state)

async def _learn_job(
    self, body: dict[str, object]
) -> ScrubbedIngestJob | JSONResponse:
    """Validate a learn body into a ScrubbedIngestJob or a 400.

    Naming, collection routing, and memory_type are single server-side
    rules (D5-D8) so no surface can drift them (bug class 3).
    """
    lesson = self._require_text(body, "lesson")
    if isinstance(lesson, JSONResponse):
        return lesson
    if len(lesson) > _MAX_LESSON_CHARS:
        return JSONResponse(
            {
                "error": (
                    f"lesson exceeds {_MAX_LESSON_CHARS} chars -- "
                    "use remember for full documents"
                )
            },
            status_code=400,
        )
    topic = self._str_field(body, "topic", "")
    name = self._str_field(body, "name", "")
    collection = await run_in_threadpool(
        LessonsCollection.for_registry_path,
        self._str_field(body, "cwd", ""),
        self.ctx.settings.registry_path,
    )
    return ScrubbedIngestJob(
        name=LessonComposer.document_name(name, topic),
        content=lesson,
        collection=collection.name,
        format_hint="auto",
        overwrite=False,
        scrub_label="learn",
        agent_handle="",
        memory_type=_RESERVED_MEMORY_TYPE,
        summary=topic,
    )
```

`_remember_job` and `_ingest_job` each gain the three-line reservation guard
from D7, reading `_RESERVED_MEMORY_TYPE` from the module-level constant
above (defined once, both guards reference it).

`route_table.py` registers the route as a sibling of `/remember`:

```python
RouteSpec(
    "/learn",
    ingestion.learn,
    ("POST",),
    TaskAccepted,
    request_model=LearnRequest,
    status_code=202,
),
```

## `LessonsCollection`

```python
# src/quarry/lesson.py -- alongside LessonComposer
@final
class LessonsCollection:
    """A project's lessons collection name, derived like CapturesCollection."""

    _LESSONS_SUFFIX = "-lessons"
    _FALLBACK_REPO = "default"

    _name: str

    def __new__(cls, name: str) -> Self:
        self = super().__new__(cls)
        self._name = name
        return self

    @property
    def name(self) -> str:
        return self._name

    @classmethod
    def for_repo(cls, repo: str) -> Self:
        return cls(f"{repo}{cls._LESSONS_SUFFIX}")

    @classmethod
    def resolve(cls, base_collection: str | None) -> Self:
        return cls.for_repo(base_collection or cls._FALLBACK_REPO)

    @classmethod
    def for_cwd(cls, cwd: str, registrations: Mapping[str, str]) -> Self:
        return cls.resolve(CapturesCollection._covering_collection(cwd, registrations))

    @classmethod
    def for_registry_path(cls, cwd: str, registry_path: Path) -> Self:
        from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

        conn = SyncRegistry(registry_path)
        try:
            registrations = {
                r.directory: r.collection for r in conn.list_registrations()
            }
        finally:
            conn.close()
        return cls.for_cwd(cwd, registrations)
```

`for_cwd` reuses `CapturesCollection._covering_collection` directly (the
ancestor-walk-against-the-registry logic is identical regardless of the
suffix) rather than copy-pasting it — the impl mission should decide whether
to promote `_covering_collection` to a shared free function both classes
call, or leave the cross-class staticmethod reference; either is acceptable,
but a *third* copy of that walk if a third collection-suffix class is ever
added would not be (extract then, per PY-RF-3's "third occurrence"
threshold — this is only the second).

## Each surface

### CLI (`src/quarry/cli_ingest.py`)

```python
def register(self, app: typer.Typer) -> None:
    app.command(name="ingest")(self._p.cli_errors(self._ingest))
    app.command(name="remember")(self._p.cli_errors(self._remember))
    app.command(name="learn")(self._p.cli_errors(self._learn))

def _learn(
    self,
    lesson: Annotated[str, typer.Argument(help="The distilled lesson text")],
    topic: Annotated[
        str, typer.Option("--topic", help="Domain tag for this lesson")
    ] = "",
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="User-visible slug for later reference"),
    ] = "",
) -> None:
    """Save a distilled lesson that gets retrieval preference.

    remember = a specific durable fact, ingest = a URL, learn = a distilled
    lesson that gets retrieval preference. Lessons are capped at 500
    characters -- use remember for anything longer.
    """
    accepted = self._p.client().learn(lesson, topic=topic, name=name)
    self._p.emit(
        accepted.model_dump(),
        f"Learn {accepted.status}: task_id={accepted.task_id}",
    )
```

### MCP (`src/quarry/mcp_server.py`)

```python
@_guard
def learn(self, lesson: str, topic: str = "", name: str = "") -> str:
    """Use learn to save a distilled lesson that should outrank ordinary
    results for related queries -- a rule, a convention, a "do it this
    way" insight, not a one-off fact.

    remember = a specific durable fact, ingest = a URL, learn = a distilled
    lesson that gets retrieval preference.

    The daemon scrubs secrets/PII before indexing, same as remember. Lessons
    are capped at 500 characters -- use remember for anything longer.
    Returns immediately -- the daemon indexes in the background.

    Args:
        lesson: The distilled lesson text (<= 500 chars).
        topic: Optional domain tag (e.g. "testing", "release-process").
        name: Optional user-visible slug for later reference.
    """
    if err := self._reject_blank(lesson, "lesson"):
        return err
    accepted = self._connect().learn(lesson, topic=topic, name=name)
    return f"▶  Learning saved ({accepted.status}, task {accepted.task_id})"
```

`register()` gains `server.add_tool(self.learn)`.

### Slash (`plugin/commands/learn.md`, `learn-dev.md`)

```markdown
---
description: Save a distilled lesson that gets retrieval preference. remember = a specific durable fact, ingest = a URL, learn = a distilled lesson that gets retrieval preference.
argument-hint: "<lesson text> [as <name>]"
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

If the arguments end with " as <name>", the text before that clause is the
lesson and <name> is the name. Otherwise the full argument string is the
lesson and no name is given.

## Task

Call `mcp__plugin_quarry_quarry__learn` with:

- `lesson` set to the lesson text
- `name` set to the parsed name, if any
- `topic` set to a short domain tag, only if one is obviously implied by the
  conversation (e.g. "testing", "release-process") -- omit if unclear

The result is already formatted by a PostToolUse hook and displayed above. Do not repeat or reformat the data. Do not send any text after the tool call.
```

`learn-dev.md` is byte-identical except
`mcp__plugin_quarry-dev_quarry__learn`, per the `remember`/`remember-dev`
precedent (Verified state).

### Python client (`src/quarry/client/client.py`)

See D5's code block.

## Boundary-sentence propagation

C2's rationale — "this is what keeps the three capture verbs from blurring"
— only holds if all three verbs carry the sentence, not just the newcomer.
This write-set therefore also touches the six existing `remember`/`ingest`
description surfaces, appending the identical sentence used above:

| File | Change |
|---|---|
| `src/quarry/mcp_server.py` (`remember` docstring) | Append the boundary sentence after the existing opener; also append "``'lesson'`` is reserved for the ``learn`` tool" to the `memory_type` `Args:` line (D7). |
| `src/quarry/mcp_server.py` (`ingest` docstring) | Append the boundary sentence. |
| `src/quarry/mcp_server.py` (`find` docstring) | One-word addition: `memory_type` `Args:` line becomes "(fact, observation, lesson, etc.)" -- discoverability, not required by C1-C3 but free. |
| `src/quarry/cli_ingest.py` (`_remember` docstring) | Append the boundary sentence; note `memory_type`'s `lesson` reservation. |
| `src/quarry/cli_ingest.py` (`_ingest` docstring) | Append the boundary sentence. |
| `plugin/commands/remember.md`, `remember-dev.md` | Extend `description:` to end with the boundary sentence. |
| `plugin/commands/ingest.md`, `ingest-dev.md` | Extend `description:` to end with the boundary sentence. |

## Write set

| File | Change |
|---|---|
| `src/quarry/api/ingestion.py` | New `LearnRequest` model; `_RESERVED_MEMORY_TYPE` guard text referenced in `RememberRequest`/`IngestRequest` docstrings (optional doc note). |
| `src/quarry/api/__init__.py` | Export `LearnRequest`, add to `__all__`. |
| `src/quarry/lesson.py` (NEW) | `LessonComposer` (D6), `LessonsCollection` (D1/derivation). |
| `src/quarry/daemon/routes/ingestion.py` | New `learn()` handler + `_learn_job()`; `MAX_LEARN_BODY_BYTES`, `_MAX_LESSON_CHARS`, `_RESERVED_MEMORY_TYPE` constants; three-line reservation guard added to `_remember_job` and `_ingest_job` (D7). |
| `src/quarry/daemon/route_table.py` | Import `LearnRequest`; register `POST /v1/learn` (`ingestion.learn`), sibling to `/remember`. |
| `src/quarry/client/client.py` | New `learn()` method (D5); `from pathlib import Path` joins existing top-level imports. |
| `src/quarry/cli_ingest.py` | New `_learn` Typer command + registration; append boundary sentence to `_remember`/`_ingest` docstrings; `--memory-type` help text unchanged (generic already). |
| `src/quarry/mcp_server.py` | New `learn` tool + registration; append boundary sentence to `remember`/`ingest` docstrings; one-word `find` docstring addition. |
| `src/quarry/retrieval/config.py` | `RetrievalConfig.lesson_boost: float = 1.0` field + docstring update. |
| `src/quarry/retrieval/fusion.py` | `RrfFusion.__new__` gains `lesson_boost: float = 1.0`; `_contribution` applies the boost (D2); `_LESSON_TYPE` constant. |
| `src/quarry/retrieval/hybrid.py:44` | `RrfFusion(config.rrf_k, config.decay_rate, config.lesson_boost)`. |
| `src/quarry/config.py` | `Settings.retrieval_lesson_boost: float = Field(default=1.5, ge=1.0, validation_alias="QUARRY_RETRIEVAL_LESSON_BOOST")`. |
| `src/quarry/daemon/routes/search.py:41` | `RetrievalConfig(decay_rate=..., lesson_boost=self.ctx.settings.retrieval_lesson_boost)`. |
| `src/quarry/doctor_memory.py` | `_tally` gains the lesson count (D9); `_summarize` reports it; `_LESSON_TYPE` constant. |
| `plugin/commands/learn.md`, `learn-dev.md` (NEW) | Per "Each surface" above. |
| `plugin/commands/remember.md`, `remember-dev.md`, `ingest.md`, `ingest-dev.md` | Boundary-sentence propagation (see table above). |
| `tests/test_http_server.py` | `POST /v1/learn` contract tests: happy path (202, `task_id` starts with `learn-`); missing `lesson` (400); `lesson` over 500 chars (400, message names the limit); `topic`/`name` land in `summary`/document-name respectively; two calls with the same `name` produce two distinct document names (D6 regression test — the exact collision this design prevents); `cwd` under a registered directory routes to `<repo>-lessons`, unregistered/empty routes to `default-lessons`; `POST /v1/remember` and `POST /v1/ingest` with `memory_type=lesson` both now 400 (D7). |
| `tests/test_retrieval_fusion.py` | Lesson boost: a lesson-tagged row outranks an equal-or-better-ranked plain row per the `r_l < boost*(60+r_p) - 60` derivation; boost is a no-op at `lesson_boost=1.0`; boost does not apply to non-`"lesson"` `memory_type` values; decay and boost compose correctly (a lesson row is never decayed regardless of `decay_rate`, since `agent_handle` is always empty). |
| `tests/test_cli.py` | `quarry learn "<lesson>"` sends the expected body; `--topic`/`--name` map to `topic`/`name`; no `--agent-handle` or `--overwrite` flag exists on the `learn` command (shape parity with C2). |
| `tests/test_mcp_server.py` | `learn` tool happy path; blank-lesson rejection (`_reject_blank`); down-daemon error string; boundary-sentence presence in `remember`/`ingest`/`learn` docstrings (extends the existing trigger-vocabulary assertions at `tests/test_mcp_server.py:740-751`). |
| `tests/test_doctor_memory.py` (or wherever `MemoryDiagnostics` is covered today) | `_tally`'s new `lessons` count: present when lesson rows exist, absent (or zero) otherwise, unaffected by `agent_handle` presence. |
| A new or existing `tests/test_lesson.py` (NEW if no existing home) | `LessonComposer.document_name`: same `name`/`topic` pair called twice never produces the same string; empty `name` falls back to `topic`; empty both falls back to `"note"`; slug lowercases, strips non-alphanumerics, truncates at 40 chars. `LessonsCollection.for_repo`/`.resolve`/`.for_cwd`: mirrors `CapturesCollection`'s own existing test coverage shape for the analogous methods. |

Not in the write set: `src/quarry/results.py` (`SearchFilter`/`SearchResult`
already carry every field `learn` needs — no `topic` filter param, per D4's
explicit deferral); `src/quarry/db/schema.py` (no new column); `src/quarry/
formatting.py` (summary rendering unchanged, per Verified state); `plugin/
hooks/hooks.json` and `suppress-output.sh` (wildcard matcher and fallback
branch already cover `learn`, per Verified state).

## OO expectations for the implementation mission

- `LessonComposer` and `LessonsCollection` are both `@final`. `LessonComposer`
  is `__slots__ = ()` (pure static behavior, no instance state — a
  Single-Method-Interface-shaped class per PY-DP-11's reasoning: if pylint's
  R0903 fires on it having effectively one public entry point, that is the
  documented false-positive case, not a real smell, since `_slugify` is a
  legitimate private helper, not a second unrelated responsibility).
  `LessonsCollection` follows `CapturesCollection`'s existing shape exactly
  (no `__slots__`, since `CapturesCollection` doesn't declare one either —
  match the sibling, don't diverge for its own sake).
- The `_remember_job`/`_ingest_job` reservation guard (D7) is three lines
  each, referencing one shared module-level constant — resist the urge to
  extract a class for this; two call sites sharing one constant is not yet
  the third-occurrence threshold PY-RF-3 uses to justify extraction.
- `IngestionRoutes` grows by one public method (`learn`) and one private
  helper (`_learn_job`), plus two 3-line guards in existing methods. If
  `daemon/routes/ingestion.py` crosses its module-size budget as a result,
  the impl mission should extract a `LearnRoutes`-shaped grouping — but
  measure first; `remember`+`ingest`+`learn` sharing one cohesive
  "content-ingestion routes" module (the file's own docstring already
  states this framing) is very likely still comfortably within budget, and
  splitting three closely related, single-purpose routes into three files
  purely to shrink line count would be exactly the negotiate-with-the-
  ratchet anti-pattern this repo's CLAUDE.md warns against.
- `Settings.retrieval_lesson_boost` and `RetrievalConfig.lesson_boost`
  follow `retrieval_decay_rate`/`decay_rate`'s existing pattern field-for-
  field (same `Field(...)` shape, same "dataclass default is neutral,
  production value threaded at wire time" split) — do not invent a
  different threading mechanism for what is structurally the same kind of
  knob.
