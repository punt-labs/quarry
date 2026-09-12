# SessionStart marker gate, reconcile drift, disable deregister

**Bead:** quarry-gx4c · **Mission:** m-2026-08-30-011 (design)
**Standard:** `punt-kit/standards/tool-enable-disable.md` §§ 2.1, 2.3, 2.9, 2.11

Two coupled defects. `handle_session_start` auto-registers any cwd whose parent
is not already covered without consulting the enabled marker, so nine of ten
registered collections are indexed-but-mute — the vendored guide never lands.
And `quarry disable`'s deregister is a call the CLI orchestrator makes in the
wrong order relative to the § 2.11 commit point, so a mid-disable failure can
leave the collection deregistered while the marker still advertises the repo as
enabled.

The fix is a three-path gate at the top of `handle_session_start`, keyed on the
marker (`EnabledMarker.is_present()`); and a reordering that puts the collection
deregister where `disable_project` currently calls it FIRST — namely LAST, after
`Enablement.disable()` commits the marker/import teardown.

## 1. Current-code inventory

Cited so the implementation mission has verified anchors.

- `src/quarry/hooks.py:201–315` — `handle_session_start`. Reads
  `payload["cwd"]` via `_as_dir`, `HookConfig.session_sync` via
  `load_hook_config`, opens `SyncRegistry` + `CollectionResolver`, then in one
  block:
  - `hooks.py:237` — `resolver.covering_collection(str(directory))`.
  - `hooks.py:243` — `conn.has_registrations_under(directory)` subsumption
    guard, warns and returns without registering.
  - `hooks.py:262` — `resolver.archived_collection_for(directory)` re-adopt.
  - `hooks.py:265` — `_daemon_chunk_collections()`; `ConnectionError` returns
    the "quarryd unreachable" defer message.
  - `hooks.py:286` — `conn.register_directory(directory, collection)`.
    **No `EnabledMarker` lookup anywhere.**
- `src/quarry/enabled_marker.py:15–75` — `EnabledMarker`. `is_present()` at
  `:51`, `write()` at `:55`, `remove()` at `:67`. Symlink-safe via
  `SafeRepoPath`; no changes needed here.
- `src/quarry/enablement.py:20–121` — `Enablement`. `enable()` at `:66` runs
  gitignore → guide → import register → marker write; `disable()` at `:92`
  runs marker remove → import prune under one `FileLock`. Both are pure
  filesystem/CLAUDE.md ops; no `RegistryClient` dependency. The § 2.11
  commit-point ordering is correct here.
- `src/quarry/enable.py:165–258` — `disable_project`. Order today:
  1. `disable_project:194–210` — `client.deregister(...)` FIRST when a covering
     registration exists.
  2. `disable_project:219–224` — `SafeRepoPath(...).remove()` for `config.md`.
  3. `disable_project:230` — `Enablement(directory).disable()` (marker +
     import).
  4. `disable_project:240–248` — `client.delete_collection(captures)`, guarded.

  The claim in the bead that "`quarry disable` leaves the collection
  registered" is out of date: `disable_project` deregisters. The real defect
  is that step 1 runs BEFORE step 3. If `Enablement.disable()` raises mid-run,
  the collection is gone but the marker and import still declare the repo
  enabled — a § 2.11 violation (import present, but no functional collection
  behind it) and precisely the "toggle deletes committed content" surprise
  § 2.9 forbids. The fix is a reorder, not an addition.
- `src/quarry/cli_project.py:66–88` — `_disable` command. Delegates to
  `disable_project`. No changes needed.
- `src/quarry/sync_registry.py:222–267` — `deregister_directory`. Idempotent,
  supports `keep_data`.
- `src/quarry/collection_resolver.py:37–54` — `covering_collection`. Walks up
  from cwd looking for an exact directory match; returns None when no coverage.
  The registration axis in the state machine is `covering_collection is not
  None`, not "any registration exists in the DB."

## 2. State machine

Three axes: marker present/absent × registration covering `cwd` yes/no × daemon
reachable/unreachable. Eight rows; four actions. "Registration" always means
`covering_collection(cwd) is not None` — a sibling or unrelated registration
does not count.

