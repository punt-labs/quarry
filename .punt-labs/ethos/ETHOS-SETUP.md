# Ethos Setup (for the agent)

Read this if you have landed in a repo where ethos is not yet enabled, or
where `ethos doctor` reports missing state. The binary is assumed present —
`ethos version` should already work. This file is deliberately not
`@`-imported anywhere so it does not ride in per-session context; open it
on demand, close it when done.

## Quick decision tree

1. `ethos doctor` — is ethos healthy in this repo?
   - **Clean**: nothing to do. Move on.
   - **"Not enabled here"**: run `ethos enable` (below).
   - **"No global content"**: run `ethos seed` first (below).
   - **"No identity resolved"**: run `ethos setup` (below).

Seed, enable and setup below can be re-run to refresh integration/content.
Older storage layouts need no step here: ethos upgrades them itself.

## `ethos seed` — populate global starter content (once per machine)

Deploys starter roles, talents, personalities, writing styles, and skills
into `~/.punt-labs/ethos/`. Also deploys review-checklist agents
(`code-reviewer`, `silent-failure-hunter`,
`invariant-completeness-reviewer`) to `.claude/agents/`.

```bash
ethos seed
```

Run once per machine. Repeat only after upgrading the ethos binary — new
versions may ship additional starter content.

## `ethos enable` — turn ethos on in the current repo

Deposits the vendored agent guide and its manifest, writes the enabled
marker, adds `@.punt-labs/ethos/CLAUDE.md` to the repo's top-level
`CLAUDE.md` (so agent sessions in this repo auto-load daily-use ethos
guidance), and chains the audit-seal and trailer git hooks.

```bash
ethos enable
```

After enable, restart Claude Code once so SessionStart fires with the new
hook configuration. `ethos disable` reverses this: it removes the import
line, deletes the marker, and unchains the git hooks. It is non-destructive:
the guide, config and audit data stay on disk. It refuses while a sibling
worktree is still enabled, because the git hooks are shared; `ethos disable
--force` unchains anyway.

## `ethos setup` — populate this repo's identities + team

Interactive wizard that writes to `.punt-labs/ethos/identities/`,
`.punt-labs/ethos/teams/`, `.punt-labs/ethos/roles/`, and
`.punt-labs/ethos.yaml`. Bundle choice affects which starter team the
wizard installs.

```bash
ethos setup                        # interactive, default bundle: foundation
ethos setup --solo                 # identity only, no team
ethos setup --bundle gstack        # use gstack starter bundle
ethos setup --file config.yaml     # non-interactive, from a config file
```

**Bundles:**

| Bundle | What it is | When to pick |
|---|---|---|
| `foundation` (default) | The general sidecar content seeded by `ethos seed` (all roles, all talents, all personalities available globally). No bundle-scoped subset — just uses everything already global. | Most repos. Default. |
| `gstack` | Small pre-composed starter team: `ceo`, `coo`, `architect`, `product-lead`, `implementer`, `reviewer`, `qa-engineer`, `security-reviewer`. Ships two pipelines: `gstack-plan` (design) and `gstack-ship` (implement). Tagline: "Boil the Lake, Search Before Building, User Sovereignty." | Repos starting greenfield with a small team and want a curated starter roster instead of building one from scratch. |

The wizard's identity graph and configuration are intended for Git — stage
and commit them explicitly so they are shared with the rest of the team.

## Storage policy (DES-079)

