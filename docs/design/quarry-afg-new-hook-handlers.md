# Four new hook handlers: SessionEnd, PostToolUse:WebSearch, PostToolUse:Read, SubagentStop

## Ratifications (2026-08-30)

- **R4b — SubagentStop ships with real content capture, not a stub.**
  The design body proposes a plumbing-only stub because the wire payload
  shape was unconfirmed. That is superseded: the operator captured one real
  payload on 2026-08-30 via a temporary debug hook. The sample is at
  `docs/design/quarry-afg-subagent-stop-payload-sample.json`.

  **Confirmed payload fields:**

  ```json
  {
    "session_id":            "<parent session id>",
    "transcript_path":       "<parent session transcript path>",
    "cwd":                   "<working directory>",
    "prompt_id":             "<opaque>",
    "permission_mode":       "default",
    "agent_id":              "<subagent id>",
    "agent_type":            "general-purpose | rmh | djb | ...",
    "effort":                {"level": "high"},
    "hook_event_name":       "SubagentStop",
    "stop_hook_active":      false,
    "agent_transcript_path": "<subagent's own transcript path, DISTINCT from parent's>",
    "last_assistant_message": "<subagent's final assistant message>",
    "background_tasks":      [...],
    "session_crons":         [...]
  }
  ```

  Key finding: `agent_transcript_path` is subagent-scoped and distinct from
  the parent's `transcript_path`. That answers the design's "Open empirical
  item 2" definitively.

  **Implementation implication:** `handle_subagent_stop` archives
  `agent_transcript_path` (not `transcript_path`), scrub-writes a local `.md`
  from it, and POSTs a `CaptureIngestRequest(cwd=cwd, session_id=agent_id,
  format_hint="markdown")` to `<repo>-captures`. Identity carrier is
  `agent_id`/`agent_type`, not `session_id` (that's the parent's). Reuse
  `SessionTranscriptCapture` from § "New module layout" parameterised by
  `label="subagent-stop"`.

  **Blocking-hook invariant unchanged:** handler must never emit a
  `decision` field or exit code 2; a bug here hangs every subagent.

## Verified state

- `src/quarry/_hook_entry.py:64-69` — `_HANDLERS` dispatches four events today:
  `session-setup`, `session-start`, `post-web-fetch`, `pre-compact`. Each entry
  is a zero-arg wrapper that lazily imports its handler and calls
  `run_hook(handler)` (`_hook_entry.py:40-61`).
- `src/quarry/_stdlib.py:140-150` — `run_hook` reads stdin JSON, calls the
  handler, writes stdout JSON, and is unconditionally fail-open (`except
  Exception: ... sys.stdout.write("{}\n")`). Every new handler inherits this
  for free — no handler needs its own top-level try/except.
- `src/quarry/_stdlib.py:38-72` — `HookConfig` is a frozen dataclass with three
  `bool` fields (`session_sync`, `web_fetch`, `compaction`), loaded from the
  `auto_capture` YAML-ish frontmatter block in `.punt-labs/quarry/config.md`
  via `Frontmatter(text).block("auto_capture")` + `_bool_field` (fails closed
  to `False` on an unparseable value, `_stdlib.py:80-103`).
- `src/quarry/enable.py:85-88` — the only place that writes `config.md`'s
  default `auto_capture` block for a newly enabled repo.
- `plugin/hooks/hooks.json:1-53` — registers `SessionStart` ×2, `PostToolUse`
  (a quarry-tool-output suppressor + `WebFetch`), and `PreCompact`. No
  `SessionEnd`, `SubagentStop`, or additional `PostToolUse` matchers exist.
- `plugin/hooks/web-fetch.sh` and `plugin/hooks/pre-compact.sh` — both are
  two-line dispatchers: a `$HOME/.punt-hooks-kill` kill-switch check, then
  `quarry-hook <event> 2>/dev/null || true`.
- `src/quarry/hooks.py` is 644 lines. Three responsibilities are interleaved
  in one module: session-start orchestration + background-sync process
  locking (`:43-316`), WebFetch capture (`:318-374`), and PreCompact transcript
  capture (`:377-643`, including the ethos-handle walker at `:377-408` and the
  daemon-send plumbing at `:438-505` that WebFetch also calls). This already
  exceeds the CLAUDE.md 500-line known-violation threshold; adding four more
  handlers in place would compound the debt CLAUDE.md requires the next touch
  to reduce.
- `src/quarry/web_capture.py` — the existing precedent for a payload-parsing
  dataclass alongside a handler: `WebFetchPayload` (frozen, `_raw: dict[str,
  object]`), two properties (`url`, `content`) that return `None` on any
  malformed/absent field, matching PY-EH-8's "absence is the documented
  contract" exception for optional lookups.
