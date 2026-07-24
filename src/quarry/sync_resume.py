"""Within-file resume policy: watermark gate and partial-hash mark decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from quarry.ingestion.progressive import FlushCheckpoint
    from quarry.sync_file_store import FileRecord

# A ``partial_hash`` marker meaning the content hash was unknown when the
# watermark was written (hashing failed on a mid-file flush). The value is not a
# hex digest, so it can never collide with a real ``blake2b`` content hash: the
# incomplete row still reads as partial (``is_partial`` stays True) and re-enters
# ingestion, while the resume gate treats it as hash-unknown and re-embeds from 0.
# A watermark whose hash we cannot verify is never trusted (DES-034 §5.3).
HASH_UNKNOWN = "__hash_unknown__"


class ResumePolicy:
    """Decide the within-file resume watermark and the partial mark to persist.

    Pure decisions over a registry ``FileRecord`` and the file's current content
    hash; the caller (the sync consumer) performs the resulting LanceDB and
    registry writes. Stateless apart from the hash-unknown sentinel it owns.
    """

    __slots__ = ("_hash_unknown",)

    _hash_unknown: str

    def __new__(cls, hash_unknown: str = HASH_UNKNOWN) -> Self:
        self = super().__new__(cls)
        self._hash_unknown = hash_unknown
        return self

    def resume_watermark(
        self,
        record: FileRecord | None,
        content_hash: str | None,
        total: int,
        *,
        deterministic: bool,
    ) -> int:
        """Return the within-file resume index, or 0 for a full (re-)embed (G3 gate).

        A watermark is trusted only when the row is partial, in range, extraction
        is deterministic, and the committed prefix's hash matches the current
        content. A hash-unknown watermark is never trusted — it re-embeds from 0.
        """
        if record is None or not record.is_partial:
            return 0
        watermark = record.chunks_committed
        if watermark <= 0 or watermark >= total:
            return 0
        if record.partial_hash == self._hash_unknown:
            return 0
        if record.partial_hash != content_hash:
            return 0
        if not deterministic:
            return 0
        return watermark

    def partial_mark(
        self, checkpoint: FlushCheckpoint, content_hash: str | None
    ) -> str | None:
        """Return the ``partial_hash`` to persist for *checkpoint*.

        A complete file clears the mark (None). An incomplete flush stores the
        content hash so the next sync can verify the prefix — or the hash-unknown
        sentinel when hashing failed, so the incomplete row still reads as partial
        instead of masquerading as complete and silently dropping the unembedded
        tail.
        """
        if checkpoint.complete:
            return None
        return content_hash if content_hash is not None else self._hash_unknown

    def clear_stale_on_failure(
        self, record: FileRecord | None, content_hash: str | None
    ) -> bool:
        """Return True when a failed re-ingest must drop the stored document.

        A within-file resume whose committed prefix still matches the on-disk
        content (verified hash) is current and is kept. Everything else — a changed
        complete file, a changed-since-partial file, or an unverifiable hash — is
        stale: its old chunks must not survive a failed sync, so removed or redacted
        content stops being searchable the moment the file changes (DES-034 §5.3).
        """
        if record is None:
            return False
        current_resume = (
            record.is_partial
            and content_hash is not None
            and record.partial_hash == content_hash
        )
        return not current_resume
