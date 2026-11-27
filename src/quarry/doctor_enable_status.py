"""The doctor's report on whether a directory has quarry enabled."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Self, final

from quarry.collection_resolver import CollectionResolver
from quarry.results import CheckResult
from quarry.sync_registry import SyncRegistry

__all__ = ["EnableStatus"]


@final
class EnableStatus:
    """Judge a directory's enablement from its covering registration and config.

    A directory is enabled when a registration covers it (its own or a
    parent's) and its own ``config.md`` exists: the registration is shared
    across a tree, the config is per repo.
    """

    __slots__ = ("_cwd", "_registry_path")

    _registry_path: Path
    _cwd: str

    def __new__(cls, registry_path: Path, cwd: str) -> Self:
        """Bind the registry database and the directory to report on."""
        self = super().__new__(cls)
        self._registry_path = registry_path
        self._cwd = cwd
        return self

    def result(self) -> CheckResult:
        """Return the pass/fail check naming the collection and what is missing."""
        with contextlib.closing(SyncRegistry(self._registry_path)) as conn:
            covering = (
                CollectionResolver(conn).covering_registration(self._cwd)
                if self._cwd
                else None
            )
        if covering is None:
            return self._check("not enabled -- run 'quarry enable'", passed=False)
        config = Path(self._cwd) / ".punt-labs" / "quarry" / "config.md"
        parts = [
            (
                f"collection: {covering.collection}, "
                f"captures: {covering.collection}-captures"
            )
        ]
        if Path(covering.directory) != Path(self._cwd).resolve():
            parts.append(f"registration: {covering.directory}")
        if not config.is_file():
            parts.append("config.md missing (run 'quarry enable')")
        return self._check(", ".join(parts), passed=config.is_file())

    @staticmethod
    def _check(message: str, *, passed: bool) -> CheckResult:
        return CheckResult(
            name="Enable status", passed=passed, message=message, required=False
        )