| # | Marker | Covering registration | Daemon | Action |
|---|--------|-----------------------|--------|--------|
| 1 | present | exists | reachable | **A — active.** Existing behavior. Use the covering collection; kick off background sync; return the enabled `additionalContext`. |
| 2 | present | exists | unreachable | **A — active.** Same as row 1. Sync is fire-and-forget; daemon reachability does not gate the response. The next sync attempt will fail and log locally; the SessionStart contract is unaffected. |
| 3 | present | none | reachable | **A′ — auto-register.** No coverage but the user has opted in. Check `has_registrations_under` first (subsumption guard, `hooks.py:243`); if children exist, refuse and return the existing subsumption message. Otherwise re-adopt via `archived_collection_for` or mint a fresh unique name from the daemon's chunk catalog, `register_directory`, kick off sync, return active `additionalContext`. |
| 4 | present | none | unreachable | **A′ — defer.** The daemon-unreachable branch (`hooks.py:265–282`) stands: writing a registration now without the chunk set risks a cross-project merge. Return the existing "quarryd is unreachable, so auto-registration of `{directory}` is deferred…" message. |
| 5 | absent | exists | reachable | **C — reconcile drift.** Marker was never written or was deleted; the collection persists. Do NOT auto-register (already registered) and do NOT auto-deregister (destructive under a keep-data policy). Log a `logger.warning`. Return the Path C `additionalContext` (§ 3 below) naming both `quarry enable <cwd>` and `quarry deregister <collection>`. |
| 6 | absent | exists | unreachable | **C — reconcile drift.** Same as row 5. Daemon state is moot; neither surfacing the drift nor naming the two doors touches the daemon. |
| 7 | absent | none | reachable | **B — nudge.** No opt-in signal. Do NOT auto-register, do NOT touch the registry, do NOT deposit the guide. Return the Path B `additionalContext` (§ 3 below) naming `quarry enable <cwd>`. |
| 8 | absent | none | unreachable | **B — nudge.** Same as row 7. Daemon state is moot; refusing to touch the registry is the whole action. |

**Invariants.**

- The marker check is a strict gate: `Enablement.disable()` writes `marker
  present ⇒ import present` (§ 2.11); SessionStart must treat that same
  biconditional as authoritative. Anything registered without a marker is by
  definition drift (row 5/6), not a valid config to preserve silently.
- Path B and Path C never write to `SyncRegistry`. No `register_directory`, no
  `deregister_directory`, no marker mutation, no guide deposit. The hook is
  read-only in those paths (§ 2.1 preserves the user-owned host file; § 2.3
  reserves both mutations to explicit `enable`/`disable` verbs).
- Path A's daemon-unreachable defer (row 4) is not a "no marker" state — the
  marker IS present. The message can and should say "auto-registration is
  deferred because the daemon is unreachable"; it must not say "not enabled."

## 3. Path B and Path C `additionalContext` strings

Both are ephemeral hook output routed through `_session_start_output` — they
are NEVER written to any host `CLAUDE.md` file. § 2.1 is preserved by
construction: the hook returns a JSON envelope; the strings live only in the
session context surface.

### Path B — no marker, no covering registration

```text
Quarry semantic search is available but not enabled for this project.
Directory: {directory}
Nothing has been indexed here yet. To turn quarry on:
  quarry enable {directory}
This runs once, commits an opt-in marker, deposits the agent guide,
and registers this directory for background sync.
```

### Path C — no marker, covering registration exists

```text
Quarry: this project has an indexed collection but no opt-in marker
({directory}, collection {collection!r}). Two doors:
  quarry enable {directory}         re-adopt: restore the marker + guide.
  quarry deregister {collection}    drop: remove the registration
                                    (keep-data policy applies).
Auto-register is refused (already registered); auto-deregister is
refused (would delete indexed data on marker drift).
```

Both strings are formatted with the resolved `directory` (post
`Path(cwd).resolve()`, matching existing hook conventions at `hooks.py:227`).
Both name the exact CLI verbs the operator's ruling requires; neither implies
either door is preferred.

## 4. `quarry disable` step ordering

