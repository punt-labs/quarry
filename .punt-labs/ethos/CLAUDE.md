# Ethos

How an agent drives ethos — not how to develop ethos itself. Ethos binds a
name, voice, email, GitHub handle, writing style, personality, and talents
into one identity that other tools read.

New in this repo, or `ethos doctor` reports missing state? Setup guide is
deposited alongside this file at `.punt-labs/ethos/ETHOS-SETUP.md` on
`ethos enable`. Pre-enable, fetch from
<https://github.com/punt-labs/ethos/blob/main/docs/ETHOS-SETUP.md>.
Either way it's a one-time task and is deliberately not `@`-imported here.

## Who am I

- `ethos whoami` — resolve your identity from the session, git config, or OS
  user.
- `ethos iam <persona>` — declare a persona for the current session.

Session hooks inject your persona into context at start and after
compaction. Ethos generates `.claude/agents/<handle>.md` from team data at
SessionStart; restart Claude Code to regenerate them after a team change.

## Delegation (missions)

- `ethos mission dispatch --worker <h> --evaluator <h> --write-set <paths> --criteria <text>`
  — write a mission contract. Dispatch writes the contract; a separate
  agent spawn does the work.
- `ethos mission show|log|results <id>` — inspect a mission.
- `ethos mission close <id>` — close a passing mission (requires a
  submitted result for the current round).
- `ethos mission abandon <id> --reason <text>` — retire a mission that
  was created but never had a worker spawned against it (zero
  delegations, zero results). Refuses if any delegation or result
  exists — use `close` for those. Not a bypass of `close`'s result
  gate; see `docs/mission-abandon.md`.
- `ethos mission pipeline list|show|instantiate <name>` — drive multi-stage
  work from a template.

Commit one logical step at a time; the write-set is enforced at runtime, so
an edit outside it fails the mission.

## Audit

- `ethos audit show --delegation <id>` — reconstruct a delegation's trail.
- `ethos audit seal` runs at pre-commit when ethos is enabled here; the
  sealed chunks travel in the same commit as the work.
- `ethos audit quarantine` — the recovery path for a corrupt chunk.

## Session

- `ethos session` — the current roster.
- `ethos session purge` — clear stale sessions.

## Review agents

`ethos seed` deploys three review-checklist agents to `.claude/agents/`:
`code-reviewer` (general code-quality and CLAUDE.md-compliance),
`silent-failure-hunter` (error handling, swallowed exceptions, fallback
logic), and `invariant-completeness-reviewer` (verifies a claimed invariant,
exhaustiveness property, or regression-guarding test actually holds, rather
than trusting the prose that asserts it). These are checklist agents, not
mission-dispatchable specialists: invoke them directly as local review
passes, never via `ethos mission dispatch --worker <handle>`.

Local review sequence, after `make check` passes:

1. Run `code-reviewer` on the diff.
2. Run `silent-failure-hunter` on the diff.
3. Run `invariant-completeness-reviewer` on the diff.
4. Fix all valid findings; re-run until all three return zero findings.

## Gotchas

- Never run `make install` from inside Claude Code — the running binary
  cannot overwrite itself. Ask a human to run it from a shell.
- Agent types are discovered at SessionStart; restart after adding one.
- `ethos doctor` checks seal-hook presence only when ethos is enabled
  here, but its hook currency checks run unconditionally, on dormant
  repos too.
