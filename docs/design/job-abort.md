# Job abort lifecycle: the model the implementation follows

**Status:** RATIFIED by the leader (D1-D3 of §9), amended after security review (A1-A6, L1-L5); model-checked
**Bead:** quarry-70zm
**Companion model:** `docs/design/job-abort.spec.tex` (Z, fuzz-checked, ProB-checked); the tool runs are recorded in §4
**Generator:** `docs/design/job-abort.spec.gen.py.txt` (§10)
**Starting point:** commit eaa5118; every `file:line` below is at the working tree after 743f87c

## Abstract

Shutdown abort accounting in the ingest queue produced a new HIGH finding in each
of three consecutive review rounds. Each fix moved the defect to a neighbour,
because the status of a job is written by five actors, none of which knows what
the others have written, and the tally that counts the jobs is a second record
that has to agree with the first. This document stops the empirical rounds. It
gives the job lifecycle a Z model, states eleven invariants as witnesses, and
shows with ProB that the lifecycle this document requires reaches none of them
(13030 states, every state visited, no deadlock), while the code as it stands
reaches seven of the eleven, and a seeded fault reaches each of the others. The
implementation follows the model: §5 maps schema to code, §6 lists every place
the code breaks it, §7 is the write-set, §8 the tests, each driven through the
real cancel path.

The model's central finding is not a fix to the abort. It is a missing actor.
When the worker is cancelled, its store thread keeps running and no one is left
to read its result. The old code's docstring says a job that stores after the
abort "is `completed` again" (`job_abort.py:46-47`). Nothing makes it so.

## 1. Motivation

The rounds whose briefs I read (`.tmp/missions/70zm-pr1-c2n-backfill.md`,
`-c2o-`, `-c2p-`):

| Round | Finding | Where the defect went next |
|---|---|---|
| C2n | Every aborted job must reach a terminal state and be counted once; the in-flight wait needs a deadline (A1-A4) | the late spool: its reason and its term (J2) |
| C2o | A late spool corrects the reason and the tally together; `_begin` and `_record` take the claim; never overwrite `withdrawn` or `committing` (J1-J5) | `fail_aborted` and the terminals |
| C2p | A committing job is overwritten through `TaskTerminal` (SFH-H1); the `fail_aborted` result is ignored, so kept jobs are still spooled and counted, and `aborted` is missing from the kept set (SFH-H2) | stopped here, for this model |

The workflow rule is: when the same class of defect crosses two or more fix and
review rounds, formalize the state machine and model-check it. The class here is
*two writers, no agreed owner*. The model gives every status a single owner and
every move a single critical section.

## 2. The model in prose

Two jobs on one collection worker; the worker holds at most one at a time. A job
has three fixed attributes: whether it reports a commit checkpoint (`ck`: a
backfill transcript), and whether the abort must spool it (`skspool`: a
`remember` or URL ingest) or can leave it to be run again (`skrerun`). A
checkpointed job is always rerunnable.

### 2.1 State (spec §State)

| Variable | Values | Meaning |
|---|---|---|
| `st` | queued, running, committing, completed, failed, withdrawn, aborted | the job's `TaskState.status` (all seven the brief names) |
| `rs` | none, term, prov, spooled, nospool, notw, unb | the reason in `TaskState.error` |
| `loc` | inq, gate, body, qtaken, out | where the worker has it; `out` while `ts` is running is *detached* |
| `ts` | t0, tpre, tcommit, tstored, tstop | the store thread; `tstored` means the rows are in the table |
| `ab` | an, atake, abegun, aspool, arec | the abort's progress on the job |
| `sp`, `fl`, `fc` | spool thread; in flight when taken; spool future cancelled | the spool write (`sp = sok` means the file is on disk) |
| `tl`, `cnt` | the job's term, and the counters the code keeps | the tally |
| `ph`, `wit` | phase; latched witnesses | |

The tally's terms are the brief's five (spooled, not written, unbuilt,
unfinished, pending) and three the model needs: *needs no spool* (a rerunnable
job; the code has it as a residual), *kept* (the abort left the job as it is:
withdrawn, aborted by its producer, or stored), and *unconfirmed* (a committing
job whose store has not ended). Without *kept* a withdrawn job has nowhere to be
counted that is not a lie, and without *unconfirmed* neither has a committing
one. The old code hides both in the residual `unspooled` (`abort_reason.py:147-153`).

### 2.2 The actors and their moves

- **Producer** (`WithdrawQueued`, `WithdrawRunning`): a queued job becomes
  `withdrawn`, a running one `aborted`; a committing, completed or failed one is
  unchanged. Another thread; under the claim.
- **Worker** (`Dequeue`, `Start`, `Skip`, `Finish*`): starts a queued job, lets a
  withdrawn one go, and when the store thread ends records the body's result
  through the terminal. In the fixed build `FinishKept` leaves `aborted` alone.
