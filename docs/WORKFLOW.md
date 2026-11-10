# Development Workflow

All work in this repository runs as three nested loops. The outermost loop
owns the backlog: what is worth doing and in what order. The middle loop owns
one pull request: a single shippable, revertible change. The innermost loop
owns one mission: a single delegated piece of work inside that change.

```text
Level 1 — Backlog loop   one iteration = one work batch     (beads)
  Level 2 — PR loop      one iteration = one pull request
    Level 3 — Mission    one iteration = one delegated mission
                         (design, code, test — a do-while)
```

Each level hands work down and receives finished results back. Escalation of
scope only goes up: a mission that uncovers a bigger problem grows its PR; a
PR that uncovers a new line of work files a bead for the backlog loop. Defects
move the other way — anything found while a PR is open is fixed in that PR,
never sent back to the backlog as a "follow-up bead." Filing a reviewer-flagged
defect as a bead and merging around it is the single most-corrected failure in
this repo's history; see Invariant 5.

The pseudocode at each level gives the control flow; a small Z schema at each
level gives the doorway conditions — what must be true to enter an iteration
and what must be true to leave it. The state those schemas observe:

```text
LoopState
  signals       : ℙ SIGNAL  -- issues, alerts, messages not yet triaged
  open          : ℙ BEAD    -- open beads: the single work funnel
  validated     : ℙ BEAD    -- re-proven against current main
  claimed       : ℙ BEAD    -- the current batch
  closed        : ℙ BEAD    -- beads completed by a merged PR
  activeWorkers : ℙ WORKER  -- sub-agents editing a shared worktree
  testCount     : ℕ         -- tests collected by the suite
  ------------------------------------------------------------------
  validated ⊆ open
  claimed   ⊆ validated
  open ∩ closed = ∅         -- a bead is open or closed, never both
```

