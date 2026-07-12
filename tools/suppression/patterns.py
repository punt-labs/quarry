"""Detect and count lint/type suppression comments in one source file."""

from __future__ import annotations

import io
import re
import token
import tokenize
from typing import Self

_NOQA_RE = re.compile(r"#\s*noqa\b")
_TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore\b")
_PYLINT_DISABLE_RE = re.compile(r"#\s*pylint:\s*disable\b")
_PYRIGHT_IGNORE_RE = re.compile(r"#\s*pyright:\s*ignore\b")

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("noqa", _NOQA_RE),
    ("type_ignore", _TYPE_IGNORE_RE),
    ("pylint_disable", _PYLINT_DISABLE_RE),
    ("pyright_ignore", _PYRIGHT_IGNORE_RE),
)

PATTERN_NAMES: tuple[str, ...] = tuple(name for name, _ in PATTERNS)

CATEGORIES: tuple[str, ...] = (*PATTERN_NAMES, "per_file_ignores")

# Token types that do not, on their own, make a line "code" for the purposes of
# counting suppressions. A line carrying only these tokens (e.g. just a comment,
# or just blank/indent/newline) cannot host a real suppression — it is
# annotation, not the code being annotated.
_NON_CODE_TOKEN_TYPES = frozenset(
    {
        token.NEWLINE,
        token.NL,
        token.INDENT,
        token.DEDENT,
        token.COMMENT,
        token.ENCODING,
        token.ENDMARKER,
    }
)


class FileSuppressions:
    """Count suppression comments on the code lines of one Python file."""

    _path: str
    _counts: dict[str, int]

    def __new__(cls, path: str, source: str) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._counts = dict.fromkeys(PATTERN_NAMES, 0)
        self._scan(source)
        return self

    @property
    def path(self) -> str:
        """Return the scanned file's path."""
        return self._path

    @property
    def total(self) -> int:
        """Return the total suppression count for this file."""
        return sum(self._counts.values())

    def count(self, category: str) -> int:
        """Return the count for one suppression category."""
        return self._counts.get(category, 0)

    def to_dict(self) -> dict[str, int]:
        """Return the non-zero category counts."""
        return {k: v for k, v in self._counts.items() if v}

    def _scan(self, source: str) -> None:
        """Scan source for suppression comments on code lines.

        Uses the ``tokenize`` module so that ``# noqa`` text inside a string
        literal is not confused with a real comment token, and any line that
        holds a true ``COMMENT`` token alongside any non-trivial code token is
        counted regardless of whether the line also overlaps a multi-line
        string. This is the principled version of the regex-plus-AST heuristic
        it replaces, which had documented blind spots (``async def``,
        ``obj.attr =``, tuple targets, single-line docstrings containing the
        word ``noqa``).
        """
        try:
            tokens = list(tokenize.tokenize(io.BytesIO(source.encode()).readline))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            # Unparseable source — degrade to zero rather than crash.
            return

        code_lines: set[int] = set()
        comments_by_line: dict[int, str] = {}
        for tok in tokens:
            if tok.type == token.COMMENT:
                comments_by_line[tok.start[0]] = tok.string
                continue
            if tok.type in _NON_CODE_TOKEN_TYPES:
                continue
            # STRING and every "real" code token (NAME, OP, NUMBER, etc.) mark
            # every line of the token's span as code. Module/function docstrings
            # are statement-position STRINGs and do carry code meaning — a noqa
            # marker outside the closing quote on the same line is a real
            # suppression for that statement.
            for line in range(tok.start[0], tok.end[0] + 1):
                code_lines.add(line)

        for lineno, comment_text in comments_by_line.items():
            if lineno not in code_lines:
                continue
            for name, pattern in PATTERNS:
                if pattern.search(comment_text):
                    self._counts[name] += 1