- **Store thread** (`Commit`, `CommitRefused`, `StoreEnds`, `StoreFails`): runs on
  its own and no cancel stops it. A checkpointed job reaches `Commit`, which
  succeeds only while the status is `running`; otherwise the thread stops before
  it deletes or inserts.
- **Observer** (`StoreEndsObserved*`, `StoreFailsObserved*`, `StoreEndsKept`,
  `StoreFailsDetachedOther`): the actor the old code lacks. When the store thread
  of a *detached* job ends, the observer, in one critical section, settles the
  status from what the thread did, clears the failure reason, moves the job's
  term, and removes the job's spool file if one was written.
- **Abort** (`AbortStart`, `CancelAtGate`, `CancelInBody*`, `Begin*`, `Decide*`,
  `SpoolEnds`, `Record`, `Deadline`, `Late`, `SpoolCancel`, `QueueCancel`):
  cancels the worker and takes the queued jobs in one synchronous step; the
  in-flight job arrives through the worker's cancel handler. `Begin` fails each
  taken job provisionally and counts it pending, *unless the job is kept or
  committing*, in which case it is counted *kept* or *unconfirmed* and goes no
  further. `Decide` starts a spool write for a spooled job. The outcome arrives
  within the wait (`Record`), or, for an in-flight job only, the wait expires
  first (`Deadline`) and the job is *unfinished* until the write ends (`Late`). A
  cancel of the queued path's gather (`QueueCancel`) also records *unfinished*
  and leaves the late callback armed.

### 2.3 Two builds, seven seeded faults, two switches

`fixed = 0` is eaa5118. `fixed = 1` is the lifecycle this document requires.
`abl = 1..7` seeds one fault into the fixed build: the observer moves a term and
not the counter (1); the late correction moves a term and not the reason (2); the
observer writes without the claim (3); the kept set lacks `aborted` (4); the
observer leaves the spool file (5); a failed committing store leaves the term
*unconfirmed* (6); the observer leaves the failure reason on a completed job (7).
The same eleven witnesses are checked against every build. The old build's
operations are guarded by `fixed = 0` and live in the same spec, so a reader sees
what changes, one operation at a time. Faults 6 and 7 are the two mutants the
security review found; the fixed build is checked against them in §4.

### 2.4 Assumption: the store has its own executor (A1)

The model treats the store thread and the spool thread as independent: neither
waits for the other. The code must make that true. The spool writes run on the
shared `DaemonThreads` pool (`daemon_threads.py:64-70`), which is bounded at four
threads. If stores ran there too, four busy collections would hold every thread
and no spool write could start, so the abort's wait would expire on every job.
`StoreRun` therefore owns a separate executor (§7.3) and never takes the shared
pool. Test T11 pins it.

## 3. The invariants

| # | Witness | Statement | Old build | Seeded fault |
|---|---|---|---|---|
| I1 | `wTerm` | A taken job whose outcome is recorded and whose store thread has ended is `completed` (and its rows are stored), `failed` (and none are), `withdrawn` or `aborted`: never queued, running, committing, and never a status that contradicts the table. | reached | |
| I2 | `wCount` | Every taken job is in exactly one term and the counters equal the terms; so the terms sum to the jobs taken. | not reached | `abl=1` |
| I3 | `wKept` | A withdrawn, aborted or stored job is counted *kept*, never spooled, lost, unfinished or needing no spool. | reached | |
| I4 | `wLost` | A job whose rows are stored is never `failed`. | reached | |
| I5 | `wLate` | A failed job's reason and its term agree; a late correction moves both, once. | not reached | `abl=2` |
| I6 | `wClaim` | Every status write holds `state.claim`. | reached | `abl=3` |
| I7 | `wOver` | A `withdrawn`, `aborted` or `completed` status is never overwritten, and a committing job is never failed while its store thread runs. | reached | `abl=4` |
| I8 | `wDisk` | A failed job's reason matches what reached disk: *spooled* only if the file is there; *not written* or *unbuilt* only if the write cannot land; *needs no spool* only if there is no file. | reached | |
| I9 | `wMatch` | A term matches its status: *unconfirmed* means `committing`; *pending*, *unfinished*, *spooled*, *not written*, *unbuilt* and *needs no spool* mean `failed`; *kept* means `withdrawn`, `aborted` or `completed`. | reached | `abl=6` |
| I10 | `wCompl` | A `completed` job carries no failure reason. | not reached | `abl=7` |
| I11 | `wGhost` | A `completed` job has no spool file on disk. | not reached | `abl=5` |

I7 is the one the brief did not list; the code needed it (SFH-H1 and the missing
`aborted` are both I7). I8 is the brief's "a reason that matches what reached
disk", made a witness of its own. I9 to I11 are the security review's: a stale
*unconfirmed* term on a failed job, a completed job still carrying its failure
reason, and a spool file surviving a store, each of which the first eight let
through.

The brief's example of a product decision, whether a job whose store committed
after the cancel reports `completed`, is settled by I1 and I4 as the brief states
them, and the leader has ratified it (§9 D1).

## 4. Tool runs

