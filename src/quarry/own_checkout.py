"""Whether a filesystem root is quarry's OWN checkout, by package identity.

Shared by :mod:`quarry.enablement` (gates a destructive retraction of the
operator's cross-harness skill deposits on ``disable``) and
:mod:`quarry.skills_install` (gates the source an ``install`` deposits
FROM) — both need the same "is this really quarry, not just a repo shaped
like it" answer, so the check lives once rather than drifting into two
almost-identical copies.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Final, Self, final

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["OWN_PACKAGE_NAME", "OwnCheckout"]

# The PyPI/pyproject name that identifies quarry's OWN checkout (PL-PL-2).
# Shape alone (a ``plugin/skills/`` tree) proves nothing -- every
# marketplace-layout Claude Code plugin (lux, prfaq, dungeon, punt-kit, ...)
# ships an identically-shaped tree.
OWN_PACKAGE_NAME: Final = "punt-quarry"


@final
class OwnCheckout:
    """Verify whether *root* is quarry's OWN checkout, per its ``pyproject.toml``."""

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    def confirmed(self) -> bool:
        """Return whether the root names quarry's package.

        Never raises: a missing or unreadable file, invalid UTF-8, malformed
        TOML, or one missing a ``[project]`` table or ``name`` key, all mean
        "not provably quarry's own checkout" (PY-EH-1) -- the safe default
        for a guard that gates a destructive or trust-sensitive filesystem
        operation. ``ValueError`` subsumes both ``tomllib.TOMLDecodeError``
        and the ``UnicodeDecodeError`` that ``read_text(encoding="utf-8")``
        raises on invalid UTF-8 bytes.
        """
        pyproject = self._root / "pyproject.toml"
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            name = data["project"]["name"]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return isinstance(name, str) and name == OWN_PACKAGE_NAME
