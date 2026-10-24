"""The instant a hook read the snapshot it captures, as the daemon accepts it."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import ClassVar, final

logger = logging.getLogger(__name__)


@final
class CaptureSnapshot:
    """Validate a capture's ``captured_at``, the time its transcript was read.

    The daemon stores it as the capture's ingestion time, so a transcript written
    after the read is newer than its document.  It must be an ISO-8601 instant
    with a UTC offset, inside the calendar and no older than the year 2000.  One
    later than the daemon's clock is clamped to it rather than refused: a hook
    clock a little ahead still names a read that has happened.
    """

    __slots__ = ()

    _EARLIEST: ClassVar[datetime] = datetime(2000, 1, 1, tzinfo=UTC)

    @classmethod
    def parse(cls, raw: str, *, now: datetime) -> datetime | None:
        """Return *raw* as an aware UTC datetime, or ``None`` when it is absent.

        Raises:
            ValueError: *raw* is not an aware ISO-8601 instant, is out of the
                calendar's range, or is too old.
        """
        return cls._bounded(cls._instant(raw), now) if raw else None

    @staticmethod
    def _instant(raw: str) -> datetime:
        """Return *raw* as an aware UTC datetime, or raise ``ValueError``."""
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            msg = "captured_at must be an ISO-8601 instant with a UTC offset"
            raise ValueError(msg) from None
        if when.tzinfo is None:
            msg = "captured_at must carry a UTC offset"
            raise ValueError(msg)
        try:
            return when.astimezone(UTC)
        except (ValueError, OverflowError):
            msg = "captured_at is out of range"
            raise ValueError(msg) from None

    @classmethod
    def _bounded(cls, when: datetime, now: datetime) -> datetime:
        """Return *when*, clamped to *now*, if it is not implausibly old."""
        if when < cls._EARLIEST:
            msg = "captured_at is too old"
            raise ValueError(msg)
        if when > now:
            logger.debug("captured_at is ahead of the daemon's clock; using now")
            return now
        return when