Standard § 2.3 lists disable steps 1–4; § 2.11 constrains the sub-order of 1
and 2 (marker before import); § 2.9 governs what disable leaves behind.
Quarry's disable adds one quarry-specific step to the standard: deregister the
sync collection (bead requirement).

**Required order in `disable_project`.** All steps idempotent.

| Step | Operation | Owner | Reason |
|------|-----------|-------|--------|
| 1 | Read `covering_collection` for `directory` | `Registrations` view of `client.list_registrations()` (existing at `enable.py:193–194`) | Establish target collection before any mutation. |
| 2 | Remove `config.md` (best-effort, `SafeRepoPath.remove`) | `disable_project` (existing at `:219–224`) | Config removal has no § 2.11 dependency; running it up-front means a retry always reaches the CLAUDE.md steps. |
| 3 | `Enablement.disable()` — delete marker, prune import (one FileLock) | `Enablement` (existing at `enablement.py:92–121`) | § 2.11 commit point: marker removal FIRST inside this call, then import prune. A failure in step 3 leaves the recoverable marker-absent + import-present state. |
| 4 | Deregister the covering collection | `client.deregister(...)` from `disable_project` | § 2.3 step 3 in quarry-specific form. **After** step 3 so a failure here leaves the repo fully disabled from the CLAUDE.md / marker perspective (§ 2.9 dormant); the registration is a runtime residue a retry cleanly reconciles. |
| 5 | Delete captures collection (best-effort, `keep_data=False` only) | `client.delete_collection(...)` (existing at `:240–248`) | Already best-effort in current code; keep it last. |

