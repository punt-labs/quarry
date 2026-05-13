# Copilot Code Review Instructions

## What to focus on

- Logic errors, off-by-one, null/None paths, unhandled edge cases
- Security: injection, credential exposure, path traversal, unsafe deserialization
- API contract violations: function returns wrong type, missing error case, changed signature without updating callers
- Concurrency issues: race conditions, shared mutable state, missing locks
- Resource leaks: unclosed files, sockets, database connections

## What NOT to flag

- OO structure scores (method_ratio, encapsulation, module_size, etc.) — `tools/oo_score.py` enforces these via a ratchet mechanism. Do not duplicate its job.
- New files that don't meet absolute OO thresholds — this is expected. The ratchet applies on subsequent changes.
- Formatting — `ruff format` handles this. Do not comment on whitespace, line length, quote style, or trailing commas.
- Import order — `ruff check --select I` handles this.

## Do NOT suggest

- Adding `# noqa`, `# type: ignore`, `# pylint: disable`, or `@pytest.mark.xfail` to suppress errors. The project policy is: fix the code or escalate to the operator. Suppressions require explicit operator approval.
- Lowering `max-complexity`, relaxing type checker strictness, or widening per-file-ignores.
- Replacing `__new__` with `__init__` — this project uses `__new__` as the constructor by design.

## Context

This project uses an OO ratchet: `make check-oo` compares OO quality scores against a committed baseline (`.oo-baseline.json`). Every commit must improve at least one metric on touched files and regress none. The baseline is updated via `make update-oo` and committed alongside the code change.
