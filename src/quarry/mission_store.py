"""Read every mission under a repo's ``.punt-labs/ethos/missions/`` tree."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, final

import yaml

from quarry.ethos_tree import EthosTree
from quarry.mission_directory import MissionDirectory
from quarry.path_guard import SealedTree, SealedTreeError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from quarry.mission_records import MissionContract, MissionRound

logger = logging.getLogger(__name__)

# ``--mission`` is user input. It is matched against the id ethos mints
# (``m-YYYY-MM-DD-NNN``) before it is ever joined onto the missions root, so a
# separator, ``..``, or an absolute path can never become a directory to read.
_MISSION_ID_PATTERN: Final = re.compile(r"m-\d{4}-\d{2}-\d{2}-\d{3,}")


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

    The tree is cloned content, so it is read as a sealed tree rooted at the
    checkout: a directory or file reached through a symlink is refused, never
    followed, because a committed link could point the sync at another
    checkout's YAML and have it POSTed to the daemon as this repo's round.

    A directory with no ``contract.yaml`` is not a mission — a delegation log
    left by another checkout, or a partially sealed tree — and the scan passes
    it by without an error line. A directory that fails to parse (malformed
    YAML, a missing ``worker``, a non-integer ``round``) becomes one error line
    and the scan continues — one bad mission must not hide the rest.
    """

    __slots__ = ("_default_repo", "_missions_dir", "_seal")

    _missions_dir: Path
    _default_repo: Path
    _seal: SealedTree

    def __new__(cls, missions_dir: Path, *, default_repo: Path) -> Self:
        self = super().__new__(cls)
        self._missions_dir = missions_dir
        self._default_repo = default_repo
        self._seal = SealedTree(default_repo)
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
        return cls(missions_dir, default_repo=EthosTree.checkout_root(missions_dir))

    def scan(self, mission_id: str = "") -> MissionScan:
        """Parse every mission directory (or just *mission_id*), collecting errors.

        A *mission_id* that names no directory is one error, not an empty
        scan: the caller asked for something specific and must hear that it
        is not there, so the CLI exits 1 rather than reporting "filed 0". So
        is an id that is not a mission id at all, or one whose directory is
        reached through a symlink. A directory that exists but holds no
        contract is the ordinary skip.
        """
        if not mission_id:
            return self._scan(self._mission_dirs())
        try:
            target = self._locate(mission_id)
        except (OSError, ValueError) as exc:
            return MissionScan(missions=(), errors=(str(exc),))
        return self._scan((target,))

    def _mission_dirs(self) -> list[Path]:
        """Return every sealed subdirectory of the missions root, sorted.

        A symlinked entry is skipped with a warning: ethos never writes one
        here, and following it would read another tree's files as this repo's.
        """
        dirs: list[Path] = []
        for entry in sorted(self._missions_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                dirs.append(self._seal.check(entry))
            except SealedTreeError as exc:
                logger.warning("missions: %s; skipped", exc)
        return dirs

    def _locate(self, mission_id: str) -> Path:
        """Return the directory *mission_id* names, or raise.

        ``ValueError`` for an id that is not the slug ethos mints — it is never
        joined onto the root — and for an id whose directory is absent;
        :class:`SealedTreeError` when the directory is reached through a symlink.
        """
        if _MISSION_ID_PATTERN.fullmatch(mission_id) is None:
            msg = f"invalid mission id: {mission_id!r}"
            raise ValueError(msg)
        target = self._seal.check(self._missions_dir / mission_id)
        if not target.is_dir():
            msg = f"mission {mission_id} not found under {self._missions_dir}"
            raise ValueError(msg)
        return target

    def _scan(self, mission_dirs: Iterable[Path]) -> MissionScan:
        """Load each mission directory; one that fails to parse is one error line."""
        missions: list[MissionRecord] = []
        errors: list[str] = []
        for path in mission_dirs:
            mission_dir = MissionDirectory(path, guard=self._seal)
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
