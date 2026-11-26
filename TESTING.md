# Testing Quarry

This guide applies Punt Kit's [Testing standard][standard] and [Python rules][python-rules].
Use Python 3.13 and uv. Provision dependencies before measuring a warm gate.
Run `make check` before opening a PR; `make check-full` also exercises a wheel.

## Trigger map (TST-1, TST-3)

| Trigger | Entry point and runner | Budget target | Result |
| --- | --- | --- | --- |
| Pre-commit | `make test-changed` on the developer machine | <30 s for a typical change | Blocks commit when the hook is installed |
| Pre-push | `make test-push` on the developer machine | <3 min | Blocks push when the hook is installed |
| PR | [Test workflow][test-ci]: default pytest with coverage on Ubuntu; `make test-wheel` on Linux and Apple Silicon macOS | <10 min wall time | Required `test` gate |
| Merge to main | [Test workflow][test-ci] on the main SHA | <10 min wall time | Repeat PR gate; retain SHA-keyed coverage |
| Nightly | [Nightly QA][nightly-ci] on upstream main, Ubuntu | 30 min check job; 5 min notification job | Advisory for PRs; red run and Biff alert on failure |
| Release candidate | [Release workflow][release-ci] on Ubuntu: `QUARRY_SMOKE_LIVE=1 bash scripts/smoke-release.sh candidate VERSION WHEEL MCPB` | 5 min smoke timeout | Test the built wheel, bundle and HTTPS fetch before promotion; verify TestPyPI serves the tested wheel |
| Post-release | [Release workflow][release-ci] on Ubuntu: `bash scripts/smoke-release.sh published VERSION [MCPB]` | <2 min target; 5 min PyPI timeout | Verify the PyPI release before attaching the tested MCPB, then verify the downloaded asset |

Install the versioned hooks with `make hooks`. This refuses to change an existing
custom hook path; chain the Quarry test hooks from those hooks instead.
The versioned hooks preserve the default `commit-msg` hook, including Ethos
trailers, unless Ethos already handles trailers in the active hook.
Pre-commit uses staged path names to select tests from the working tree. Stage
and test the same changes; avoid partial staging when relying on this result.
Shared fixtures/configuration and unmapped source files fall back to the default
suite. Pre-push runs the default suite plus fast real hook entry-point tests.
A warm pre-push run took 5m47s in the rollout Linux VM. This exceeds the target:
profile the slow tests rather than adding workers (DES-047).
A warm one-file test change took 2.2 s in the rollout Linux VM; the shared-file
fallback runs the whole default suite and may exceed the small-change target.

Wheel and release smokes use curl and require network access to provision public
packages and the pinned model. The installed daemon runs offline on an ephemeral
local port; the candidate job also explicitly runs the public HTTPS canary.
Supply `QUARRY_SMOKE_MODEL_CACHE` as an absolute path to an already provisioned
Hub cache for a local smoke. See the [manual plugin smoke guide][manual-smoke]
for operator-level MCP, CLI, and install checks.

## Select a suite (TST-2)

| Suite | Selector | Prerequisites and scope |
| --- | --- | --- |
| Default | `make test` | Dev extra; `jq` on Linux for handoff release tests; unit, local component and deterministic domain guards |
| Unit | `make test-unit` or an in-process pytest path | Fast client and target contract sample; no running daemon |
| Subprocess | `make test-subprocess-fast`; `make test-subprocess` | Fast hook commands need no model; full hook suite uses the pinned ONNX model and test-owned daemon |
| E2E | `make test-e2e` | Installed wheel and real CLI indexing/retrieval; public dependencies and the pinned ONNX model |
| Slow and embedding | `make test-slow`; `make test-embedding` | Local TLS, socket and real model tests; provision the model and install dev and eval extras |
| Domain/retrieval | `make test-retrieval-eval`; `uv run --extra dev pytest -m resource` | Eval extra for ranking; default resource guards exercise local lifetime invariants |
| Live | `make test-live` | Authorized public HTTPS fetch, no credentials; not a PR test |
| Agent | `make test-agent` | Authorized run with scoped `ANTHROPIC_API_KEY`, Claude Code, CPU model, and a fresh Quarry daemon |
| Quarantine | `make test-quarantine`; `uv run --extra dev pytest -m quarantine` | List marks, or run selected cases with their own prerequisites |

Provision the pinned model with
`uv run --extra dev python -c 'from quarry.embeddings import OnnxEmbeddingBackend; OnnxEmbeddingBackend.download_model_files()'`.
Then run broader offline checks with `make test-nightly`. Model-backed suites
reuse `HF_HOME` or the provisioned cache under the operator's cache directory.
Add `and not quarantine` to any hand-written marker expression for supported suites.

## Isolation and coverage (TST-4, TST-5)

Default pytest replaces HOME, Quarry state, DNS and ONNX where applicable.
Loopback and daemon tests use private state and test-owned ports. Installed-wheel
tests use a clean virtual environment and runtime state; provisioning may fetch
public assets but the running daemon does not need external credentials.

The Test workflow's `make coverage-ci` publishes line and branch XML for
`src/quarry` executed in the default pytest process. It does not count Python
running in children or wheel environments, shell scripts, or opt-in suites.
Successful main results are keyed by the main commit. Use `make coverage-ci`
locally for the same scope.

## Trusted tests and quarantines (TST-6, TST-9)

The upstream-main Nightly job uses the provider key only in the agent step.
Claude Code gets a fresh configuration, a synthetic document, one permitted MCP
tool, and time/turn/spend limits. The MCP child receives no provider key.
Do not enable trusted tests on a PR or pass personal credentials to them.

Quarantine a confirmed flaky test, not an entire optional tier. Register
`@pytest.mark.quarantine(issue="PL-1234", owner="Name", expires="YYYY-MM-DD")`.
The expiry must be within 14 days; review the list on Nightly and fix or remove
the case by expiry. An expired or malformed mark fails collection.

## Open deviations (TST-7, TST-8, TST-10)

Candidate and published checks verify exact versions, exercise the installed
CLI and launch the bundled MCP entry point. If a check fails, inspect the
install and daemon logs, then rerun failed jobs before the build artifacts
expire. A post-publication failure does not roll back an uploaded artifact.
Nightly credential and alert delivery and real-agent recall against the exact
candidate still need evidence.
There is no browser application to exercise; CLI, HTTP, and MCP apply.

The [tracking issue][deviations] records those deviations, local/hosted budget
measurement and the child coverage scope. Owner: Quarry maintainers. Review by
2026-12-07.

[standard]: https://github.com/punt-labs/punt-kit/blob/main/standards/testing.md
[python-rules]: https://github.com/punt-labs/punt-kit/blob/main/lang-rules/python/python-testing.md
[test-ci]: .github/workflows/test.yml
[nightly-ci]: .github/workflows/nightly.yml
[release-ci]: .github/workflows/release.yml
[manual-smoke]: docs/smoke-test.md
[deviations]: https://linear.app/punt-labs/issue/PL-1419/close-quarry-testing-standard-deviations