**§ 2.9 rationale for step 4.** Disable transitions the repo to the dormant
state (§ 2.9 table row: directory present, marker absent, import absent). The
vendored guide stays; the runtime *behavior* stops. Quarry's registration is
runtime state (like vox's music cache) — it is what actually drives the
behavior (background sync, chunk writes). Leaving the registration live in
the dormant state contradicts § 2.9's promise that disable stops composition.
Removing it AFTER the § 2.11 commit point means: (a) an operator observing
the CLAUDE.md surface sees a coherent story at every failure boundary, (b)
the retry converges cleanly, and (c) `keep_data=True` still archives chunks
under the retained-marker mechanism (`SyncRegistry.deregister_directory`
handles this at `sync_registry.py:222–267`).

**Why the current order is wrong.** `disable_project:207–210` calls
`client.deregister` FIRST. If `Enablement(directory).disable()` at `:230`
raises (a hostile symlink, a lock contention, a filesystem error not caught by
the `except ValueError` at `:114`), the collection is already gone but the
marker still declares the repo enabled and the import still resolves the
guide. From the § 2.11 auditor's view, the state is invalid — enabled by the
biconditional, but with no functional collection. Reordering to step 4
eliminates this failure mode.

**No new dependency for `Enablement`.** The design deliberately leaves
`Enablement` as a pure marker+import composer. Threading a `RegistryClient`
through it would drag the whole daemon/HTTP layer into a file-system class
and violate the layered import direction (`enablement.py` currently imports
only stdlib, `SafeRepoPath`, guidance, and file-lock utilities; adding an API
port would push it up two layers). Ordering the deregister at the
orchestrator level (`disable_project`) is the correct seam.

## 5. Unit-test expectations

Four tests are required. Names follow the existing `TestT##…` and
`Test…` conventions in `tests/test_hooks.py` (§ Path A tests already exist at
`test_hooks.py:1491` `TestT16SessionStartAutoRegisters`,
`test_hooks.py:1524` `TestT16bSessionStartParentOfChildrenSkipsAutoRegister`,
etc.). The three SessionStart tests exercise the three actions of the state
machine; the disable regression test locks in the reordered flow.

Fixtures reuse the `_ReachableDaemonEmptyCatalog` base already present at
`test_hooks.py:345` for daemon mocking. `EnabledMarker(project).write()` and
`EnabledMarker(project).remove()` are how tests toggle the marker (both are
symlink-safe and idempotent per `enabled_marker.py:55,67`).

### 5.1 Path A — marker + covering registration → active

`tests/test_hooks.py::TestSessionStartMarkerGate::test_path_a_marker_and_registration_runs_active_flow`

- Setup: create `project` dir; write marker via `EnabledMarker(project).write()`;
  pre-register `project` in `SyncRegistry` with collection `proj`.
- Act: `handle_session_start({"cwd": str(project)})`.
- Assert:
  - Response contains the enabled string: `additionalContext.startswith("Quarry semantic search is active for this project.")`.
  - Registration count unchanged (still one — the pre-existing row).
  - `_sync_in_background` was invoked (patched).
  - No `register_directory` call beyond the pre-seeded one (assert by
    registration count, not by patching — the pre-seeded row is what proves
    the flow reused coverage).

### 5.2 Path B — no marker, no registration → nudge

`tests/test_hooks.py::TestSessionStartMarkerGate::test_path_b_no_marker_no_registration_nudges_enable`

- Setup: create `project` dir; do NOT write the marker; empty `SyncRegistry`.
- Act: `handle_session_start({"cwd": str(project)})`.
- Assert:
  - `additionalContext` contains `"not enabled"` and the literal
    `f"quarry enable {project}"`.
  - `additionalContext` does NOT contain `"quarry deregister"`.
  - `SyncRegistry(...).list_registrations()` returns an empty list (no
    `register_directory` fired).
  - `_sync_in_background` was NOT invoked.
  - `_daemon_chunk_collections` was NOT invoked (Path B is registry-free —
    the daemon is not consulted).

### 5.3 Path C — no marker, covering registration → reconcile drift

`tests/test_hooks.py::TestSessionStartMarkerGate::test_path_c_no_marker_registration_exists_surfaces_drift`

- Setup: create `project` dir; do NOT write the marker; pre-register `project`
  in `SyncRegistry` with collection `proj`.
- Act: `handle_session_start({"cwd": str(project)})` with
  `caplog.at_level(logging.WARNING, logger="quarry.hooks")`.
- Assert:
  - `additionalContext` contains both `f"quarry enable {project}"` and
    `f"quarry deregister {project}"`.
  - Registration count unchanged (no `register_directory`, no
    `deregister_directory`).
  - `logger.warning` fired with a message naming the drift (matches on a
    stable substring, e.g. `"no opt-in marker"`).
  - `_sync_in_background` was NOT invoked.

Two parametrisations on row 5 vs row 6 (daemon reachable vs unreachable) are
optional but cheap — the assertions are identical because Path C does not
consult the daemon.

### 5.4 Disable regression — `Enablement.disable()` runs before `client.deregister`

`tests/test_enablement.py::TestDisableProjectOrdering::test_deregister_runs_after_marker_removal`

Locks in the § 4 ordering. Not a `TestEnablement` internal test — `Enablement`
itself did not change; the ordering lives in `disable_project`.

- Setup: `directory` with marker + import present. Fake `RegistryClient`
  recording call ordering (`list_registrations`, `deregister`,
  `delete_collection`). Instrument `Enablement.disable()` (e.g. patch to
  record its invocation order alongside the fake client's).
- Act: `disable_project(directory, fake_client)`.
- Assert:
  - Call sequence: `list_registrations` → `Enablement.disable` →
    `deregister` → `delete_collection`.
  - After the call: marker absent, import absent, fake client's
    `deregister` invoked with the right collection, `keep_data=False`.
  - `list_registrations` was called BEFORE any mutation (to resolve the
    covering registration).
  - Idempotence: a second `disable_project(directory, fake_client)` call
    succeeds; fake client's `deregister` is not called a second time
    (covering is None on the second pass).

A companion table-driven test verifying "if `Enablement.disable` raises, the
fake client's `deregister` was NEVER called" locks in the fail-closed
property. This is the regression that would have caught the current bug.

## 6. Write set for the implementation mission

The implementation mission executes this design and only this design. Write set:

- `src/quarry/hooks.py` — introduce the marker gate at the top of
  `handle_session_start` (after cwd / config resolution at
  `hooks.py:217–225`, before the `SyncRegistry` open at `:233`). Extract the
  three Path A / B / C handlers as private helpers (`_session_start_path_a`,
  `_session_start_path_b`, `_session_start_path_c`) if the branching pushes
  `handle_session_start` past the module's complexity budget (radon CC ≥ B).
  The subsumption guard (`has_registrations_under`) and the daemon-unreachable
  defer stay inside Path A′. `_session_start_output` unchanged.
- `src/quarry/enable.py` — reorder `disable_project` (§ 4 above): move
  `client.deregister(...)` from `:207–210` to AFTER
  `Enablement(directory).disable()` at `:230`. Preserve the covering lookup
  and the child-of-parent guard at `:198–203`. No signature change.
- `src/quarry/enablement.py` — no functional change; update the `disable()`
  docstring at `enablement.py:92` to name the invariant this method
  guarantees (marker+import atomicity under one lock) and to state that
  collection deregister is the caller's responsibility, so the seam is
  documented at the class it lives on. **This is the enablement.py entry the
  mission contract calls out; the deregister logic itself stays in
  `disable_project` for the layering reason in § 4.**
- `tests/test_hooks.py` — the three SessionStart path tests (§ 5.1–5.3),
  grouped in a new `TestSessionStartMarkerGate` class alongside the existing
  `TestT16…` classes.
- `tests/test_enablement.py` — the disable-ordering regression (§ 5.4). If
  the existing file is `tests/test_enable.py` (or similar) instead, add there
  — the mission checks that the file exists before creating a new one.

**Files the design considered and rejected from the write set.**

- `src/quarry/enabled_marker.py` — API is already correct and symlink-safe;
  `is_present()` at `:51` is the exact predicate the gate needs.
- `src/quarry/cli_project.py` — `_disable` at `:66` already delegates to
  `disable_project`; the reorder lands entirely inside the orchestrator.
- `src/quarry/sync_registry.py` — `deregister_directory` at `:222` already
  handles `keep_data` and returns document names; no schema change is
  required. The client-side deregister uses the daemon's copy of this same
  code path.

## 7. OO ratchet (mandatory per PL-OA-1)

Both edited source files are already above the module-size threshold
(`hooks.py` at 645 lines by `wc -l`; `enable.py` at 272). The implementation
mission's ratchet obligation is real — it may not simply add lines. Two
opportunities the mission should take:

- `hooks.py` — extracting `_session_start_path_a/b/c` moves a chunk of the
  `handle_session_start` body out and drops `max_complexity` on that
  function. If the three helpers cluster around a small state value
  (`cwd`, `directory`, `config`, `settings`, `conn`, `resolver`), promote
  them to a `SessionStartContext` dataclass (`frozen=True, slots=True`) so
  the helpers are methods on a class that owns the state — closing the
  PY-OO-7 "helpers next to a class" trigger before it opens. The
  implementation mission decides between "three helpers on a context class"
  and "one dispatcher method with three private methods" based on which
  moves `oo_score.py` further.
- `enable.py` — `disable_project` currently interleaves five ordered steps
  in one function body (registrations lookup, config remove, Enablement
  disable, client.deregister, captures purge). Extracting the four
  fallible steps into named private helpers (`_resolve_covering`,
  `_remove_config_file`, `_deregister_covering`, `_purge_captures`) drops
  `max_complexity` and produces a step ordering that the § 5.4 test can
  assert against directly by calling the helpers in the desired order,
  rather than by patching cross-cutting calls.

Neither improvement is a "cheapest legal change" — both retire real
complexity. The mission may pursue either (or both); it may not skip the
ratchet.

## 8. Not in scope

- The enabled-repo `additionalContext` string rewrite. Owned by bead `lqup`.
- Retroactive cleanup of the nine indexed-but-mute repos. Path C's UI IS the
  cleanup mechanism; no batch command is added.
- Adding hook/config settings.json deregister to `Enablement.disable()`
  (§ 2.3 step 3). Quarry ships its hooks through the marketplace plugin, not
  through per-repo `.claude/settings.json` writes; the existing `_stdlib.py`
  first-run setup at `_stdlib.py:354–406` is user-scope, not per-repo, and
  is out of the disable path.
- The MCP `mic:enablement`-shaped surface for quarry `enable`/`disable`
  (§ 2.14 "dual surface"). Quarry's MCP server does not yet expose this
  today; the design leaves the CLI-only path in place.