See the
[storage and publication contract](https://github.com/punt-labs/ethos/blob/main/docs/storage-layout.md)
for paths and the test that proves each promise.

Resolve the canonical store root **S** through Git's common directory,
separately from the invoking checkout **C**. S owns shared source/blame records
under `S/.punt-labs/ethos/`. All repository-local live logs, retained archives,
overlays, locks, receipts, scratch and temporary staging belong under
`S/.punt-labs/local/ethos/`. C owns
generated agents, guide/marker/import deposits and publication/index effects.
In a normal checkout S = C; a linked feature checkout usually differs.

```yaml
# S/.punt-labs/ethos.yaml (alongside the existing identity/team settings)
record: blame   # default if omitted; full opts in prospectively
```

- Both modes retain local prompts, full reflections, mission events and
  session audits at S. Default blame sealing does not query or change the
  index. Full mode adds eligible published copies into C, including sealed
  session audits, without relocating the only local copy.
- Consent is captured per immutable entry. A mode switch neither promotes
  private older history nor deletes published history. Missing/legacy consent
  is private. Previously eligible pending copies remain retryable after a
  switch back to blame; the switch alone publishes nothing.
- Validate configuration once per request, including each long-lived MCP
  call. Invalid `record` values fail visibly; no fallback location is allowed.
- Re-enable maintains the shared local-zone exclusion, never mode-specific
  ignores inside `.punt-labs/ethos/`. Use ordinary staging for intended
  publication/export output, never force-add.
- All repository temporaries, including rollback and symlink-target staging,
  stay in S's local zone. Cross-filesystem rename/link refuses with the old
  target intact, without adjacent-temp or copy-over fallback.
- Removing a linked C distinct from S must retain prompts, reflections,
  chunks and unsealed tails without SessionEnd. Deleting S or its local data
  is not covered. Local retained history is not disposable scratch.
- Worker artifacts, including `MISSION_ARTIFACTS_DIR`, must target local
  scratch. Ethos never moves, exports or redacts agent-owned files.

### Automatic storage upgrade

Ethos upgrades its own storage. Every CLI command, hook and MCP call first
checks a stamp (`S/.punt-labs/local/ethos/layout.yaml` and
`~/.punt-labs/ethos/layout.yaml`); when a stamp is missing, ethos converts
older layouts into the shared local zone and appends what it did to
`~/.punt-labs/ethos/logs/ethos.log`. The only output is one line, and only
when something changed:

`ethos: upgraded storage (N items; see ~/.punt-labs/ethos/logs/ethos.log)`

No command is needed, and none exists. A busy lock defers the upgrade to the
next run. Losses are logged, never fatal.

Shared identities, contracts, results, corrections, provenance and ADRs,
private credentials, configuration, global data and agent-owned files are never
moved or deleted. Ethos performs no Git staging or commit; review tracked
changes and commit normally. Reconnect long-lived MCP servers after installing
a new binary so they run it.

## Verify

```bash
ethos doctor       # should report no missing state
ethos whoami       # resolves your identity (session roster → git config → OS user)
ethos session      # current session roster (empty until `ethos session iam <handle>` is called)
ls .claude/agents/ # regenerated agent files, one per team member with kind: agent
```

`ethos doctor` failures point at what to fix and how. Address one at a time,
re-run.

## After setup — declare identity per session

Every session where you want the persona block injected:

```bash
ethos session iam <handle>  # e.g. ethos session iam claude
```

Then restart Claude Code (or trigger the SessionStart hook manually). The
persona block appears as `additionalContext` on the first turn.

## The ethos-in-ethos exception

This file lives IN the ethos repo. In the ethos repo itself, the top-level
`CLAUDE.md` deliberately does NOT `@`-import `.punt-labs/ethos/CLAUDE.md`
— developer guidance for ethos is in `docs/development.md`, opened on
demand. Same for `.punt-labs/{vox,z-spec}/CLAUDE.md` — those are end-user
docs for their tools, not needed for developing ethos.

In every OTHER repo (biff, quarry, langlearn, etc.), `ethos enable`
DOES add the `@`-import so daily-use guidance auto-injects.

## Troubleshooting for the agent

- **`ethos doctor` reports "hook missing"** → the storage upgrade
  reinstalls the audit-seal and trailer git hooks on the next ethos run.
- **`ethos doctor` reports "stale binary version"** → the binary on PATH
  is older than the plugin. Ask the human to run `make install` from a
  shell (agent sessions cannot overwrite their own running binary on
  macOS).
- **Persona block not appearing** → SessionStart hook only fires at
  session start. Restart Claude Code. Also verify `ethos session iam <handle>`
  was called for this session.
- **`.claude/agents/<handle>.md` regenerated with wrong content** → the
  source is `.punt-labs/ethos/identities/<handle>.yaml` +
  `.punt-labs/ethos/personalities/<slug>.md` +
  `.punt-labs/ethos/writing-styles/<slug>.md`. Edit the source, not the
  generated agent file.
- **Regen removes files** → it doesn't. Ethos generation adds/updates but
  never deletes. To remove a stale `.claude/agents/*.md`, first remove
  the identity from every team's roster, then `git rm` the file.
- **Uninstall** → `ethos disable` in each repo, then `ethos uninstall`
  (or `ethos uninstall --purge` to also remove `~/.punt-labs/ethos/`).
