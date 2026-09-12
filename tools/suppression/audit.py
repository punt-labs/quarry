"""The append-only audit log of suppression baseline verdicts."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Self


class SuppressionAuditError(Exception):
    """The ``.suppression-audit.jsonl`` log could not be parsed.

    Raised instead of letting a ``json.JSONDecodeError`` escape
    ``relaxations_since``, so a corrupt or hand-broken audit log becomes a
    controlled non-zero outcome (the CLI catches it) rather than a traceback.
    """


class SuppressionAudit:
    """Append and query ``.suppression-audit.jsonl`` -- the ratchet's trail."""

    _path: Path

    FILENAME: str = ".suppression-audit.jsonl"

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._path = root / cls.FILENAME
        return self

    @property
    def path(self) -> Path:
        """Return the on-disk audit log path."""
        return self._path

    def append(
        self,
        *,
        total: int,
        by_category: dict[str, int],
        verdict: str,
        deltas: dict[str, dict[str, list[int]]],
        commit: str | None,
        source: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Append one verdict entry, recording its source (PR/bead ref).

        ``reason`` carries the human justification for a ``relaxed`` verdict;
        it is an audit marker, not an enforcement gate.
        """
        entry = {
            "ts": self._now(),
            "commit": commit,
            "source": source,
            "verdict": verdict,
            "reason": reason,
            "deltas": deltas,
            "total": total,
            "by_category": by_category,
        }
        with self._path.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def relaxations_since(self, base_text: str | None) -> frozenset[str]:
        """Return files relaxed by the *current* change only.

        A relaxation counts only when its audit entry is absent from the base
        commit's audit log (``base_text``) -- this scopes the waiver to the
        change under review, so a historical relaxation cannot bless a fresh
        increase re-locked via ``--update``. Entries are matched *structurally*
        (canonical JSON) so a reformat of the base log does not make a base
        relaxation look new.
        """
        base_keys = self._canonical_set(base_text)
        files: set[str] = set()
        for line in self._raw_lines():
            entry = self._parse(line)
            if self._canonical(entry) in base_keys:
                continue
            if entry.get("verdict") != "relaxed":
                continue
            deltas = entry.get("deltas")
            if isinstance(deltas, dict):
                files.update(deltas)
        return frozenset(files)

    @classmethod
    def _canonical_set(cls, base_text: str | None) -> frozenset[str]:
        if not base_text:
            return frozenset()
        return frozenset(
            cls._canonical(cls._parse(line))
            for line in base_text.splitlines()
            if line.strip()
        )

    @staticmethod
    def _canonical(entry: dict[str, object]) -> str:
        """Return a formatting-independent identity for an audit entry."""
        return json.dumps(entry, sort_keys=True)

    @staticmethod
    def _parse(line: str) -> dict[str, object]:
        """Parse one audit line, or raise ``SuppressionAuditError`` naming it."""
        try:
            parsed: dict[str, object] = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"malformed suppression audit entry {line[:80]!r}: {exc}"
            raise SuppressionAuditError(msg) from exc
        return parsed

    def _raw_lines(self) -> list[str]:
        if not self._path.exists():
            return []
        return [ln for ln in self._path.read_text().splitlines() if ln.strip()]

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
