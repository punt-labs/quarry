# Requirements: quarryd Host-Adapter Contract and opencode Support

**Status:** Draft for review — not yet a design
**Date:** 2026-08-30
**Tracking:** Epic quarry-dego
**Provenance:** Operator rulings and research sessions of 2026-08-29/30.
**Sibling documents:** `biff/docs/requirements-biffd-multihost.md` and
`vox/docs/requirements-voxd-multihost.md` — one org program, three
repos. The operator's stated goal: bring Punt Labs products to an
architecture that enables multi-harness support, add opencode as a
second harness as a first-class integration, and enhance each product
where opencode's architecture permits things Claude Code does not.
**Relationship to DESIGN.md:** This document states *what* the work
must achieve. Design missions produce the *how* as DES entries. Where
this document names a mechanism (`session.compacted`, the SDK client),
that mechanism is a verified host capability — a constraint, not a
design choice left open.

Same engineering bar as the sibling programs. Nothing here is a pilot
or a shortcut: every module goes through the full lifecycle (design
mission, tests, Phase-3 verification through a real entry point,
operator confirmation), and no requirement bends for convenience.

---

## 1. Overview

Quarry has already ratified its own daemon-first restructuring:
DES-031 v2.2 (`docs/des-client-architecture.md`, building on
`docs/des-031v2-daemon-first.md`) makes `quarryd` the one engine
process — owner of the database, embeddings, ingestion, and retrieval
— with the CLI, `quarry mcp`, and `quarry-hook` as pure clients via
`QuarryClient`, explicitly modeled on vox's `voxd` + `vox mcp` shape.
That program is ratified and in flight; this document does not modify
it.

What this program adds, on top of that foundation:

1. **M1** adopts the org host-adapter contract (vox M1 is the
   template) and pins the DES-031 v2 dependency: multihost work
   builds on engine-free clients, not on today's mixed paths.
2. **M2–M5** add an opencode adapter: quarry's session-lifecycle
   behaviors re-homed onto opencode plugin events, native tool
   parity, enablement deposits, and an npm release channel.
3. **M6** names the harness-unique enhancement quarry is uniquely
   positioned for — continuous session capture — and requires it to
   be evaluated as a design question, not improvised.

| Module | Name | Depends on |
|--------|------|------------|
| M1 | Contract adoption + DES-031 v2 dependency | DES-031 v2 |
| M2 | opencode adapter: session lifecycle | M1 |
| M3 | opencode adapter: tool surface + agent | M1 |
| M4 | Enablement and deposits | M2, M3 |
| M5 | Distribution and release | M2, M3 |
| M6 | Harness-unique enhancements | M2 |

Requirement keywords MUST, SHOULD, and MAY follow RFC 2119. Every
requirement carries an ID for traceability into design docs, beads,
and mission contracts.

---

## 2. M1 — Contract adoption and the DES-031 v2 dependency

### Requirements

- **R-C.1** The org host-adapter contract (vox requirements M1,
  R-C.1–R-C.6) MUST be adopted for quarry and logged as a DES entry:
  adapters contain delivery mechanics only; all engine access goes
  through `quarryd` via `QuarryClient`; semantics are
  host-independent; adapters MAY differ only in how a semantic event
  is delivered, never in what it means.
- **R-C.2** DES-031 v2 completion for the touched paths is a hard
  dependency. An opencode adapter MUST NOT be built against the
  in-process engine paths that v2 retires: no adapter work may import
  the engine, and adapter missions MUST sequence after the
  `QuarryClient`/`quarryd` seams they depend on exist.
- **R-C.3** Per-repo behavior MUST remain host-independent:
  collection resolution, registration state, capture destinations,
  and enablement semantics MUST be identical whichever host performed
  them. A document ingested from an opencode session MUST be
  indistinguishable, as data, from one ingested from Claude Code.
- **R-C.4** Concurrent hosts against one `quarryd` MUST work: a
  Claude Code session and an opencode session on the same machine at
  the same time, with serialization owned where DES-031 v2 puts it —
  in the daemon.

### Acceptance

M1 is done when the contract DES entry is merged, the dependency
edges to the DES-031 v2 beads are recorded on this epic's children,
and the opencode design mission cites both documents as governing.

---

## 3. M2 — opencode adapter: session lifecycle

