"""Read every mission under a repo's ``.punt-labs/ethos/missions/`` tree."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

import yaml

from quarry.ethos_tree import EthosTree
from quarry.mission_directory import MissionDirectory

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from quarry.mission_records import MissionContract, MissionRound

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class MissionRecord:
    """A parsed mission directory: its contract and every numbered round."""

    contract: MissionContract
    rounds: tuple[MissionRound, ...]

    def frozen_rounds(self) -> tuple[MissionRound, ...]:
        """Return the rounds ethos can no longer append to."""
        return tuple(r for r in self.rounds if r.is_frozen(self.contract))


@final
@dataclass(frozen=True, slots=True)
class MissionScan:
    """Every mission that parsed, and one line per directory that did not."""

    missions: tuple[MissionRecord, ...]
    errors: tuple[str, ...]


@final
class MissionStore:
    """The on-disk mission tree, read one directory at a time.

    A directory with no ``contract.yaml`` is not a mission — a delegation log
    left by another checkout, or a partially sealed tree — and the scan passes
    it by without an error line. A directory that fails to parse (malformed
    YAML, a missing ``worker``, a non-integer ``round``) becomes one error line
    and the scan continues — one bad mission must not hide the rest.
    """

    __slots__ = ("_default_repo", "_missions_dir")

    _missions_dir: Path
    _default_repo: Path

    def __new__(cls, missions_dir: Path, *, default_repo: Path) -> Self:
        self = super().__new__(cls)
        self._missions_dir = missions_dir
        self._default_repo = default_repo
        return self

    @classmethod
    def for_repo(cls, cwd: Path) -> Self | None:
        """Return the store for the checkout containing *cwd*, or ``None``.

        ``None`` is the documented "this repo runs no missions" outcome: the
        legacy flat layout under ``~/.punt-labs/ethos/missions/`` is out of
        scope, and a repo without the sidecar tree is ordinary.
        """
        missions_dir = EthosTree(cwd).missions_dir()
        if missions_dir is None:
            logger.info("missions: no .punt-labs/ethos/missions/ above %s", cwd)
            return None
        # <repo>/.punt-labs/ethos/missions -> <repo>
        return cls(missions_dir, default_repo=missions_dir.parents[2])

    def scan(self, mission_id: str = "") -> MissionScan:
        """Parse every mission directory (or just *mission_id*), collecting errors.

        A *mission_id* that names no directory is one error, not an empty
        scan: the caller asked for something specific and must hear that it
        is not there, so the CLI exits 1 rather than reporting "filed 0". A
        directory that exists but holds no contract is the ordinary skip.
        """
        if not mission_id:
            return self._scan(
                sorted(p for p in self._missions_dir.iterdir() if p.is_dir())
            )
        target = self._missions_dir / mission_id
        if not target.is_dir():
            msg = f"mission {mission_id} not found under {self._missions_dir}"
            return MissionScan(missions=(), errors=(msg,))
        return self._scan((target,))

    def _scan(self, mission_dirs: Iterable[Path]) -> MissionScan:
        """Load each mission directory; one that fails to parse is one error line."""
        missions: list[MissionRecord] = []
        errors: list[str] = []
        for mission_dir in map(MissionDirectory, mission_dirs):
            if not mission_dir.has_contract():
                logger.info(
                    "missions: %s has no contract.yaml; skipped", mission_dir.path
                )
                continue
            try:
                missions.append(self._load(mission_dir))
            except (OSError, yaml.YAMLError, ValueError) as exc:
                errors.append(f"{mission_dir.path}: {exc}")
        return MissionScan(missions=tuple(missions), errors=tuple(errors))

    def _load(self, mission_dir: MissionDirectory) -> MissionRecord:
        return MissionRecord(
            contract=mission_dir.contract(self._default_repo),
            rounds=mission_dir.rounds(),
        )
