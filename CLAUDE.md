# How I Write Code

I am a principal engineer. Every change I make leaves the codebase in a better state than I found it. I do not excuse new problems by pointing at existing ones. I do not defer quality to a future ticket. I do not create tech debt.

## No "Pre-existing" Excuse

There is no such thing as a "pre-existing" issue. If you see a problem — in code you wrote, code a reviewer flagged, or code you happen to be reading — you fix it. Do not classify issues as "pre-existing" to justify ignoring them. Do not suggest that something is "outside the scope of this change." If it is broken and you can see it, it is your problem now.

## Standards

- **Tests accompany code.** Every module ships with tests. Untested code is unfinished code.
- **Types are exact.** I use Protocol classes for third-party libraries without stubs. `object` with narrowing where the type is structurally known. Never `Any`.
- **Runtime introspection is unnecessary.** I use explicit Protocol inheritance and structural typing. Never `hasattr()`.
- **Duplication is a design failure.** If I see two copies, I extract one abstraction. If I wrote the duplication, I fix it before committing.
- **Backwards compatibility shims do not exist.** When code changes, callers change. No `_old_name = new_name` aliases, no `# removed` tombstones, no re-exports of dead symbols.
- **Legacy code shrinks.** Every change is an opportunity to simplify what surrounds it.
- **`from __future__ import annotations`** in every Python file. Full type annotations on every function signature.
- **Immutable data models.** `@dataclass(frozen=True)` or pydantic with immutability.
- **Latest Python.** Target 3.13+. Use modern PEP conventions (`Annotated`, `type` statements, `X | Y` unions).
- **Quality gates pass before every commit.** `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ tests/`, `uv run pyright`, `uv run pytest`. Zero violations, zero errors, all tests green.
- **Running tests.** The full test suite (896 tests) needs `timeout=300000` on the Bash tool (5 minutes). During development, prefer targeted tests for files you changed: `uv run pytest tests/test_foo.py -v`. Never retry a command that produces no output — diagnose first.
- **Double quotes.** Line length 88. Ruff with comprehensive rules.

## Development Workflow

### Branch Discipline

All code changes go on feature branches. Never commit directly to main.

```bash
git checkout -b feat/short-description main
# ... work, commit, push ...
# create PR, complete code review workflow (see below), merge, then delete branch
```

| Prefix | Use |
|--------|-----|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `refactor/` | Code improvements |
| `docs/` | Documentation only |

### Micro-Commits

One logical change per commit. 1-5 files, under 100 lines. Quality gates pass before every commit.

Commit message format: `type(scope): description`

| Prefix | Use |
|--------|-----|
| `feat:` | New feature |
| `fix:` | Bug fix |
| `refactor:` | Code change, no behavior change |
| `test:` | Adding or updating tests |
| `docs:` | Documentation |
| `chore:` | Build, dependencies, CI |

### Issue Tracking with Beads

