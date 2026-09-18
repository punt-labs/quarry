# Agent memory loop — design (m-2026-09-09-006)

Worker: rmh · Evaluator: kpz · Leader: claude · Date: 2026-09-08

Goal (operator-ratified): agents remember the work they do and benefit from it.
Recall-on-launch already works and is not redesigned here. This document
designs the dormant write side as three loops and hands the implementation
mission a precise write-set:

1. **Loop 1 — persist habit + correct attribution.**
2. **Loop 2 — evaluator feedback → the worker's memory.**
3. **Loop 3 — capture distillation for SubagentStop transcripts.**

Every claim below was checked against the working tree at `refactor/pipeline-decomp`
(HEAD `a0a4ac4`), the live daemon, and the sibling `ethos` checkout. Three
statements in the mission's "ground truth" turned out to be inaccurate; they
are corrected in §a.7 with evidence, and the design builds on the corrected
facts.

Section map: a current state · b loop 1 · c loop 2 · d loop 3 · e write-set ·
f test plan · g rejected alternatives + ADR plan · h rollback coherence.

**Round 2 amendments** (kpz's round-1 reflection, all findings resolved in
place): Loop 2 document names carry a repo discriminator and the existence
check became an identity check (§c.3–c.4); the vendored ext refresh rides
`quarry enable`, not `quarry doctor` (§b.4, D3, §g.3, §h); the two modules
already past the 500-line limit are extracted, not grown — `handle_pre_compact`
leaves `hooks.py` for `hooks_compact.py`, the MCP guard and the new tool leave
`mcp_server.py` for `mcp_guard.py`/`mcp_missions.py`, and relax R7 is gone
(§e); no-reflection frozen rounds are specified and the composed memory labels
the worker's verdict as the worker's (§c.1, §c.3, Appendix B); the demo gate
expects doctor's `identity_active` warning after Loop 3 first fires (§h);
the `collections.abc` coupling holds are named as scorer bookkeeping (§e.5);
the bundle layer and the walk-vs-precedence attribution are stated (§a.5,
§b.2); the evaluator-prose and `id8`-budget notes are in §c.3 and §d.1.

---

## a. Current-state map

### a.1 Recall works