All runs: `probcli` 1.15.1 from `~/Applications/ProB`, run on a copy of the spec
under `.tmp/` so no tool artifact lands in `docs/design/`. Every run uses
`-p MAX_INITIALISATIONS 100 -p TIME_OUT 900000 -p MAX_OPERATIONS 100000`; the
old-build and seeded-fault runs add `-nodead`, because an unfixed build may stop
in a state the fixed build never reaches. The constants `ck`, `sk` are free: nine
setups.

**Type check.** `z-spec check docs/design/job-abort.spec.tex`: `fuzz: job-abort.spec.tex OK`.

**Fixed build** (`fixed = 1`, `abl = 0`), `-goal 'card(wit)>0'`, with ProB's
default deadlock check on:

- 13030 states analysed, 30534 transitions, all states visited,
  `No counter example found`: no witness and **no deadlock**. The settled state
  is the explicit `Quiesce` self-loop, so any other deadlock would be a finding.
- Of 51 operations, 15 never fire, as they must not: eight are old-build
  operations (`StoreEndsOld`, `StoreFailsOld`, `FinishStoredOld`, `FinishStopOld`,
  `CancelInBodyOld`, `BeginOld`, `SpoolCancelOld`, `QueueCancelOld`, all guarded
  by `fixed = 0`) and seven are seeded faults (`StoreEndsObservedNoCount`,
  `StoreEndsObservedUnclaimed`, `StoreEndsObservedGhost`,
  `StoreEndsObservedUnbegunKeepsReason`, `StoreFailsObservedCommittingStale`,
  `BeginAbortedOverwritten`, `LateNoReason`). Every operation of the fixed build
  fires.
- **All eleven witnesses are unreachable.**
- `AG({ph = pre} or EF({settled}))`, `settled` = every job is either out of the
  abort's hands or recorded, and no store thread runs: **TRUE**. From any state
  after the abort starts, a settled state is reachable.

**Old build** (`fixed = 0`): the full search visits 24507 states, 59556
transitions. Per witness, the first counter-example ProB found:

| Witness | Trace length | States analysed | Reached |
|---|---|---|---|
| `wTerm` | 11 | 1008 | yes |
| `wKept` | 6 | 24 | yes |
| `wLost` | 14 | 663 | yes |
| `wClaim` | 9 | 149 | yes |
| `wOver` | 10 | 239 | yes |
| `wDisk` | 13 | 122 | yes |
| `wMatch` | 5 | 16 | yes |
| `wCount` | | 24507 (all) | **no** |
| `wLate` | | 24507 (all) | **no** |
| `wCompl` | | 24507 (all) | **no** |
| `wGhost` | | 24507 (all) | **no** |

The old build never breaks I2, I5, I10 or I11 on its own: its counters and its
late correction are consistent, and it has no late completion to leave a reason or
a file behind. What breaks is what they are counting. The liveness formula holds
in the fixed build and fails in the old build (below), where a cancelled
queued spool is never recorded.

### 4.1 Seeded faults

| `abl` | Fault | Witness | Trace length | States analysed |
|---|---|---|---|---|
| 1 | observer moves the term, not the counter | `wCount` | 11 | 459 |
| 2 | late correction moves the term, not the reason | `wLate` | 11 | 254 |
| 3 | observer writes without the claim | `wClaim` | 11 | 292 |
| 4 | kept set lacks `aborted` | `wOver` | 10 | 362 |
| 5 | observer leaves the spool file | `wGhost` | 15 | 1176 |
| 6 | failed committing store leaves the term *unconfirmed* | `wMatch` | 11 | 230 |
| 7 | observer leaves the failure reason on a completed job | `wCompl` | 9 | 1117 |

Each fault is reached, so each witness can fire; the fixed build reaches none.

### 4.2 The security review's mutants, replayed

The two mutants in `.tmp/scratch-djb/` are single-line edits of the previous
spec. I applied the same two edits to this spec (the term and counter left
unchanged in `StoreFailsObservedCommittingCore`; the reason left in
`StoreEndsObservedUnbegunCore`) and ran the goal of the witness added for each.

| Mutant | Edit | Witness | Trace length | States analysed |
|---|---|---|---|---|
| m1 | stale *unconfirmed* on a failed job | `wMatch` | 11 | 254 |
| m2 | a completed job keeps its reason | `wCompl` | 9 | 323 |

Both are caught. The previous spec's eight witnesses did not fire on either.

