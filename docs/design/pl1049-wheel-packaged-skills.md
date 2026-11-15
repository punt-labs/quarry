# PL-1049: `quarry enable` deposits the installed binary's own skills

Status: proposed (awaits ratification)
Issue: PL-1049

## Goal

`quarry enable` in any repo is the whole setup. A user who enables quarry in
their own project gets the `quarry-recall` / `quarry-capture` agent skills on
their non-Claude harness (pi / opencode / codex) with no second command. Claude
Code already reads the skills from the installed plugin, so it needs nothing.

## Why the first attempt (branch `feat/pl1049-enable-deposits-skills`) is wrong

That branch made `enable()` deposit from `<enabled-repo>/plugin/skills/`, gated
on the enabled repo being quarry's own checkout (`OwnCheckout`). Two failures,
one root cause:

1. **Never fires for a real user.** A user's own project has no `plugin/skills/`
   and no `punt-quarry` pyproject, so the deposit no-ops. It only works when you
   enable the quarry source checkout itself — which no end user does.
2. **Forgeable (djb, MEDIUM).** Because it trusts the enabled repo's tree, a
   hostile repo named `punt-quarry` shipping `plugin/skills/evil/` would get
   `evil` auto-installed into every harness on `enable`.

Root cause: it deposits **from the enabled repo's tree** because the installed
`quarry` wheel carries no skills of its own (`[tool.uv.build-backend]
module-name = "quarry"` ships only `src/quarry/`; the skills live at repo-root
`plugin/skills/`, outside the wheel).

## Decision

Make the installed binary own its skills, and deposit from there.

1. **Package the canonical skills into the wheel.** `plugin/skills/` stays at
   repo root — Claude Code's marketplace auto-discovers it there and that cannot
   move. Add a build step so the same tree also ships inside the wheel at
   `src/quarry/_skills/`. One source of truth, two shipped locations:
   - Build-time copy or a uv build-backend include that places `plugin/skills/`
     content under the `quarry` package as `_skills/`. (Confirm the exact
     `[tool.uv.build-backend]` include key during implementation; if uv_build
     cannot force-include outside the module, fall back to a `make`/build hook
     that copies `plugin/skills/` → `src/quarry/_skills/` before `uv build`, and
     a repo check that the two stay in sync.)
   - `src/quarry/_skills/` is generated, gitignored, and never hand-edited; the
     editable dev install resolves skills from repo-root `plugin/skills/` when
     `_skills/` is absent (see point 3).

2. **`enable()` deposits the binary's own skills, ungated by the enabled repo.**
   Replace `SkillDeposit(enabled_repo_root)` with a deposit that reads quarry's
   OWN packaged skills (`importlib.resources` over `quarry._skills`, or the
   editable-dev fallback). The enabled repo's tree is never read, so:
   - it works in any repo (closes failure 1), and
   - there is nothing for a hostile repo to forge (closes failure 2) — the
     `OwnCheckout` gate on the deposit path is removed because the source is no
     longer repo-derived.
   `disable()`'s retraction keeps the manifest-ownership guard (unchanged — it
   already only removes entries carrying quarry's own `.quarry-skill.json`).

3. **`SkillSource.locate` prefers the packaged copy.** Order: packaged
   `quarry._skills` (installed binary) → repo-root `plugin/skills/` (editable
   dev checkout). `quarry skills install` uses the same resolution, so it and
   `enable` deposit identical content.

## What stays

- The deposit/retraction machinery in `skills_sync.py`, the manifest/content-hash
  ownership model in `skills_install.py`/`skills_source.py`, the best-effort
  OSError discipline, the `EnableResult.skills_deposited` report wiring, the
  `project_config.py` extraction, and the `EnableReport` "Deposited agent skills
  to:" line — all reusable from the parked branch.

## Carry-over fixes (code-reviewer, on the parked branch)

- `QuarryHome.home()` must move inside the best-effort `try` in `SkillDeposit.run`
  and `SkillRetraction._remove_all_deposits` (or catch `HomeUnresolvedError`), so
  an unset `$HOME` cannot abort `enable`/`disable`.
- Delete the dead `Enablement.root` property.

## Acceptance

- `quarry enable` in a fresh non-quarry repo, with a detected pi/opencode/codex
  harness, deposits `quarry-recall` + `quarry-capture` into that harness and
  prints "Deposited agent skills to: <harness>". Proven by driving the installed
  wheel (not the editable checkout) against a temp `$HOME`.
- A hostile repo named `punt-quarry` shipping `plugin/skills/evil/` deposits
  NOTHING from its own tree on `enable` (the deposit source is the binary, not
  the repo).
- `plugin/skills/` and the packaged copy are byte-identical (a repo check or
  build invariant).
- `make check` green; the forgery test from djb's finding is added and passes.

## Rejected alternatives

- **Hide `quarry skills install`** (make it internal): leaves the deposit manual,
  so a non-Claude user still silently has no skills. Hides the gap, doesn't close
  it.
- **Keep depositing from the enabled repo's tree, harden the gate**: no gate on a
  repo-supplied file is unforgeable, and it still never fires for a normal user.
- **Fold deposit into `quarry install`** (once per machine) instead of `enable`:
  a harness installed after `quarry install` is never covered; enable is the
  natural per-use trigger and already the one setup command.