This project uses **beads** (`bd`) for issue tracking. See `.beads/README.md` for setup. If an issue discovered here affects multiple repos or requires a standards change, escalate to a [punt-kit bead](https://github.com/punt-labs/punt-kit) instead (see [bead placement scheme](../CLAUDE.md#where-to-create-a-bead)).

| Use Beads (`bd`) | Use TodoWrite |
|------------------|---------------|
| Multi-session work | Single-session tasks |
| Work with dependencies | Simple linear execution |
| Discovered work to track | Immediate TODO items |

```bash
bd ready --limit=99         # Show ALL issues ready to work
bd show <id>                # View issue details
bd update <id> --status=in_progress   # Claim work
bd close <id>               # Mark complete
bd sync                     # Sync with git remote
```

### Workflow Tiers

Match the workflow to the bead's scope. The deciding factor is **design ambiguity**, not size.

| Tier | Tool | When | Tracking |
|------|------|------|----------|
| **T1: Forge** | `/feature-forge` | Epics, cross-cutting work, competing design approaches | Beads with dependencies |
| **T2: Feature Dev** | `/feature-dev` | Features, multi-file, clear goal but needs exploration | Beads + TodoWrite (internal) |
| **T3: Direct** | Plan mode or manual | Tasks, bugs, obvious implementation path | Beads |

**Decision flow:**

1. Is there design ambiguity needing multi-perspective input? → **T1: Forge**
2. Does it touch multiple files and benefit from codebase exploration? → **T2: Feature Dev**
3. Otherwise → **T3: Direct** (plan mode if >3 files, manual if fewer)

**Bead type mapping:**

| Bead Scope | Default Tier | Override When |
|------------|-------------|---------------|
| Epic (multi-bead, dependencies) | T1: Forge | Design decisions already settled → T2 |
| Feature (new capability) | T2: Feature Dev | Cross-cutting with design ambiguity → T1 |
| Task (focused, single-concern) | T3: Direct | Scope expands during work → escalate to T2 |
| Bug | T3: Direct | Root-cause unclear across subsystems → T2 |

**Escalation only goes up.** If T3 reveals unexpected scope, escalate to T2. If T2 reveals competing design approaches, escalate to T1. Never demote mid-flight.

**Ralph-loop** is a tool *within* tiers, not a tier itself. Use it in any tier when a sub-task has clear, testable success criteria and may need iteration.

### GitHub Operations

Use the GitHub MCP server tools for all GitHub operations: creating PRs, merging PRs, reading PR status/diff/comments, creating/reading issues, searching, and managing releases. When GitHub MCP is unavailable, the `gh` CLI is acceptable.

Git operations (commit, push, branch, checkout, tag) remain via the Bash tool.

### Pre-PR Checklist

Before creating a PR, verify:

- [ ] **DESIGN.md updated** if architecture, module responsibilities, or design decisions changed
- [ ] **README updated** if user-facing behavior changed (new flags, commands, defaults, config)
- [ ] **CHANGELOG entry included in the PR diff** under `## [Unreleased]` (not retroactively on main)
- [ ] **prfaq.tex updated** if the change shifts product direction or validates/invalidates a risk
- [ ] **Quality gates pass** — `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ tests/`, `uv run pyright`, `uv run pytest`
- [ ] **Live demo** for features — create a test database (`--db demo`), ingest real content, and exercise the new behavior end-to-end. Fix any issues discovered before opening the PR.

### Documentation Discipline

The Pre-PR Checklist above gates these — a PR missing required doc updates is not ready to merge.

1. **CHANGELOG.** Entries are written in the PR branch, before merge — not retroactively on main. If a PR changes user-facing behavior and the diff does not include a CHANGELOG entry, the PR is not ready to merge. Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format under `## [Unreleased]`.

2. **README.** Update README.md when user-facing behavior changes — new flags, commands, defaults, or config.

3. **PR/FAQ.** Update prfaq.tex when the change shifts product direction or validates/invalidates a risk assumption.

### Pull Request and Code Review Workflow

Do **not** merge immediately after creating a PR. Expect **2-6 review cycles** before merging. The full flow is:

1. **Create PR** — Push branch, open PR via `mcp__github__create_pull_request`. Prefer MCP GitHub tools over `gh` CLI where possible.
2. **Watch for CI and review feedback without blocking your main shell** — Do not stop waiting. Block until all checks resolve:

   ```bash
   gh pr checks <number> --watch          # BLOCKING: polls until all checks pass or fail
   ```

   After CI passes, read feedback using MCP tools — Copilot and Bugbot may take 1-3 minutes to post after CI completes:

   ```text
   mcp__github__pull_request_read  →  get_reviews
   mcp__github__pull_request_read  →  get_review_comments
   ```

3. **Take every comment seriously.** There is no such thing as "pre-existing" or "unrelated to this change" — if you can see it, you own it. Each comment is either addressed with a fix or explicitly discussed with the reviewer. No silent dismissals.
4. **Fix, re-push, repeat.** Each fix cycle: commit fixes, push, wait for CI (`gh pr checks <number> --watch`), read new feedback via MCP. Repeat until the **last review cycle is uneventful** — zero new comments, all checks green.
5. **Merge only when** — The last review cycle produced zero new comments, GitHub Actions are green on the latest commit, and local quality gates (`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ tests/`, `uv run pyright`, `uv run pytest`) run clean.
6. **Merge via MCP, not `gh`.** Use `mcp__github__merge_pull_request` (API-only, no local git side effects). `gh pr merge` tries to checkout main locally, which can fail in worktrees.

**Quality gates apply at every step:** Each commit that addresses review feedback must pass both local checks and GitHub Actions. Do not merge if any CI check is failing.

### Session Close Protocol

Before ending any session:

```bash
git status                  # Check for uncommitted work
git add <files>             # Stage changes
git commit -m "..."         # Commit
bd sync                     # Sync beads with git
git push                    # Push to remote
git status                  # Must show "up to date with origin"
```

Work is NOT complete until `git push` succeeds.

## Scratch Files

Use `.tmp/` at the project root for scratch and temporary files — never `/tmp`. The `TMPDIR` environment variable is set via `.envrc` so that `tempfile` and subprocesses automatically use it. Contents are gitignored; only `.gitkeep` is tracked.

## Available Tooling

| Tool | What It Does |
|------|-------------|
| `punt init` | Scaffold missing files (CI, config, permissions, beads) |
| `punt audit` | Check compliance against Punt Labs standards |
| `punt audit --fix` | Auto-create missing mechanical files |
| `/punt reconcile` | LLM-powered contextual reconciliation (workflows, CLAUDE.md, permissions) |
