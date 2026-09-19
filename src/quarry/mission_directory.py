"""One mission directory on disk: the contract and per-round YAML ethos writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

import yaml

from quarry.mission_records import MissionContract, MissionRound
from quarry.mission_round_parts import EvaluatorReflection, WorkerResult
from quarry.path_guard import FOLLOW_SYMLINKS

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from quarry.path_guard import PathGuard


@final
class MissionDirectory:
    """The sidecar files under ``missions/<id>/``, read one file at a time.

    ``contract.yaml`` is what makes a directory a mission. A directory without
    one — a delegation log left by another checkout, or a partially sealed
    tree — has nothing a sync can file, and :meth:`has_contract` lets the scan
    pass it by. A contract that is present but unreadable is corruption, and
    every reader here raises so the scan records it.

    Every file read passes the *guard* first: a store scanning cloned content
    hands in a sealed tree so a symlinked ``contract.yaml`` is refused rather
    than read from wherever it points.
    """

    __slots__ = ("_guard", "_path")

    _path: Path
    _guard: PathGuard

    def __new__(cls, path: Path, *, guard: PathGuard = FOLLOW_SYMLINKS) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._guard = guard
        return self

    @property
    def path(self) -> Path:
        return self._path

    def has_contract(self) -> bool:
        """Return whether ``contract.yaml`` is present — the mark of a mission."""
        return (self._path / "contract.yaml").is_file()

    def contract(self, default_repo: Path) -> MissionContract:
        """Parse ``contract.yaml``; *default_repo* stands in for a missing ``repo:``."""
        data = self._read("contract.yaml")
        if not isinstance(data, dict):
            msg = "contract.yaml is not a mapping"
            raise ValueError(msg)
        return MissionContract.from_mapping(data, default_repo=default_repo)

    def rounds(self) -> tuple[MissionRound, ...]:
        """Pair each round's result with its reflection, in round order."""
        results = {
            r.round: r
            for r in map(
                WorkerResult.from_mapping, self._entries("results.yaml", "results")
            )
        }
        reflections = {
            r.round: r
            for r in map(
                EvaluatorReflection.from_mapping,
                self._entries("reflections.yaml", "reflections"),
            )
        }
        numbers = sorted(results.keys() | reflections.keys())
        return tuple(
            MissionRound(n, results.get(n), reflections.get(n)) for n in numbers
        )

    def _entries(self, name: str, key: str) -> list[Mapping[str, object]]:
        """Return the per-round list under *key*; an absent file is an empty list."""
        if not (self._path / name).is_file():
            return []
        data = self._read(name)
        entries = data.get(key) if isinstance(data, dict) else None
        if entries is None:
            return []
        if not isinstance(entries, list):
            msg = f"{name}: {key!r} is not a list"
            raise ValueError(msg)
        # YAML is a wire boundary: each entry is checked by the record parser.
        return [e for e in entries if isinstance(e, dict)]

    def _read(self, name: str) -> object:
        return yaml.safe_load(self._guard.check(self._path / name).read_text())