Quarry's Claude Code hook layer, re-homed. Today: SessionStart
auto-indexes the working directory, PreCompact captures the session
transcript before compaction, and WebFetch results are auto-ingested.
All three are `quarry-hook` invocations — already pure clients under
DES-031 v2 (R6).

### Requirements

- **R-L.1** The adapter MUST deliver the same semantic lifecycle
  behaviors, mapped to opencode events:

  | Semantic behavior | Claude Code today | opencode source |
  |-------------------|-------------------|-----------------|
  | working-directory auto-index | SessionStart hook | `session.created` |
  | pre-compaction transcript capture | PreCompact hook | `session.compacted` |
  | web-fetch auto-ingest | PostToolUse hook | `tool.execute.after` |

  The exact event mapping is design-confirmed against opencode source
  (SP-Q.2), but the semantic set is fixed: no capture or indexing
  behavior a Claude Code user gets goes missing on opencode.
- **R-L.2** Capture timing MUST be verified, not assumed: whether
  `session.compacted` fires before or after the transcript is
  rewritten determines whether capture-at-compaction is even the
  right mechanism on opencode (see M6 for the alternative). If the
  event fires too late to capture pre-compaction content, parity MUST
  be achieved another way; silently capturing less than Claude Code
  captures is not acceptable.
- **R-L.3** Auto-ingest MUST apply the same inclusion rules on both
  hosts (which fetched content is worth ingesting, dedup against
  already-ingested URLs), sourced from the same engine-side logic —
  the adapter forwards, `quarryd` decides.
- **R-L.4** The adapter MUST NOT poll, and lifecycle failures MUST be
  inert to the coding session: a `quarryd` outage or adapter fault
  MUST NOT block, delay, or error the user's session. Failures are
  logged through quarry's existing logging, never swallowed silently.

### Acceptance

M2 is done when a live opencode session demonstrably indexes its
working directory at start, captures its transcript around
compaction with content parity to Claude Code, and auto-ingests a
fetched URL — verified through the real entry points and
operator-confirmed.

---

## 4. M3 — opencode adapter: tool surface and researcher agent

### Requirements

- **R-T.1** The full quarry MCP tool surface MUST be available as
  native opencode tools with argument and output parity: `find`,
  `show`, `ingest`, `remember`, `use`, `status`, `list`, `delete`,
  `register_directory`, `deregister_directory`,
  `sync_all_registrations`. Tools MUST be thin `QuarryClient` clients
  (R-C.1); any logic beyond validate-call-render is a contract
  violation.
- **R-T.2** Output conventions carry over: quarry emits preformatted
  unicode tables that agents MUST render verbatim. Where opencode's
  rendering makes a convention impossible, the deviation is
  documented in the deposited agent guide, not silently improvised.
- **R-T.3** Slash-command equivalents (`/find`, `/ingest`,
  `/remember`, `/explain`, `/source`, `/quarry`, `/use`) MUST ship in
  opencode's command format, routing to the native tools.
- **R-T.4** The `researcher` agent MUST ship in opencode's agent
  format (`mode: subagent`) with capability parity: quarry-first
  search, web research for gaps, auto-ingest of valuable findings.
  Divergences forced by host differences (tool names, permission
  models) are documented in the agent definition.

### Acceptance

M3 is done when every tool has a passing parity test against its
opencode counterpart (same `quarryd` calls for the same inputs), the
commands invoke them end-to-end in a live session, and the researcher
agent completes a real research task from an opencode session.

---

## 5. M4 — Enablement and deposits

### Requirements

- **R-E.1** Quarry's enablement MUST detect installed host surfaces
  and deposit what each needs. For opencode: the tools shim, command
  files, agent definition, the `opencode.json` plugin entry, and the
  agent guide — wired so opencode actually loads it, meaning
  referenced from `AGENTS.md` or listed in `opencode.json`
  `instructions`, since opencode does not parse the `@`-imports
  Claude Code uses. For Claude Code: unchanged.
- **R-E.2** Disable MUST remove exactly what enable deposited, per
  host. Enable/disable MUST remain idempotent and MUST NOT run git.
- **R-E.3** A repo enabled for both hosts MUST behave correctly in
  both simultaneously (R-C.4), from one registration state (R-C.3).

### Acceptance

M4 is done when enable/disable round-trips cleanly on a repo used
from both hosts, verified by driving both sessions.

---

## 6. M5 — Distribution and release

### Requirements