- `src/quarry/capture.py:26-88` — `CaptureRequest` + `CaptureWriter`, the single
  choke point (DES-036) for scrubbing and atomically writing a local `.md`
  capture file. Both `handle_pre_compact` and the new `handle_session_end`
  need it identically.
- `src/quarry/transcript_reader.py:17-37,39-62,79-98` — `TranscriptReader(path)`:
  `.text()` reads and truncates the JSONL transcript (missing file → `""`,
  read failure → `""`, never raises); `.archive(session_id, sessions_dir)`
  copies the raw JSONL, dedups prior archives for the same session (by
  filename prefix, keeping only the newest), and lazily prunes files older
  than 90 days. Because the on-disk transcript is cumulative across a
  session, calling `.archive()` again at `SessionEnd` after an earlier
  `PreCompact` naturally *supersedes* the compaction-time archive with the
  fuller one — no extra dedup logic needed.
- `src/quarry/api/capture_ingest.py:8-30` — `CaptureIngestRequest` fields:
  `content, cwd, document_name, session_id, overwrite=True, format_hint,
  agent_handle, memory_type, summary, source_url`. The docstring
  (`:11-13`) states the daemon derives `<repo>-captures` (or
  `default-captures`) from `cwd` — there is no `collection` field on this
  request type, unlike `IngestRequest`/`RememberRequest`.
- `src/quarry/daemon/routes/captures.py:31-79` — `CaptureRoutes.capture`
  always resolves the collection via `CapturesCollection.for_registry_path(cwd,
  registry_path)` (`:63-67`) and builds a `ScrubbedIngestJob` with
  `scrub_label="capture"`. There is no code path from `CaptureIngestRequest`
  to a `memory-<handle>` collection today.
- `src/quarry/daemon/ingest_jobs.py:28-51,59-77` — `ScrubbedIngestJob`'s
  docstring is explicit: "The free-form metadata (document name and summary)
  is scrubbed too, but at the choke point" — `_scrubbed(self.name)` runs
  `scrub_and_log(name, scrub_label)` before storage. **This means a raw
  filesystem path used as `document_name` is already PII-scrubbed
  server-side** (the `_PATH_RE`/`_EMAIL_RE`/hostname passes in
  `scrub.py:101,114,276-302` all run over `name`, not just `content`) — no
  client-side path redaction is needed for the new Read handler, unlike
  WebFetch's URL, which needs a client-side *structural* strip of
  userinfo/query/fragment (`hooks.py:345-347`) because that isn't PII-regex
  shaped and the generic scrubber wouldn't catch a bare `?token=...` query
  string.
- `hooks.py:377-408` — `_read_ethos_agent_handle(cwd)` walks up from `cwd`
  looking for `.punt-labs/ethos/config.yaml` and returns its `agent` field.
  Used today only by PreCompact. `docs/design/quarry-8kdo-memory-activation.md`
  (a sibling in-flight, **not yet implemented** — verified via `git log`,
  no commits landed on `main`) independently proposes extracting this exact
  function to `src/quarry/ethos_handle.py:read_agent_handle(cwd) -> str`
  because its design also needs it from a doctor module. This design reaches
  the same conclusion independently (SessionEnd and SubagentStop both need
  the handle too) — flagging the extraction so whichever mission lands first
  doesn't collide with a duplicate module.
- `docs/design/quarry-8kdo-memory-activation.md:135-169` (D3, **not yet
  ratified or implemented**) recommends PreCompact-style captures keep
  writing to `<repo>-captures` rather than `memory-<handle>`, with identity
  discovery happening at *read* time via `SearchFilter.agent_handle`. This
  design follows that same rule for `SessionEnd` (the same capture type) for
  consistency, and does not create a second, conflicting routing scheme.
- `docs/claude-code-quarry.tex:94` — `SessionEndReason ::= seClear | seLogout
  | sePromptExit | seBypassDisabled | seOther`; `:954,964` marks SessionEnd
  "Not yet wired — no SessionEnd hook is registered" in quarry's own Z model.
- `z-spec/examples/claude-code.tex:794-811` (`EndSession`) and `:627-650`
  (`StopSubagent`) are the abstract state-machine schemas: `SessionEnd` fires
  non-blocking with a `reason`; `SubagentStop` fires **blocking** — "exit 2
  prevents the subagent from stopping, forcing it to continue"
  (`:624-625`). `punt-kit/standards/hooks.md:70` confirms: `SubagentStop |
  Subagent finishes | spProcessing | Yes` (blocking = yes), unlike every
  other hook this design adds.
- `tests/test_hooks.py:34-24` (imports), `:1318-1339` (`TestHookWiring`) —
  the existing wiring test walks every `hooks.json` entry and asserts the
  referenced script exists and is executable. It will pick up new entries
  automatically; no test-file change is required there beyond adding the new
  scripts.

## Open empirical items — confirm before finalizing the two new payload parsers

