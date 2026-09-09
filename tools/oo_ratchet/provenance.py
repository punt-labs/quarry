"""Extraction provenance: distinguish honest extraction from new debt.

``compare.py`` scores a file with no baseline entry ("new") against every
metric's *absolute* threshold, because it has no prior value to hold steady
against. That is correct for a file invented from nothing. It is wrong for a
file whose content is an honest extraction out of an *already-baselined* god
module: the origin only ever had to not regress, so the extracted limb is
held to a stricter bar than the code it came from, and an author who does not
want a bare method_ratio/class_to_func_ratio failure ends up wrapping plain
functions in fake ``@staticmethod`` classes to clear the shape metrics --
exactly the PY-OO-7 "fake OO" smell the ratchet exists to catch.

PY-OO-7 already documents the carve-out this module implements: "The module
is explicitly a 'primitives' module -- a small toolkit of stateless
utilities used across many classes elsewhere... The Single Responsibility
Principle is satisfied because there are no classes in the module that the
functions *should* belong to." A module that used to be part of a baselined
file and is now a standalone zero-class module is exactly that case -- IF its
content demonstrably came from that baselined file in this diff, not merely
"any zero-class file touching any PR."

This module supplies the provenance check: does a new file's content trace
to lines actually removed from a still-present, still-baselined origin in the
same diff? Only ``method_ratio`` and ``class_to_func_ratio`` -- the two shape
metrics a plain function module cannot pass -- are ever waived, and only when
provenance is proven. Every other new-file metric (module_size, avg_params,
max_complexity, avg_complexity, encapsulation_ratio, public_attr_violations,
init_violations, future_annotations) keeps its full absolute-threshold bar:
a brand-new, fat-parameter or oversized module still fails on its own merits,
extraction or not.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Self

# The two metrics PY-OO-7's own primitives/boundary-module carve-out excuses
# for a genuine extraction. Every other metric in Thresholds.TABLE keeps
# enforcing its absolute new-file bar regardless of provenance.
SHAPE_METRICS: frozenset[str] = frozenset({"method_ratio", "class_to_func_ratio"})

# The majority of the new file's substantive lines must trace to lines
# removed from the candidate origin in the same diff -- a minority overlap is
# coincidental (shared imports, common idioms), not an extraction.
_OVERLAP_THRESHOLD = 0.5

# Lines shorter than this, once stripped, are too generic to carry
# provenance signal ("return", ")", "else:" recur in every module) and would
# inflate the overlap score without showing real extraction.
_MIN_SUBSTANTIVE_LEN = 8


class ExtractionDetector:
    """Find which baselined origin, if any, explains a new file's content."""

    _removed_by_path: dict[str, frozenset[str]]

    def __new__(cls, removed_by_path: dict[str, frozenset[str]]) -> Self:
        self = super().__new__(cls)
        self._removed_by_path = removed_by_path
        return self

    @staticmethod
    def substantive_lines(lines: Iterable[str]) -> frozenset[str]:
        """Return the stripped, non-trivial lines from ``lines``.

        Shared by every caller that builds a line set for overlap comparison
        (removed lines from a candidate origin, added lines of a new file) so
        both sides of ``origin_of``'s comparison are normalized identically.
        """
        result: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if len(stripped) >= _MIN_SUBSTANTIVE_LEN:
                result.add(stripped)
        return frozenset(result)

    def origin_of(self, new_lines: frozenset[str]) -> str | None:
        """Return the origin path whose removed lines best explain ``new_lines``.

        ``None`` when no candidate origin explains at least
        ``_OVERLAP_THRESHOLD`` of the new file's substantive lines -- the new
        file is then scored as genuinely new, with no carve-out.
        """
        if not new_lines:
            return None
        best_path: str | None = None
        best_overlap = 0.0
        for path, removed in self._removed_by_path.items():
            if not removed:
                continue
            overlap = len(new_lines & removed) / len(new_lines)
            if overlap > best_overlap:
                best_overlap = overlap
                best_path = path
        return best_path if best_overlap >= _OVERLAP_THRESHOLD else None
