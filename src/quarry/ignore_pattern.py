"""Git's ignore-file pattern language: one pattern, and one directory's file of them."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Final, Self, final

logger = logging.getLogger(__name__)

_IGNORE_FILE_NAMES: Final[tuple[str, ...]] = (".gitignore", ".quarryignore")

_POSIX_CLASSES: Final[dict[str, str]] = {
    "alnum": "a-zA-Z0-9",
    "alpha": "a-zA-Z",
    "blank": " \\t",
    "cntrl": "\\x00-\\x1f\\x7f",
    "digit": "0-9",
    "graph": "!-~",
    "lower": "a-z",
    "print": " -~",
    "punct": re.escape("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"),
    "space": " \\t\\n\\r\\f\\v",
    "upper": "A-Z",
    "xdigit": "0-9A-Fa-f",
}


@final
class _Glob:
    """Translate one gitignore glob (git's ``wildmatch`` dialect) to regex source."""

    __slots__ = ("_text",)

    _text: str

    def __new__(cls, text: str) -> Self:
        """Hold *text*: a pattern stripped of ``!`` and its trailing ``/``."""
        self = super().__new__(cls)
        self._text = text
        return self

    def regex(self) -> str:
        """Return regex source that must match a whole ``/``-separated path."""
        out: list[str] = []
        index = 0
        while index < len(self._text):
            piece, index = self._token(index)
            out.append(piece)
        return "".join(out)

    def _token(self, index: int) -> tuple[str, int]:
        """Return the regex for the glob token at *index* and the next index."""
        char = self._text[index]
        if char == "\\":
            if index + 1 >= len(self._text):
                msg = f"pattern ends with a backslash: {self._text!r}"
                raise ValueError(msg)
            return re.escape(self._text[index + 1]), index + 2
        if char == "*":
            return self._stars(index)
        if char == "?":
            return "[^/]", index + 1
        if char == "[":
            return self._bracket(index)
        return re.escape(char), index + 1

    def _stars(self, index: int) -> tuple[str, int]:
        """Translate a run of ``*``; a segment of exactly ``**`` spans directories."""
        end = index
        while end < len(self._text) and self._text[end] == "*":
            end += 1
        starts_segment = index == 0 or self._text[index - 1] == "/"
        if end - index >= 2 and starts_segment:
            if end == len(self._text):
                return ".+", end
            if self._text[end] == "/":
                return "(?:.*/)?", end + 1
        return "[^/]*", end

    def _bracket(self, index: int) -> tuple[str, int]:
        """Translate a ``[...]`` class; an unterminated ``[`` is a literal."""
        pos = index + 1
        negated = pos < len(self._text) and self._text[pos] in "!^"
        if negated:
            pos += 1
        body: list[str] = []
        first = True
        while pos < len(self._text):
            char = self._text[pos]
            if char == "]" and not first:
                return f"(?!/)[{'^' if negated else ''}{''.join(body)}]", pos + 1
            piece, pos = self._class_item(pos)
            body.append(piece)
            first = False
        return re.escape("["), index + 1

    def _class_item(self, pos: int) -> tuple[str, int]:
        """Return one class member (character, range endpoint or POSIX class)."""
        text = self._text
        if text.startswith("[:", pos):
            close = text.find(":]", pos + 2)
            if close != -1 and text[pos + 2 : close] in _POSIX_CLASSES:
                return _POSIX_CLASSES[text[pos + 2 : close]], close + 2
        if text[pos] == "\\" and pos + 1 < len(text):
            return re.escape(text[pos + 1]), pos + 2
        if text[pos] == "-":
            return "-", pos + 1
        return re.escape(text[pos]), pos + 1


@final
class IgnorePattern:
    """One line of an ignore file, matched against a path relative to that file."""

    __slots__ = ("_dir_only", "_negated", "_regex")

    _regex: re.Pattern[str]
    _negated: bool
    _dir_only: bool

    def __new__(cls, regex: re.Pattern[str], *, negated: bool, dir_only: bool) -> Self:
        """Build from a compiled whole-path regex; use :meth:`parse` for a line."""
        self = super().__new__(cls)
        self._regex = regex
        self._negated = negated
        self._dir_only = dir_only
        return self

    @classmethod
    def parse(cls, line: str) -> IgnorePattern | None:
        """Return the pattern *line* defines, or ``None`` for a blank or comment line.

        Raises ``ValueError`` for a line git itself rejects (a trailing
        backslash).
        """
        text = line.rstrip("\r\n")
        while text.endswith(" ") and not text.endswith("\\ "):
            text = text[:-1]
        if not text or text.startswith("#"):
            return None
        negated = text.startswith("!")
        text = text.removeprefix("!")
        dir_only = text.endswith("/")
        text = text.rstrip("/")
        if not text:
            return None
        anchored = "/" in text
        body = _Glob(text.removeprefix("/")).regex()
        prefix = "" if anchored else "(?:.*/)?"
        return cls(re.compile(prefix + body), negated=negated, dir_only=dir_only)

    @property
    def negated(self) -> bool:
        """Whether a match re-includes the path instead of ignoring it."""
        return self._negated

    def matches(self, rel: str, *, is_dir: bool) -> bool:
        """Return whether the path *rel* itself (not a parent) matches."""
        if self._dir_only and not is_dir:
            return False
        return self._regex.fullmatch(rel) is not None


@final
class IgnoreFile:
    """The ordered patterns that apply to one directory's subtree."""

    __slots__ = ("_patterns",)

    _patterns: tuple[IgnorePattern, ...]

    def __new__(cls, patterns: tuple[IgnorePattern, ...]) -> Self:
        """Hold *patterns* in file order; the last matching one decides."""
        self = super().__new__(cls)
        self._patterns = patterns
        return self

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> Self:
        """Compile *lines*, skipping (and logging) any that git would reject."""
        patterns: list[IgnorePattern] = []
        for line in lines:
            try:
                pattern = IgnorePattern.parse(line)
            except ValueError as exc:
                logger.warning("Skipping invalid ignore pattern %r: %s", line, exc)
                continue
            if pattern is not None:
                patterns.append(pattern)
        return cls(tuple(patterns))

    @classmethod
    def load(cls, directory: Path, defaults: Iterable[str] = ()) -> IgnoreFile | None:
        """Read *directory*'s ``.gitignore`` then ``.quarryignore`` after *defaults*.

        Both files apply, the later one overriding the earlier. Returns
        ``None`` when there is no line at all, so a directory without ignore
        files costs nothing to match against.
        """
        lines = list(defaults)
        for name in _IGNORE_FILE_NAMES:
            lines.extend(cls._read_lines(directory / name))
        return cls.from_lines(lines) if lines else None

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        """Return an ignore file's lines, or ``[]`` when absent/non-regular/unreadable.

        ``is_file()`` inside the ``try`` (not before it) keeps a FIFO or a symlink
        to a character device named ``.gitignore`` from blocking ``read_text()``
        forever, while the ``OSError`` guard keeps a raced deletion — present at
        the check, gone at the read — from aborting the whole caller's walk
        (bug class 1/2).  ``UnicodeDecodeError`` (a non-UTF-8 ignore file) is a
        ``ValueError``, not an ``OSError`` — caught here for the same reason.
        """
        try:
            if not path.is_file():
                return []
            return path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("Skipping unreadable ignore file %s: %s", path, exc)
            return []
        except UnicodeDecodeError as exc:
            logger.warning("Skipping non-UTF-8 ignore file %s: %s", path, exc)
            return []

    def verdict(self, rel: str, *, is_dir: bool) -> bool | None:
        """Return ``True`` to ignore *rel*, ``False`` to re-include it.

        ``None`` means no pattern matched, so a shallower file decides; the
        three states are the contract, not a failure to produce a value.
        """
        for pattern in reversed(self._patterns):
            if pattern.matches(rel, is_dir=is_dir):
                return not pattern.negated
        return None
