"""Durable server-side spool for admitted ingest jobs aborted at shutdown.

A clean shutdown drains every admitted (queued or in-flight) job to completion
within the drain budget; only a *genuinely exceeded* drain deadline aborts the
stragglers.  Captures survive that abort because their transcript ``.md`` predates
the POST and ``quarry backfill`` re-ingests it.  A ``remember`` (and a plain URL
``ingest``) has no such client-side artifact, so aborting it would drop admitted
knowledge with no way to recover it.

This spool closes that gap: an aborted job with no durable client copy writes a
recoverable snapshot here — scrubbed at write time, atomically — under
the daemon's private data dir, so the knowledge is never *silently* lost.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.private_write import PrivateWrite
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.config import Settings

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class SpoolRecord:
    """A recoverable snapshot of an aborted job that has no durable client copy.

    ``payload`` is already scrubbed by the producing job (the remember content or
    the ingest URL's storage form), so the spooled file never holds secrets.
    """

    kind: str
    collection: str
    name: str
    payload: str
    # False when the job's own text could not be persisted (an ingest URL whose query
    # was dropped): the file is then only a note, and recovery is by re-running it.
    recoverable: bool = True

    def as_json(self) -> str:
        """Return the record as a one-object JSON document for the spool file."""
        return json.dumps(
            {
                "kind": self.kind,
                "collection": self.collection,
                "name": self.name,
                "payload": self.payload,
                "recoverable": self.recoverable,
                "spooled_at": time.time(),
            }
        )

    def filename(self) -> str:
        """Return a spool filename no earlier record can share (a full UUID)."""
        return f"{self.kind}-{uuid.uuid4().hex}.json"


@final
class JobSpool:
    """Writes :class:`SpoolRecord`s atomically under the daemon's spool dir."""

    __slots__ = ("_dir",)

    _dir: Path

    def __new__(cls, spool_dir: Path) -> Self:
        self = super().__new__(cls)
        self._dir = spool_dir
        return self

    @classmethod
    def for_settings(cls, settings: Settings) -> Self:
        """Return the spool rooted at ``<quarry_root>/spool`` for *settings*."""
        return cls(settings.quarry_root / "spool")

    def write(self, record: SpoolRecord) -> bool:
        """Persist *record* atomically and privately; return whether it stuck.

        Returns ``True`` only when the rename succeeded, so the record is in
        place and recoverable.  What is durable, exactly: the file's bytes are
        synced before the rename, and the spool directory is synced after it, so
        the entry survives a crash; when this call created the spool directory,
        its entry in the parent is synced too.  A failed directory sync is a
        warning and still returns ``True``: the record is there, but a crash
        before the filesystem flushes could lose its name.  Any other failure
        returns ``False`` and never raises into shutdown, so the caller records
        the truth instead of a false recoverability claim: the content is lost,
        not spooled.  It is logged with the collection so the loss stays visible.

        The spool holds best-effort-scrubbed content (regex redaction, not a
        guarantee), so it is created private: the directory ``0o700`` and the
        file ``0o600`` from the start, never group/other-readable even for an
        instant.  The name carries a full UUID, so a record never replaces an
        earlier one.
        """
        try:
            PrivateWrite.ensure_directory(self._dir, "spool directory")
            self._atomic_write(self._dir / record.filename(), record.as_json())
        except Exception as exc:
            logger.error(
                "failed to spool aborted %s job for %s",
                record.kind,
                record.collection,
                exc_info=RedactedFailure.of(exc),
            )
            return False
        return True

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write *content* privately and durably to *path*, via a sibling temp."""
        PrivateWrite("spooled record").commit(
            path.with_name(path.name + ".tmp"), path, content
        )