- **R-R.1** The opencode plugin ships as an npm package,
  version-locked to quarry's existing release channels: all channels
  ship together on every version bump.
- **R-R.2** CI MUST build and test the adapter (Bun/TypeScript
  toolchain), including at least one end-to-end test in which a real
  opencode session performs a `find` and a capture against a live
  `quarryd`.
- **R-R.3** The plugin MUST pin or bound its `@opencode-ai/plugin`
  dependency, with CI catching upstream breakage before users do.
- **R-R.4** README, the deposited agent guide, and CHANGELOG document
  the opencode surface per the repo's documentation discipline.

### Acceptance

M5 is done when a clean machine can install quarry, enable a repo,
open opencode, and search/capture — with all channels at one version.

---

## 7. M6 — Harness-unique enhancements

The operator's program goal is explicit: beyond parity, each product
is enhanced where opencode's architecture permits what Claude Code
does not. For biff that enhancement is push delivery; for vox it is
recap-without-a-model-turn. For quarry it is capture fidelity.

### Requirements

- **R-X.1** Continuous session capture MUST be evaluated as a design
  question: opencode's event bus (`message.updated`, message-part
  events) permits capturing session content as it happens, rather
  than only at compaction boundaries as Claude Code's hook model
  forces. The design mission MUST evaluate it against
  capture-at-compaction on completeness, storage and indexing load,
  retrieval quality (does finer-grained capture improve recall of
  "what did we decide"), and privacy scope — and log the decision
  with rejected alternatives as a DES entry.
- **R-X.2** Any adopted enhancement MUST be additive and
  host-honest: parity behaviors continue to mean the same thing on
  both hosts (R-C.3); an enhancement is a superset on the host that
  supports it, documented as such in the agent guide — never a fork
  of semantics.
- **R-X.3** Enhancements ship only through the same full lifecycle as
  parity work: requirements traceability, design mission, tests,
  Phase-3 verification, operator confirmation. A capability being
  exciting is not a reason to skip a step.

### Acceptance

M6 is done when the continuous-capture decision is a merged DES entry
with a real evaluation behind it, and any adopted enhancement meets
the same acceptance bar as M2.

---

## 8. Spikes (resolve before design closes)

Shared spikes are run once across the three programs and cited
everywhere.

- **SP-Q.1** SDK transcript access from a plugin (shared with vox
  SP-V.1 / biff SP-2): API, measured token usage, visibility,
  behavior across compaction. Gates R-L.2 and M6.
- **SP-Q.2** Event semantics: does `session.compacted` fire with
  pre-compaction content still reachable? Which tool name does
  opencode's web-fetch surface expose to `tool.execute.after`? Gates
  R-L.1/R-L.2/R-L.3.
- **SP-Q.3** Plugin lifecycle (shared with biff SP-4 / vox SP-V.3):
  process model, restart behavior, sessions-per-plugin-instance —
  decides where the `QuarryClient` connection lives.
- **SP-Q.4** Continuous-capture volume: measure event rates and
  payload sizes for a realistic session to ground M6's load
  evaluation in data.

---

## 9. Non-goals

- **Modifying DES-031 v2.** The daemon-first program proceeds on its
  own ratified plan; this program consumes its seams and adds
  adapters.
- **Changing the engine.** Ingestion, retrieval, embeddings,
  collections, and capture semantics are untouched except where M6's
  ratified decision explicitly extends capture.
- **Other punt-labs tools on opencode.** Tracked by their own repos'
  epics (biff-00v, vox-o718); this document is quarry only.
- **Replacing mcp-proxy for its other consumers.** Out of scope here,
  as it is in DES-031 v2.2 (R2).

---

## 10. Sequencing and delivery

1. **Spikes** SP-Q.1–SP-Q.3 (shared spikes run once org-wide);
   SP-Q.4 alongside the M6 design mission.
2. **M1 contract adoption** — DES entry + dependency edges to the
   DES-031 v2 beads.
3. **M2 + M3** — the adapter, one design mission, implementation with
   commit-per-step, gated on the DES-031 v2 seams existing (R-C.2).
4. **M4 enablement**, then **M5 release**.
5. **M6** — design mission may start with M2; implementation only
   after its DES entry is ratified.

Each stage is an ethos mission with this document's requirement IDs
in the contract criteria. Phase-3 verification for every user-facing
stage means a live opencode session driven end-to-end, introspection
captured, operator-confirmed — `make check` alone closes nothing
here.
