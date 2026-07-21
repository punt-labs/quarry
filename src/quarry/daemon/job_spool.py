"""Durable server-side spool for admitted ingest jobs aborted at shutdown.

A clean shutdown drains every admitted (queued or in-flight) job to completion
within the drain budget; only a *genuinely exceeded* drain deadline aborts the
stragglers.  Captures survive that abort because their transcript ``.md`` predates
the POST and ``quarry backfill`` re-ingests it.  A ``remember`` (and a plain URL
``ingest``) has no such client-side artifact, so aborting it would drop admitted
knowledge with no way to recover it.

This spool closes that gap: an aborted job with no durable client copy writes a
recoverable snapshot here — scrubbed at write time (DES-036), atomically — under
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

if TYPE_CHECKING:
    from quarry.config import Settings

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class SpoolRecord:
    """A recoverable snapshot of an aborted job that has no durable client copy.

    ``payload`` is already scrubbed by the producing job (the remember content or
    the ingest source URL), so the spooled file never holds unredacted secrets.
    """

    kind: str
    collection: str
    name: str
    payload: str

    def as_json(self) -> str:
        """Return the record as a one-object JSON document for the spool file."""
        return json.dumps(
            {
                "kind": self.kind,
                "collection": self.collection,
                "name": self.name,
                "payload": self.payload,
                "spooled_at": time.time(),
            }
        )

    def filename(self) -> str:
        """Return a collision-free spool filename for this record."""
        return f"{self.kind}-{uuid.uuid4().hex[:12]}.json"


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

    def write(self, record: SpoolRecord) -> None:
        """Persist *record* atomically; never raise into the shutdown path.

        A spool-file problem must not wedge daemon shutdown, so a write failure
        is logged (with the collection, so the loss is at least visible) rather
        than propagated — the abort loop keeps failing the remaining jobs.
        """
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / record.filename()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(record.as_json(), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.exception(
                "failed to spool aborted %s job for %s", record.kind, record.collection
            )
