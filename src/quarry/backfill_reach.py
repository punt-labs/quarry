"""Whether ``quarry backfill-sessions`` would visit a session started in a directory."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import final

from quarry._hook_trace import ReadAdmission

logger = logging.getLogger(__name__)


@final
class BackfillReach:
    """Answer which sessions a backfill can recover.

    Backfill walks one Claude Code project directory per registration: the one
    named for the registered directory itself.  A session started anywhere else,
    in an unregistered directory or in a subdirectory of a registered one, keeps
    its transcript in a directory backfill never opens, so telling its user to
    run backfill would be a false promise.
    """

    __slots__ = ()

    @staticmethod
    def includes(cwd: str) -> bool:
        """Return whether a session started in *cwd* is one backfill recovers.

        False when *cwd* is empty or the registry cannot be read: the caller then
        says nothing about backfill rather than something that may be untrue.
        """
        if not cwd:
            return False
        resolver = ReadAdmission.collection_resolver_for(cwd)
        if resolver is None:
            return False
        try:
            registration = resolver.covering_registration(cwd)
            return (
                registration is not None
                and Path(registration.directory) == Path(cwd).resolve()
            )
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            logger.debug("backfill reach unknown: %s", type(exc).__name__)
            return False
