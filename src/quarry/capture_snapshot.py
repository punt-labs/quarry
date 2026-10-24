"""The instant a hook read the snapshot it captures, as the daemon accepts it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar, final


@final
class CaptureSnapshot:
    """Validate a capture's ``captured_at``, the time its transcript was read.

    The daemon stores it as the capture's ingestion time, so a transcript written
    after the read is newer than its document.  It must be an ISO-8601 instant
    with a UTC offset, no later than the daemon's clock (a little skew is allowed
    between a hook and the daemon on one host) and no older than the year 2000.
    """

    __slots__ = ()

    _SKEW: ClassVar[timedelta] = timedelta(seconds=5)
    _EARLIEST: ClassVar[datetime] = datetime(2000, 1, 1, tzinfo=UTC)

    @classmethod
    def parse(cls, raw: str, *, now: datetime) -> datetime | None:
        """Return *raw* as an aware UTC datetime, or ``None`` when it is absent.

        Raises:
            ValueError: *raw* is not an aware ISO-8601 instant, is in the future,
                or is too old.
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
        return when.astimezone(UTC)

    @classmethod
    def _bounded(cls, when: datetime, now: datetime) -> datetime:
        """Return *when* if it is neither in the future nor implausibly old."""
        if when > now + cls._SKEW:
            msg = "captured_at is in the future"
            raise ValueError(msg)
        if when < cls._EARLIEST:
            msg = "captured_at is too old"
            raise ValueError(msg)
        return when
