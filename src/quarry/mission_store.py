"""Read every mission under a repo's ``.punt-labs/ethos/missions/`` tree."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

import yaml

from quarry.ethos_tree import EthosTree
from quarry.mission_records import MissionContract, MissionRound
from quarry.mission_round_parts import EvaluatorReflection, WorkerResult

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

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

    A directory that fails to parse (malformed YAML, a missing ``worker``, a
    non-integer ``round``) becomes one error line and the scan continues —
    one bad mission must not hide the rest.
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
        """Parse every mission directory (or just *mission_id*), collecting errors."""
        missions: list[MissionRecord] = []
        errors: list[str] = []
        for mission_dir in self._mission_dirs(mission_id):
            try:
                missions.append(self._load(mission_dir))
            except (OSError, yaml.YAMLError, ValueError) as exc:
                errors.append(f"{mission_dir}: {exc}")
        return MissionScan(missions=tuple(missions), errors=tuple(errors))

    def _mission_dirs(self, mission_id: str) -> Iterator[Path]:
        if mission_id:
            candidate = self._missions_dir / mission_id
            if candidate.is_dir():
                yield candidate
            return
        yield from sorted(p for p in self._missions_dir.iterdir() if p.is_dir())

    def _load(self, mission_dir: Path) -> MissionRecord:
        contract_data = self._read(mission_dir / "contract.yaml")
        if not isinstance(contract_data, dict):
            msg = "contract.yaml is not a mapping"
            raise ValueError(msg)
        contract = MissionContract.from_mapping(
            contract_data, default_repo=self._default_repo
        )
        results = {
            r.round: r
            for r in map(
                WorkerResult.from_mapping,
                self._entries(mission_dir / "results.yaml", "results"),
            )
        }
        reflections = {
            r.round: r
            for r in map(
                EvaluatorReflection.from_mapping,
                self._entries(mission_dir / "reflections.yaml", "reflections"),
            )
        }
        numbers = sorted(results.keys() | reflections.keys())
        rounds = tuple(
            MissionRound(n, results.get(n), reflections.get(n)) for n in numbers
        )
        return MissionRecord(contract=contract, rounds=rounds)

    def _entries(self, path: Path, key: str) -> list[Mapping[str, object]]:
        """Return the per-round list under *key*; an absent file is an empty list."""
        if not path.is_file():
            return []
        data = self._read(path)
        entries = data.get(key) if isinstance(data, dict) else None
        if entries is None:
            return []
        if not isinstance(entries, list):
            msg = f"{path.name}: {key!r} is not a list"
            raise ValueError(msg)
        # YAML is a wire boundary: each entry is checked by the record parser.
        return [e for e in entries if isinstance(e, dict)]

    @staticmethod
    def _read(path: Path) -> object:
        return yaml.safe_load(path.read_text())