`z-spec test` (its default suite, `--max-ops 100000 --timeout 900000`) passes
`init`, `animate` and `cbc_assertions`, and reports `model_check` as a warning
(3002 states, "not certified complete": the suite does not pass
`MAX_INITIALISATIONS`, so it visits one of the nine constant setups). It fails
`cbc_deadlock`: that check is constraint-based, searches *any* type-correct state
and not a reachable one, and the spec states no state invariant, since a violated
invariant would make an operation unsatisfiable (spec, "Witnesses, not
invariants"). It fails `coverage` for the same bounded-run reason. The `Quiesce`
self-loop does its job where it can: the reachable search above has no deadlock.
The old build stops where a cancelled queued spool is pending for good, and the
same liveness formula is **FALSE** there; the counter-example ProB gives is
`WithdrawQueued(ja)`, `WithdrawQueued(jb)`, `Dequeue(jb)`, `Skip(jb)`,
`AbortStart`, `BeginOld(ja)`, `DecideSpool(ja)`, `QueueCancelOld(ja)`.

## 5. Schema to code

Each row names the code that must implement the schema. "New" is code the model
requires that does not exist.

| Schema | Code |
|---|---|
| `WithdrawQueued`, `WithdrawRunning` | `JobLifecycle.withdraw` (`job_lifecycle.py:91-106`); callers `queued_ingest.py:113`, `queued_job.py:110` |
| `Dequeue`, `Start`, `Skip` | `CollectionWorker._run` (`collection_worker.py:179-211`); `JobLifecycle.run`, `start` (`job_lifecycle.py:74-81`, `123-137`) |
| `Commit`, `CommitRefused` | `JobLifecycle.commit` (`job_lifecycle.py:83-89`); `QueuedJob.checkpoint` (`queued_job.py:83-87`) |
| `StoreEnds`, `StoreFails` | the threadpool call in each job's `run`: `content_jobs.py:82`, `:183`; `ingest_jobs.py:70`; `index_jobs.py:62`, `:116`, `:162`; `finalize_job.py:63`, `:105` |
| `StoreEndsObserved*`, `StoreFailsObserved*`, `StoreEndsKept`, `StoreFailsDetachedOther`, `SpoolEndsRemoved` | **new**: `StoreRun` (the store's future, kept by the job) and `JobLifecycle.observe_store` |
| `FinishStored`, `FinishStop`, `FinishKept` | `RefusalAwareTerminal.complete`, `_fail` (`refusal_aware_terminal.py:68-97`); `TaskTerminal.recording` (`task_terminal.py:28-58`); the direct writes in `index_jobs.py:64-68`, `:117-118`, `:163-164` and `finalize_job.py:64`, `:106-107` |
| `AbortStart` | `CollectionWorker.abort`, `abort_off_loop`, `_take_queued` (`collection_worker.py:119-162`); `QueueAbort.run` (`queue_abort.py:39-58`) |
| `CancelAtGate`, `CancelInBody*` | `_run`'s `except CancelledError` (`collection_worker.py:197-206`); the cancel handler of `TaskTerminal.recording` (`task_terminal.py:41-44`) |
| `BeginKept`, `BeginUnconfirmed`, `BeginFail` | `JobAbort._begin` (`job_abort.py:213-217`); `JobLifecycle.fail_aborted` (`job_lifecycle.py:108-121`) |
| `DecideRerun`, `DecideSpool` | `JobAbort._outcome` (`job_abort.py:195-211`); `AbortReason.of` (`abort_reason.py:64-89`) |
| `SpoolEnds` | `JobSpool.write` (`job_spool.py:103-134`) on `DaemonThreads.run` (`daemon_threads.py:72-90`) |
| `Record` | `JobAbort._record` (`job_abort.py:219-223`), `_one_off_loop` (`:181-193`) |
| `Deadline` | `JobAbort.fail_in_flight`, `await_spool` (`job_abort.py:107-150`) |
| `Late` | `JobAbort._ended_late` (`job_abort.py:152-179`) |
| `SpoolCancel` | `await_spool`'s `spooling.cancelled()` branch (`job_abort.py:140-141`) |
| `QueueCancel` | `JobAbort._one_off_loop` (`job_abort.py:181-193`), `fail_off_loop` (`:102-105`) |
| `tl`, `cnt`, terms | `AbortTally` (`abort_reason.py:92-164`); `QueueAbort._summary` (`queue_abort.py:60-69`) |
| `Latch`, `Quiesce` | no code: the invariant tests of §8 |

## 6. Where the code violates the model

Each row is a path the old-build search reaches, or an absence the model shows
cannot be tolerated. The witness is the invariant broken.

| # | Violation | `file:line` | Witness |
|---|---|---|---|
| V1 | The cancel handler of `TaskTerminal.recording` writes `failed` and `error` with no claim and over any status: a committing job whose store is running (SFH-H1), a producer's `aborted`, and a job whose store has in fact already landed. | `task_terminal.py:41-44` | I6, I7, I4 |
| V2 | The `except Exception` branch and the `finally` write `failed` the same way; `finally` takes `aborted` and `committing` for "did not finish". | `task_terminal.py:45-58` | I6, I7 |
| V3 | `RefusalAwareTerminal.complete` and `_fail` write `completed` and `failed` unclaimed and over any status, including a producer's `aborted`. | `refusal_aware_terminal.py:82-83`, `:96-97` | I6, I7 |
| V4 | The kept set lacks `aborted`, so `fail_aborted` overwrites a producer's `aborted` with `failed`. | `job_lifecycle.py:51-53`, `:116-121` | I7 |
| V5 | `_begin` ignores what `fail_aborted` answered and counts every taken job pending; `_record` does the same. A kept job is counted spooled, lost or "needs no spool". | `job_abort.py:213-217`, `:219-223` | I3, I9 |
| V6 | `_outcome` never reads the status, so a withdrawn, aborted or committing job is spooled when it is a spooled kind. | `job_abort.py:195-203`, called at `:100`, `:120`, `:184` | I3, I8 |
| V7 | **No observer.** The job awaits its store thread inside the terminal; a cancel abandons the await and the thread's result with it, so `terminal.complete` never runs and the job stays `failed` with its rows in the table. The docstring claims the opposite. | `content_jobs.py:80-82`, `:181-183`; `ingest_jobs.py:68-70`; `collection_worker.py:197-206`; claim at `job_abort.py:43-47` | I4, I1 |
| V8 | `await_spool` turns a cancelled spool *future* into `NOT_WRITTEN` while the thread can still write the file; nothing corrects it, because the late callback is attached only to the unfinished path. | `job_abort.py:140-141`, `:124-125` | I8 |
| V9 | `_ended_late` ignores what `fail_aborted` answered and moves the term whatever the status. The model shows no path to this today; it is the same shape as V5 and breaks the moment V7 is fixed, because a late store makes the status `completed`. | `job_abort.py:173-174` | I5, I9 |
| V10 | The tally has no *kept* or *unconfirmed* term; "needs no spool" is a residual computed by subtraction, so any job counted anywhere is silently in it. I2 holds only because the residual absorbs the difference. | `abort_reason.py:93-164`, `:147-153` | I2 |
| V11 | The body's exit after a producer's withdraw records `failed` over `aborted`. `aborted` is not a terminal status (`tasks.py:40`), so `TaskTerminal` treats it as "exited mid-job". Outside a shutdown this is harmless; inside one it is the overwrite of V1 and V4. | `task_terminal.py:52-58`; `tasks.py:40` | I7 |
| V12 | `QueueAbort._summary` has no term for kept or unconfirmed jobs, so the summary line is wrong even when the status is right. | `queue_abort.py:60-69` | I2 |
| V13 | `RefusalAwareTerminal.complete` never clears `state.error`. Today a completed job never held a reason; once a late store completes a job the abort had already failed, it would carry "daemon shut down before ingest" next to its results. | `refusal_aware_terminal.py:82-83` | I10 |
| V14 | `_one_off_loop` catches `Exception`, and a cancel is a `BaseException`: a cancelled gather skips `_record`, the job stays pending for good, and its late callback is never armed. | `job_abort.py:183-193`; `fail_off_loop` `:102-105` | liveness |
| V15 | Four jobs write their terminal status directly, with no claim and over any status: `FileIndexJob` (`index_jobs.py:64-68`: `failed`, `error`, `completed`, `results`), the delete and sync jobs (`index_jobs.py:117-118`, `:163-164`), and `finalize_job.py:64`, `:106-107`. | as listed | I6, I7 |
| V16 | `JobSpool.write` returns `bool`, not the path it wrote, so nothing can remove a record once its job has stored. | `job_spool.py:103-134` | I11 |

Not violations, and not changed: a queued job's spool has no deadline
(`job_abort.py:102-105`); it stays *pending* until the write ends, and the summary
says so. §9 D3.

## 7. The implementation write-set the model implies

The design mission's output is the write-set; the specialist decides how to cut
it. These are the responsibilities and where they must live, with the class
shapes the OO rules ask for.

1. **One writer for a job's status.** `JobLifecycle` (`job_lifecycle.py`) becomes
   the *only* code that assigns `state.status`, `state.error` or `state.results` of
   a queued job; a grep for those assignments outside it is zero (T6 enforces it).
   It gains the moves the model has, each one critical section under the claim:
   - `settle_stored(results)` and `settle_failed(reason)`: the worker's terminal
     write. `settle_stored` also **clears `state.error`** (I10). Both are refused
     (return `False`) when the status is `withdrawn` or `aborted`. This is
     `FinishStored`, `FinishStop`, `FinishKept`.
   - `cancelled(store)`: the cancel handler. Reads the store's state: stored
     becomes `completed`; a running job with no rows becomes `failed`; a committing
     job whose store runs, and `aborted`, are left. This is `CancelInBody*`.
   - `observe_store(settlement)`: the observer (§7.3). `failed` or `committing`
     becomes `completed` (error cleared, results set) or, for a failed store of a
     committing job, `failed`; returns what it changed, so the abort can move the
     term in the same turn.
   - `fail_aborted` returns a disposition (failed, kept, unconfirmed), not a
     `bool`, and its kept set is `{withdrawn, aborted, completed}`, with
     `committing` answering *unconfirmed*.
2. **Every terminal and every job stops assigning status.** `TaskTerminal` and
   `RefusalAwareTerminal` call the lifecycle's moves from their cancel, exception
   and `finally` branches; so do `FileIndexJob`, the delete and sync jobs
   (`index_jobs.py:64-68`, `:117-118`, `:163-164`) and `finalize_job.py:64`,
   `:106-107` (V15). `TaskTerminal.recording` is also used by tasks that are not
   queued jobs (`routes/base.py:196`, `routes/maintenance.py:93`, `:110`,
   `watch_loop.py:254`). The lifecycle's rules must change nothing for them, and
   T15 pins that for each caller. They do not all hold only `running`,
   `completed` and `failed` in the sense of one terminal write: a `BackfillTask`
   records a partial `completed` and then fails on cancel, a sequence the
   queued-job rules would refuse (completed is never overwritten). The
   implementation keeps the old semantics for such tasks (`TaskState.is_job`,
   below).
   The `finally` clause writes only when the status is still `running`: `aborted`
   and a committing job whose store runs are not "exited without a terminal
   status".
3. **A store the job keeps** (new module, `store_run.py`). `StoreRun(call, settle)`
   starts the blocking call and keeps its future.
   - **Its own executor (A1).** `StoreRun` takes a dedicated bounded
     `DaemonThreads` instance (its own name, daemon threads, one bound for the
     whole daemon), never `DaemonThreads.shared()`. A blocked store must not delay
     a spool write (T11).
   - **The settle continuation (A6).** The job supplies `settle(result) ->
     Settlement`, run by the normal path and by the observer alike, so a detached
     store is settled by the same code that settles an attached one. For a content
     job it is `RefusalAwareTerminal.is_stored(results)`: stored becomes
     `Settlement.stored(results)`, otherwise `Settlement.failed(reason)` with the
     fixed no-pages or nothing-stored text, so **zero rows is never `completed`**.
     For `FileIndexJob` it is its outcome: `outcome.error` becomes `failed`, its
     results become `stored`. A bare stored-or-failed flag is not enough, because
     the results and the error text are the job's.
   - **The exception contract (A6).** A detached store that raised has its
     exception *retrieved* (`future.exception()`, so it is never "never
     retrieved"), logged once through `RedactedFailure.of`, and its reason is
     `FailureReason.of(exc)`: a refusal's text, else `internal error: <Class>`. No
     message that can quote a URL, a path or a credential reaches `state.error`.
   - **The closed loop.** The thread hands its result back with
     `call_soon_threadsafe`, guarded against a closed loop (`RuntimeError` is the
     "nobody is left to tell" case, as in `daemon_threads.py:104-112`); the
     callback is dropped, and a test pins it (T14).
   - It replaces `await run_in_threadpool(...)` inside the terminal at
     `content_jobs.py:82`, `:183`, `ingest_jobs.py:70`, `index_jobs.py:62`, `:116`,
     `:162`, `finalize_job.py:63`, `:105`.
4. **One ledger for the tally** (`abort_reason.py` and `job_abort.py`).
   `AbortTally`'s five counters are replaced by a per-job record, a term (an
   `Enum` of the model's nine) per taken job, with one `move(job, term)`. The
   counters are *derived*, so I2 holds by construction and `wCount` cannot fire;
   the summary reads the ledger. `JobAbort`:
   - `_begin` reads the disposition: kept becomes *kept*, committing becomes
     *unconfirmed*, only failed becomes *pending* and goes on to `_outcome`;
   - **`_record` is a no-op unless the job's term is still *pending*** (L3), as is
     the deadline path; `_ended_late` is a no-op unless it is still *unfinished*.
     The observer may have moved the job to *kept* first, and a stale outcome must
     not move it back (the model's `Record`, `Deadline` and `Late` guards);
   - `_record`, the deadline path and `_ended_late` each move one job's term and
     its reason in one call that holds the claim;
   - `await_spool`'s cancelled branch returns *unfinished*, not *not written*;
   - `_one_off_loop` also handles `CancelledError` (A4): it records *unfinished*,
     arms the late callback on the spool thread's future, which it **shields** so
     the cancel does not cancel it, and re-raises (`QueueCancel`);
   - it registers the observer's listener per taken job, so a store that lands
     moves the job's term to *kept* in the same turn that completes it.
5. **The ghost is removed, not tolerated (L1).** `JobSpool.write` returns the path
   it wrote (or `None`), not a `bool`; `JobAbort` keeps the path per job. When the
   observer completes a job whose spool file exists it removes the file, and the
   spool thread's completion callback removes the file it has just written if the
   job is by then `completed` (`SpoolEndsRemoved`). Removal is best effort, logged
   on failure. **Replay idempotence is unverified:** no replay code exists, so
   nothing here shows that running a spooled record again is harmless. The
   removal is what makes the question moot for the case the model covers; the
   residual (a crash between the store and the removal) leaves a record that the
   daemon's start-up sweep does not touch, because it removes temps, not records.
6. **The summary** (`queue_abort.py`) prints the kept and unconfirmed terms; "not
   yet accounted for" stays *pending* only.
7. **`CollectionWorker`** hands each taken job to the ledger and nothing else
   changes: its abort and cancel flow is the model's.

Commit-gate semantics: `JobLifecycle.commit` and `QueuedJob.checkpoint` are
unchanged (`Commit`, `CommitRefused` are the model of what they do now). What
changes is what a *refused* checkpoint does to a producer's `aborted`: nothing.
`JobWithdrawnError` ends the body without a failure record over a status the
producer owns (`FinishKept`). A committing job whose store never ends stays
`committing` and counted *unconfirmed* for good (T16): the model has no way out of
it by design, and the summary says so.

### Implementation refinements

The implementation splits some of the model's terms more finely, and keeps the old
semantics for tasks that are not queued jobs. None of it changes a witness: each
finer code term maps to exactly one model term, so the model's counts are the sums
of the code's.

| Code term | Model term | Note |
|---|---|---|
| `SPOOLED` | *spooled* (`tspooled`) | a record written and recoverable |
| `UNRECOVERABLE` | *spooled* (`tspooled`) | a record written as a note only (a URL without its query); the model has one term for both, as the old tally had two counters under one reason class |
| `NOT_WRITTEN` | *not written* (`tnotw`) | the spool write failed |
| `UNBUILT` | *unbuilt* (`tunb`) | the record could not be built |
| `LOST` | *not written* or *unbuilt* | the old term; split by reason into the two above |

The reason string for `UNRECOVERABLE` differs from `SPOOLED`'s, so I8 reads it as
*spooled*: a file exists in both.

`TaskState.is_job` separates queued jobs from the other background tasks.
`BackfillTask`, for one, records a partial `completed` (its results so far) and then
fails on cancel, which the lifecycle's rule that a `completed` status is never
overwritten would refuse. The task is not a queued job: no producer withdraws it,
no abort takes it, and no store thread outlives it, so none of I1 to I11 speaks
of it. `is_job` keeps the old terminal writes for it, and the lifecycle's moves
apply only where it is true. §7.2's statement that every non-queued caller holds
only `running`, `completed` and `failed` is corrected accordingly: they hold those
statuses, but not always in one write each.

The OO ratchet applies to every file touched; the ledger and `StoreRun` are
extractions, and `job_abort.py` and `abort_reason.py` should shrink.

## 8. The tests the model implies

One per invariant, each driven by `IngestQueue.aclose(drain_timeout=…)` or
`CollectionWorker.abort_off_loop` with real jobs, a real `JobLifecycle` and a real
`JobSpool` in `tmp_path`. A test never assigns `state.status`. The store is a real
function blocked on a `threading.Event` so the test chooses when it ends; the spool
write blocks or fails through `JobSpool.write`, the one seam the code has.

| # | Invariant | Scenario through the real path | Assertion |
|---|---|---|---|
| T1 | I1 terminal | A `remember` job in flight and a backfill job committing, stores blocked; abort; release (stored, then failed, in two runs). | Each ends `completed` with results, or `failed` with the right reason; none stays `queued`, `running` or `committing`. |
| T2 | I2 exactly once | Mixed set: a withdrawn queued job, a spooled queued job, an in-flight job whose spool outlasts a short `SPOOL_WAIT_S`, a committing job. Read the summary after the take, after the deadline, and after the late write. | At every read the terms sum to the jobs taken. |
| T3 | I3 no contradicted spool | `abort_off_loop`; before awaiting it, withdraw a queued spooled job and a running one through `JobLifecycle.withdraw` (the producer's call). | No spool file for either; both counted *kept*; the status is `withdrawn` and `aborted`. |
| T4 | I4 no lost store | An in-flight `remember` job whose store is blocked; abort; assert `failed`; release the store. Then the race: the store ends before the cancel is delivered. | Both end `completed`, with `results`, counted *kept*; never `failed` with rows. |
| T5 | I5 late correction | An in-flight spooled job whose write is blocked past the deadline; abort; assert *unfinished*; release the write. | Reason and term change together, once; a second late callback changes nothing. |
| T6 | I6 single writer | Replace `state.claim` with an **owner-tracking lock** (L2): one that records which thread holds it, so a write can assert that *it* holds it, not merely that somebody does. Instrument `status`, `error` and `results` on the job's `state` to record the holder at each write. Drive T1, T3, T4, T7, `FileIndexJob`, the delete and sync jobs, and `finalize_job` through the abort, and a producer thread withdrawing in a loop against all of them. | Every recorded write was made by the lock's owner; no write is recorded for any job outside `JobLifecycle`. |
| T7 | I7 kept stays kept | A running job withdrawn by its producer, a committing job, a withdrawn queued job; cancel the worker. | `aborted`, `committing` (until its store ends), `withdrawn`. A committing job whose store then fails is `failed` with the rerun reason; one whose store lands is `completed`. |
| T8 | I8 reason matches disk | A spool write that succeeds, one that raises, one whose future is cancelled while its thread then writes. | The reason says *spooled* only with a file; the cancelled one is *unfinished*, never *not written*. |
| T9 | liveness | After T1..T8 release every event. | Every job settled; `pending` is zero; the summary has no "not yet accounted for". |
| T10 | I11 ghost | A spooled job whose write ends, then whose store lands; and a job whose store lands while its spool thread still runs. | `completed`, counted *kept*, **no spool file**, in both orders; the summary does not count it spooled. |
| T11 | A1 executor | Block every thread of the store executor on events; then abort a spooled job. | The spool write runs and records while the stores are still blocked; `StoreRun` never holds `DaemonThreads.shared()`. |
| T12 | A4 queued cancel | Abort with a queued spooled job whose write is blocked; cancel the task returned by `abort_off_loop`; release the write. | The job is counted *unfinished*, not pending; when the write ends the late callback corrects reason and term together. |
| T13 | I9, I10 term and reason | A late store completes a job the abort had failed, and a committing job's store fails after the abort. | After the late store: `completed`, `error == ""`, `results` set, term *kept*. After the failed committing store: `failed`, term *needs no spool*; never *unconfirmed* on a failed job. |
| T14 | observer, closed loop | A store ends after the loop has closed. | No exception reaches the thread; the callback is dropped. |
| T15 | observer contract; callers | A detached store that raises; one that returns zero rows; a `FileIndexJob` store with an `error`. Then each non-queued `TaskTerminal.recording` caller (`routes/base.py:196`, `routes/maintenance.py:93`, `:110`, `watch_loop.py:254`) through cancel and failure. | The exception is retrieved and logged once, redacted (a message that quotes a URL or a credential does not reach `state.error`); zero rows is `failed`, never `completed`; the index job's error is its own; every caller ends as before. |
| T16 | committing, store never ends | A committing job whose store never returns; abort. | `committing`, counted *unconfirmed*; the process exits within its stop timeout. |

Each failing test must fail on the starting commit for the reason in §6; T2, T5
and T13 are the ones that need the seeded faults of §4.1 in mind, because the old
build never breaks them by itself: write them against the new ledger first and
show them failing under the fault.

## 9. Rulings

The leader ratified D1 to D3 on the condition of the amendments above (A1-A6,
L1-L5), which this revision makes. Recorded so the next reader has the reasons.

**D1. A job whose store commits after the cancel reports `completed`.** I1 and I4
as the brief states them require it. A producer that already read `failed` has
acted on it: the backfill run counts the transcript failed and may run it again.
The alternative, a distinct `failed` with the reason "stored after the abort",
breaks I1 and I4 as written and makes the status lie about the table.

**D2. A status the producer wrote is never overwritten by anyone.** The body's exit
after a producer's withdraw leaves `aborted`, not `failed`. That changes the
non-shutdown path (V11); I7 cannot hold for taken jobs and fail for the rest, and
nothing reads the status after the producer has withdrawn (`queued_ingest.py:112-114`
raises).

**D3. The queued path has no deadline.** A queued job's spool stays *pending* until
it ends, or until a cancel of the gather ends the wait (`QueueCancel`: *unfinished*,
late callback armed); the summary says so. The queue's abort wait bounds the
shutdown (`queue_abort.py:42`), and a pending job is counted and reported, not lost.

## 10. Reproducing the model

The spec is generated: its fifty-odd operations differ by a handful of lines each,
and a hand-edited copy drifts. `docs/design/job-abort.spec.gen.py.txt` is the
generator (a `.txt` so that the repository's Python lint, which has no business
with a generator of LaTeX, does not take it for project code):

```bash
python3 docs/design/job-abort.spec.gen.py.txt OUT.tex [FIXED [ABL]]
```

The committed `job-abort.spec.tex` is `FIXED=1 ABL=0`, the defaults. `FIXED=0`
is the code at eaa5118. `ABL` is 0, or 1 to 7 for a seeded fault (§2.3, §4.1). The
two switches are the whole difference between the variants: the other bytes of
the output are the same. To model-check a variant, generate it to a scratch path
and run `probcli` on that file with the flags of §4; do not run the tools inside
`docs/design/`, which they write into.

## 11. What the model does not claim

- Time. `Deadline` fires whenever its guard holds; whether it fires after
  `SPOOL_WAIT_S` is a code fact the test T5 pins.
- A check-then-write race *inside* an unclaimed write. The model makes each write
  atomic, so the old build's unclaimed terminal writes are more orderly in the model
  than in the code. The claim is a precondition of every fixed-build move; the model
  shows the fixed build never needs a move outside it.
- Two jobs and one worker. The witnesses are about one job's moves against another's
  tally, which two jobs exercise; more jobs are the same moves over more keys.
- A store thread that never ends. The job stays *unconfirmed* or *pending* by
  design; the liveness check is a possibility claim.
- Thread-pool contention. The model assumes the store executor is separate (§2.4)
  and has no pool to starve.
- Replay of a spooled record, and the spool record's bytes, recoverability flag and
  directory sweep, and the log lines. Both spooled outcomes are one term.
