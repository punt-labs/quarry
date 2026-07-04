# Quarry Documentation

Index of the `docs/` tree. `DESIGN.md` (repo root) is the ADR source of truth (DES-001+);
this directory holds reference material, active design work, and archived process artifacts.

## Reference (authoritative, kept current)

- **`architecture.tex` → `architecture.pdf`** — full system architecture: modules, search and
  retrieval, embedding/provider, deployment. The consolidation target every archived design
  points to (DES-012).
- **`claude-code-quarry.tex` → `.pdf`** — standalone whitepaper on the Claude Code integration.
- **`tex/`** — LaTeX build support (`fuzz.sty`, MetaFont) for `make docs`.

## Active work

- **`retrieval-quality-improvements.md`** — 2026-07 research synthesis (turbopuffer / reranker /
  late-chunking) plus the proposed direction and eval-harness plan for fixing token-density
  ranking. Not yet implemented.

## Operations

- **`smoke-test.md`** — post-release manual smoke test: 14 MCP + 17 CLI + 7 enable/disable
  checks, plus install verification. Run after every release.

## Archive (`archive/`)

Completed build-plans, design reviews, and superseded designs — preserved for history, not
maintained. Each maps to a settled ADR in `DESIGN.md`. Do not treat as current.

| Archived doc | Feature | ADR |
|---|---|---|
| `async-ops.md` | HTTP async task model | DES-001 |
| `provider-detection-design.md`, `provider-detection-review.md`, `build-plan-provider-detection.md` | ONNX provider auto-detection | DES-016 |
| `build-plan-remote-cli-parity.md` | Remote CLI routing | DES-021 |
| `cli-logging-ux.md`, `cli-logging-impl.md` | CLI logging / verbosity | DES-028 |
| `prfaq-quarry-enable.md`, `quarry-enable-impl.md` | `quarry enable` / `disable` | DES-029 |
| `sync-concurrency-fix.md` | Concurrent-sync guard (batch-write portion superseded by DES-034) | DES-026 |
| `testing-legacy.md` | Older testing strategy (now in CLAUDE.md) | — |
| `oo-refactoring/` | Completed OO-refactoring initiative | — |
