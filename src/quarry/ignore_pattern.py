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
        """Translate a ``[...]`` class the way git's ``wildmatch`` reads it.

        An unescaped ``-`` after a plain character makes the next raw
        character the range's high end (a ``[`` included); after a POSIX
        class or a finished range it is a literal.  Raises ``ValueError`` for
        a class git aborts the whole match on: unterminated, or an unknown
        POSIX class name.
        """
        text = self._text
        pos = index + 1
        negated = pos < len(text) and text[pos] in "!^"
        if negated:
            pos += 1
        parts: list[str] = []
        prev = ""
        while True:
            piece, pos, prev = self._class_item(pos, prev)
            parts.append(piece)
            if pos >= len(text):
                raise self._unterminated()
            if text[pos] == "]":
                break
        return f"(?!/)[{'^' if negated else ''}{''.join(parts)}]", pos + 1

    def _class_item(self, pos: int, prev: str) -> tuple[str, int, str]:
        """Translate the class member at *pos*; return ``(regex, next index, prev)``.

        *prev* is the preceding plain character, or ``""`` after a POSIX
        class or a finished range.
        """
        text = self._text
        if pos >= len(text):
            raise self._unterminated()
        char = text[pos]
        if char == "-" and prev and pos + 1 < len(text) and text[pos + 1] != "]":
            high, pos = self._unescape(pos + 1)
            # git matched the low end literally already, so a reversed range
            # still matches it.
            piece = f"{re.escape(prev)}-{re.escape(high)}" if prev <= high else ""
            return piece, pos + 1, ""
        if char == "[" and text.startswith(":", pos + 1):
            return self._class_member(pos)
        char, pos = self._unescape(pos)
        return re.escape(char), pos + 1, char

    def _unterminated(self) -> ValueError:
        """Return the error for a bracket expression git aborts on."""
        return ValueError(f"unterminated bracket expression: {self._text!r}")

    def _unescape(self, pos: int) -> tuple[str, int]:
        """Return the character at *pos* (unescaping a backslash) and its index."""
        text = self._text
        if text[pos] == "\\":
            pos += 1
            if pos >= len(text):
                msg = f"pattern ends with a backslash: {text!r}"
                raise ValueError(msg)
        return text[pos], pos

    def _class_member(self, pos: int) -> tuple[str, int, str]:
        """Translate ``[:name:]`` at *pos*; return ``(regex, next index, prev)``.

        Anything that is not a closed ``[:...:]`` is a plain ``[``.
        """
        text = self._text
        close = text.find("]", pos + 2)
        if close == -1:
            raise self._unterminated()
        if close == pos + 2 or text[close - 1] != ":":
            return re.escape("["), pos + 1, "["
        name = text[pos + 2 : close - 1]
        if name not in _POSIX_CLASSES:
            msg = f"unknown character class [:{name}:] in {text!r}"
            raise ValueError(msg)
        return _POSIX_CLASSES[name], close + 1, ""


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
        backslash) or that cannot be compiled.
        """
        text = cls._bytes_view(line.rstrip("\r\n"))
        while text.endswith(" ") and not text.endswith("\\ "):
            text = text[:-1]
        if not text or text.startswith("#"):
            return None
        negated = text.startswith("!")
        text = text.removeprefix("!")
        dir_only = text.endswith("/")
        text = text.removesuffix("/")
        if not text:
            return None
        anchored = "/" in text
        body = _Glob(text.removeprefix("/")).regex()
        prefix = "" if anchored else "(?:.*/)?"
        try:
            regex = re.compile(prefix + body)
        except re.error as exc:
            msg = f"cannot compile {line!r}: {exc}"
            raise ValueError(msg) from exc
        return cls(regex, negated=negated, dir_only=dir_only)

    @staticmethod
    def _bytes_view(text: str) -> str:
        """Return *text* as one character per UTF-8 byte, as git's wildmatch sees it."""
        return text.encode("utf-8", "surrogateescape").decode("latin-1")

    @property
    def negated(self) -> bool:
        """Whether a match re-includes the path instead of ignoring it."""
        return self._negated

    def matches(self, rel: str, *, is_dir: bool) -> bool:
        """Return whether the path *rel* itself (not a parent) matches."""
        if self._dir_only and not is_dir:
            return False
        return self._regex.fullmatch(self._bytes_view(rel)) is not None


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

        Lines split on a line feed only, as in git, and a leading byte order mark
        is dropped. Bytes that are not UTF-8 survive as surrogate escapes that
        the matcher turns back into the same raw bytes, so one bad byte
        costs only the line that holds it.

        ``is_file()`` inside the ``try`` (not before it) keeps a FIFO or a symlink
        to a character device named ``.gitignore`` from blocking ``read_bytes()``
        forever, while the ``OSError`` guard keeps a raced deletion — present at
        the check, gone at the read — from aborting the whole caller's walk
        (bug class 1/2).
        """
        try:
            if not path.is_file():
                return []
            raw = path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("Skipping unreadable ignore file %s: %s", path, exc)
            return []
        return raw.decode("utf-8", "surrogateescape").removeprefix("\ufeff").split("\n")

    def verdict(self, rel: str, *, is_dir: bool) -> bool | None:
        """Return ``True`` to ignore *rel*, ``False`` to re-include it.

        ``None`` means no pattern matched, so a shallower file decides; the
        three states are the contract, not a failure to produce a value.
        """
        for pattern in reversed(self._patterns):
            if pattern.matches(rel, is_dir=is_dir):
                return not pattern.negated
        return None
