"""Input surface of the ingest commands: shared options and UTF-8 text intake."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Annotated, NoReturn, Self, final

import typer

from quarry.memory_types import MemoryType

if TYPE_CHECKING:
    from collections.abc import Callable

# The three agent-memory options ``ingest`` and ``remember`` share, declared
# once so their help text (and the vocabulary it names) cannot drift between
# the two commands. The daemon cannot infer an agent's identity — a
# subagent's working directory resolves to the repo's leader — so the handle
# is always the caller's own statement.
AgentHandleOption = Annotated[
    str,
    typer.Option(
        "--agent-handle",
        help="Your own agent handle; routes to memory-<handle> unless --collection",
    ),
]
MemoryTypeOption = Annotated[
    str,
    typer.Option(
        "--memory-type",
        help=(
            f"Memory type: {MemoryType.agent_choices()} "
            f"('{MemoryType.LESSON}' is reserved for quarry learn)"
        ),
    ),
]
SummaryOption = Annotated[
    str, typer.Option("--summary", help="One-line summary of the content")
]


@final
class Utf8Source:
    """Read or vet text, routing undecodable bytes to one caller-supplied refusal."""

    __slots__ = ("_fail",)

    _fail: Callable[[str], NoReturn]

    def __new__(cls, fail: Callable[[str], NoReturn]) -> Self:
        self = super().__new__(cls)
        self._fail = fail
        return self

    def read_stdin(self) -> str:
        """Return all of stdin, refusing bytes that are not UTF-8."""
        # Decode the raw bytes ourselves: the text layer's codec and error
        # handler depend on the locale (surrogateescape under UTF-8 mode,
        # latin-1 under a legacy locale) and would let binary through.
        try:
            return sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError:
            self._refuse("stdin")

    def require(self, text: str, source: str) -> None:
        """Refuse *text* that carries undecodable bytes (surrogate-escaped argv)."""
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            self._refuse(source)

    def _refuse(self, source: str) -> NoReturn:
        self._fail(f"Error: {source} is not UTF-8 text; binary content is refused")
