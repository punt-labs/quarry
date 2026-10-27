"""Git diff result value objects: touched-file sets and line-level content.

Split out of ``gitio.py`` (which owns *running* git commands) so the parsed
result shapes own their own parsing: ``Diff.from_name_status`` and
``LineChange.parse_patch`` are alternative constructors (PY-CC-5) for the two
distinct ``git diff`` output formats the ratchet reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self


@dataclass(frozen=True, slots=True)
class Diff:
    """Files changed across a commit range, with rename provenance.

    ``renames`` maps a new path to the old path it was renamed from, so a
    renamed file can inherit its predecessor's baseline entry (S8).
    """

    touched: frozenset[str]
    renames: dict[str, str] = field(default_factory=dict)

    def python_files(self) -> frozenset[str]:
        """Return the touched paths that are Python modules."""
        return frozenset(p for p in self.touched if p.endswith(".py"))

    @classmethod
    def from_name_status(cls, out: str) -> Self:
        """Parse ``git diff --name-status -M`` output into a ``Diff``."""
        touched: set[str] = set()
        renames: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            if status.startswith(("R", "C")) and len(parts) >= 3:
                old, new = parts[1], parts[2]
                touched.add(new)
                if status.startswith("R"):
                    renames[new] = old
            elif len(parts) >= 2:
                touched.add(parts[1])
        return cls(frozenset(touched), renames)


@dataclass(frozen=True, slots=True)
class LineChange:
    """The raw content lines added to and removed from one touched path.

    Lines keep their diff order: docstring detection needs to see an opening
    delimiter before the prose it encloses.

    Feeds ``provenance.ExtractionDetector``: a new file's ``added`` lines can
    be compared against a baselined origin's ``removed`` lines from the same
    diff to prove (or disprove) that the new file is an honest extraction.
    """

    removed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()

    @staticmethod
    def parse_patch(patch: str) -> dict[str, LineChange]:
        """Parse a ``--unified=0`` patch into per-path added/removed lines."""
        by_path = LineChange._group_hunk_lines(patch)
        return {path: LineChange._classify(lines) for path, lines in by_path.items()}

    @staticmethod
    def _group_hunk_lines(patch: str) -> dict[str, list[str]]:
        """Group every hunk content line (with its ``+``/``-`` prefix) by path.

        The ``+++ `` header names the path every following content line
        belongs to, until the next header; ``--- `` and ``@@`` structural
        lines carry no content and are skipped.
        """
        by_path: dict[str, list[str]] = {}
        current: str | None = None
        for line in patch.splitlines():
            if line.startswith("+++ "):
                current = LineChange._target_path(line)
                if current is not None:
                    by_path.setdefault(current, [])
                continue
            if current is None or line.startswith(("--- ", "@@")):
                continue
            by_path[current].append(line)
        return by_path

    @staticmethod
    def _target_path(header: str) -> str | None:
        """Return the post-image path from a ``+++ `` header line.

        ``None`` for ``+++ /dev/null`` -- the file was deleted, so it has no
        post-image path to attribute content lines to.
        """
        raw = header[4:]
        return None if raw == "/dev/null" else raw.removeprefix("b/")

    @staticmethod
    def _classify(lines: list[str]) -> LineChange:
        """Split one path's raw hunk lines into added and removed content."""
        added = [line[1:] for line in lines if line.startswith("+")]
        removed = [line[1:] for line in lines if line.startswith("-")]
        return LineChange(removed=tuple(removed), added=tuple(added))
