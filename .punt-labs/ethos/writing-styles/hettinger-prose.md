# Hettinger Prose

Technical writing in the style of Raymond Hettinger's documentation
and PEP contributions.

## Prose

- Lead with the what, then the why, then the how
- Show the code first — explain after
- Use concrete examples, not abstract descriptions
- Short paragraphs — one idea per paragraph

## Code Comments

- Docstrings: imperative mood, one line if possible
  (`"""Return the chunks sorted by relevance."""`)
- Inline comments for non-obvious decisions, not for obvious code
- Comments explain why, not what
- No commented-out code — version control remembers

## Error Messages

- Include what failed and with what input:
  `msg = f"Unsupported format: {suffix}"`
- User-facing: lowercase, no period, no "error:" prefix
- Internal: use `logger.exception()` for full traceback context
- Carry context through the call chain: `raise ValueError(msg) from exc`

## Naming

- Snake_case for everything except classes (PascalCase)
- Short for locals: `db`, `conn`, `path`, `doc`, `chunk`
- Descriptive for public API: `ingest_document`, `resolve_db_paths`
- Boolean variables read as assertions: `is_indexed`, `has_content`
- Constants: `UPPER_SNAKE_CASE`
- Private: single underscore prefix, never double

## Structure

- Module docstring: one sentence describing the module's purpose
- Imports: `from __future__ import annotations` first, then stdlib,
  third-party, local — each group separated by a blank line
- Public functions before private helpers
- Related functions grouped together, not alphabetized
- One class per file when the class is the module's purpose
