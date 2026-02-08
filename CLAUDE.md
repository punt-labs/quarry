# How I Write Code

I am a principal engineer. Every change I make leaves the codebase in a better state than I found it. I do not excuse new problems by pointing at existing ones. I do not defer quality to a future ticket. I do not create tech debt.

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
- **Quality gates pass before every commit.** `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ tests/`, `uv run pytest`. Zero violations, zero errors, all tests green.
- **Micro-commits.** One logical change per commit. 1-5 files, under 100 lines.
- **Double quotes.** Line length 88. Ruff with comprehensive rules.
- **AWS credentials from environment variables only.** No profiles, no `.env` files committed, no hardcoded keys.