A bead's lifecycle is a walk through these sets:
`open → validated → claimed → closed`. Steps that were performed ("the recap
was sent", "the daemon was
exercised") appear as named predicates over declared terms, never as bare
primed flags.

## Roles

The operator owns requirements and design direction, rules on genuine design
forks, and confirms demos for user-facing changes. Claude (the leader) runs
everything else: the backlog, the missions, the review cycles, and the merges.
Review of code is done by agents — the ethos evaluator inside each mission,
the local review agents on the diff, and Qodo and Bugbot on the PR.
There is no human **code-review** gate: the operator reads code in the IDE, but
that inspection is separate, feeds design discussion only, and nothing in these
loops waits on it. The one human gate is the **demo** (Level 2) — the operator
confirms observed behavior for user-facing changes; that is verification of what
ran, not review of the diff.

## Level 1 — The backlog loop

The backlog loop keeps the bead tracker true and decides what to work on next.
Beads (`bd`, one shared DoltDB across the org, `quarry-` prefix) are the single
funnel: every piece of work, whatever its origin, is a bead before it is
anything else. One iteration selects and completes one batch of work. The loop
runs at session start and again whenever the current batch is done. Start every
session with `/loop 2m /biff:read` and a `bd ready --limit=99` sweep.

```text
function backlog_loop():

    # 1. INTAKE — every signal becomes a bead or is disposed at the door
    for signal in [github_issues, dependabot_alerts, biff_messages,
                   operator_requests, new_scope_found_while_working]:
                   # a defect inside an open PR's unit is fixed in that PR,
                   # never filed — only genuinely NEW scope arrives here
        if duplicate(signal):  close_at_door(signal, link_existing_bead)
        elif invalid(signal):  close_at_door(signal, stated_reason)
        else:                  bd_create(signal, labels, link_back_to_source)

    # 2. VALIDATE — a bead must be true before it is workable
    for bead in candidates(bd_ready):
        confirm it is still real against current main
        confirm nothing merged or decided has superseded it
        confirm it is one rollback-coherent unit (split or merge if not)
        confirm its blocked-by links reflect reality
        otherwise: fix the bead, or close it with the reason

    # 3. ASSESS — automatic ordering; escalate only on a genuine fork
    order = sort(validated, by:
        security severity                  # open HIGH/CRITICAL first, always
        > broken user journeys             # bugs on paths users actually hit
        > active epic continuity           # finish what is started (DES-031 …)
        > debt that blocks throughput      # god-module decomp, OO ratchet debt
        > features)                        # v1 scope before v2
    if the ordering hits a fork the charter cannot resolve:
        ask the operator for a focus ruling
    else:
        proceed without asking

    # 4. SELECT AND EXECUTE
    batch = claim a realistic set from the top of the order
    #   bd update <id> --status=in_progress per claimed bead
    #   TaskCreate one entry per claimed bead (session-visible display only)
    for unit in rollback_coherent_units(batch):
        pr_loop(unit)                      # Level 2

    # 5. CLOSE OUT
    close GitHub issues resolved by the merged PRs, linking each PR
    send the batch recap via beadle-email  # covers what the per-merge recaps
                                           # do not: intake dispositions, beads
                                           # closed at validation, order changes
    return to intake — new signals have accrued while working
```

Entry and exit for one batch iteration:

```text
EnterBatch ≙ [ LoopState ]                -- no precondition: the backlog loop
                                          -- is a do-while(true); intake always
                                          -- has standing to run

ExitBatch ≙ [ Δ LoopState; mergedPRs : ℙ PR |   -- the PRs merged this batch
  intakeDisposed(signals)                 -- every signal observed at this
                                          --   iteration's intake became a bead
                                          --   or was closed at the door with a
                                          --   reason; new signals keep accruing,
                                          --   so the live queue is never empty
  ∧ claimed′ = ∅ ∧ claimed ⊆ closed′      -- the batch drained: every claimed
                                          --   bead closed by a merged PR
  ∧ resolvedIssuesClosed(mergedPRs)       -- GitHub issues answered with their PR
  ∧ batchRecapSent(claimed) ]
```

### Intake

Work arrives from five places: GitHub issues, Dependabot alerts, biff messages
from agents in other repositories, operator requests, and new lines of work we
discover ourselves. That last source carries a boundary: a defect found inside
the unit of an open PR is fixed in that PR and never becomes a bead; only
genuinely new scope — discovered outside any open PR, or clearly a separate
rollback unit — enters at intake. At intake, each signal is either turned into
a bead or closed at the door with a stated reason. Nothing is left sitting in
an external queue: a GitHub issue gets a reply naming its bead and closes when
that bead closes. Every biff message gets a reply — acknowledge, answer, or
report blocked; silence is not acceptable.

Security alerts map severity to priority: a critical or high alert becomes a P1
bead at the front of the order. Security work does not wait in the backlog.

Intake must not depend on remembering to look. A recurring `/loop` poll or a
session-start sweep checks the GitHub issue list and the Dependabot alert list,
so an alert filed overnight is a bead by the time the first batch is selected.

### Validation

The codebase moves; beads rot. Before a bead is workable, confirm it is still
real against current main: reproduce the bug or re-check the premise, confirm
no merged PR or design decision has superseded it (read `DESIGN.md` — the
ADR log — before touching settled architecture), confirm it is one
rollback-coherent unit, and confirm its dependency links are correct. A stale
bead is closed with the reason. A bloated bead is split into an epic. Validation
covers the candidates for the coming batch, not the whole backlog every time.

### Assessment

Ordering is automatic in the steady state. The sort is:

1. **Security severity.** Open high or critical outranks everything.
2. **Broken user journeys.** A bug on a path users actually hit — across any of
   the four surfaces (CLI, MCP, HTTP, plugin) — beats new work.
3. **Active epic continuity.** An epic in flight (e.g. DES-031 daemon-first)
   keeps its claim until done. Interleaving epics churns without shipping.
4. **Debt that blocks throughput.** God-module decomposition (`__main__.py`,
   `http_server.py`, `pipeline.py`, `hooks.py`), OO-ratchet debt, testability.
5. **Features.** v1 scope before v2.

The operator is asked for a focus ruling only when the ordering hits a genuine
fork the rules cannot resolve: two epics competing to be active, a strategic
pivot, or a drift in the debt-versus-feature balance. Direction belongs to the
operator; sequencing inside a settled direction does not require asking.

## Level 2 — The PR loop

One iteration of the PR loop produces one merged pull request. It is entered
when the backlog loop hands down a unit of work sized for throughput, and it
runs the missions (Level 3) needed to build that unit. Quarry ships to PyPI
(`punt-quarry`) and the Claude Code plugin marketplace; all repos require PRs —
no direct pushes to main, even for docs.

### Sizing: throughput, not purity

Nobody reads these diffs. Agents review them, and they squash-merge. So the
size of a PR is an economic decision, not a hygiene one:

- **The floor is transaction cost.** Every PR pays a roughly fixed overhead —
  branch, full-diff review rounds, the demo, CI, remote review cycles, the
  merge gate, the recap. A PR too small pays that overhead for too little value.
- **The ceiling is reviewer effectiveness.** The reviewers are agents, and past
  a certain diff size their quality drops. A PR too large buys throughput at the
  cost of review quality.
- **The typical right size** is several small beads batched together, or one
  coherent slice of a larger bead. A bead too large to slice into one PR is
  mis-filed: decompose it into an epic whose children fit the band.
- **Rollback coherence still binds.** Whatever merges must revert together
  sensibly. That is the one structural constraint. "Purity," "one concern per
  PR," and "keep the diff small" are **not** criteria — a docs fix, an OO/
  complexity paydown, or an adjacent bug fix riding along is welcome, and an
  improvement is never held back or split out for tidiness. The operator
  explicitly rejects rules that make it harder to improve code.

```text
function pr_loop(unit):

    # A. BUILD — missions, one at a time
    branch from main                       # <prefix>/short-description
    if unit is architectural (new contract/types/cross-cutting flow,
                              anything touching /search, the embedding
                              pipeline, TLS, or the local↔remote boundary):
        design mission first, with no prescribed write-set   # standard pipeline
        leader reviews the design end-to-end, cites file:line for each issue
        substantive issues go to the operator as concrete decisions
            (each with a "recommend X"), and implementation WAITS for the
            operator's ratification — no contract, no branch, no code until then
    for mission in unit:
        mission_loop(mission)              # Level 3
        # bug fixes are TDD: a failing test reproduces the defect first
        # coverage rises with every mission; concurrency work gets djb

    # B. FULL-DIFF VERIFICATION (local, before any PR exists)
    make check                             # lint + type + test + the THREE
                                           # merge-base ratchets (oo, coupling,
                                           # suppressions) — zero suppressions
    if local_review(full diff) is blocked: stop the unit until the
        operator rules                     # local_review: pr-review.md's,
                                           #   repo selection § Local review

    # C. DEMO — the daemon install→restart→exercise gate (see below)
    make build && uv tool install --force dist/*.whl
    restart the daemon service             # launchctl kickstart -k / systemctl
    write down the expected outcome, then drive the REAL entry point
        (quarry find/remember/ingest, an MCP tool, a fired hook)
    observe real output; confirm it matches; for user-facing behavior the
        operator confirms

    # D. SHIP
    bd close the bead(s)                   # Phase 6, before the push
    push; create the PR via the GitHub MCP tools (not gh, where MCP suffices)
    if the PR changes a file that is not demo_exempt:
        comment "demo confirmed: <head sha>"   # once step C passed on that
                                               #   head, with the operator's
                                               #   confirmation only for
                                               #   user-facing behavior
        # Qodo reviews once, on open (.pr_agent.toml); Bugbot reviews once,
        #   on its own, at open (Cursor dashboard: once per PR) — no
        #   "bugbot run" comment for round 1
    if the operator asked for Copilot: request one review, at open   # never
                                           #   by default, never on push,
                                           #   never post "/copilot review"
    schedule a background poll with /loop 3m   # never gh pr checks --watch,
                                               # never a foreground sleep loop

# poll_tick and merge_gate are NOT redefined here. They are
# pr-review.md's, pinned at punt-kit commit
# ca3365f8ead1701ec87545e7473c75d00356b81e (the links below point at that
# SHA, not blob/main) so a change to the gate requires a visible bump of
# this pin, not a silent edit in another repo. Run them exactly as written
# there — including every term (round, qodo_in, bugbot_in, copilot_in,
# rerun, pending, finding, answered, escalation, skipped, reviewed_head,
# delta_head/delta_base/delta range, bugbot_open/bugbot_stuck, demoed_head,
# demo_exempt, demo_ok, the "ledger comment"), the pending(pr) clause's
# `or not copilot_in` term (a requested Copilot review must arrive before
# the gate opens, PR #577), the Qodo-outage local fallback, the round-2
# Qodo+Bugbot
# trigger after a round-1 High fix, the reviewed_head / local delta-review
# transition that keeps a changed head from merging unreviewed, the
# separate check for queued or in-progress Bugbot runs, and the demo
# re-confirmation after a non-exempt fix push (demo_ok). The rules are
# model-checked in punt-kit standards/pr-review-model.tex and
# pr-review-model-bugbot.tex. Restating a subset here would create a
# second, weaker gate that drifts from the standard; do not. Quarry adds
# only these deltas on top of the standard's loop, nothing subtracted:
#   - CI failure: `gh run view --log-failed`; fix in this PR
#   - a fix commit re-runs local_review (§ Local review before the PR) on
#     its diff before the push; if that review is blocked, nothing is
#     pushed and the block escalates to the operator (pr-review.md)
#   - the demo gate is quarry's install→restart-daemon→exercise (§ The demo
#     gate); its re-confirmation after a non-exempt fix push is the
#     standard's demo_ok, where demo_exempt(f) is: *.md, docs/, tests/,
#     .github/, or a ratchet file (.oo-*, .suppression-*), and NOT under
#     plugin/ — commands, skills, agents, and hooks there are behavior even
#     when Markdown, because Claude Code executes them
#   - merge is squash with expectedHeadSha; mergeStateStatus must be in
#     {CLEAN, HAS_HOOKS}
# When the standard's merge_gate holds: merge (squash); close_out(). Do not
# ask, do not wait.

function close_out():
    cancel the poll loop
    delete the branch (local + remote); checkout main; pull
    send the merge recap via beadle-email  # to jim@punt-labs.com, every merge,
                                           # unprompted — the 8-part recap plus
                                           # the review-metrics recap block from
                                           # ../.bin/review-metrics.py pr
                                           # punt-labs/quarry <N> --format recap
    start the next unit immediately        # no stopping to report
```

The local review sequence `local_review` runs — reviewer selection, round
limits, the dismissal standard, and when a review counts as blocked — is
[punt-kit/standards/pr-review.md](https://github.com/punt-labs/punt-kit/blob/ca3365f8ead1701ec87545e7473c75d00356b81e/standards/pr-review.md)'s;
this repo's selection is in [Local review before the PR](#local-review-before-the-pr) below.

Entry and exit for one PR iteration. The pre-merge gate is pr-review.md's
`merge_gate`, not a subset restated here; `ExitPR` describes the state after
close-out completes and carries every conjunct the standard's gate requires.

```text
EnterPR ≙ [ LoopState; unit : ℙ BEAD |
  unit ⊆ claimed
  ∧ rollbackCoherent(unit)                -- reverts together sensibly
  ∧ inThroughputBand(unit)                -- above transaction cost,
                                          --   below reviewer degradation
  ∧ (architectural(unit) ⇒ designRatified(unit)) ]  -- operator has ruled on
                                          --   the design before code started

ExitPR ≙ [ Δ LoopState; pr : PR |
  merged(pr)
  ∧ localReviewClean(pr)                  -- a local round that left nothing
                                          --   to fix, BEFORE the PR opened and
                                          --   before every fix push
  ∧ demoConfirmed(pr)                     -- install→restart→exercise, BEFORE
                                          --   the PR opened
  ∧ ciGreen(head(pr))
  ∧ ¬ pending(pr)                         -- every requested review is in
  ∧ ¬ bugbotOpen(pr)                      -- no Bugbot run on any commit of
                                          --   the PR was open at merge
  ∧ bugbotRerunsPosted(pr)                -- each round with a failed run
                                          --   had its rerun(N)
  ∧ reviewedHead(pr) = head(pr)           -- merged code was reviewed, bot-
                                          --   authored PRs included
  ∧ demoOk(pr)                            -- demo_ok at the merge
  ∧ (∀ f : findings(pr) • answered(f))    -- fixed on head, or reasoned
  ∧ round(pr) ≤ 2                         -- round 2 only after a High fix
  ∧ unresolvedThreads(pr) = ∅
  ∧ openEscalations(pr) = ∅               -- each closed by a recorded ruling
  ∧ beadsOf(pr) ⊆ closed′
  ∧ mergeRecapSent(pr) ]
```

### Local review before the PR

Full-diff local review is where issues die cheaply. A local round costs
seconds; a remote review cycle costs minutes to hours. The `local_review`
sequence — the round structure, the dismissal standard, and when a review
counts as blocked — is
[punt-kit/standards/pr-review.md](https://github.com/punt-labs/punt-kit/blob/ca3365f8ead1701ec87545e7473c75d00356b81e/standards/pr-review.md)'s.
For quarry: `code-reviewer`, `silent-failure-hunter`, and `qodo review` run on
the unit's first full diff and on every fix diff of 20 lines or more; a fix
diff under 20 lines gets `qodo review` alone. `djb` joins any diff that touches
credentials, secrets, or caller text that reaches a file, a log line, or a
header — in quarry that is TLS, the URL and path guards, and write-time
scrubbing — whatever the diff's size. A cross-cutting or concurrency/TLS/boundary
change also gets the ethos evaluator (djb for security/concurrency, gvr for
design) on the substance. Local rounds repeat until a round leaves nothing to
fix: at most four fix rounds plus one review that verifies the last fixes.
Every fix diff goes through the same loop before it is pushed, including fixes
made for PR findings.

From the second round on, the review is scoped to what the diff changed:
regressions it introduces and Highs inside it. Without that boundary a
failure-focused reviewer walks outward into code the diff never touched and
the rounds do not converge. The three reviewers are not substitutes — in the
vox pilot each found defects the other two missed. `qodo review` is the
engine that later reviews the PR, so what it finds locally does not come back
as a PR round.

Every finding is fixed in this PR. A dismissal requires the exact finding, the
specific reason it does not apply, and the code reference. Opening a PR with
unresolved local findings is a procedural violation. Nothing is deferred to a
bead.

### Remote review: Qodo and Bugbot, at most two rounds

Remote review is automated-only and runs on already-clean code, so findings
should be few. The full specification is
[`punt-kit/standards/pr-review.md`](https://github.com/punt-labs/punt-kit/blob/ca3365f8ead1701ec87545e7473c75d00356b81e/standards/pr-review.md);
this section names what quarry does with it.

Qodo is the primary remote reviewer (`.pr_agent.toml`). It reviews when the PR
opens and never on push. High findings post inline as threads, and the
`required_review_thread_resolution` ruleset rule makes each one block the
merge. Medium findings appear in the summary. Lows are dropped by
`comments_routing`. Bugbot reviews each PR once, on its own, when the PR opens
(Cursor dashboard setting); a push draws no Bugbot review. Its findings are
handled whenever they arrive.

Round 1 is the review at open. Round 2 — one `/agentic_review` plus one
`bugbot run` — happens only when round 1's fixes included a High, and it is
the last. A new High in round 2 goes to the operator.

Copilot is not a default reviewer. Repo rulesets carry no
`copilot_code_review` rule, so Copilot reviews only when requested: at most
one request per PR, at open, never on push. Its findings are findings like any
other.

This holds only when four settings are in place: `.pr_agent.toml` is on
`main` (Qodo reads it from the default branch); "Review every push" is
Disabled in the Qodo portal; Bugbot is set to review each PR once, at open;
and the ruleset has no `copilot_code_review` rule. If a push still starts a
Bugbot run, its findings are findings like any other.

### The demo gate — install, restart, exercise, observe

`make check` passing means the code compiles and the tests pass. It does **not**
mean the feature works, and a green `quarry doctor` does **not** mean the new
code is running. Before any PR opens:

1. **Build + install** the wheel: `make build`, then
   `uv tool install --force dist/*.whl` (`--force` reinstalls the same version
   number with new code).
2. **Restart the daemon service.** A running `quarryd` holds the OLD engine in
   memory until restarted; installing the wheel does nothing to the live
   process. There is no `quarryd restart` subcommand.
   - macOS (launchd, label `com.punt-labs.quarry`):
     `launchctl kickstart -k gui/$(id -u)/com.punt-labs.quarry`
   - Linux (systemd `--user`, unit `quarry.service`):
     `systemctl --user restart quarry`
   - Confirm the PID changed and its age is seconds, not days.
3. **Exercise the real entry point and observe real output**, expected written
   down first. Read path: `quarry find "<today's own work>"` returns the merged
   artifacts. Write path (through the ingest queue): `quarry remember` a unique
   token → `quarry find` it back → `quarry delete` → re-find shows zero. Hook
   path: a real `WebFetch <url>` fires the PostToolUse hook → daemon
   capture-ingest (scrub → queue → LanceDB) → `quarry find` surfaces the
   capture. Cover one invalid input, one missing-dependency case, one boundary.
4. For user-facing behavior, the **operator confirms** the observed outcome.

A synthetic in-process test is not a substitute for driving the installed
artifact against the restarted daemon. Docs-only changes (CLAUDE.md, DESIGN.md,
README, ADRs) have no entry point to run; a markdownlint pass and read-through
is their verification. This is the one human gate, and it is a demo, not a diff.

## Level 3 — The mission loop (design, code, test)

One iteration is one delegated mission: a single piece of design,
implementation, test, or review work executed by a specialist sub-agent under
an ethos mission contract. The next mission does not start until this loop
completes on the current one. All code changes are delegated — the leader does
not write production code; the only files the leader authors directly are
`CHANGELOG.md`, `CLAUDE.md`, `DESIGN.md`, `README.md`, `docs/WORKFLOW.md`,
memory files, and plan files, and even those go through a PR.

The mission loop is a do-while: the work runs at least once, and the
review-and-fix cycle repeats until a round comes back clean.

```text
function mission_loop(mission):
    contract = write_contract(mission)   # problem, invariants, quality bar,
                                         # commit discipline — NEVER a write-set
                                         # for design work; the design's output
                                         # IS the write-set
    dispatch(contract)                   # ethos mission create + a SEPARATE
                                         # Agent(subagent_type=<worker>,
                                         # run_in_background=true) spawn;
                                         # verify the worker is actually running
    do:
        worker designs / codes / tests   # tests lead; TDD for bug fixes
        worker commits locally           # each commit passes make check
        result   = worker submits
        findings = evaluator review      # a DIFFERENT specialist (pairings)
                 + leader verification   # run the change against the live
                                         # daemon; review agents on the diff;
                                         # the five bug classes as a checklist
        if findings: reflect(findings)   # another round, same mission
    while findings remain
    close(mission)
    quarry_missions_sync(mission)    # Loop 2 (DES-055): file this mission's
                                     # per-round evaluator feedback to memory-<worker>
```

Entry and exit for one mission iteration:

```text
EnterMission ≙ [ LoopState; m : MISSION |
  contracted(m)                           -- problem, invariants, quality bar,
                                          --   commit-per-step discipline
  ∧ workerRunning(m)                      -- dispatch is TWO operations; a
                                          --   contract alone is orphaned work
  ∧ soleWriter(m)                         -- one writer per worktree; a second
                                          --   concurrent writer gets its own
                                          --   git worktree (isolation)
  ∧ (concurrencyClass(m) ⇒ adversariallyVerified(m)) ]
                                          -- concurrency / stateful-protocol
                                          --   work gets a dedicated djb
                                          --   adversarial pass (and a z-spec/
                                          --   ProB model-check where a model
                                          --   covers it) BEFORE it is accepted

ExitMission ≙ [ Δ LoopState; m : MISSION |
  verdict(m) = accept                     -- from an evaluator ≠ worker
  ∧ findings(m) = ∅                       -- the do-while ran dry
  ∧ (∀ c : commitsOf(m) • checkGreen(c))  -- every commit passed make check
                                          --   AND the three merge-base ratchets
  ∧ testCount′ ≥ testCount                -- coverage never decreases
  ∧ (touchesScoredSource(m) ⇒            -- a real OO improvement on any scored
       ooImproved(touchedFiles(m)))       --   source files touched, sized to the
                                          --   opportunity — the ratchet is debt
                                          --   amortization; a docs- or test-only
                                          --   mission has nothing to score
  ∧ missionClosed(m) ]
```

### Who does what

The leader runs the workflow; the specialists produce the work. The boundary is
strict in both directions.

**The leader owns the workflow.** The backlog, the mission contract, dispatch,
monitoring, the local review agents (the leader's tools, run on each mission's
diff and on the full diff), the demo, and every git and GitHub operation:
branches, pushes, the PR, driving remote review, resolving threads, merging,
and close-out. The leader does not write production code.

**The worker owns the work.** The thinking and the code inside its mission — the
design decisions its contract leaves open, the tests, the implementation, and
local commits on the current branch. A worker never creates branches, never
pushes, never opens PRs, and never touches review threads. Workers commit and
push their OWN work on their own timeline; the leader does not commit by proxy
while a worker is actively editing. Putting workflow operations into a worker's
prompt is a contract defect. The default isolation is one worker per worktree;
when two workers must share one worktree, sequence them so no one edits the same
uncommitted lines simultaneously — the only real constraint here is not losing
work, not scope.

**The evaluator** — always a different specialist from the worker — reviews the
worker's result inside the mission before the leader accepts it. The
worker/evaluator pairings by task type:

| Task type | Worker | Evaluator |
|---|---|---|
| Embedding pipeline / ONNX provider | `kpz` | `rmh` |
| Quantization, GPU/CPU dispatch, model loading | `kpz` | `gvr` |
| Search algorithm (hybrid, RRF, decay, BM25) | `kpz` | `rmh` |
| LanceDB schema / chunks table / migrations | `rmh` | `gvr` |
| Python implementation (library, CLI logic) | `rmh` | `gvr` |
| MCP server (stdio + WebSocket) | `rmh` | `mdm` |
| HTTP API / `/search` / param contracts | `rmh` | `djb` |
| TLS / cert generation / pinned-CA contexts | `djb` | `rmh` |
| Concurrency (queue, serialization, async) | `rmh` | `djb` |
| Install scripts / launchd / systemd service | `adb` | `djb` |
| Agent memory: identity, summary, decay | `rmh` | `kpz` |
| Document loaders / format ingestion | `gvr` | `rmh` |
| CLI surface (`find`, `ingest`, `remember`) | `mdm` | `rmh` |
| Performance / latency / index benchmarks | `kpz` | `adb` |

Within each row the worker and evaluator are distinct handles; Claude is the
leader, never the evaluator.

**Remote review findings are the one delegation that is not a mission.** When
Qodo or Bugbot report on the PR, the leader reads each finding and
hands the mechanical fix to a bare `Agent()`, then pushes and resolves the
thread itself.

### The lifecycle

1. **Contract.** The leader writes the mission contract: the problem, the
   invariants, the quality bar, and the commit discipline — one commit per
   logical step, each passing `make check`, never more than thirty minutes of
   work uncommitted. A design mission's contract never prescribes a write-set;
   the specialist decides what to create, split, or extract (prescribing a
   write-set before design is how `__main__.py` reached 2,000 lines). Every
   implementation contract directs the worker to make a real OO improvement on
   the files it touches, sized to the opportunity — the ratchet is debt
   amortization, not a limbo bar. For protocol or data work the contract cites
   the OO rules with an example, because sub-agents revert to procedural habits
   when the prompt is not explicit.
2. **Dispatch is two operations.** `ethos mission create` writes the contract;
   a separate `Agent(subagent_type=<worker>, run_in_background=true)` spawn
   starts the worker. Verify the worker is actually running — a contract with no
   agent behind it is orphaned work. Every implementation sub-agent runs in the
   background.
3. **The worker executes.** Tests lead: a bug fix starts from a failing test
   that reproduces the defect, and the fix is done when it passes; a feature
   ships its tests with the code; coverage rises and the test count never goes
   down. Concurrency or stateful-protocol work gets its djb adversarial pass
   (and a z-spec/ProB model-check where a Z model covers it) before the result
   is accepted. Every commit passes `make check` — zero exceptions, zero
   suppressions (no `# noqa`, `# type: ignore`, `--no-verify`, `xfail`; the
   only pre-authorized suppressions are those a punt-kit standard or lang-rule
   enumerates, each citing its rule).
4. **The leader monitors by the filesystem, never by git activity.** A worker
   editing files is working, even with zero commits — analysis and reading are
   invisible work. Progress is judged by whether the working tree is changing
   and advancing (`git status`, `git diff`, reading the files). An empty commit
   log is never a reason to intervene, commit by proxy, ping "where's your
   commit?", or `TaskStop`. A genuine stall — no file changes over a long window
   and no response to a status message — is the only cause for taking over.
   Never call a resulting flaky test "flaky": name the race condition or bug.
5. **Result and evaluation.** The worker submits; the evaluator (a different
   specialist) reviews; reflect-and-advance rounds continue until the evaluator
   accepts. The evaluator applies the five recurring bug classes as a checklist
   (see below).
6. **The leader verifies and closes.** Check the result against the contract:
   build + install the wheel, restart the daemon, and exercise the change
   through its real entry point with expected output written first — one invalid
   input, one missing dependency, one boundary. Run the applicable local review
   agents on the mission diff and fix every finding. If the result raises a
   design question, it goes to the operator as a concrete decision — with a
   recommendation — before any dependent mission dispatches. Then close the
   mission.

### The five recurring bug classes (evaluator checklist)

Ten review cycles on the TLS remote-access feature revealed five bug classes
that recur. Every mission evaluator and every code review checks for these; a
change in one of these areas is not accepted until its class is tested.

1. **File I/O safety.** `os.write()`/`os.fdopen()` may not write all bytes or
   may raise before owning the fd; atomic rename must be inside the `try`;
   create files with the correct mode, not chmod-after. *Test:* success, fd
   closed on `os.fdopen` raising, temp file removed on any write failure,
   correct mode from creation.
2. **Exception boundaries.** A function promising `(bool, str)` or a clean
   fallback must not let a dependency raise before the `try`. *Test:* make the
   underlying call raise and assert `(False, <non-empty>)`, not propagation;
   every CLI command reading optional config falls back (exit 0, warning) on
   malformed config.
3. **Remote/local divergence.** The HTTP API must be a faithful proxy of every
   local operation — same params, same response fields, same behavior. *Test:*
   an equivalence test asserting identical JSON field names local vs remote, and
   an HTTP server test asserting every CLI query param reaches the DB query. A
   new filter or field on one path fails a test until it exists on both.
4. **TLS semantics.** IP SANs use `x509.IPAddress`, not `DNSName`;
   `not_valid_before` backdates ≥ 5 min; a pinned-CA context excludes system
   roots; CA cert and key are verified to match. *Test:* assert SAN type,
   past-dated validity, pinned context has no system roots, mismatched cert/key
   raises before any write.
5. **Install-script logic.** Shellcheck catches syntax, not logic (API key
   checked after a slow download, service on loopback while the script runs on
   0.0.0.0, `quarry.toml` never created). *Test:* `shellcheck -x` in CI, plus
   logic tests with a mock `quarry` binary asserting ordering, host binding,
   non-zero exit on daemon failure, and the login call after start.

## Invariants

1. **Beads are the single funnel.** Every piece of work is a bead before it is
   anything else; external queues (issues, alerts) drain into it at intake.
   `TaskCreate` is session-visible display only, never the durable record.
2. **A satisfied merge gate means merge now.** The gate is the only path to a
   merge, and nothing waits once it passes — no asking, no one-more-empty-round.
   Merge is always explicit — never auto-merge, never an admin override.
3. **Do or do not.** A finding is fixed in this PR or dismissed with a
   reason, the moment it arrives. It is never deferred to a bead or a
   follow-up PR. A thread is resolved only once it is answered per
   `answered(f)`, one by one, never in bulk. Batch fixes across a round; one
   push per round.
4. **A push restarts CI, not review.** Local rounds run until clean (at most
   four fix rounds plus a verifying review) before the PR opens and before
   every fix push. PR rounds are at most two, and the second only after a
   High fix. Never merge until `reviewed_head` is the PR head.
5. **Defects flow inward, scope flows outward.** A reviewer-flagged defect found
   while a PR is open is fixed in that PR — never laundered into a "follow-up
   bead" and merged around. "Pre-existing", "by design", "intentional", and
   "expected" are not reasons, and a bead is not a disposition for a real
   finding. Only a genuinely new, separate line of work becomes a bead. This is
   the most-corrected failure in this repo; do not run the bead factory.
6. **The operator gates demos and direction, never diffs or sequencing.**
   Between a merged design and implementation, the leader escalates substantive
   design issues to the operator as concrete decisions and waits for the ruling.
7. **Verification is install → restart → exercise → observe.** `make check`
   green and a green `quarry doctor` are necessary, never sufficient; the feature
   is not verified until the installed artifact runs against the restarted
   daemon and produces the expected real output.
8. **Every surface or none.** A feature works on all four surfaces (CLI, MCP,
   HTTP, plugin) or documents which it applies to; a new query param or response
   field exists on the local and remote paths simultaneously.
9. **Close-out is inside the loop.** The recap email, branch hygiene, and
   starting the next unit are steps of the loop, not afterthoughts.
