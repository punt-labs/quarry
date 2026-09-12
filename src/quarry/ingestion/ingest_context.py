"""Progress reporting and the shared bulk-ingest context (DES-036 leaf module)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.config import Settings
    from quarry.db import Database

logger = logging.getLogger(__name__)

# "" is unset; "lesson" is written only by the daemon's learn route
# (RESERVED_MEMORY_TYPE) and rejected at every other boundary — see
# daemon/routes/base.py's reject_reserved_memory_type and the domain
# documented in mcp_server.py's remember() docstring.
type MemoryType = Literal["", "fact", "observation", "opinion", "procedure", "lesson"]


@final
class Progress:
    """Reports a progress line: always to the logger, optionally to a callback."""

    __slots__ = ("_callback", "_log")

    _callback: Callable[[str], None] | None
    _log: bool

    def __new__(
        cls, callback: Callable[[str], None] | None, *, log: bool = True
    ) -> Self:
        self = super().__new__(cls)
        self._callback = callback
        self._log = log
        return self

    @classmethod
    def silent(cls) -> Self:
        """Return a Progress that reports nothing — no logger call, no callback.

        ``plan_file_chunks`` emits zero progress output today; a bare
        ``Progress(None)`` would still log every message and regress the
        "no behavior change" invariant this decomposition promises.
        """
        return cls(None, log=False)

    def __call__(self, fmt: str, *args: object) -> None:
        if self._log:
            logger.info(fmt, *args)
        if self._callback is not None:
            self._callback(fmt % args if args else fmt)


@dataclass(frozen=True, slots=True)
class IngestContext:
    """The knobs common to every bulk ingest path: target, mode, memory tags."""

    database: Database
    settings: Settings
    overwrite: bool = False
    collection: str = "default"
    agent_handle: str = ""
    memory_type: MemoryType = ""
    summary: str = ""