Every payload field this design cites for `SessionStart`/`WebFetch`/
`PreCompact` is confirmed against this codebase's own tests
(`tests/test_hooks.py:712-724,761-767,948-950`) or the Z spec. Two of the
four new hooks touch fields nothing in this repo has captured yet:

1. **`PostToolUse:WebSearch`** — the exact shape of `tool_response` (a list of
   result objects vs. a single JSON-string blob) is not evidenced anywhere in
   this codebase or its docs.
2. **`SubagentStop`** — whether the payload carries a subagent-scoped
   `transcript_path`/result field distinct from the parent session's
   transcript is not evidenced anywhere in this codebase or its docs.

Per this repo's own debugging standard ("reproduce first, then fix — no
guessing"), the implementation mission's first step for each of these two
handlers must be to capture one real payload (print `payload` to stderr from
a temporary debug build, fire the tool once, inspect the JSON — the same
technique the investigation doc used to confirm the real `SessionStart`
context string, `docs/investigations/2026-08-30-agent-integration-gap.md:104-111`)
before writing the parser. `SessionEnd` and `PostToolUse:Read` reuse field
names (`session_id`, `transcript_path`, `cwd`, `tool_input`, `tool_response`)
already exercised by this codebase's own tests, so those two carry no such
gap.

## New module layout

`hooks.py` is already past its size budget (644 lines vs. the 500-line known-
violation ceiling in CLAUDE.md). CLAUDE.md's rule — "the next change to that
module must include extraction" — applies directly. This design extracts
three pieces of duplicated/misplaced logic out of `hooks.py` and adds the four
new handlers to a fresh module, rather than growing `hooks.py` further.

