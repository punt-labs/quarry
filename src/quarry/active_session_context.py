"""The SessionStart context of an active session: sync status and what is indexed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.session_start_templates import SessionStartTemplates
from quarry.sync_lock import SyncLock

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.results import CoverageCounts

logger = logging.getLogger(__name__)


@final
class ActiveSessionContext:
    """Build the ``additionalContext`` of a session that quarry is active in.

    Starts the background sync and asks the daemon what is indexed for the
    collection; when the daemon cannot say, the context says so instead of showing
    counts.
    """

    __slots__ = ("_collection", "_directory")

    _directory: Path
    _collection: str

    def __new__(cls, directory: Path, collection: str) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        self._collection = collection
        return self

    def text(self) -> str:
        """Return the active-mode context, launching the background sync."""
        captures_collection = f"{self._collection}-captures"
        sync_status = SyncLock().launch_background_sync()
        counts = self._session_coverage(self._collection)
        if counts is None:
            return SessionStartTemplates.active_unreachable_coverage(
                self._directory, self._collection, captures_collection, sync_status
            )
        return SessionStartTemplates.active(
            self._directory, self._collection, captures_collection, counts, sync_status
        )

    @staticmethod
    def _session_coverage(collection: str) -> CoverageCounts | None:
        """Fetch per-repo coverage counts from the daemon.

        Returns ``None`` whenever the client cannot obtain counts — daemon
        unreachable, HTTP error (including 401 not-authorized), malformed
        response, or client misconfiguration.  ``ClientConfigError`` is a
        ``QuarryError`` subclass, so a single ``except QuarryError`` catches
        every failure mode the client hierarchy names.

        Client-specific exceptions become a single ``None`` signal, so the
        SessionStart template can fall back to a coverage-unavailable message
        without importing the client exception hierarchy.  ``None`` is the
        documented "unavailable" contract here (the failure's class is logged at
        WARNING) — the caller distinguishes it from the empty-catalog case where
        the daemon answered with zeros.
        """
        from quarry.client import (  # noqa: PLC0415
            ClientConfigError,
            QuarryError,
            TargetResolver,
        )

        try:
            resp = TargetResolver.connect().coverage(collection)
        except (ClientConfigError, QuarryError) as exc:
            logger.warning("session coverage unavailable: %s", type(exc).__name__)
            return None
        return {
            "documents_indexed": resp.documents_indexed,
            "transcripts_captured": resp.transcripts_captured,
            "memories_saved": resp.memories_saved,
        }
