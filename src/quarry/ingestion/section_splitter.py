"""Splitting a document into sections on its format's section boundary."""

from __future__ import annotations

import re
from typing import Final, Self, final

# A heading opens a markdown section; the lookahead keeps the heading with the
# body that follows it.
_MD_HEADER: Final[re.Pattern[str]] = re.compile(r"^(?=#+\s)", re.MULTILINE)
_LATEX_SECTION: Final[re.Pattern[str]] = re.compile(r"(?=\\(?:sub)?section\{)")
_BLANK_LINE: Final[re.Pattern[str]] = re.compile(r"\n\s*\n")

# Used only to decide whether a part carries content, never to rewrite one.
_HTML_COMMENT: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)


@final
class SectionSplitter:
    """Split text on a boundary pattern, discarding parts that carry no content.

    One class rather than three functions because the three formats differ only
    in their boundary pattern and in whether comments count as content — the
    variation is data, not behaviour. Callers select a format through the named
    constructors instead of branching on a format string.
    """

    __slots__ = ("_boundary", "_comments_are_content")

    _boundary: re.Pattern[str]
    _comments_are_content: bool

    def __new__(cls, boundary: re.Pattern[str], *, comments_are_content: bool) -> Self:
        """Choose section boundaries and whether comment-only parts count as content."""
        self = super().__new__(cls)
        self._boundary = boundary
        self._comments_are_content = comments_are_content
        return self

    @classmethod
    def markdown(cls) -> Self:
        """Return a splitter that breaks on headings of any level.

        Comments do not count as content here: a file opening with a lint
        directive -- ``<!-- markdownlint-disable MD025 -->`` -- would otherwise
        yield a leading section holding nothing but that directive, which is
        then embedded, stored, and returned as a search hit. Genuine prose
        before the first heading is still kept, comments and all.
        """
        return cls(_MD_HEADER, comments_are_content=False)

    @classmethod
    def latex(cls) -> Self:
        r"""Return a splitter that breaks on ``\section`` and ``\subsection``."""
        return cls(_LATEX_SECTION, comments_are_content=True)

    @classmethod
    def plain(cls) -> Self:
        """Return a splitter that breaks on blank lines (paragraph boundaries)."""
        return cls(_BLANK_LINE, comments_are_content=True)

    def detects(self, text: str) -> bool:
        """Return whether *text* contains this format's section boundary.

        The same pattern that splits a document also identifies it, so format
        detection asks the splitter rather than reaching for its regex.
        """
        return self._boundary.search(text) is not None

    def split(self, text: str) -> list[str]:
        """Return the sections of *text*, in order, dropping empty parts."""
        return [p for p in self._boundary.split(text) if self._has_content(p)]

    def _has_content(self, part: str) -> bool:
        """Return whether *part* is more than whitespace (and, maybe, comments)."""
        if self._comments_are_content:
            return bool(part.strip())
        return bool(_HTML_COMMENT.sub("", part).strip())