Ethos injects each identity's `session_context` (from the `quarry.yaml` ext) at
SessionStart, PreCompact, and SubagentStart (`ethos/internal/hook/extension_context.go:11-38`
joins every namespace's `session_context` key). Quarry owns the text: the
template is `_SESSION_CONTEXT_TEMPLATE` at `src/quarry/doctor_ethos.py:12-36`,
written by `EthosExtDiagnostics.write_session_context` (`doctor_ethos.py:85-109`)
and by `quarry enable`'s `EthosMemoryBootstrap` (`src/quarry/ethos_memory.py:61-93`).
The rendered block a spawned `rmh` subagent receives is the one on disk at
`~/.punt-labs/ethos/identities/rmh.ext/quarry.yaml` and, byte-identical, at
the vendored `.punt-labs/ethos/identities/rmh.ext/quarry.yaml` in this repo
(`diff` is empty; both are v1 of the template).

The read path honours the handle end to end: `find(agent_handle=...)` is a
`SearchFilter` predicate on the shared `chunks` table
(`src/quarry/results.py:209-237`), applied by `SearchRoutes._filter`
(`src/quarry/daemon/routes/search.py:66-76`), with decay and lesson boost
threaded from settings (`search.py:41-44`; `src/quarry/config.py:102-121`).
Nothing on the read side changes in this design.

### a.2 The three write paths (dormant)

| Verb | Route / job | Collection rule | `agent_handle` | `memory_type` | Scrub |
|---|---|---|---|---|---|
| `remember` | `POST /v1/remember` → `IngestionRoutes._remember_job` (`src/quarry/daemon/routes/ingestion.py:97-134`) → `ScrubbedIngestJob` (`src/quarry/daemon/ingest_jobs.py:51-121`) | explicit wins; else `memory-<handle>` when a handle is given; else `default` (`ingestion.py:176-183`) | caller-supplied (`ingestion.py:120`); no server-side resolve | caller-supplied; only `"lesson"` rejected (`ingestion.py:116-119`; `routes/base.py:102-117`) | always, server-side, before embed/store (`ingest_jobs.py:76-81,103-121`; pipeline choke point `src/quarry/ingestion/pipeline.py:450-456`) |
| `learn` | `POST /v1/learn` → `_learn_job` (`ingestion.py:136-174`) | `<repo>-lessons` from `cwd` via `LessonsCollection.for_registry_path` (`ingestion.py:159-163`; `src/quarry/lesson.py:84-95`) | forced `""` (`ingestion.py:171`) | forced `"lesson"` (`ingestion.py:172`); 500-char cap (`ingestion.py:147-156`) | same job |
| `ingest` | `POST /v1/ingest` → `_ingest_job` (`ingestion.py:185-212`) | explicit / URL host / `<repo>-captures` when `scrub` | caller-supplied (`ingestion.py:209`) | caller-supplied, `"lesson"` rejected (`ingestion.py:195-198`) | only when `scrub` (web-fetch re-fetch) |
| `capture` (hooks) | `POST /v1/capture` → `CaptureRoutes._capture_job` (`src/quarry/daemon/routes/captures.py:47-80`) | always `<repo>-captures` from `cwd` (`captures.py:68,82-90`) | caller-supplied (`captures.py:76`) | caller-supplied, `"lesson"` rejected (`captures.py:60-62`); every hook sends `""` | always (`scrub_label="capture"`, `captures.py:75`) |

The wire models carry exactly these fields: `RememberRequest`
(`src/quarry/api/ingestion.py:8-24`, `collection` empty-string sentinel),
`LearnRequest` (`api/ingestion.py:48-60`, `cwd` set by the client at
`src/quarry/client/client.py:154-173`), `CaptureIngestRequest`
(`src/quarry/api/capture_ingest.py:8-30`, no `collection` field by design).

Surfaces that already expose `remember` with the memory fields: CLI
`quarry remember --agent-handle --memory-type --summary`
(`src/quarry/cli_ingest.py:93-165`; note the `--memory-type` help text at
`cli_ingest.py:126` lists "fact, observation, opinion" and omits `procedure`),
MCP `remember(agent_handle=, memory_type=, summary=)` (`src/quarry/mcp_server.py:225-280`),
slash `/remember` (`plugin/commands/remember.md`, passes only `content` and
`document_name`).

### a.3 `memory_type` taxonomy, decay, boost

The vocabulary is defined **three times**, by hand, with no shared source:

- `src/quarry/retrieval/fusion.py:16-19` — `_DECAYABLE_TYPES = {fact, observation, opinion, procedure}`, `_LESSON_TYPE = "lesson"`.
- `src/quarry/doctor_memory.py:21-27` — `_MEMORY_TYPES` (same four), `_LESSON_TYPE`.
- `src/quarry/daemon/routes/base.py:29-32` — `RESERVED_MEMORY_TYPE = "lesson"`.

Behaviour, from `RrfFusion._contribution` (`fusion.py:80-97`):

- decay applies iff `decay_rate > 0 and memory_type in _DECAYABLE_TYPES and agent_handle` (`fusion.py:86-91`) — agent-owned, classified rows only;
- `lesson_boost` multiplies rows with `memory_type == "lesson"` (`fusion.py:96`);
- `""` (plain document / capture) never decays and is never boosted.

Production defaults: `retrieval_decay_rate = 0.000963` (30-day half-life,
`config.py:95-106`), `retrieval_lesson_boost = 1.5` (`config.py:108-121`).
Any other string (`"facts"`, `"Procedure"`) is accepted today by all three
write routes and stored as a row that neither decays nor matches a typed
filter — a silent misclassification.

### a.4 The capture path (the transcript-dump problem)

`HookAgent.subagent_stop` (`src/quarry/hooks_agent.py:258-306`) is a **blocking**
hook (`hooks_agent.py:10-13,262-266`; `plugin/hooks/hooks.json` `SubagentStop`
timeout 30000 ms). It reads `agent_transcript_path` and `agent_id`
(`hooks_agent.py:285-291`), resolves the handle as
`agent_type or EthosConfig.agent_handle_at(cwd)` (`hooks_agent.py:297,303`),
and runs `SessionTranscriptCapture(...).capture()` (`src/quarry/session_transcript.py:82-122`):

1. archive the raw JSONL to `~/.punt-labs/quarry/sessions/session-<id8>-<ts>.jsonl` (`session_transcript.py:124-132`; `src/quarry/transcript_reader.py:79-106`);
2. extract text — every user/assistant turn, tool results ≤500 chars, front-truncated to 500 000 chars (`transcript_reader.py:39-77,108-171`);
3. write the scrubbed `.md` under `<cwd>/.punt-labs/quarry/captures/` (`session_transcript.py:134-154`; `src/quarry/capture.py:57-88`);
4. POST `CaptureIngestRequest(content=wire_text, cwd, session_id=agent_id, agent_handle, format_hint="markdown")` (`session_transcript.py:156-171`) via `DaemonCaptureSender.send_capture` with a 5 s cap (`src/quarry/daemon_capture.py:28,48-55`).

The daemon files it as document `session-<agent_id[:8]>` (`captures.py:109-124`)
in `<repo>-captures`, `memory_type=""`, `summary=""`. So a subagent's whole
transcript — prompts, tool chatter, the final report — lands as an unclassified
dump, with no summary, that never decays. The payload also carries
`last_assistant_message`, confirmed in the operator-captured sample
(`docs/design/quarry-afg-subagent-stop-payload-sample.json:1`), which the hook
ignores today.

### a.5 Attribution today — every site, what it resolves

`EthosConfig.agent_handle_at(cwd)` (`src/quarry/ethos_handle.py:28-40`) walks
from `cwd` to `/` (`ethos_handle.py:42-52`) reading
**only** `.punt-labs/ethos/config.yaml` (`ethos_handle.py:19`).

| Site | Resolution | Correct for |
|---|---|---|
| PreCompact `hooks.py:755-761` | `agent_handle_at(cwd)` | the parent session (the leader) |
| SessionEnd `hooks_agent.py:71-77` | `agent_handle_at(cwd)` | the parent session |
| SubagentStop `hooks_agent.py:297-304` | `agent_type`, else `agent_handle_at(cwd)` | the subagent — **when `agent_type` is an identity handle** |
| doctor `identity_active` `doctor_memory.py:113-148` | `agent_handle_at(cwd)` | the leader |
| `remember` / `learn` / `ingest` | none — caller-supplied | whoever passes it |

Two nuances the mission names, plus one it did not:

- **cwd ≠ subagent.** A subagent's `cwd` is the repo; the repo's pin names the leader. Auto-resolving a subagent's `remember` from `cwd` would tag `rmh`'s memory as `claude`. The subagent knows its handle only from the injected `## Memory` block. SubagentStop already sidesteps this with `agent_type` (`hooks_agent.py:303`; test `tests/test_hooks_agent.py:549-571`).
- **`agent_type` is not always an identity.** The confirmed sample carries `agent_type: "general-purpose"` (`quarry-afg-subagent-stop-payload-sample.json:1`); bare `Agent()` reviewers (`code-reviewer`, `silent-failure-hunter`, `researcher`) also fire SubagentStop. Today those captures get `agent_handle="general-purpose"` — a handle no identity owns and no `find --agent-handle` will ever ask for.
- **The walker reads the wrong file (new finding).** Ethos ≥ 4 reads `.punt-labs/ethos.yaml` first and falls back to the legacy `.punt-labs/ethos/config.yaml` (`ethos/internal/resolve/resolve.go:213-223`). This repo has only the new file (`.punt-labs/ethos.yaml`: `agent: claude`, `resolution: repo-only`); no ancestor has the legacy file (checked `quarry/`, `punt-labs/`, `~`). So in this repo `agent_handle_at(cwd) == ""` on every call: PreCompact/SessionEnd captures are unattributed, the SubagentStop fallback is `""`, and `quarry doctor` reports "no ethos identity active". Two behaviours meet here and only one is ethos's: the *ancestor walk* is quarry's own (`ethos_handle.py:42-52`; ethos's `LoadRepoConfig` reads the git root only, no walk), while the *new-then-legacy file precedence* is ethos's (`resolve.go:213-223`). The fix keeps quarry's walk and adopts ethos's precedence at each step of it.

### a.6 The loop has never run

`quarry list collections` against the live daemon (active database) returns 7
collections: `pembroke`, `pembroke-captures`, `punt-labs`, `punt-labs-captures`,
`sorcery`, `web-captures`, `xboing-c`. **Zero `memory-*` collections and zero
`*-lessons` collections exist.** No agent has ever called `remember` with a
handle, and nobody has called `learn`. The recall side has been reading an
empty shelf.

### a.7 Corrections to the mission's ground truth

1. **Mission bodies are not in the log JSONL.** `.punt-labs/local/ethos/missions/<id>/<session>.log.jsonl` holds event rows only — `{"event":"reflect","details":{"converging":false,"recommendation":"pivot","signal_count":6}}` — no signal text, no prose. The bodies live in the sealed per-mission directory `.punt-labs/ethos/missions/<id>/`: `contract.yaml` (`mission_id, status, repo, leader, worker, evaluator.handle, inputs, write_set, success_criteria, budget, current_round, context`), `results.yaml` (`results: [{mission, round, created_at, author, verdict, confidence, files_changed[{path,added,removed}], evidence[{name,status}], open_questions[], prose}]`), `reflections.yaml` (`reflections: [{mission, round, created_at, author, converging, signals[], recommendation, reason}]`), plus `delegations/d-*/{prompt.md,record.yaml}` and sealed `log-*.jsonl` copies. `.punt-labs/ethos/missions.jsonl` keeps only closed summaries (no bodies). All of it is gitignored runtime state (`.gitignore:276-277`; `.punt-labs/local/` at `.gitignore:24`). Loop 2 reads the YAML trio.
2. **`agent_handle_at(cwd)` yields `""` here, not `claude`** — §a.5. The mission's premise ("yields the REPO DEFAULT (claude)") is what the code intends; the legacy path makes it worse in practice.
3. **The injected ext comes from the vendored repo layer.** Under `resolution: repo-only`, ext data is loaded from the identity's own source layer (`ethos/internal/identity/layered.go:94-100`). Quarry's writers touch only `~/.punt-labs/ethos/identities/` (`ethos_memory.py:15`; `doctor_ethos.py:53-54`). A guide upgrade that only rewrites the global file never reaches an `rmh` spawned in this repo.

---

## b. Loop 1 — persist habit and correct attribution

### b.1 Decision: (iii) both, with one explicit non-decision

**(i) Guidance** is the only mechanism that can make an agent call `remember`
at the right moment with the right handle, because the daemon has no identity
signal for a `remember` (§a.2: caller-supplied; `RememberRequest` has no `cwd`).
The current template says *how* to call, never *when* or *what*. Version 2 of
the block (b.4) fixes that and is delivered through the existing writer — but
the writer must learn to refresh a stale block and to write the vendored layer.

**(ii) Server-side validation** of the `memory_type` vocabulary (b.5) — one
`MemoryType` source of truth replacing the three hand copies in §a.3, and a
400 on an unknown type on all three write routes.

**Not done: server-side auto-resolve of `agent_handle`.** Rejected for three
independent reasons: the daemon receives no `cwd` on `remember`; even with one,
`cwd` resolves to the leader for every subagent (§a.5); and in remote mode
(`QUARRY_URL`) the daemon host has no view of the client's repo at all. The
handle is the caller's statement of identity and stays that way. The fix for
"a subagent must not be tagged as the leader" is therefore two-sided: the
guide tells the agent to always pass its own handle (b.4), and the one hook
that runs *for* a subagent resolves identity from `agent_type`, never `cwd`
(b.2).

### b.2 Attribution rule — one table, one class per side

| Producer | Handle source | Module |
|---|---|---|
| Parent-session hooks (PreCompact, SessionEnd) | `EthosConfig.agent_handle_at(cwd)`, now reading `.punt-labs/ethos.yaml` first, legacy `config.yaml` second, at every ancestor — the walk is quarry's existing behaviour (`ethos_handle.py:42-52`), the new-then-legacy precedence is ethos's (`resolve.go:213-223`) | `ethos_handle.py` (MODIFY) |
| SubagentStop | `SubagentCapture.handle_for(agent_type, cwd)`: `agent_type` **iff** it is a registered identity — `<handle>.yaml` exists in the nearest vendored `.punt-labs/ethos/identities/` or in `~/.punt-labs/ethos/identities/` — else `""`. `agent_type` absent → `agent_handle_at(cwd)` (unchanged fallback). The handle is validated against `^[a-z0-9-]{1,64}$` before any path is built: `agent_type` is hook input and must never become a path segment. | `subagent_capture.py` (NEW) via `EthosTree` (NEW) |
| `remember` / `learn` (interactive) | caller-supplied; the v2 guide instructs the agent to pass its own handle and says why | `ethos_ext_block.py` (NEW) |
| Loop 2 mission memories | `contract.yaml: worker` — never `cwd` | `mission_memory.py` (NEW) |
| Loop 3 distilled reports | same as SubagentStop | `subagent_capture.py` |

Consequences, stated so nobody is surprised: a `general-purpose`/`Explore`/
`code-reviewer` subagent's transcript is filed **unattributed** (`""`) rather
than as `general-purpose`, and no distilled memory is written for it (d.2).
Attributing it to the leader was considered and rejected: it would inflate
the leader's `identity_active` count (`doctor_memory.py:196-207`) and pollute
`find(agent_handle="claude")` with work the leader did not do.

**Known v1 limitation — the bundle layer.** `EthosTree.identity_exists`
looks in exactly two places: the nearest vendored `identities/` and the
global one. Ethos also resolves identities from read-only bundles under
`~/.punt-labs/ethos/bundles/{foundation,gstack}` (`layered.go:635-643`), so a
subagent whose `agent_type` names a *bundle-only* identity is filed
unattributed (`""`) and gets no distilled report, exactly like
`general-purpose`. Nobody on the quarry roster is bundle-only (all eight are
vendored *and* global), the bundle layout is ethos-internal and unversioned
from quarry's side, and reading it would be a third path to keep in step; it
is stated here so the gap is a known one, and revisiting it is a one-method
change to `EthosTree` when a bundle-only worker appears.

### b.3 What is durable, when to write it, where it lands

| Kind of memory | `memory_type` | Collection | Document name | Written by |
|---|---|---|---|---|
| Non-obvious root cause, gotcha, verified fact | `fact` | `memory-<handle>` (daemon routes on handle) | `<topic>-<slug>` | the agent (guide moment 1–2) |
| Ratified decision with its reason | `fact` | `memory-<handle>` | `<topic>-<slug>` | the agent |
| Repeatable how-to | `procedure` | `memory-<handle>` | `<topic>-<slug>` | the agent |
| A judgement to revisit | `opinion` | `memory-<handle>` | `<topic>-<slug>` | the agent |
| "What I would tell myself next time" at mission end | `observation` | `memory-<handle>` | `mission-<id>-note` | the agent (guide moment 5) |
| Project rule the whole team should follow | `lesson` | `<repo>-lessons`, no handle | daemon-generated | the agent via `learn` |
| Evaluator feedback on a round | `observation` | `memory-<worker>` | `mission-<repo>-<id>-r<n>` | Loop 2 |
| A subagent's final report | `observation` | `memory-<handle>` | `subagent-<id8>-report` | Loop 3 |
| Raw transcript | `""` | `<repo>-captures` | `session-<id8>` | existing hooks (unchanged) |

The "when" list is the substance of the guide: after a root cause, after a
ratified decision, after working out a how-to, when forming a judgement worth
revisiting, and once before submitting a mission result. The "not" list:
progress narration, file contents, tool output, anything already in the repo.
This matches R3 verbatim (`hooks.py:53-54`) and the `remember`/`learn`
boundary sentence (DES-053 item 8).

### b.4 Guidance delivery: a versioned `session_context` block

**Text.** The v2 block (full text in Appendix A) keeps DES-019's shape — a YAML
literal block scalar appended verbatim — and adds: the five moments, the
not-list, the decay note (30-day half-life), the cross-project note, and the
attribution sentence: *"Always pass `agent_handle="{handle}"`. The daemon
cannot infer your identity — a subagent's working directory resolves to the
repo's leader, not to you."* Its first line is the version header
`## Memory (quarry guide v2)`.

**Refresh rule.** `write_session_context` today returns `already_set` whenever
the key exists (`doctor_ethos.py:99-100`), so no existing identity would ever
receive v2. New `SessionContextBlock` (`ethos_ext_block.py`) locates the
`session_context: |` literal block in the raw text (the key line through the
last indented-or-blank line) and classifies it:

| Block state | First body line | Action | Return |
|---|---|---|---|
| absent | — | append v2 (existing behaviour) | `updated` |
| stale (v1, or any older quarry block) | starts with `## Memory` but ≠ v2 header | splice v2 over the block | `updated` |
| current | == v2 header | nothing | `already_set` |
| custom (hand-authored) | anything else | nothing — never overwrite a human's block | `already_set` |

The write is atomic via `AtomicFile.replace` (`src/quarry/atomic_file.py:72-119`:
temp in the target dir, fsync, `os.replace`, mode preserved, temp unlinked on
any failure) — the current `open("a")` append (`doctor_ethos.py:107-108`) is
replaced for both the append and the splice paths. DES-019's "raw text, never
`yaml.dump`" rule is kept: `yaml.safe_load` is still used only to read
`memory_collection`; the splice is a line-range replacement on the raw text.

**Targets and entry points.** Two writers exist today and neither runs from
`quarry doctor`: `EthosExtDiagnostics.configure()` runs **only** as
`quarry install` step 8 (`doctor.py:630-637`; `check_environment` at
`doctor.py:645-` never calls it), and `EthosMemoryBootstrap().run()` runs from
`quarry enable` (`enable.py:135,149`). Both touch only the global tree
(`doctor_ethos.py:53-54`; `ethos_memory.py:15`). The design keeps that split
and adds the vendored layer on the per-repo side:

| Entry point | Tree | Creates ext files? | Refreshes stale blocks? |
|---|---|---|---|
| `quarry install` (step 8, `configure()`) | global only — install has no repo context | no (unchanged) | yes (new) |
| `quarry enable <dir>` (`EthosMemoryBootstrap.for_repo(dir).run()`) | global, **then** the nearest vendored `.punt-labs/ethos/identities/` above `dir` (`EthosTree.vendored_identities`) | global: yes (unchanged); vendored: **never** — its roster is curated by `ethos vendor` (CLAUDE.md "Ethos & Delegation") | yes, both trees (new) |
| `quarry doctor` | — | no | no — doctor stays read-only; it does not write ext files today and this design does not make it start |

`quarry enable` is the right per-repo write step for three reasons: it is
already the command that deposits and *upgrades* the repo guide
(`repo-guide.md`) — "re-running upgrades the deposited guide" is the
documented lifecycle (`enable.py:127-133`); it already runs the global ext
writer, so the vendored pass is the same scanner on a second directory; and
it is idempotent, so re-running after a quarry upgrade is the operator's
one-liner. A vendored refresh is a working-tree diff the operator commits
via PR, the same pattern CLAUDE.md prescribes for `ethos enable` refreshes;
`EnableResult` gains `ethos_vendored_updated` and `EnableReport` prints
`Ethos guide refreshed (vendored — commit via PR): claude, rmh, …` so the
diff is announced, not discovered.

One scanner serves both callers: `EthosExtDiagnostics.refresh(identities_dir)
-> ExtScanOutcome` (frozen: `updated`, `already_set`, `no_collection`,
`failed`; `message()`), which `configure()` wraps into its `CheckResult` and
`EthosMemoryBootstrap.run()` calls once per tree. That deletes the bootstrap's
five-parameter `_write_context` list-threading (`ethos_memory.py:95-115`) and
narrows `_scan`'s `except Exception  # noqa: BLE001` (`doctor_ethos.py:154`)
to the `(OSError, YAMLError, UnicodeDecodeError)` tuple the bootstrap already
uses — install's step 8 wraps `configure()` in its own best-effort `try`
(`doctor.py:632-637`), so nothing depended on the broad catch. One suppression
retired.

**Other guidance surfaces, aligned in the same PR:** MCP `remember` docstring
(`mcp_server.py:236-260`) gains the attribution sentence and the five moments
in one line; `find`'s `agent_handle` arg doc (`mcp_server.py:164`) says "your
own handle to recall only your memories"; `plugin/commands/remember.md` tells
the model to pass `agent_handle` and `memory_type` from its `## Memory`
context (still a thin door — parsing, not logic); `plugin/skills/recall/SKILL.md`
gains the five moments under "When to use it"; `src/quarry/data/repo-guide.md`
(deposited per repo on `enable`) gains one bullet. `hooks.py`'s `_TRIGGER_RULES`
R3 is unchanged (it is already the canonical sentence).

### b.5 Server-side vocabulary validation

New `src/quarry/memory_types.py` (layer 1, zero heavy deps):

```python
class MemoryType(StrEnum):
    FACT = "fact"; OBSERVATION = "observation"; OPINION = "opinion"
    PROCEDURE = "procedure"; LESSON = "lesson"

    @classmethod
    def parse(cls, raw: str) -> MemoryType: ...   # ValueError on unknown (PY-EH-8)
    @property
    def is_decayable(self) -> bool: ...            # every type but LESSON

DECAYABLE_MEMORY_TYPES: Final[frozenset[str]]
```

`RouteGroup.reject_reserved_memory_type` (`base.py:102-117`) becomes
`reject_invalid_memory_type(memory_type)`: `""` passes; `"lesson"` → the
existing 400 text (three tests assert it: `tests/test_http_server.py:2017,2269,2628`);
any other non-member → `400 {"error": "unknown memory_type 'facts'; expected one of fact, observation, opinion, procedure"}`.
All three routes already call the one method (`ingestion.py:117,196`;
`captures.py:61`), so the rule cannot drift between surfaces. `fusion.py`,
`doctor_memory.py`, and `lesson.py` import the vocabulary instead of restating it.

This is the one deliberate behaviour change outside the three loops proper:
a mistyped `memory_type` becomes a 400 instead of a silent misfile. It needs
the leader's explicit ratification (§g lists it as decision D1).

---

## c. Loop 2 — evaluator feedback → memory

### c.1 Source of truth (real shapes, read from disk)

From `.punt-labs/ethos/missions/m-2026-09-09-001/`:

```yaml
# contract.yaml (lines 1-12, 34) — `repo:` is absent on older contracts
# (m-2026-09-02-003 has none); c.3 states the fallback
mission_id: m-2026-09-09-001
status: open
type: implement
created_at: "2026-09-09T05:14:15Z"
repo: /home/jfreeman/Coding/punt-labs/quarry
leader: claude
worker: gvr
evaluator: {handle: rmh, pinned_at: "2026-09-09T05:14:15Z", hash: 0539…}
current_round: 2

# results.yaml (lines 1-12, 23-26)
results:
  - {mission: m-2026-09-09-001, round: 1, created_at: …, author: gvr,
     verdict: pass, confidence: 0.85,
     files_changed: [{path: …, added: 772, removed: 0}],
     evidence: [{name: …, status: pass}, …],
     open_questions: [ … ], prose: | … }

# reflections.yaml (m-2026-09-02-003, lines 1-17)
reflections:
  - {mission: …, round: 1, created_at: …, author: claude, converging: true,
     signals: [ 'evaluator djb: REJECT — blocker: …', … ],
     recommendation: continue, reason: … }
```

`results` and `reflections` are append-only per round (`ethos mission result --help`,
`reflect --help`: "a second submission for the same round is refused").
`ethos mission advance` requires a reflection, so **a round numbered below
`current_round` is frozen**; when `status` is anything but `open`
(`closed`, or `failed` from `close --status failed`), every round is frozen.
That immutability is what makes Loop 2 idempotent without a ledger.

A frozen round does not always have both halves. An advanced round has a
reflection by construction, but a closed mission's *final* round usually has a
result and no reflection (`m-2026-09-09-001` round 2: result, then `close`;
`contract.yaml:2,6`), and `close --status failed` may freeze a round with
either half missing. The rule, so nothing is unspecified:

| Frozen round has | Action |
|---|---|
| result + reflection | file (the normal case) |
| result, no reflection | **file**, with the reflection section reading `Evaluator reflection: none — closed <contract.status> at round <n>` — the worker's self-assessment and the closure are still the round's record |
| reflection, no result | file, with `Worker verdict: none — no result submitted` |
| neither | skip with one INFO line — there is nothing to remember |

"File-or-skip" is decided per round from the two YAML lists alone; the
document name (c.3) is the same either way, so a round filed before a late
reflection could exist is impossible (only frozen rounds are read) and the
truth table is total.

### c.2 Trigger — quarry side, as a leader workflow step

**Chosen:** a quarry client verb, `quarry missions sync`, run by the leader
in the mission loop right after `ethos mission close <id>` (and safe to run
at any time — it is idempotent). `docs/WORKFLOW.md`'s `function mission_loop`
gains the line after `close(mission)`; Invariant 9 ("close-out is inside the
loop") already frames it. The worker never runs it (workers own no workflow
operations); the evaluator never runs it.

Why this and not the alternatives:

- **An ethos change** — forbidden by hard constraint 1 (ethos DES-001 sidecar; quarry DES-008/DES-019: ethos knows nothing of quarry). Rejected.
- **A daemon file watcher on `.punt-labs/ethos/missions/`** — the daemon's watch loop indexes registered document trees, and the missions dir is gitignored runtime state ethos writes under its own locks (`.lock` files in every mission dir). Adding a second watcher class for a leader-only workflow is machinery for a step the leader already performs by hand. Rejected.
- **A Claude Code hook** — no hook fires on `ethos mission reflect`; SessionEnd/SessionStart sweeps would be late, and DES-041 wants hooks thin. Rejected.
- **A new daemon route that reads the missions dir from a client-supplied `cwd`** — DES-041 explicitly rejected "a daemon path-read mode (arbitrary-local-file-read surface — content-over-path avoids it)" (`DESIGN.md:1720-1723`), and in remote mode (`QUARRY_URL`) the daemon host does not even have the repo. Rejected. The client reads the files; the daemon receives content.

### c.3 Mechanism

```text
EthosTree.missions_dir(cwd)            # nearest .punt-labs/ethos/missions
  → MissionStore.scan()                # MissionContract + MissionRound per dir; parse errors collected
  → frozen rounds only (c.1 rule)
  → MissionMemoryComposer.compose(contract, round) -> RememberRequest
  → QuarryClient.show_document(ShowRequest(name, page=1))
        404            → not filed yet: file it
        200, same key  → already filed: skip (unless --force)
        200, other key → name collision: error entry, never overwrite
  → QuarryClient.remember(request)     # POST /v1/remember → ScrubbedIngestJob
```

**The request** (one per frozen round):

- `name = f"mission-{repo}-{contract.id}-r{round.number}"`, where `repo` is the basename of `contract.repo` (`contract.yaml:7`, e.g. `quarry`) — so `mission-quarry-m-2026-09-09-006-r2`. **Older contracts have no `repo:` field** (`m-2026-09-02-003/contract.yaml` lines 1-8: `mission_id, status, type, created_at, updated_at, closed_at, leader, worker` — the key arrived with a later ethos), so `MissionStore` supplies the repo root it is scanning (the directory holding `.punt-labs/ethos/missions/`) as `MissionContract.from_mapping(mapping, default_repo=root)`'s default; `repo` is therefore always present on the record, and for a mission filed from the checkout it was run in the two sources name the same directory. The repo discriminator is there because **mission IDs are not globally unique**: `m-YYYY-MM-DD-NNN` is allocated from a per-*machine*, per-day counter file (`ethos/internal/mission/id.go:44,83` — `~/.punt-labs/ethos/counters/missions-YYYY-MM-DD`), while `memory-<worker>` is cross-machine by ruling (hard constraint 4). Two machines each running their first mission of the day produce `m-…-001`; without the discriminator the second one's rounds would be silently skipped by an existence check, or clobbered by `--force`. The name is unique per round, so RRF's `(document_name, chunk_index, page_number)` dedup key (`fusion.py:100-106`) can never merge two rounds (the DES-053 item-4 hazard).
- **Residual and how it is caught.** Two machines that both hold a checkout with the *same basename* and both allocate the *same sequence number on the same day* still collide on the name. That residual is not silent: the existence check reads page 1 and compares the document's first line — the header carries `contract.created_at` (`contract.yaml:4`, second-resolution UTC) and the repo path — against what the composer would write. A mismatch is recorded as a `MissionSyncOutcome` error (`name collision: mission-quarry-m-…-r1 holds a round created 2026-09-09T05:14:15Z from another checkout`) and nothing is written; `--force` overwrites only when the keys match, so it can never replace another machine's round. Folding `created_at` into the *name* was rejected: it would make every document name a 40-character timestamped token that no human types into `quarry show`, to close a case the header check already catches.
- `agent_handle = contract.worker`, `collection = ""` — the daemon's routing rule (`ingestion.py:176-183`) puts it in `memory-<worker>`; the client never spells the collection.
- `memory_type = "observation"`.
- `summary = f"{contract.id} r{n} ({repo}): worker {verdict}/{recommendation} — {first signal, ≤100 chars}"`; when the round has no reflection, `recommendation` reads `closed`/`failed` from `contract.status`.
- `overwrite = True` (a `--force` re-file replaces in place, keys matching).
- `content` — markdown (template in Appendix B): header line (mission, round, repo, worker, evaluator, `created_at`), **the worker's verdict** — `results.yaml: verdict` is the worker's *self-assessment* submitted with the result, not the evaluator's judgement, and the document says so (`Worker verdict (self-assessed): pass, confidence 0.90`) — then **the evaluator's reflection** (author, converging, recommendation, reason, every signal as a bullet, or the `none — closed …` line from c.1), the worker's `open_questions`, then the worker's `prose` under "Worker report". Scrubbing is the daemon's: `ScrubbedIngestJob` redacts content, name and summary before a chunk is written (`ingest_jobs.py:53-64`; `pipeline.py:450-456`) — the repo path in `contract.repo` becomes `~/…`, emails become `[REDACTED:email]`.

**What the worker does and does not receive.** The evaluator's full review —
the prose kpz or djb writes when they review a round — is **not** in the YAML
trio. `reflections.yaml` holds what the reflection's author put in `signals`
and `reason` (the leader's distillation of the review, or the evaluator's own
if the evaluator authored the reflection); `results.yaml` holds the worker's
own submission. So Loop 2 files the reflection's signals and reason — nothing
more exists on disk to file. The evaluator's full review text reaches memory
only as the *evaluator's* own SubagentStop report, into `memory-<evaluator>`
via Loop 3 (d.1), where whole-DB `find` (no handle filter) can surface it for
anyone. Nobody should expect `memory-<worker>` to contain the review verbatim;
it contains the round's signals, which is what the worker acts on.

**Why `memory-<worker>` as `observation`, not a lesson.** A reflection is the
evaluator's assessment of one worker's round: specific, dated, and meant to
fade — exactly the decayable, agent-owned class (`fusion.py:86-91`). A lesson
is curated, ≤500 chars, project-scoped, and *boosted* (DES-053 items 1, 2, 6);
filing raw signal lists there would flood the boosted tier with un-distilled
text and defeat "distilled" — the word the boundary sentence hinges on. When a
reflection *contains* a rule the project should keep, the leader files it with
`learn`, by hand, as today. The evaluator gets no copy: whole-DB `find`
(no handle filter) already surfaces it for anyone, and a second row would
double the chunk count for one view (the same argument 8kdo D3 made against
dual-writing captures).

**Why frozen rounds only.** A round with a result but no reflection yet would
be filed incomplete and then skipped forever by the existence check. Waiting
for `advance`/`close` costs nothing (the leader runs sync at close).

### c.4 Idempotency and re-runs

- Identity check: `QuarryClient.show_document(ShowRequest(document=name, collection="", page=1))` (`api/show.py:8-18`: `page >= 1` returns a `ShowPageResponse` with `text`) — a 404 `HttpError` (the documented "not found" outcome, `mcp_server.py:368-375`) means "file it"; a 200 whose first line matches the composer's header means "already filed, skip"; a 200 with a different header is a name collision and becomes an error entry (c.3); any other `HttpError` is an error entry, not a skip. `--force` overwrites a matching key and still refuses a collision.
- Every parse failure (malformed YAML, missing `worker`, non-int `round`) is recorded in `MissionSyncOutcome.errors` with the file path and the run continues (bug class 2). The CLI exits 1 when `errors` is non-empty, mirroring `captures push` (`src/quarry/cli_captures.py:77-80`).
- `--mission <id>` restricts to one directory; `--dry-run` lists what would be filed and posts nothing.
- Legacy flat layout (`~/.punt-labs/ethos/missions/<id>.{yaml,results.yaml,reflections.yaml}`, pre-`migrate`) is out of scope; the store logs a one-line INFO when the repo has no `.punt-labs/ethos/missions/` at all.

### c.5 Every-surface story

| Surface | Shape | Notes |
|---|---|---|
| CLI | `quarry missions sync [--mission ID] [--dry-run] [--force]`, `--json` supported via `CliPlumbing.emit` | new `MissionsCli` sub-app, registered in `__main__.py` |
| MCP | `missions_sync(mission: str = "", dry_run: bool = False, force: bool = False) -> str` | same `MissionMemorySync`; lives on `MissionTools` in the sibling module `mcp_missions.py`, registered by `McpTools.register` and wrapped by the same `ToolGuard.wrap` boundary as the eleven existing tools (e.2); twelfth tool |
| HTTP | **no new route** — the write is `POST /v1/remember`, already on every surface | a daemon route would be wrong in remote mode and is a path-read surface (c.2) |
| Plugin | `/quarry missions sync` subcommand in `plugin/commands/quarry.md` → calls `mcp__quarry__missions_sync` | thin door (DES-053 C3 precedent) |

Local and remote cannot diverge because there is no fork: both CLI and MCP
build the same `RememberRequest` sequence from the same files through one
class; the equivalence test (f.3) asserts the sequences are identical.

### c.6 Remote mode

With `QUARRY_URL` set, the mission files are read from the client's repo and
the memories are posted to the remote daemon — the correct semantics (the
mission belongs to the repo the leader is sitting in). Content crosses the wire
only over loopback or TLS (DES-041's transport guarantee, `client/resolver.py:160-173`).

---

## d. Loop 3 — capture distillation

### d.1 What "distilled" means without an LLM

The subagent has already written its own distillation: its final assistant
message is the report the parent reads (for an ethos worker, the result
summary; for a reviewer, the findings). The transcript's last assistant turn
is authoritative for it; the payload's `last_assistant_message` is the same
text as delivered by Claude Code (sample: `…"last_assistant_message":"ok"…`)
but the transcript is already parsed for the raw capture, so it is the single
source. Add the `## Session Artifacts` header the raw capture already computes
(`src/quarry/artifacts.py:72-80,100-113`) and the result is a short, classified,
attributed memory:

```text
# Subagent report — rmh (agent a0f13948)
Parent session: 304fdeb9 · agent_type: rmh

## Session Artifacts
Commits: 6668998 · Beads: quarry-0bej

## Final report
<last assistant text, ≤ 32 000 chars; tail-truncated with "[truncated N chars]">
```

`name = f"subagent-{agent_id[:8]}-report"`, `memory_type = "observation"`,
`agent_handle = <resolved handle>`, `collection = ""` → `memory-<handle>`,
`summary =` first non-empty report line (≤120 chars), `overwrite = True`
(a repeated SubagentStop for the same `agent_id` replaces, never duplicates).

The `[:8]` is deliberate parity, and it inherits a known budget. The raw
capture is named `session-<agent_id[:8]>` by the daemon (`captures.py:109-124`),
so the two documents for one agent share one 8-hex-digit key — 32 bits of
identifier, a birthday collision expected after roughly 2^16 ≈ 65 000 agents
in one database. On a collision the report's `overwrite=True` would replace a
different agent's report, exactly as the raw capture already replaces the
other agent's transcript today. The design does not widen the report's prefix
alone: a wider report key next to a narrower capture key would be two
conventions for one id. The prefix length becomes one shared constant on the
wire module both sides import (`SESSION_ID_PREFIX_LEN = 8` in
`api/capture_ingest.py`, used by `captures.py` and `subagent_capture.py`), so
widening it is a one-line change that moves both names together.

No per-chunk LLM pass: DES-018 deferred entity extraction to v2 for cost
(~1 s/chunk); quarry ships no local LLM; and a model call from a *blocking*
hook with a 30 s budget is exactly the per-hook heavy work DES-041 removed.

### d.2 Flow — `SubagentCapture`

```text
HookAgent.subagent_stop (unchanged gates: cwd, config, payload → {} always)
  → SubagentCapture(cwd, agent_id, agent_type, transcript_path).capture()
      handle  = SubagentCapture.handle_for(agent_type, cwd)          # b.2
      report  = SubagentReport.from_transcript(reader, handle, agent_id)   # may be empty
      raw     = SessionTranscriptCapture(TranscriptSource(...), agent_handle=handle,
                                         summary=report.summary).capture()   # existing path + summary
      if handle and report.text and raw.text_captured:
          distilled = DaemonCaptureSender().send_remember(report.remember_request(), unreachable_log=…)
      return SubagentCaptureOutcome(raw=raw, handle=handle, distilled=distilled)
```

Rules:

- **Raw archive preserved.** The JSONL archive, the `.md` capture, and the `<repo>-captures` document are produced exactly as today (same name, same content, same `memory_type=""`). The only change to the raw row is a populated `summary` — the report's first line — so `quarry show session-<id8>` and search hits now say what the session was.
- **Distill only for an identity.** `handle == ""` (non-identity `agent_type`, or absent and no pin) → raw capture only. Without a handle a `remember` would route to `default` (`ingestion.py:183`) — a memory nobody owns.
- **One switch.** Distillation is gated by the existing `auto_capture.subagent_stop` key (`_stdlib.py:58`); it is one capture event with two rows, not a new hook family. A separate `distill` key was considered and rejected as a knob with no independent use.
- **Fire-and-forget, bounded.** `DaemonCaptureSender.send_remember` is the third door beside `send_capture`/`send_ingest_url` (`daemon_capture.py:48-62`), same 5 s cap, same four failure classes; `QuarryClient.remember` gains the `timeout` keyword its two siblings already have (`client.py:138-152`). Worst case the hook spends 10 s on two POSTs, inside the 30 s SubagentStop budget.

### d.3 DES-041 compliance

The hook imports no engine (only `quarry.api`, `quarry.client`, stdlib, and
the existing thin helpers); it does one extra POST; the daemon scrubs content,
name and summary before embed/store through the same `ScrubbedIngestJob`
(`ingest_jobs.py:51-121`) — a raised scrub writes zero chunks. There is no
daemon-side post-processing job reading transcripts back (that would be a
second reader of `<repo>-captures` and a path-read surface); there is no
per-hook engine.

### d.4 Blocking-hook invariant

`SubagentCapture.capture()` never raises: `SessionTranscriptCapture.capture`
already funnels every failure into outcome flags (`session_transcript.py:82-89`);
`SubagentReport.from_transcript` reads via `TranscriptReader.records()` (which
degrades to empty on read failure exactly as `text()` does today,
`transcript_reader.py:46-52`); `send_remember` returns `False` on all four
failure classes. `HookAgent.subagent_stop` returns `{}` on every path; the
existing crafted-payload test (`tests/test_hooks_agent.py:490-511`) keeps
asserting it, and `_trace_transcript_outcome` gains the `"distilled"` detail
via `HookTrace.capture(detail)` (`src/quarry/_hook_trace.py:70`).

### d.5 Rejected for Loop 3

- **Hook-side heuristic classification** of the raw transcript into fact/procedure — guesswork stored as truth; the four types are meaningful only when an agent chose them.
- **Daemon-side distill job** over `<repo>-captures` — a second reader, path-read surface, and it would re-embed the largest documents in the DB on every run.
- **SessionEnd/PreCompact distillation** — the parent's last message is rarely a report ("Done."); deferred as a follow-on bead after Loop 3's data shows what the reports look like.
- **Filing the report under `<repo>-captures`** — it is agent memory; cross-project recall is the ruled intent (hard constraint 4).

---

## e. Implementation write-set

Every module is projected under 500 lines; the OO shape follows
`../.claude/rules/python-*.md` (`@final`, `__new__`, `__slots__`, frozen
dataclasses for threaded args, Protocol at boundaries, no suppressions).
Baselines are from `.oo-baseline.json` / `.oo-coupling-baseline.json` and the
live scorers.

### e.1 CREATE

| File | Responsibility | Lines | Classes / shape |
|---|---|---|---|
| `src/quarry/memory_types.py` | The `memory_type` vocabulary: `MemoryType(StrEnum)`, `parse`, `is_decayable`, `DECAYABLE_MEMORY_TYPES` | ~55 | 1 enum |
| `src/quarry/ethos_tree.py` | Locate ethos sidecar artifacts from a cwd: `EthosTree.ancestors`, `nearest`, `vendored_identities` (`Path or None` — absence is the documented "no vendored tree" contract), `identities_dirs` (vendored-then-global, present ones only), `missions_dir`, `identity_exists` (handle regex-validated; vendored + global, not bundles — b.2) | ~105 | 1 `@final` class, `__slots__ = ()` |
| `src/quarry/hooks_compact.py` | The PreCompact hook, extracted from `hooks.py`: `PreCompactTarget` (frozen: `cwd`, `session_id`, `transcript_path`; `source(label) -> TranscriptSource`) and `PreCompactHook` (`__slots__ = ()`; `handle(payload)` — today's `handle_pre_compact` verbatim in behaviour, the capture built as `SessionTranscriptCapture.for_session(target.source("pre-compact"))` so the handle resolution lives in one place (C6) and the hook imports no `EthosConfig`; `_target(payload) -> PreCompactTarget or None`, `None` being the documented no-op skip contract today's `_precompact_target` has). Payload coercion comes from `HookPayload.as_str/as_dir` (`_hook_trace.py:102-141`), which `hooks.py:683-705` duplicates line for line today | ~120 | 2 (`HookAgent` precedent: a `@final` static-method hook class, `hooks_agent.py`) |
| `src/quarry/mcp_guard.py` | The MCP tool-boundary decorator, extracted from `mcp_server.py:55-74`: `ToolGuard.wrap(method)` — a `@final`, `__slots__ = ()` namespace class in the `HookPayload` style (`_hook_trace.py:102-116` states the PY-OO-7 rationale) so a sibling tool module and `McpTools` share one boundary without either importing the other | ~40 | 1 |
| `src/quarry/mcp_missions.py` | `MissionTools` (`__new__(connect)`, `register(server)`, `@ToolGuard.wrap missions_sync(mission, dry_run, force) -> str` over `MissionMemorySync`) — the twelfth tool in its own module so `mcp_server.py` (553 lines) shrinks instead of growing | ~70 | 1 |
| `src/quarry/hooks_compact.py` tests → `tests/test_hooks_compact.py` | `TestHandlePreCompact`, `TestPreCompactCaptureRedaction`, `TestCwdHardeningPreCompact` **moved** from `tests/test_hooks.py:1359-1754,2173-2205,2284-` and re-pointed at `PreCompactHook.handle`; the moved suite passing unchanged is the extraction's behaviour-preservation guard (PY-RF-2) | ~450 (moved) | — |
| `tests/test_hook_trace.py` | New (PL-BS-6: `_hook_trace.py` has no test file today); `TestAsDir` moves here from `tests/test_hooks.py:2207-2222` against `HookPayload.as_dir`, plus `as_str` cases | ~40 | — |
| `tests/test_mcp_guard.py`, `tests/test_mcp_missions.py` | guard: a raising method returns `Error: <Type>: …` and logs; missions: tool registered under `missions_sync`, direct call and registered call share the boundary, daemon-down returns an error string | ~90 | — |
| `tests/test_doctor_ethos.py` | New (PL-BS-6: `doctor_ethos.py` is tested today only inside `tests/test_doctor.py` and `tests/test_ethos_memory.py`); the `EthosExtDiagnostics` cases move here and the refresh/`ExtScanOutcome` cases are added | ~120 (mostly moved) | — |
| `src/quarry/ethos_ext_block.py` | The versioned guide: `MEMORY_GUIDE_HEADER`, template text, `SessionContextBlock` (locate / classify / render / splice) | ~130 | 1 frozen dataclass + 1 module constant block |
| `src/quarry/subagent_capture.py` | `SubagentReport` (frozen; `from_transcript`, `summary`, `remember_request`), `SubagentCapture` (`handle_for`, `capture`), `SubagentCaptureOutcome` (frozen) | ~160 | 3 |
| `src/quarry/mission_records.py` | `MissionContract` (frozen; `from_mapping(mapping, default_repo)` — `repo` falls back to the scanned root for pre-`repo:` contracts; `repo_name` property = basename), `MissionRound` (frozen; `from_mapping`; `is_frozen(contract)`; `has_result`/`has_reflection` for the c.1 truth table) | ~130 | 2 |
| `src/quarry/mission_store.py` | `MissionStore` (reads the YAML trio per mission dir, collects errors), `MissionScan` (frozen: missions, errors) | ~110 | 2 |
| `src/quarry/mission_memory.py` | `MissionMemoryComposer` (round → `RememberRequest`), `MissionMemorySync` (existence check, post, tally), `MissionSyncOutcome` (frozen; `to_dict`, `render`) | ~170 | 3 |
| `src/quarry/cli_missions.py` | `MissionsCli.build()` — the `missions` sub-app with `sync` | ~75 | 1 |
| `tests/test_memory_types.py`, `tests/test_ethos_tree.py`, `tests/test_ethos_ext_block.py`, `tests/test_subagent_capture.py`, `tests/test_mission_records.py`, `tests/test_mission_store.py`, `tests/test_mission_memory.py`, `tests/test_cli_missions.py` | one test module per new source module (PL-BS-6) | ~900 total | — |
| `tests/fixtures/missions/` | two mission dirs (one closed, one open at round 2) built from the real files with prose trimmed and an embedded fake email + home path to prove scrubbing | — | — |

Layering (`.importlinter`): none of the new source modules imports
`quarry.db`, `quarry.embeddings`, `quarry.ingestion`, `quarry.retrieval`,
`quarry.sync`, or `quarry.daemon`; `mission_memory.py` and `subagent_capture.py`
import `quarry.client`/`quarry.api` (client tier), so `mcp_server`/`__main__`/
hooks may import them under the `client-never-imports-engine` contract.
`hooks_compact.py` imports only `_hook_trace`, `_stdlib`, and
`session_transcript` (the handle is resolved inside `for_session`, C6), so the
hook stays engine-free and `TestHookImportsNoEngine` (`test_hooks.py:1277`)
keeps covering it with the engine poisoned. `mcp_guard.py` imports nothing
internal; `mcp_missions.py` imports `mcp_guard`, `mission_memory`, and
`quarry.client`.

### e.2 MODIFY

| File | Change | Δ lines | OO / coupling effect |
|---|---|---|---|
| `src/quarry/ethos_handle.py` | `agent_handle_at` reads `.punt-labs/ethos.yaml` then `.punt-labs/ethos/config.yaml` per ancestor; `_walk_up` deleted in favour of `EthosTree.ancestors`; module docstring's "hooks.py still carries its own walker" sentence (stale) removed | −4 | module_size ↓; coupling 0→1 |
| `src/quarry/session_transcript.py` | `TranscriptSource` frozen dataclass (cwd, session_id, transcript_path, label); `__new__(cls, source, *, agent_handle="", summary="")`; `for_session(source)` classmethod resolving the handle; `summary` threaded onto `CaptureIngestRequest` | +22 | **avg_params ↓** (5-kwarg `__new__` → 3); classes 2→3; module_size ↑ (relax R1) |
| `src/quarry/transcript_reader.py` | `records()` iterator (parsed JSON, bad lines skipped, unreadable → empty); `text()` rewritten over it; `last_assistant_text()` | +6 | **max/avg complexity ↓** (`text` loses its nested try); module_size ↑ (relax R2) |
| `src/quarry/daemon_capture.py` | `send_remember(req, *, unreachable_log)`; the no-op `__new__` (`:45-46`) deleted | +5 | module_size ↑ (relax R3) |
| `src/quarry/client/client.py` | `remember(req, *, timeout=None)` parity with `capture`/`ingest_url`; `await_task`'s poll body extracted to `_poll_once` | +4 | **max_complexity 8 → ≤5**; module_size ↑ (relax R4) |
| `src/quarry/client/errors.py` | `HttpError.is_conflict` property | +6 | module_size ↑ (relax R5) |
| `src/quarry/hooks_agent.py` | `session_end` → `SessionTranscriptCapture.for_session(TranscriptSource(...))`; `subagent_stop` delegates to `SubagentCapture`; `_trace_transcript_outcome(trace, outcome, detail)`; imports: −`ethos_handle` +`subagent_capture`; the docstring at `:90` that cites "`handle_pre_compact` branching in `quarry.hooks`" now cites `PreCompactHook.handle` in `quarry.hooks_compact` | −18 | module_size ↓, max_complexity ↓ (6→4); **coupling 7→7** (ceiling held) |
| `src/quarry/hooks.py` (783 lines — over the limit, so this touch **extracts**) | `handle_pre_compact` + `_precompact_target` leave for `hooks_compact.py` (`:708-784`); the duplicate coercers `_as_str`/`_as_dir` (`:683-705`) are **deleted** — `handle_session_start` (`:412`) and `handle_post_web_fetch` (`:615`) call `HookPayload.as_dir` from the `_hook_trace` import the module already has; imports −`ethos_handle` −`session_transcript` | **−104** (≈679 after) | **module_size ↓ 674→~575, method_ratio ↑** (four free functions gone), **coupling 6→4**. Still over 500: the residual mass is the SessionStart family (`_SessionStartTemplates`, `_SessionStartContext`, the sync-lock functions, `:59-600`), which nothing in this PR rewires — it is the next touch's extraction, named here so it is not forgotten, and left alone now because dragging ~500 untouched lines through this diff buys review cost, not rollback coherence |
| `src/quarry/_hook_entry.py` | `_pre_compact` imports `PreCompactHook` from `quarry.hooks_compact` and runs `PreCompactHook.handle` (`:58-61`); lazy in-function import as today, so the coupling scorer (top-level nodes only, `imports.py:33`) sees no change | ±0 | unchanged (2) |
| `src/quarry/daemon/routes/base.py` | `reject_reserved_memory_type` → `reject_invalid_memory_type` over `MemoryType`; `RESERVED_MEMORY_TYPE` constant deleted (no shim, PL-PP-1); `from collections.abc import Callable, Coroutine` (annotation-only: `accept`, `run_delete`) moves under `TYPE_CHECKING` | ±0 | **coupling 4→4 — bookkeeping, not decoupling.** The scorer's `_absolute` (`tools/coupling/imports.py:59-62`) matches an import's top segment against the package's module keys, and `src/quarry/collections.py` exists, so stdlib `collections.abc` is counted as an internal dependency; and it walks only top-level nodes (`imports.py:33`), so a `TYPE_CHECKING` block is invisible to it. Moving an annotation-only import under the guard is legitimate PY-TS-7 hygiene on its own merits, and here it also frees the slot the real `memory_types` import takes. The module's true internal fan-out goes 3→4; the number holds at 4 only because a false positive leaves. Stated so nobody reads "4→4" as a decoupling. public_names 2→1 |
| `src/quarry/daemon/routes/ingestion.py` | call the renamed guard; `_learn_job` uses `LessonComposer.memory_type()` and `LessonComposer.check_length(lesson)` (ValueError → 400), dropping the inline cap block; `_MAX_LESSON_CHARS` moves to `lesson.py` | −9 | **module_size ↓, `_learn_job` complexity ↓**; coupling 7→7 (imports `base` still; `memory_types` not imported here) |
| `src/quarry/daemon/routes/captures.py` | call the renamed guard; `_capture_name` slices with `SESSION_ID_PREFIX_LEN` from `quarry.api.capture_ingest` (d.1) instead of the literal `8` (`:121`) | ±0 | coupling unchanged (`quarry.api` already imported) |
| `src/quarry/api/capture_ingest.py` | `SESSION_ID_PREFIX_LEN: Final = 8` — the one place the `session-<id8>`/`subagent-<id8>-report` key width lives | +3 | wire module; no model change (`docs/openapi.json` untouched) |
| `src/quarry/lesson.py` | `LessonComposer.MAX_CHARS`, `memory_type()`, `check_length()` | +14 | module_size ↑ (relax R6); coupling 1→2 |
| `src/quarry/retrieval/fusion.py` | import `DECAYABLE_MEMORY_TYPES`/`MemoryType.LESSON`; delete the two local constants | −4 | module_size ↓ |
| `src/quarry/doctor_memory.py` | same replacement; `identity_active`'s zero-rows remedy text (`:139-143`, "check that ethos config resolves and PreCompact fires") names the real remedies now that other handles' rows are the expected state — `… has zero memory rows while other agents have {n}; the leader's next PreCompact, or 'quarry remember --agent-handle {handle}', populates it` (§h demo step f) | −4 | module_size ↓ |
| `src/quarry/doctor_ethos.py` | template moves to `ethos_ext_block.py`; `write_session_context` = `SessionContextBlock` + `AtomicFile`; new public `refresh(identities_dir) -> ExtScanOutcome` (frozen: four buckets + `message()`) replaces `_scan`'s 4-tuple and `_message`; `configure(identities_dir=None)` keeps its signature and **stays global-only** (it runs from `quarry install` step 8, `doctor.py:630-637`, where there is no repo), wrapping `refresh`; the `except Exception  # noqa: BLE001` at `:154` narrows to `(OSError, YAMLError, UnicodeDecodeError)` | −30 | **avg_params ↓** (4-list threading gone), module_size ↓, **one suppression retired**; classes 1→2 |
| `src/quarry/ethos_memory.py` | `EthosMemoryBootstrap.for_repo(directory)` classmethod (global tree + `EthosTree.vendored_identities(directory)`); `run()` = ensure global ext files (unchanged, `_ensure_ext`) → `refresh(global)` → `refresh(vendored)` refresh-only; `_write_context` (5 params, `:95-115`) **deleted** — its bucketing is `ExtScanOutcome`'s; `EthosMemoryResult.vendored_updated: list[str]` | ±0 | **avg_params ↓** (5→≤2 on the worst method), max_complexity ↓; coupling 1→2 (C7) |
| `src/quarry/enable.py` | `EthosMemoryBootstrap.for_repo(directory).run()` (`:149`); `EnableResult.ethos_vendored_updated` | +3 | unchanged (lazy import at `:135` is not scored) |
| `src/quarry/enable_report.py` | `_ethos_lines` adds `Ethos guide refreshed (vendored — commit via PR): …` when `ethos_vendored_updated` is non-empty (`:63-81`) | +4 | unchanged |
| `src/quarry/cli_ingest.py` | `--memory-type` help names the vocabulary from `MemoryType`; the three memory options shared by `ingest`/`remember` become module-level `Annotated` aliases | −8 | module_size ↓; coupling 1→2 |
| `src/quarry/mcp_server.py` (553 lines — over the limit, so this touch **extracts**) | `_guard` (`:55-74`, with its `functools` import) leaves for `mcp_guard.py`; every `@_guard` becomes `@ToolGuard.wrap`; `McpTools.register` adds one line, `MissionTools(self._connect).register(server)`, and the tool itself lives in `mcp_missions.py`; `main()`'s `LoggingConfig.configure(stderr_level="INFO")` (`:546`) moves to the only launcher, `__main__.mcp()` (`:303-307`, which already imports `LoggingConfig` at `:31`; `doctor.py:323` registers the `quarry mcp` command, and nothing runs `python -m quarry.mcp_server` — `plugin/`, `pyproject.toml`, `doctor.py` checked), so the `if __name__ == "__main__"` block (`:552-553`) and the `logging_config` import go; `from collections.abc import Callable` (annotation-only: `_connect`, `__new__`) moves under `TYPE_CHECKING`; `remember`/`find` docstrings (b.4); "eleven tools" (`:10`) → "twelve" | **−15** (≈538 after, ≤553) | **module_size ↓ 481→~465**, method_ratio ↑ (`_guard` and `main`-adjacent free code gone); **coupling 7→7** — −`collections` (false positive, see the `base.py` row) −`logging_config` +`mcp_guard` +`mcp_missions`: the two real additions are paid for by one real removal and one bookkeeping removal, and the ceiling is held with a truthful ledger. No relax |
| `src/quarry/__main__.py` | register `MissionsCli`; `"missions"` in `_COMMAND_ORDER`; `mcp()` configures stderr logging before `mcp_main` (+1); `_cli_errors` uses `exc.is_conflict` (drops `quarry.client.errors` import); the dead hidden `hooks` Typer sub-app deleted (`:90-94,338-362`; every plugin script dispatches `quarry-hook`, `plugin/hooks/*.sh`; no caller in plugin/scripts/tests/settings) | −23 | **module_size ↓**; coupling 13→13 |
| `plugin/commands/quarry.md`, `plugin/commands/remember.md`, `plugin/skills/recall/SKILL.md`, `src/quarry/data/repo-guide.md` | b.4 / c.5 text | +18 | docs |
| `docs/WORKFLOW.md` (leader-authored) | `quarry missions sync` after `close(mission)`; worker's pre-result memory line | +6 | docs |
| `DESIGN.md` (leader-authored) | DES-055 (§g.2) | +40 | docs |
| `CHANGELOG.md` `## [Unreleased]`, `README.md` (missions verb, memory guide, attribution note) | | +30 | docs |
| `tests/test_hooks_agent.py`, `tests/test_hooks.py` (pre-compact suites move out — e.1; `TestHookImportsNoEngine` stays and imports `PreCompactHook`), `tests/test_session_transcript.py`, `tests/test_transcript_reader.py`, `tests/test_doctor.py` + `tests/test_ethos_memory.py` (their `EthosExtDiagnostics` cases move to the new `tests/test_doctor_ethos.py`; `test_ethos_memory.py` gains the `for_repo`/vendored-refresh cases), `tests/test_enable.py`, `tests/test_enable_report.py`, `tests/test_ethos_handle.py`, `tests/test_retrieval_fusion.py`, `tests/test_agent_memory.py`, `tests/test_doctor_memory.py`, `tests/test_http_server.py`, `tests/test_mcp_server.py`, `tests/test_cli.py`, `tests/test_client.py`, `tests/test_lesson.py`, `tests/test_daemon_capture.py` (new file; today `DaemonCaptureSender` is covered only inside `test_hooks*.py`) | §f | | |
| `.oo-baseline.json`, `.oo-audit.jsonl`, `.oo-coupling-baseline.json`, `.oo-coupling-audit.jsonl`, `.suppression-baseline.json`, `.suppression-audit.jsonl` | `make update-oo` / `update-coupling`; suppressions unchanged (zero added) | | |

`docs/openapi.json` is **not** touched: no route or wire model changes
(`RememberRequest`, `CaptureIngestRequest` unchanged; `timeout` is client-side).

### e.3 DELETE

- `__main__.py:90-94, 338-362` — the hidden `quarry hooks {session-start,post-web-fetch,pre-compact}` sub-app (dead since `quarry-hook`, `_hook_entry.py:88-97`).
- `hooks.py:683-705` — `_as_str`/`_as_dir`, verbatim duplicates of `HookPayload.as_str/as_dir` (`_hook_trace.py:118-141`); `hooks.py:708-784` — `_precompact_target`/`handle_pre_compact` (moved to `hooks_compact.py`, behaviour unchanged).
- `mcp_server.py:55-74, 546, 552-553` — `_guard` (moved to `mcp_guard.py`), the logging call (moved to `__main__.mcp()`), the launcher-less `__main__` block.
- `daemon/routes/base.py:29-32` — `RESERVED_MEMORY_TYPE`.
- `retrieval/fusion.py:16-19`, `doctor_memory.py:21-27` — the duplicated vocabularies.
- `doctor_ethos.py:12-36, 159-187` — the v1 template and `_message`; `:154` — the `# noqa: BLE001` broad catch.
- `ethos_memory.py:95-115` — `_write_context` (its bucketing is `ExtScanOutcome`'s).
- `ethos_handle.py:42-52` — `_walk_up`.

### e.4 Four-surface parity points

1. **`memory_type` validation** — one daemon rule on `/remember`, `/ingest`, `/capture`; CLI `--memory-type` help and MCP `remember`/`ingest` docs name the same four values from `MemoryType`; equivalence test asserts the CLI and MCP send the value untouched and the daemon returns the same 400 body for both.
2. **`missions sync`** — CLI, MCP, slash door; HTTP is `/remember`. Equivalence test (f.3) asserts CLI and MCP produce identical `RememberRequest` sequences.
3. **SubagentStop `summary`** — already in `CaptureIngestRequest`; HTTP test asserts `/capture` persists `summary` on every chunk (it does today via `pipeline.py:474`; the test pins it).
4. **Guide text** — the five moments appear in the session_context v2, the MCP `remember` docstring, the recall skill, and `remember.md` (a docstring-splice test extends `tests/test_mcp_server.py:774-821`'s pattern).

### e.5 Ratchet projection (honest)

Improvements on touched files (`make check-oo` needs ≥1 and no unwaived
regression, `tools/oo_ratchet/ratchet.py:220-230`): `ethos_handle.py`,
`hooks_agent.py`, `hooks.py` (module_size 674→~575, method_ratio ↑),
`mcp_server.py` (module_size 481→~465, method_ratio ↑), `routes/ingestion.py`,
`fusion.py`, `doctor_memory.py`, `doctor_ethos.py`, `cli_ingest.py`,
`__main__.py` (module_size ↓ and/or complexity ↓), `session_transcript.py`
(avg_params ↓), `ethos_memory.py` (avg_params ↓, max_complexity ↓),
`transcript_reader.py` (complexity ↓), `client/client.py` (max_complexity ↓).

The two modules already over the 500-line limit are both **extracted, not
grown** (CLAUDE.md "Module size limits": the next change to such a module
must include extraction): `hooks.py` 783→~679 and `mcp_server.py` 553→~538.
Neither needs a module_size relax.

Scoped relaxes expected (module_size only, each carrying feature substance,
each `--relax --justify`-audited — the sanctioned mechanism used by
m-2026-09-02-003 and accepted by its evaluator; R1–R6 pre-authorized by kpz
in round 1, R7 denied and removed):

| Id | File | Metric | Why unavoidable |
|---|---|---|---|
| R1 | `session_transcript.py` | module_size +22 | `TranscriptSource` + `for_session` + `summary`; avg_params improves in the same file |
| R2 | `transcript_reader.py` | module_size +6 | `records()`/`last_assistant_text()`; complexity improves in the same file |
| R3 | `daemon_capture.py` | module_size +5 | third scrubbed door (`send_remember`) |
| R4 | `client/client.py` | module_size +4 | `timeout` parity on `remember`; max_complexity improves in the same file |
| R5 | `client/errors.py` | module_size +6 | `is_conflict` (lets `__main__` drop an import) |
| R6 | `lesson.py` | module_size +14 | the lesson's own cap and type move to the lesson's class |

The files at the `efferent_coupling <= 7` ceiling (`hooks_agent.py`,
`mcp_server.py`, `routes/ingestion.py`; `__main__.py` at 13 of 15) hold their
counts by the import swaps named in e.2. Seven files gain exactly one internal
import — the shared vocabulary or the shared tree locator replacing a local
copy — and the coupling ratchet compares per file against the merge-base, so
each is a +1 regression it will flag (the m-2026-09-02-003 precedent:
`sync_discovery.py efferent_coupling 1->2` for importing the extracted
`IgnoreRules`, relaxed with justification and accepted). Coupling relaxes
expected, all far inside the threshold (C1–C6 pre-authorized in round 1; C7
is new in round 2 and needs the leader's nod — D4):

| Id | File | efferent_coupling | Why |
|---|---|---|---|
| C1 | `lesson.py` | 1→2 | imports `MemoryType` for the lesson's own type |
| C2 | `cli_ingest.py` | 1→2 | help text names the vocabulary from `MemoryType` |
| C3 | `ethos_handle.py` | 0→1 | `_walk_up` replaced by `EthosTree.ancestors` |
| C4 | `retrieval/fusion.py` | 2→3 | local `_DECAYABLE_TYPES` copy replaced by the import (`from collections import defaultdict` is a runtime use and cannot move) |
| C5 | `doctor_memory.py` | 3→4 | local `_MEMORY_TYPES` copy replaced (`Counter` is a runtime use) |
| C6 | `session_transcript.py` | 1→2 | `for_session` resolves the handle via `EthosConfig` |
| C7 | `ethos_memory.py` | 1→2 | `for_repo` locates the vendored tree via `EthosTree` (D3) |

Offsets in the same PR: `hooks.py` **6→4** (drops `EthosConfig` and
`SessionTranscriptCapture` with the extraction), `__main__.py` 13→13 (drops
`client.errors`). Two holds are **bookkeeping against a scorer false
positive, not decoupling**, and are recorded as such: `base.py` 4→4 and
`mcp_server.py` 7→7 each move an annotation-only `collections.abc` import
under `TYPE_CHECKING` — legitimate PY-TS-7 hygiene — and the scorer stops
counting it only because `_absolute` (`tools/coupling/imports.py:59-62`)
had been matching the `collections` top segment against
`src/quarry/collections.py`. `base.py`'s real fan-out rises 3→4 and
`mcp_server.py`'s stays 6→6 (−`logging_config` +`mcp_guard` +`mcp_missions`
−`collections`, of which only the last is fictional). New modules start under
the ceiling: `hooks_compact.py` 3, `mcp_guard.py` 0, `mcp_missions.py` 3,
`ethos_tree.py` 0, `subagent_capture.py` 4, `mission_memory.py` 3. No
suppression is added, and one is retired (`doctor_ethos.py:154`), so
`make check-suppressions` improves.

---

## f. Test plan

Hermetic per DES-047: every test below uses `tmp_path` trees, the
`InProcessDaemon` (`tests/inproc_daemon.py`, real routes, `FakeEmbeddingBackend`)
for HTTP, patched `TargetResolver.connect` for CLI/MCP, and never loads a
model or touches the real `~/.punt-labs`. Test count today: **3758 collected
(62 deselected)**; the plan adds ~95 and deletes none, so the count rises.

### f.1 By bug class

**Class 1 — file I/O safety.** `write_session_context` now writes through
`AtomicFile`: (a) append and splice succeed and the file is byte-identical
outside the block (CRLF preserved); (b) `os.fdopen` raising closes the fd and
leaves no temp (`AtomicFile` already tests this; the new tests assert the
*writer* uses `AtomicFile.replace` and that a raising replace leaves the v1
block intact); (c) mode preserved. Loop 2 and Loop 3 write no local files.

**Class 2 — exception boundaries.**
`MissionStore.scan` with a malformed `results.yaml`, a `contract.yaml` missing
`worker`, a non-int `round`, and an unreadable file → each becomes one
`errors` entry, the other missions still scan, nothing propagates.
`MissionMemorySync.run` with the daemon returning 500 on `show_document` → an
error entry, not a skip; a 200 whose first line is another round's header →
a `name collision` error entry and **no** `remember` call, with and without
`--force`; a `QuarryConnectionError` on `remember` → recorded, run continues.
`EthosMemoryBootstrap.for_repo` with no vendored tree → global-only, result
`vendored_updated == []`; with a vendored `quarry.yaml` that raises
`UnicodeDecodeError` → that handle in `failed`, the others refreshed, `enable`
exits 0 with the failure printed. `EthosExtDiagnostics.refresh` with a
non-OSError bug injected → propagates (the narrowed catch is asserted, not
just the happy path). `PreCompactHook.handle` keeps every skip contract the
moved suite asserts (`TestHandlePreCompact`: no transcript, disabled by
config, non-JSONL, unresolvable path, non-string fields → `{}`). `SubagentCapture.capture` with an unreadable transcript, a
transcript with no assistant turn, `send_remember` returning `False`, and
`send_remember` raising a non-client exception (must not — asserted via the
`{}` invariant on `subagent_stop`). `EthosTree.identity_exists` with an
`agent_type` of `../claude`, `""`, `"A B"` → `False`, no path built.
`EthosConfig.agent_handle_at` with a malformed `.punt-labs/ethos.yaml` → `""`
and a warning (extends `tests/test_ethos_handle.py:54-60`).
`reject_invalid_memory_type("facts")` → 400 with the expected text on all three
routes; `"lesson"` keeps the existing text (`test_http_server.py:2017,2269,2628`).

**Class 3 — remote/local divergence.**

- `tests/test_mission_memory.py::test_cli_and_mcp_produce_identical_requests`: run `quarry missions sync --json` (CLI runner, recording transport as in `tests/test_cli.py:263-301`) and `McpTools.missions_sync()` against the same fixture tree; assert the ordered list of `RememberRequest` bodies is identical.
- `test_http_server.py`: `/remember`, `/ingest`, `/capture` each return the same 400 body for `memory_type="facts"`; `/capture` with `summary="s"` stores `summary == "s"` on every chunk.
- `test_cli.py` / `test_mcp_server.py`: `--memory-type procedure` / `memory_type="procedure"` reach the wire unchanged.

**Class 4 — TLS.** Not applicable: no certificate, context, or bind change.
Stated so the checklist is complete.

**Class 5 — install scripts.** Not applicable: no shell or `hooks.json`
change. `TestHookWiring` (`tests/test_hooks.py`) keeps covering the existing
scripts.

### f.2 By module

- `test_memory_types.py`: parse each member; `parse("Facts")`, `parse("")`, `parse("lesson ")` raise; `is_decayable` false only for LESSON; `DECAYABLE_MEMORY_TYPES` equals the set `fusion.py` used to hard-code (golden).
- `test_ethos_tree.py`: ancestors order; `nearest` finds the vendored dir from a subdirectory; `identities_dirs` returns vendored-then-global, each only if present; `missions_dir` `None` when absent; `identity_exists` happy/unknown/invalid-handle.
- `test_ethos_ext_block.py`: absent/stale(v1 verbatim from `rmh.ext/quarry.yaml`)/current/custom classification; splice keeps preceding keys and trailing keys; render is a valid literal block (round-trip through `yaml.safe_load` yields the text); header is line 1.
- `test_doctor_ethos.py` (new file, e.1): refresh returns `updated` once then `already_set`; custom block untouched; `configure()` still scans the global dir only; `refresh` on an absent dir → empty outcome; `ExtScanOutcome.message()` golden.
- `test_ethos_memory.py`: `for_repo` with/without a vendored tree; vendored ext files are refreshed and never created (a vendored identity with no `quarry.yaml` stays without one); `EthosMemoryResult.vendored_updated` lists the refreshed handles; existing bucket tests keep passing against the `ExtScanOutcome`-backed `run()`.
- `test_enable.py` / `test_enable_report.py`: `enable_project` on a fixture repo with a vendored tree reports `ethos_vendored_updated`; the report line appears only when non-empty.
- `test_hooks_compact.py` (moved): the whole `TestHandlePreCompact` / redaction / cwd-hardening suite against `PreCompactHook.handle`, unchanged assertions; `PreCompactTarget.source("pre-compact")` yields the `TranscriptSource` `for_session` expects.
- `test_hook_trace.py` (new): `HookPayload.as_dir` (the moved `TestAsDir`) and `as_str`.
- `test_hooks.py`: `handle_session_start`/`handle_post_web_fetch` cwd-hardening tests unchanged (they now exercise `HookPayload.as_dir` through the handlers); `TestHookImportsNoEngine` imports `PreCompactHook`.
- `test_mcp_guard.py`: a raising tool returns `Error: ValueError: …` and logs with `exc_info`; a returning tool passes through; `functools.wraps` preserves the name FastMCP registers.
- `test_mcp_missions.py`: `MissionTools.register` adds `missions_sync`; a direct `MissionTools(connect).missions_sync()` and the registered tool return the same error string on a down daemon (shared boundary); `dry_run` posts nothing.
- `test_doctor_memory.py`: `identity_active` zero-rows message names both remedies; `passed=False` still (the warning is correct — §h step f).
- `test_ethos_handle.py`: new-file precedence over legacy at the same ancestor; legacy-only still works; `test_walks_up_to_ancestor_config` unchanged.
- `test_transcript_reader.py`: `records()` skips bad lines; `last_assistant_text` picks the last assistant turn, ignores `tool_use` blocks, `""` on none; `text()` golden unchanged against an existing fixture.
- `test_session_transcript.py`: `TranscriptSource` threading; `for_session` resolves the handle from a `.punt-labs/ethos.yaml` fixture; `summary` lands on the wire request.
- `test_subagent_capture.py`: `handle_for` — identity present → handle; `general-purpose` → `""`; absent `agent_type` + pin → leader; report composition incl. artifacts header; truncation boundary at exactly 32 000 and 32 001 chars; distilled request fields (name, type, handle, empty collection, overwrite); no distill when handle empty; no distill when report empty; raw capture still sent in both.
- `test_hooks_agent.py`: `subagent_stop` still returns `{}` under the crafted payloads (unchanged test); new: `rmh` payload → two sends (capture then remember) with the expected shapes; `general-purpose` → one send; trace line carries `-> capture:distilled`.
- `test_hooks.py`: `TestPreCompactEthosTagging` fixtures move from `config.yaml` to `.punt-labs/ethos.yaml` and assert the same handle (`tests/test_agent_memory.py:638-701`).
- `test_mission_records.py` / `test_mission_store.py`: the fixture trio parses to the expected records; `is_frozen` truth table (open/round<current, open/round==current, closed, failed); a contract without `repo:` (the `m-2026-09-02-003` shape) takes the store's root and names `mission-<rootbasename>-…`; scan error collection.
- `test_mission_memory.py`: compose golden (name, summary, type, handle, empty collection, content sections in order); existence 404 → filed, 200 → skipped, `--force` → filed with `overwrite=True`; dry-run posts nothing; `--mission` filter; outcome `render()`.
- `test_cli_missions.py`: `quarry missions sync --json` shape; exit 1 on errors; `--dry-run`; help text.
- `test_mcp_server.py`: `McpTools.register` registers twelve tools including `missions_sync` (via `MissionTools`); docstring splices; daemon-down returns an error string (extends `TestDaemonDown`); `main()` no longer configures logging — `test_cli.py` asserts `quarry mcp` does.
- `test_mission_memory.py`: `compose` header carries repo basename and `contract.created_at`; a no-reflection frozen round composes the `Evaluator reflection: none — closed closed at round 2` line (fixture: `m-2026-09-09-001`-shaped); a reflection-only round composes `Worker verdict: none`; a round with neither is skipped with an INFO line; name golden `mission-quarry-m-2026-09-09-001-r2`.
- `test_daemon_capture.py` (new): the four failure classes for `send_remember`, plus the existing two doors relocated from `test_hooks*.py` where they are only exercised indirectly.
- `test_client.py`: `remember(timeout=)` reaches the transport; `await_task` behaviour golden after the extraction.
- `test_lesson.py`: `check_length` boundary at 500/501; `memory_type()` is `"lesson"`.

### f.3 Equivalence and golden guards named

- `test_cli_and_mcp_produce_identical_requests` (class 3, Loop 2).
- `test_memory_type_rejection_identical_on_three_routes` (class 3, Loop 1).
- `test_capture_summary_persists_per_chunk` (class 3, Loop 3).
- `test_decayable_set_matches_fusion_golden` (guards that the vocabulary move changes no ranking).
- `test_v1_block_refreshes_to_v2_once` (guards idempotency of the guide writer).
- `test_precompact_suite_unchanged_after_extraction` — not one test but the moved `TestHandlePreCompact` class passing with zero assertion edits (PY-RF-2 behaviour preservation for the `hooks.py` extraction).
- `test_mission_name_collision_is_error_not_overwrite` (guards the cross-machine residual in c.3).

Coverage cannot decrease: every deleted line is dead code, a verbatim
duplicate (`_as_str`/`_as_dir`), or a moved function/constant whose tests
move with it; every new module ships its own test file, and two existing
modules that had none (`_hook_trace.py`, `doctor_ethos.py`) gain one.

---

## g. Rejected alternatives and the ADR plan

### g.1 Decisions the leader must ratify before dispatch

- **D1** — unknown `memory_type` becomes a 400 on `/remember`, `/ingest`, `/capture` (b.5). Recommend: proceed; a misfiled type is a silent bug today.
- **D2** — non-identity subagents (`general-purpose`, reviewers) are filed unattributed instead of as `general-purpose` (b.2). Recommend: proceed.
- **D3** — `quarry enable <dir>` refreshes the *vendored* `.punt-labs/ethos/identities/*.ext/quarry.yaml` blocks above `dir` (refresh-only, never creating), producing a working-tree diff the operator commits via PR; `quarry install` step 8 keeps refreshing the global tree; `quarry doctor` stays read-only (b.4). Recommend: proceed; `enable` is already the per-repo write step that deposits and upgrades the repo guide (`enable.py:127-149`), and it is the only way a `repo-only` repo ever sees the v2 guide. (Round 1 had attributed this to `doctor`; `EthosExtDiagnostics.configure()` is in fact called only from install, `doctor.py:630-637`.)
- **D4** — the six scoped module_size relaxes (R1–R6) and seven +1 coupling relaxes (C1–C7) in e.5. R1–R6 and C1–C6 are pre-authorized by kpz's round-1 reflection; **C7** (`ethos_memory.py` 1→2, the `EthosTree` import that D3 needs) is new this round and needs the leader's nod. R7 is withdrawn — `mcp_server.py` shrinks. Anything beyond the list returns to the leader.

### g.2 Rejected alternatives (consolidated)

| Alternative | Why rejected |
|---|---|
| Server-side `agent_handle` auto-resolve on `remember`/`learn` | no `cwd` on the wire; cwd → leader for subagents; remote daemon has no repo (b.1) |
| Attribute non-identity subagents to the leader | inflates the leader's counts, pollutes `find(agent_handle=leader)` (b.2) |
| Keep `already_set` as terminal (no refresh) | v2 would never reach any existing identity (b.4) |
| Refresh the vendored ext from `quarry doctor` | doctor is read-only and never calls the ext writer today (`check_environment`, `doctor.py:645-`); making a diagnostic write working-tree files is a category change, and `enable` is already the per-repo write step (b.4, D3) |
| A read-only doctor check "guide v1 (stale) — run `quarry enable`" | useful, but it lands in `doctor.py` (687 lines, over the limit), so the one-line registration would force an unrelated `doctor.py` extraction into this PR; `enable`'s report line is the signal for now, and the check is a follow-on once `doctor.py` is decomposed |
| Fold `contract.created_at` into the Loop 2 document name | closes the same-basename/same-day/same-sequence residual, at the price of unreadable names; the page-1 header comparison catches the collision without it (c.3) |
| Grow `mcp_server.py` with the new tool (round-1 R7) | the module is over the 500-line limit; CLAUDE.md requires extraction on the next touch. Sibling `mcp_missions.py` + shared `mcp_guard.py` instead (e.2) |
| Wrap the sibling tool with the guard at registration time only | a direct `MissionTools.missions_sync()` call would raise where the registered tool returns a string — two boundaries for one tool; `ToolGuard.wrap` applied at definition keeps the "direct call == registered tool" invariant `McpTools.register` documents (`mcp_server.py:117-123`) |
| Extract the database-selection tools (`use_database`, `_list_databases`) to make coupling room in `mcp_server.py` | more churn than needed: moving one logging call to the only launcher (`__main__.mcp()`) frees the slot with a one-line change, and `main()` becomes a pure composition root |
| Leave `_as_str`/`_as_dir` in `hooks.py` and have `hooks_compact.py` import them | they are line-for-line duplicates of `HookPayload` (`_hook_trace.py:118-141`); importing the duplicate would entrench it. Delete, use the original |
| YAML round-trip to rewrite the block | destroys comments/ordering — DES-019 alt 1 (b.4) |
| Ethos-side trigger for Loop 2 | one-way dependency (c.2) |
| Daemon watcher / daemon route reading `.punt-labs/ethos/missions/` | DES-041 path-read rejection; wrong in remote mode (c.2) |
| File reflections as lessons | un-distilled text in the boosted tier; DES-053 items 1, 2, 6 (c.3) |
| Duplicate the memory into the evaluator's collection | whole-DB recall already covers it; doubles rows (c.3) |
| A filed-ledger file for idempotency | stable names + `show_document` 404 need no state (c.4) |
| LLM distillation pass | cost, no local model, heavy work in a blocking hook (d.1) |
| Heuristic type classification of transcripts | guesswork stored as truth (d.5) |
| Daemon-side distill job over captures | second reader, path-read, re-embeds the largest docs (d.5) |
| Separate `auto_capture.distill` key | a knob with no independent use (d.2) |
| Project scoping of agent memory | operator-ruled out (hard constraint 4) |

### g.3 ADR — DES-055 "The agent-memory write loop"

Extends: DES-017 (decay applies to what the loops write, and the vocabulary is
now one enum), DES-018 (the taxonomy is validated at the write boundary),
DES-019 (the ext block is versioned and refreshed; the vendored layer is a
refresh target under `repo-only`), DES-029 (`enable` still bootstraps global
ext files and now also refreshes the vendored layer above the enabled
directory; `install` refreshes global; `doctor` stays read-only), DES-030 (subagent transcripts *are* now
ingested — the "not currently ingested" implication at `DESIGN.md:893` is
superseded by afg/R4b and this ADR distils them), DES-041 (a third scrubbed
door, `send_remember`, same core, hook stays thin), DES-053 (reflections are
observations, never lessons).

Asserts: (1) identity for a write is the caller's statement — the injected
`## Memory` block for agents, `agent_type` validated against the identity
registry (vendored + global; bundles are a known v1 gap) for SubagentStop,
`contract.worker` for mission memory; `cwd` names the leader and only the
leader. (2) `memory_type` is a closed vocabulary enforced once, server-side.
(3) Evaluator feedback reaches the worker through a quarry-side, idempotent,
leader-run verb that reads ethos artifacts and writes through `/remember`;
ethos is never called; mission memories are named with a repo discriminator
because mission IDs are per-machine counters and agent memory is
cross-machine. (4) A subagent's transcript is captured raw and its own final
report is filed as an `observation` in its memory — no model, no per-hook
engine. (5) Agent memory has no project dimension. (6) The memory guide has
a version header; `install` refreshes the global tree, `enable` refreshes
global and vendored, `doctor` reads.

---

## h. Rollback coherence

**One PR.** The three loops share the attribution fix (b.2), the vocabulary
module (b.5), and the guide (b.4); reverting any one loop alone would leave
either a guide that names a verb that does not exist or a validator without
the guide that explains it. Reverting the whole PR restores today's behaviour
exactly: no schema change, no wire change, no migration, no data rewrite
(memories already filed simply stop being written; existing rows are inert
`observation`/`fact` rows the old code already understands).

**Internal commit order** (each passes `make check`; for bisectability, not
for separate merge):

1. `memory_types.py` + the three replacements + route guard rename + `lesson.py` (D1).
2. `ethos_tree.py` + `ethos_handle.py` pin-file fix + tests.
3. `ethos_ext_block.py` + `doctor_ethos.py` `refresh`/`ExtScanOutcome` + `ethos_memory.py` `for_repo` + `enable`/`enable_report` + guide text on every surface (D3).
4. `hooks_compact.py` extraction: `PreCompactHook` moved verbatim, `_as_str`/`_as_dir` deleted for `HookPayload`, `_hook_entry` repointed, the pre-compact test suite moved — a pure move, green on its own.
5. `TranscriptSource`, `records()`/`last_assistant_text()`, `send_remember`, `remember(timeout=)`, `SESSION_ID_PREFIX_LEN`.
6. `subagent_capture.py` + `hooks_agent.py`/`hooks_compact.py` `for_session` rewiring (Loop 3, D2).
7. `mcp_guard.py` extraction + `mcp_server.py` logging move to `__main__.mcp()` — a pure move, green on its own.
8. `mission_records.py`, `mission_store.py`, `mission_memory.py`, `cli_missions.py`, `mcp_missions.py`, slash door, `__main__` dead-code deletion (Loop 2).
9. CHANGELOG, README, WORKFLOW, DES-055, baselines.

Steps 4 and 7 are behaviour-preserving moves committed on their own so a
bisect that lands on either sees identical behaviour with a different file
layout (PY-RF-1: one transformation per step).

**Demo gate** (`docs/WORKFLOW.md` "The demo gate"): build + `uv tool install --force`,
`systemctl --user restart quarry`; then:

- (a) `quarry enable .` reports `Ethos updated: …` for any stale global block and `Ethos guide refreshed (vendored — commit via PR): adb, claude, djb, gvr, jfreeman, kpz, mdm, rmh` (all eight vendored identities carry a `quarry.yaml` ext today); `git status` shows exactly those eight `.punt-labs/ethos/identities/*.ext/quarry.yaml` modified, each block now headed `## Memory (quarry guide v2)`; a second `quarry enable .` reports them `already_set` and `git status` is unchanged. `quarry doctor` reports `identity 'claude' active` — it **passes** here because the corpus holds zero memory rows (`doctor_memory.py:134-135`), and it does not mention the ext files, because doctor never writes them and this design does not make it start.
- (b) spawn an `rmh` subagent that calls `remember(... agent_handle="rmh" ...)` → `quarry list collections` shows `memory-rmh`.
- (c) `quarry missions sync --dry-run` lists `m-2026-09-02-003` rounds 1–2 and `m-2026-09-09-001` rounds 1–2 — each mission has two results and one reflection on disk, so each round 2 is a closed mission's final, reflection-less round and its line reads `… r2 (quarry): worker pass/closed`; `m-2026-09-02-003` has no `repo:` field and still names as `mission-quarry-…` from the scanned root. Then a real run files four documents named `mission-quarry-m-…-r<n>` and a second run skips four.
- (d) the SubagentStop of that `rmh` agent yields both `session-<id8>` in `punt-labs-captures` (with a summary) and `subagent-<id8>-report` in `memory-rmh`.
- (e) invalid: `quarry remember --memory-type facts` → exit 1 with the 400 text; missing dependency: daemon stopped → `missions sync` reports `unreachable`, exit 1; boundary: a mission dir with a malformed `results.yaml` → one error line, the other missions filed.
- (f) **expected warning, not a defect:** after (b)–(d) the corpus holds memory rows owned by `rmh`, `gvr`, and `claude`-none, so `quarry doctor` now reports `Memory identity: identity 'claude' active in this repo but has zero memory rows while other agents have N; the leader's next PreCompact, or 'quarry remember --agent-handle claude', populates it` with `passed=False` (`doctor_memory.py:136-144`). That is the walker fix working — before it, `agent_handle_at` returned `""` here and doctor said "no ethos identity active". The operator then runs `quarry remember "demo note" --agent-handle claude --memory-type observation` and doctor reports `identity 'claude' has 1 memory rows`. Written down here so the demo does not read the warning as a regression.

---

## Appendix A — session_context v2 (rendered for `rmh`)

```text
## Memory (quarry guide v2)

You have persistent memory in quarry. It is cross-project and cross-machine:
what you remember here is findable from any repo, by you and by teammates.

Collection: "memory-rmh"  ·  Handle: "rmh"

Recall — before answering a why/how/what-did-we-decide question:
  find(query, agent_handle="rmh")   only your own memories
  find(query)                       everything: teammates' memories, lessons, captures

Persist — call remember at these moments, not at the end and not never:
  1. you found a non-obvious root cause or gotcha           memory_type="fact"
  2. a design decision was ratified, with its reason         memory_type="fact"
  3. you worked out a repeatable how-to                      memory_type="procedure"
  4. you formed a judgement you will want to revisit         memory_type="opinion"
  5. before submitting a mission result, one note on what
     you would tell yourself next time                       memory_type="observation"

  remember(content, document_name="<topic>-<slug>", agent_handle="rmh",
           memory_type=..., summary="<one line>")

Always pass agent_handle="rmh". The daemon cannot infer your identity — a
subagent's working directory resolves to the repo's leader, not to you.

Do not remember progress narration, file contents, tool output, or anything
already in the repo. A rule the whole project should follow is a lesson:
learn(lesson) — project-scoped, no handle, retrieval preference.

Memory types: fact = objective, verifiable · observation = neutral summary ·
procedure = how-to · opinion = subjective assessment with confidence.
Memories decay with a 30-day half-life; lessons and documents do not.
```

## Appendix B — mission memory document (Loop 2)

```text
# Mission m-2026-09-02-003 — round 1 (repo quarry, worker rmh, evaluator djb, created 2026-09-02T12:42:20Z)

Worker verdict (self-assessed): pass, confidence 0.90
Evaluator reflection: continue (converging: true), authored by claude

## Reflection signals
- evaluator djb: REJECT — blocker: WatchedTree._watches mutated from event-loop and observer threads with no lock; …
- evaluator djb: major — schedule_tree aborts the whole tree on any OSError; …
- …

## Recommendation
continue — Round 1's DRY consolidation stands; round 2 replaces the per-directory scheduling mechanism …

## Open questions (worker)
- Two coupling-ratchet metrics were relaxed with an inline audit-log justification …

## Worker report
<results.yaml prose, verbatim; scrubbed server-side>
```

The first line is the identity key the c.4 check compares. `Worker verdict`
is labelled self-assessed because that is what `results.yaml: verdict` is —
the worker's own submission, not the evaluator's judgement; the evaluator's
judgement is the reflection block. For a closed mission's reflection-less
final round the second block reads `Evaluator reflection: none — closed
closed at round 2` and the `## Reflection signals` / `## Recommendation`
sections are omitted (c.1). The evaluator's full review prose is not on disk
and therefore not here (c.3).

`summary`: `m-2026-09-02-003 r1 (quarry): worker pass/continue — evaluator djb: REJECT — blocker: WatchedTree._watches mutated from event-loop and obs…`

Document name: `mission-quarry-m-2026-09-02-003-r1`.
