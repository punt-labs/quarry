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

# Bodies that carry no moved logic: a copied signature over one of these is a
# stub, not an extracted function.
_TRIVIAL_LINES: frozenset[str] = frozenset({"pass", "...", "return None"})

_DOCSTRING_DELIMITERS = ('"""', "'''")
_DEF_PREFIXES = ("def ", "async def ")


class _CodeLineFilter:
    """Keep only lines that are moved logic, not module boilerplate.

    Stateful over one ordered pass: docstrings and parenthesised imports span
    lines, so whether a line is code depends on the lines before it.
    """

    _docstring_delimiter: str | None
    _in_import_parens: bool

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._docstring_delimiter = None
        self._in_import_parens = False
        return self

    def keeps(self, line: str) -> bool:
        """Return whether ``line`` is substantive code; advance the state."""
        stripped = line.strip()
        if self._in_docstring(stripped) or self._in_import(stripped):
            return False
        if stripped.startswith("#") or stripped in _TRIVIAL_LINES:
            return False
        return len(stripped) >= _MIN_SUBSTANTIVE_LEN

    def _in_docstring(self, stripped: str) -> bool:
        """Return whether ``stripped`` is part of a docstring."""
        if self._docstring_delimiter is not None:
            if self._docstring_delimiter in stripped:
                self._docstring_delimiter = None
            return True
        for delimiter in _DOCSTRING_DELIMITERS:
            if stripped.startswith(delimiter):
                # One delimiter opens a block; two on a line is a one-liner.
                if stripped.count(delimiter) == 1:
                    self._docstring_delimiter = delimiter
                return True
        return False

    def _in_import(self, stripped: str) -> bool:
        """Return whether ``stripped`` is part of an import statement."""
        if self._in_import_parens:
            self._in_import_parens = ")" not in stripped
            return True
        if not stripped.startswith(("import ", "from ")):
            return False
        self._in_import_parens = stripped.endswith("(")
        return True


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

        Imports, comments, docstrings and trivial bodies are module setup,
        not moved logic, so they are dropped. Shared by every caller that
        builds a line set for overlap comparison (removed lines from a
        candidate origin, added lines of a new file) so both sides of
        ``origin_of``'s comparison are normalized identically. ``lines`` must
        be in diff order.
        """
        code = _CodeLineFilter()
        return frozenset(line.strip() for line in lines if code.keeps(line))

    def origin_of(self, new_lines: frozenset[str]) -> str | None:
        """Return the origin path whose removed lines best explain ``new_lines``.

        ``None`` when no candidate origin explains at least
        ``_OVERLAP_THRESHOLD`` of the new file's substantive lines, or when
        the overlap holds no function moved with its body (a ``def`` line and
        at least one non-``def`` line) -- the new file is then scored as
        genuinely new, with no carve-out.
        """
        if not new_lines:
            return None
        best_path: str | None = None
        best_overlap = 0.0
        for path, removed in self._removed_by_path.items():
            if not removed:
                continue
            shared = new_lines & removed
            if not self._moved_a_function(shared):
                continue
            overlap = len(shared) / len(new_lines)
            if overlap > best_overlap:
                best_overlap = overlap
                best_path = path
        return best_path if best_overlap >= _OVERLAP_THRESHOLD else None

    @staticmethod
    def _moved_a_function(shared: frozenset[str]) -> bool:
        """Return whether ``shared`` holds a ``def`` line and a body line."""
        has_def = any(line.startswith(_DEF_PREFIXES) for line in shared)
        has_body = any(not line.startswith(_DEF_PREFIXES) for line in shared)
        return has_def and has_body
