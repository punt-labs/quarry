---
paths:
  - "**/*.py"
  - "tests/**"
---

# Python Testing Rules

**Updated:** 2026-11-22

Language rules for the [Testing standard](https://github.com/punt-labs/punt-kit/blob/main/standards/testing.md).

## PL-TT-1: Map pytest selections to gates

**Implements:** TST-1, TST-2, TST-3, TST-10 · **Gate:** `make test`, PR CI and review (code-reviewer)

Use pytest paths or registered markers with strict marker validation.
Select affected tests for Pre-commit, broadening the selection when shared
fixtures change. Run the credential-free default suite and fast local process
selections for Pre-push. Include the required installed-wheel and domain
checks in PR CI and on main. Document the actual selectors and commands
(such as `uv run pytest -m e2e`), along with tools and credentials required.

## PL-TT-2: Coverage Increases With Every Change

**Implements:** TST-5 · **Gate:** project coverage entry point, CI coverage report and review (code-reviewer)

**Statement**: New behavior gets tests. A bug fix includes a regression test.

**Criterion**:

- Pass: new code has corresponding tests; bug fixes include reproduction test
- Fail: new function without test; bug fix without regression test

**Tooling**:

- LLM review: diff includes test changes proportional to source changes

Use coverage.py with pytest-cov and branch measurement. Start instrumentation
in the subprocesses or installed-wheel entry points whose coverage the report
claims. Combine worker and child data with source paths mapped to the same
package. When a project has a coverage ratchet, use equivalent selection and
instrumentation at head and merge base.

## PL-TT-3: Tests Document Behavior

**Implements:** TST-5 · **Gate:** review (code-reviewer)

**Statement**: The test suite is the executable specification. When someone asks
"what happens when X?", the answer should be in a test. Tests that only verify
happy paths are incomplete.

**Criterion**:

- Pass: error paths, edge cases, and boundary conditions are tested
- Fail: only happy-path tests; error handling untested

**Tooling**:

- LLM review: for each public function, are error cases tested?

## PL-TT-4: Test Infrastructure Is First-Class

**Implements:** TST-4, TST-9 · **Gate:** pytest isolation fixtures and review (code-reviewer)

**Statement**: Fixtures, factories, helpers, and test utilities are real code
that deserves the same quality as production code. Flaky tests are bugs. Slow
tests are performance issues.

**Criterion**:

- Pass: shared fixtures in conftest.py; no copy-paste between test files
- Fail: duplicated setup code across tests; flaky tests ignored

**Tooling**:

- Grep: duplicate setup patterns across test files
- CI: flaky test = immediate fix, not retry

Use `tmp_path` and explicit environment overrides for HOME, XDG paths,
caches and credentials. Pass that environment to children and use test-owned
ports or sockets. Tear down children in fixtures even on failure.

## PL-TT-5: Humble Object Testing

**Implements:** TST-4 · **Gate:** command unit tests without a running server

**Statement**: When a project uses the commands layer (PL-PA-3), command functions
are testable without mocks, subprocesses, or network. Construct context with an
in-memory protocol implementation, call the command function directly, assert on
result fields.

**Criterion**:

- Pass: command tests run in milliseconds; no subprocess spawning
- Fail: command tests require a running server or subprocess

**Tooling**:

- Test runtime: command tests should complete in < 100ms each
- Reference: biff commands tests with `LocalRelay(tmp_path)`

## PL-TT-6: Select and track quarantine

**Implements:** TST-9 · **Gate:** pytest collection and review (code-reviewer)

**Statement**: Fix or quarantine a flaky case. Use a registered quarantine
marker whose reason names its owner and Linear issue. Record the
fix-or-remove deadline in that issue. Reserve skips for cases that do not
apply to a platform or configuration; do not use skip or expected failure
to hide a failure in a required case.

**Criterion**:

- Pass: quarantined cases have a reason, issue, owner and deadline
- Fail: a flaky required test is hidden behind a skip or untracked marker

**Tooling**:

- Exclude quarantine from required runs with `-m "not quarantine"` or
  group the explicit selection before appending `and not quarantine`, for
  example `-m "(integration or e2e) and not quarantine"`. A default
  expression in pytest configuration does not cover CI commands that pass
  their own `-m`.
- List quarantines with `uv run pytest --collect-only -m quarantine`.
- Run the separate selection with `uv run pytest -m quarantine`.

## PL-TT-7: Test the live and distributed entry points

**Implements:** TST-6, TST-7, TST-8 · **Gate:** Nightly and release workflows

**Statement**: Select live and agent cases explicitly with pytest markers or paths in
trusted CI. Install the candidate wheel non-editably in a fresh environment
and invoke its installed entry point without resyncing the checkout. For
Post-release, reuse that check against the exact published version installed
from the production index.
