---
applyTo: "**/*.py"
---

# Python Review Rules

## Construction

- `__new__` is the constructor, not `__init__`. Flag any `__init__` definition outside of `@dataclass` classes.
- Constructors must establish all invariants before returning. No partially-constructed objects.
- `@dataclass` must use `frozen=True, slots=True`.

## Encapsulation

- All instance attributes must start with `_` (protected) or `__` (private). Flag `self.name =` without underscore.
- Expose state via `@property`, not public attributes.

## Types

- `from __future__ import annotations` must be the first import in every file.
- Full type annotations on all function signatures and return types.
- Use `Protocol` for structural interfaces, `ABC` for shared implementation. Never `hasattr()`.
- Return `NotImplemented` from binary operators, never raise `NotImplementedError`.
- `cast()` must use string form: `cast("Type", val)`.
- Import abstract types from `collections.abc`, not `typing`.

## Error handling

- Validate at boundaries, trust internally. No defensive try/except in internal code.
- Never catch broad `Exception` except at CLI entry points or MCP tool handlers.
- `ValueError` for domain violations, `TypeError` for construction bypass.

## Style

- `ruff format` is the formatter (not `black`).
- Double quotes, 88-char line length.
- No backwards-compatibility shims, no `# removed` tombstones.

## Tools directory

- `tools/oo_score.py` is intentionally complex (subprocess calls to git, large check/update methods). Do not flag C901, S603, or S607 on files in `tools/`.
