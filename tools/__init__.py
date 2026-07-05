"""Developer tooling for quarry (not shipped in the wheel).

Making ``tools`` a package lets the type checkers and pytest resolve
``tools.eval`` — the retrieval evaluation harness — as an importable module.
"""

from __future__ import annotations

__all__: list[str] = []