| File | Contents | Why a new module |
|---|---|---|
| `src/quarry/daemon_capture.py` (NEW) | `DaemonCaptureSender`, a `@final` class wrapping the three free functions currently at `hooks.py:438-505` (`_send_to_daemon`, `_capture_via_daemon`, `_ingest_url_via_daemon`) as methods. Same behavior, same four-failure-class logging (config error / connection / non-2xx / malformed response). | These three functions already violate PY-OO-7 — they operate on `QuarryClient`/`CaptureIngestRequest`/`IngestRequest` types, not on anything in `hooks.py` itself, and are about to gain three more call sites (SessionEnd, WebSearch, Read). A class stops the fourth copy-paste and gives every capture-producing hook one shared send path. |
| `src/quarry/ethos_handle.py` (NEW) | `read_agent_handle(cwd: str) -> str`, moved verbatim from `hooks.py:377-408`. | Three call sites after this design (PreCompact, SessionEnd, SubagentStop) justify extraction on its own; also avoids a second, divergent copy if `quarry-8kdo`'s independent extraction lands first — whichever mission lands second finds the module already exists and only needs to update its call site. |
| `src/quarry/session_transcript.py` (NEW) | `SessionTranscriptCapture`, a `@final` class extracted from the duplicated "archive JSONL → extract artifacts → write scrubbed `.md` → build `CaptureIngestRequest` → send" pipeline that today lives only inside `handle_pre_compact` (`hooks.py:564-643`). One method, e.g. `capture(cwd, session_id, transcript_path, label) -> CaptureOutcome`, parameterized by `label` (`"pre-compact"` vs `"session-end"`) so the scrub-log line still names its producer. `handle_pre_compact` is rewritten to call this class; it does not change behavior, only moves it. | `handle_session_end` needs the *identical* pipeline. Without extraction this is a fourth copy-paste (WebFetch/Read/WebSearch already share `daemon_capture.py`; this is the second near-duplicate, transcript-specific pipeline). |
| `src/quarry/web_search_capture.py` (NEW) | `WebSearchPayload`, a frozen dataclass parallel to `web_capture.py`'s `WebFetchPayload`: `_raw: dict[str, object]`, properties `query -> str \| None` and `digest -> str \| None` (a scrubbed markdown summary of the result list — see below). | Mirrors the existing `WebFetchPayload` precedent exactly; keeps parsing logic out of the handler function, same separation `hooks.py` already uses for WebFetch. |
| `src/quarry/read_capture.py` (NEW) | `ReadPayload` (parses `tool_input.file_path`, `tool_response`'s text) and `ReadCaptureFilter`, a `@final` class owning the four-step admission filter (in-tree exclusion, secret-path denylist, extension allowlist, size cap) as one method, e.g. `.should_capture(file_path, cwd) -> bool`. | The filter is the substantive logic this handler needs (bead quarry-afg item 3, mission criterion (d)) — it is not a one-liner, and bundling four independent checks into a single class method (rather than four free functions in `hooks_agent.py` that all take `file_path`) is exactly the PY-OO-7 pattern this codebase's rules name explicitly. |
| `src/quarry/hooks_agent.py` (NEW) | The four new handler functions: `handle_session_end`, `handle_post_web_search`, `handle_post_read`, `handle_subagent_stop`. Same shape as the handlers in `hooks.py` — module-level functions matching `Callable[[dict[str, object]], dict[str, object]]`, each doing config-gate → payload-parse → filter/decide → send. | Keeps the four new capture-family handlers out of the already-oversized `hooks.py`, and groups them together since they share `daemon_capture.py`, `ethos_handle.py`, and (for SessionEnd) `session_transcript.py`. `hooks.py` shrinks from 644 lines to roughly 350 after the three extractions above, comfortably under budget, and gains zero new lines for the four new handlers. |

`hooks.py` itself is touched only to: delete the three extracted pieces, add
the three new imports, and rewrite `handle_pre_compact` to call
`SessionTranscriptCapture` instead of its inline body. `handle_session_start`
and `handle_post_web_fetch` are otherwise unchanged.

## `HookConfig` additions

`src/quarry/_stdlib.py:38-72` — add four fields to the frozen `HookConfig`
dataclass and their `_bool_field` reads in `load_hook_config`:

```python
session_end: bool = True
web_search: bool = True
read: bool = False       # deliberate exception — see PostToolUse:Read below
subagent_stop: bool = True
```

`src/quarry/enable.py:85-88` — the default `config.md` template gains the
matching four lines so a freshly enabled repo's config documents every key
`load_hook_config` reads (mission criterion: "`.punt-labs/quarry/config.md`
gains matching `auto_capture` keys"). `read` is written as `false` in the
template, matching its code default.

## 1. `SessionEnd` → `handle_session_end`

**Payload** (fields confirmed by this codebase's identical use for
`PreCompact`, `hooks.py:191-198,542-561`, plus `docs/claude-code-quarry.tex:94`
for the reason enum): `session_id: str`, `transcript_path: str`, `cwd: str`,
`reason: str`.

**What to capture**: the full session transcript, via
`SessionTranscriptCapture` (above) — identical content to `PreCompact`'s
capture, just triggered on a different, *guaranteed* event. This is the
highest-leverage item in bead quarry-afg: `PreCompact` only fires on context
compaction, so any session short enough to never compact produces zero
capture (`docs/investigations/2026-08-30-agent-integration-gap.md:25-29,
184-193` — one transcript in `quarry-captures` from three months prior,
against seven live, never-captured transcripts in
`~/.claude/projects/*quarry*/`). `SessionEnd` fires on every session close
(`z-spec/examples/claude-code.tex:788` — "fires (non-blocking)" — unconditional
per the `EndSession` schema's guard, `sessionPhase = spIdle`), closing that
gap unconditionally.

**Collection routing**: `<repo>-captures` (or `default-captures` if `cwd` is
empty/unregistered), via the same `CaptureIngestRequest(cwd=..., session_id=...,
agent_handle=..., format_hint="markdown")` shape `PreCompact` already sends —
per 8kdo's D3 (not yet ratified, but the only decision on record), keep
transcript-type captures out of `memory-<handle>`.

**Scrub rules**: unchanged from `PreCompact` — `SessionTranscriptCapture`
routes through the existing `CaptureWriter`/`scrub_and_log` pipeline
(`capture.py:1-8,62-88`) for the local `.md` file, and the daemon's
`ScrubbedIngestJob` choke point (`ingest_jobs.py:59-77`) scrubs the wire
content and name again server-side. No new scrub category is needed; this is
the same content type `PreCompact` already produces safely.

**Error handling on daemon-unreachable**: identical fallback to `PreCompact`
(`hooks.py:625-636`) — the JSONL archive and the scrubbed `.md` are already
durable on disk by the time the daemon send is attempted, so a failed send
only means "not indexed yet"; `backfill-sessions` (bead quarry-kl1y) recovers
it later. Unlike `PreCompact`, `SessionEnd` has **no live user** to read a
`systemMessage` — the session is closing (`hook-lifecycle.md:178`: "Unlimited"
latency budget, "Session is ending anyway"). The handler always returns `{}`;
it does not construct a `systemMessage` on any path.

**Config gate**: `HookConfig.session_end`, key `auto_capture.session_end`.

**Example scenario**: a two-turn session with no compaction. User asks a
question, gets an answer, closes the terminal (`reason="clear"` or similar —
confirm the literal wire value empirically; the enum in
`claude-code-quarry.tex:94` names the cases but not their JSON spelling).
`handle_session_end` reads `transcript_path`, archives it
(`TranscriptReader.archive`, `transcript_reader.py:79-98`), writes the
scrubbed `.md` under `<cwd>/.punt-labs/quarry/captures/`, and POSTs a
`CaptureIngestRequest` to the daemon. In the *next* session, `quarry find
"what we discussed last time"` returns this transcript — where before this
change it returned nothing, per the investigation doc's verified corpus
count (`docs/investigations/2026-08-30-agent-integration-gap.md:184-188`).

## 2. `PostToolUse:WebSearch` → `handle_post_web_search`

**Matcher**: `"WebSearch"`, parallel to the existing `"WebFetch"` matcher
(`plugin/hooks/hooks.json:32-39`).

**Payload**: `tool_input: {"query": str, ...}` (confirmed shape family —
every `PostToolUse` `tool_input` in this codebase is a dict keyed by the
tool's own parameter names, per `WebFetchPayload.url`,
`web_capture.py:24-28`). `tool_response`'s exact shape is the first open
empirical item above — `WebSearchPayload.digest` must be written defensively
(try/except around any parse, matching `WebFetchPayload.content`'s
`except (ValueError, TypeError): return None` at `web_capture.py:41-44`) and
verified against one real captured payload before the mission closes.

**What to capture**: the query plus each result's title/URL/snippet, as a
short markdown digest — not a full page fetch (`WebSearch` never fetches
page bodies). Per bead quarry-afg item 2: "Auto-ingest valuable hits under
quarry-captures alongside WebFetch bodies."

**Collection routing**: `<repo>-captures`, via
`CaptureIngestRequest(content=digest, cwd=cwd, document_name=<scrubbed query
slug>, format_hint="markdown")`. No `source_url` — a search has no single
URL to re-fetch, so the SSRF-gated re-fetch fallback (`captures.py:81-96`) is
never exercised for this hook, matching how it is never exercised for
inline-content WebFetch either.

**Scrub rules**: the query text can contain anything the user typed,
including secrets pasted into a prompt that produced the search. Both the
document name and the content go through the daemon's existing
`ScrubbedIngestJob` choke point unconditionally (`ingest_jobs.py:59-77`) — no
new scrub category needed, same as every other capture type.

**Error handling on daemon-unreachable**: fire-and-forget via
`DaemonCaptureSender`; log at WARNING (parallel to `_WEB_FETCH_UNREACHABLE`,
`hooks.py:487-489`). There is no durable local copy of search results (unlike
a transcript, which is archived to disk first) — a lost send here is
genuinely lost, the same accepted tradeoff `WebFetch`'s inline-content path
already carries.

**Config gate**: `HookConfig.web_search`, key `auto_capture.web_search`.

**Example scenario**: agent runs `WebSearch("python 3.13 free-threading")`,
gets five results. The hook builds a digest ("# Web search: python 3.13
free-threading\n\n- [title](url): snippet\n..."), scrubs it, and POSTs to
`<repo>-captures`. A later `/find "free-threading research"` surfaces the
digest — the same write-then-retrieve loop `WebFetch` already closes
(investigation doc cause 6), extended to the search surface named
explicitly in bead quarry-afg item 2.

## 3. `PostToolUse:Read` → `handle_post_read`

**Matcher**: `"Read"`.

**Payload**: `tool_input: {"file_path": str, ...}`; `tool_response` carries
the file's text content (exact response-object shape is not load-bearing for
this design, since `ReadPayload` only needs the text and the path — the
implementation mission confirms the specific key(s) against one real
payload, same discipline as `WebSearch` above, but the risk is lower here
because the target field is "the text that was read," not a novel structure).

**Filter — the mission-critical part (bead quarry-afg item 3, criterion
(d))**: `Read` fires on every file read, including dozens of in-tree source
reads per session. `ReadCaptureFilter.should_capture(file_path, cwd)` runs
four checks, in order, fail-closed (any failure → do not capture, no daemon
call, no logging above DEBUG — this fires too often for INFO-level noise on
every skip):

1. **In-tree exclusion.** Resolve `cwd`'s covering collection via the same
   `CollectionResolver.covering_collection` `handle_session_start` already
   uses (`hooks.py:214,237`). If `file_path` is inside that directory, skip —
   it is already indexed by the session-start sync (`hooks.py:296`);
   capturing it again via `Read` is pure duplication.
2. **Secret-path denylist.** Reject `file_path` matching a fixed,
   case-insensitive set of fragments regardless of location: `.env`,
   `.env.*`, `id_rsa`, `id_ed25519`, `*.pem`, `*.key`, `known_hosts`,
   `.netrc`, `.aws/credentials`, any path under `.ssh/`. This runs
   independently of the in-tree check — a secrets file symlinked into the
   tree, or a path outside any registered collection, must never be
   captured either way. This is the concrete answer to mission criterion
   (d): "PostToolUse:Read may see file paths that shouldn't be indexed —
   think .env, SSH keys, tokens."
3. **Extension allowlist.** Only capture formats quarry's `loaders/` already
   understand as prose/documents (`.md`, `.txt`, `.rst`, `.pdf`, `.docx`,
   and similar) — reading an out-of-tree `.py`/`.json`/`.log` is not "durable
   knowledge" by default; no config override for this list in v1.
4. **Size cap.** Skip content over a fixed byte cap (e.g. 200 KB) so one
   large PDF read cannot dominate the capture queue.

**Collection routing**: `<repo>-captures`, via
`CaptureIngestRequest(content=file_text, cwd=cwd, document_name=file_path,
format_hint="auto")`. No client-side path redaction is needed —
`document_name` is scrubbed server-side at the `ScrubbedIngestJob` choke
point (`ingest_jobs.py:59-77`, confirmed above), which already redacts home
directories via `_PATH_RE` (`scrub.py:101`). This differs from `WebFetch`'s
URL, which needs a client-side *structural* strip (userinfo/query/fragment)
because that shape is not something the PII regex passes target.

**Error handling on daemon-unreachable**: fire-and-forget, WARNING log, no
retry — the original file is untouched on disk (nothing is lost except the
*capture*, and a future `Read` of the same path re-attempts it).

**Config gate**: `HookConfig.read`, key `auto_capture.read`, **default
`False`** — the one deliberate exception to every other `auto_capture` key
in this codebase, which default `True` (`_stdlib.py:69-71`). `Read` fires
far more often than any other hook and has the highest secret-leak surface
of the three checks above; shipping it opt-in lets an operator confirm the
filter set is not producing garbage before it captures unattended.

**Example scenario**: agent runs `Read("~/Documents/vendor-api-spec.pdf")`
while researching an external integration. All four filter checks pass
(external path, `.pdf` allowed, under the size cap, not a denylisted secret
path). The hook POSTs the extracted text to `<repo>-captures`; a later
`/find "vendor API rate limits"` surfaces it. Contrast: `Read(".env")` is
rejected by the secret denylist; `Read("src/quarry/hooks.py")` is rejected by
the in-tree exclusion — neither reaches the daemon.

## 4. `SubagentStop` → `handle_subagent_stop`

**This hook is blocking, unlike the other three new hooks and every existing
quarry hook.** `z-spec/examples/claude-code.tex:624-625`: "exit 2 prevents
the subagent from stopping, forcing it to continue."
`punt-kit/standards/hooks.md:70` confirms `SubagentStop` is the only new
event in this design where "Can block?" is `Yes`. Quarry's handler **must
never** populate a decision/block field in its response — always return `{}`
(or, once content-capture is implemented, `{}` plus at most
`hookSpecificOutput.additionalContext`, never a `decision` key). This is
already how every other quarry handler behaves (`run_hook`'s fail-open
wrapper, `_stdlib.py:140-150`, never sets a decision field either), so this
is a **must-not-regress** item for the evaluator checklist rather than new
behavior to write — but it is the one item on this design where a bug has a
uniquely bad consequence: hanging every subagent in the session, not just
losing a capture.

**Payload**: the abstract model (`z-spec/examples/claude-code.tex:627-650`)
names `agentId`, `hookResult`, `resultContext` — but does not establish the
real wire field names, and nothing in this codebase's tests or docs has
captured a real `SubagentStop` payload. This is the second (and higher-risk)
open empirical item above: it is not established whether the payload
carries a subagent-scoped `transcript_path`/result field distinct from the
parent session's own transcript.

**Recommendation — ship the plumbing now, defer the content parser.** Rather
than guess a payload shape, wire `subagent-stop` into `_HANDLERS`,
`hooks.json`, and `HookConfig` completely (so the config surface is stable
and the dispatcher entry exists), but have `handle_subagent_stop` do nothing
beyond a config check and a `logger.debug` breadcrumb, returning `{}`
unconditionally:

```python
def handle_subagent_stop(payload: dict[str, object]) -> dict[str, object]:
    """Handle SubagentStop hook.

    Content capture is not yet implemented — the real payload shape has not
    been confirmed against a live session.  This stub exists so the
    dispatcher entry, matcher, and config key are stable before that work
    lands; see docs/design/quarry-afg-new-hook-handlers.md.
    """
    cwd = _as_dir(payload.get("cwd"))
    if cwd and not load_hook_config(cwd).subagent_stop:
        logger.debug("subagent-stop: disabled by config")
    else:
        logger.debug("subagent-stop: payload shape not yet confirmed, no-op")
    return {}
```

This is more honest than shipping a parser against a guessed field name that
may never match the real wire payload and would fail silently forever (fail-
open hides exactly this class of bug — a stub that says so in its own log
line is strictly better than a parser that is wrong quietly). The follow-up
increment — once a live payload is captured — fills in content capture using
the same `CaptureIngestRequest` → `<repo>-captures` path as the other three
handlers (or `memory-<handle>` if 8kdo's routing has landed by then and the
subagent's `agent_handle` resolves via `ethos_handle.read_agent_handle`).

**Timeout**: `hooks.json`'s `SubagentStop` entry needs an explicit `timeout`
well above quarry's own daemon-send budget so quarry is never the reason a
subagent hangs (`_CAPTURE_SEND_TIMEOUT = 5.0` at `hooks.py:482`, unchanged) —
`8000` ms, comfortably above that but far below `PreCompact`'s existing
`30000` (`hooks.json:47`), since a *blocking* hook's timeout cost is paid by
every subagent, every time, and this stub does no network I/O at all.

**Config gate**: `HookConfig.subagent_stop`, key `auto_capture.subagent_stop`,
reserved now (default `True`, matching the "observational, low risk" default
every other capture hook uses) so the config schema does not need a second
change when content capture is filled in.

## `plugin/hooks/hooks.json` entries

```json
{
  "hooks": {
    "SessionStart": [ ... unchanged ... ],
    "PostToolUse": [
      { "matcher": "mcp__(plugin_quarry(-dev)?_)?quarry(-proxy)?__.*", "hooks": [ ... unchanged ... ] },
      { "matcher": "WebFetch", "hooks": [ ... unchanged ... ] },
      {
        "matcher": "WebSearch",
        "hooks": [
          { "type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/web-search.sh" }
        ]
      },
      {
        "matcher": "Read",
        "hooks": [
          { "type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/post-read.sh" }
        ]
      }
    ],
    "PreCompact": [ ... unchanged ... ],
    "SessionEnd": [
      {
        "hooks": [
          { "type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-end.sh" }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/subagent-stop.sh",
            "timeout": 8000
          }
        ]
      }
    ]
  }
}
```

`hooks.json`'s schema already supports all three new event keys and an
additional `PostToolUse` matcher entry — the existing file is proof: it
already has two independent `PostToolUse` matcher blocks
(`hooks.json:21-40`) and a `PreCompact`/`SessionStart` top-level key
alongside them (`hooks.json:1,41`), so `SessionEnd` and `SubagentStop` are
siblings of the same shape, not a new schema feature. `tests/test_hooks.py:
1318-1339` (`TestHookWiring`) already walks every entry generically and will
cover the four new ones without modification, once the four new shell
scripts exist.

## `plugin/hooks/*.sh` — four new two-line dispatchers

Exact copy of the `pre-compact.sh`/`web-fetch.sh` pattern (kill-switch +
`quarry-hook <event> 2>/dev/null || true`):

- `plugin/hooks/session-end.sh` → `quarry-hook session-end`
- `plugin/hooks/web-search.sh` → `quarry-hook post-web-search`
- `plugin/hooks/post-read.sh` → `quarry-hook post-read`
- `plugin/hooks/subagent-stop.sh` → `quarry-hook subagent-stop`

All four must be `chmod +x` — `TestHookWiring.test_all_referenced_scripts_exist`
(`tests/test_hooks.py:1326-1339`) asserts executability, not just existence.

## `src/quarry/_hook_entry.py` dispatcher additions

Four new wrapper functions, same shape as the existing four
(`_hook_entry.py:40-61`), all importing from the new `hooks_agent` module:

```python
def _session_end() -> None:
    from quarry.hooks_agent import handle_session_end  # noqa: PLC0415

    run_hook(handle_session_end)


def _post_web_search() -> None:
    from quarry.hooks_agent import handle_post_web_search  # noqa: PLC0415

    run_hook(handle_post_web_search)


def _post_read() -> None:
    from quarry.hooks_agent import handle_post_read  # noqa: PLC0415

    run_hook(handle_post_read)


def _subagent_stop() -> None:
    from quarry.hooks_agent import handle_subagent_stop  # noqa: PLC0415

    run_hook(handle_subagent_stop)


_HANDLERS: dict[str, Callable[[], None]] = {
    "session-setup": _session_setup,
    "session-start": _session_start,
    "post-web-fetch": _post_web_fetch,
    "pre-compact": _pre_compact,
    "session-end": _session_end,
    "post-web-search": _post_web_search,
    "post-read": _post_read,
    "subagent-stop": _subagent_stop,
}
```

Event-name mapping follows the existing convention exactly: hyphenated,
verb-first for `PostToolUse` matchers (`post-web-fetch` → `post-web-search`,
`post-read`), bare event name otherwise (`pre-compact` → `session-end`,
`subagent-stop`). No change to `main()` or the CLI's dispatch logic
(`_hook_entry.py:24-34`) — it already looks up by string key with no
knowledge of the handler count.

## Write set for the implementation mission

| File | Change |
|---|---|
| `src/quarry/daemon_capture.py` (NEW) | `DaemonCaptureSender`, extracted from `hooks.py:438-505`. |
| `src/quarry/ethos_handle.py` (NEW) | `read_agent_handle(cwd)`, extracted from `hooks.py:377-408`. Check whether `quarry-8kdo`'s mission already created this file first; if so, only update call sites. |
| `src/quarry/session_transcript.py` (NEW) | `SessionTranscriptCapture`, extracted from `hooks.py:564-643`'s inline pipeline. |
| `src/quarry/web_search_capture.py` (NEW) | `WebSearchPayload`. |
| `src/quarry/read_capture.py` (NEW) | `ReadPayload`, `ReadCaptureFilter`. |
| `src/quarry/hooks_agent.py` (NEW) | `handle_session_end`, `handle_post_web_search`, `handle_post_read`, `handle_subagent_stop`. |
| `src/quarry/hooks.py` | Delete the three extracted pieces; rewrite `handle_pre_compact` to call `SessionTranscriptCapture`; no other behavior change. Net: shrinks well under the 500-line ceiling. |
| `src/quarry/_hook_entry.py` | Four new wrapper functions + four new `_HANDLERS` entries, per above. |
| `src/quarry/_stdlib.py` | `HookConfig` gains `session_end`, `web_search`, `read` (default `False`), `subagent_stop`; `load_hook_config` reads all four via `_bool_field`. |
| `src/quarry/enable.py` | Default `config.md` template (`:85-88`) gains the four matching lines. |
| `plugin/hooks/hooks.json` | `SessionEnd` and `SubagentStop` top-level keys; two new `PostToolUse` matcher entries (`WebSearch`, `Read`). |
| `plugin/hooks/session-end.sh`, `web-search.sh`, `post-read.sh`, `subagent-stop.sh` (NEW, executable) | Two-line dispatchers per the existing pattern. |
| `tests/test_daemon_capture.py` (NEW) | `DaemonCaptureSender`: the four failure classes (config error, connection, non-2xx, malformed response) each return `False` without propagating (bug class 2 checklist) — same cases `hooks.py`'s current tests already cover for the free-function form, moved and extended per new call sites. |
| `tests/test_ethos_handle.py` (NEW, or shared with `quarry-8kdo`'s if it lands first) | Walker behavior, moved from wherever `test_hooks.py` covers `_read_ethos_agent_handle` today. |
| `tests/test_session_transcript.py` (NEW) | `SessionTranscriptCapture.capture`: happy path, missing transcript, daemon-unreachable fallback, archive-dedup-on-second-call (PreCompact then SessionEnd in one session). |
| `tests/test_hooks_agent.py` (NEW) | One test class per handler: `TestHandleSessionEnd`, `TestHandlePostWebSearch`, `TestHandlePostRead`, `TestHandleSubagentStop`. Each: happy path, config-off path (bug class 2's "falls back, exit 0" pattern applied to a hook context: returns `{}` without raising), malformed payload. `TestHandlePostRead` additionally covers all four filter branches independently (in-tree, secret path, disallowed extension, oversized) plus one path that passes all four. `TestHandleSubagentStop` asserts the stub never emits a `decision`/block field under any input, including a payload crafted to look like a real result. |
| `tests/test_web_search_capture.py` (NEW) | `WebSearchPayload` parsing: valid payload, missing `tool_input`, malformed `tool_response` (mirrors `web_capture.py`'s own test coverage pattern). |
| `tests/test_read_capture.py` (NEW) | `ReadPayload` parsing; `ReadCaptureFilter.should_capture` — each of the four checks tested independently and in combination. |
| `tests/test_hooks.py` | Delete the extracted-function tests that move to the new test files; `TestHandlePreCompact` keeps asserting the same observable behavior against the rewritten (delegating) `handle_pre_compact`. `TestHookWiring` needs no change — it is generic over `hooks.json`'s contents. |
| `.punt-labs/quarry/config.md` (this repo's own) | Add the four new keys so quarry's own dogfood config documents itself, matching `enable.py`'s new template. |

## OO expectations for the implementation mission

- `DaemonCaptureSender`, `SessionTranscriptCapture`, `ReadCaptureFilter` are
  each `@final`, `__slots__ = ()` where they hold no state beyond
  constructor-injected config (none of the three need instance state — they
  are behavior classes over their method arguments, the correct shape per
  PY-OO-7 once the free functions they replace are absorbed as methods).
- `WebSearchPayload`, `ReadPayload` are `@dataclass(frozen=True, slots=True)`
  wrapping `_raw: dict[str, object]`, exactly matching `WebFetchPayload`'s
  existing shape (`web_capture.py:9-19`) — do not invent a different pattern
  for the two new payload types.
- `hooks_agent.py`'s four handler functions are the one place free functions
  are correct: they are the `Callable[[dict[str, object]], dict[str,
  object]]` shape `run_hook` requires (`_stdlib.py:140`), matching every
  existing handler in `hooks.py` — do not wrap them in a class merely to
  satisfy a method-ratio metric; a module of thin orchestration functions
  that each delegate to a real class (`DaemonCaptureSender`,
  `SessionTranscriptCapture`, `ReadCaptureFilter`) is the intended shape, not
  a violation.
- If `hooks_agent.py` grows past 300 lines once all four handlers and their
  docstrings are written, split by hook family before it reaches `hooks.py`'s
  current size — do not let the new module recreate the debt this design
  extracts `hooks.py` out of.
