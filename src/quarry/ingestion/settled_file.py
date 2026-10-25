"""Recognise a watched file whose stored chunks are already current."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from quarry.sync_change import FileChange, FileChangeDetector

if TYPE_CHECKING:
    import os
    from pathlib import Path

    from quarry.sync_file_store import FileRecord
    from quarry.sync_registry import SyncRegistry


@final
class SettledFile:
    """Decide that a file needs no re-embedding, and re-stamp its row if it moved.

    A watch event fires on every write, and most writes leave the content
    identical — an editor saving in place, a branch switch restoring the same
    bytes, a ``touch``.  Re-embedding those recomputes the same vectors and
    rewrites the same rows, which is what made one document embed five times in
    a row under churn.
    """

    __slots__ = ("_registry",)

    _registry: SyncRegistry

    def __new__(cls, registry: SyncRegistry) -> Self:
        self = super().__new__(cls)
        self._registry = registry
        return self

    def is_current(self, record: FileRecord | None, file_path: Path) -> bool:
        """Return whether *file_path*'s stored chunks need no re-embedding.

        A file that vanished since the event has no ``stat`` to compare; that is
        the normal TOCTOU case, and ``False`` hands it to the regular path, which
        reports it as the per-file error it is.
        """
        try:
            stat = file_path.stat()
        except OSError:
            return False
        decision = FileChangeDetector().classify(record, file_path, stat)
        if decision.needs_embedding:
            return False
        if record is not None and decision.change is FileChange.REFRESH:
            self._refresh_row(record, stat, decision.content_hash)
        return True

    def _refresh_row(
        self, record: FileRecord, stat: os.stat_result, content_hash: str | None
    ) -> None:
        """Re-stamp *record*'s metadata for content that did not actually change.

        The file's ``(mtime, size)`` moved but its bytes did not, so the stored
        chunks stay and only the row is brought current — which stops the next
        scan from re-deciding the same non-change.
        """
        self._registry.files.upsert_file(
            replace(
                record,
                mtime=stat.st_mtime,
                size=stat.st_size,
                ingested_at=datetime.now(UTC).isoformat(),
                content_hash=content_hash,
            ),
            commit=True,
        )
