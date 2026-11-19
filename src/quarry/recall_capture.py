"""Manage the daemon's shared telemetry facade and retry unavailable capture."""

from __future__ import annotations

import logging
from contextlib import contextmanager, suppress
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, Self, final

from punt_observe.telemetry import NoopTelemetry, TelemetryConfig, open_telemetry
from punt_observe.telemetry.salt import load_salt

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from punt_observe.telemetry import IdentifierHash, SignalSchema, Telemetry

    from quarry.config import Settings


logger = logging.getLogger(__name__)


@final
class RecallCapture:
    """Open capture during daemon startup and retry when a search arrives."""

    __slots__ = ("_lock", "_retry_at", "_schemas", "_settings", "_target", "_telemetry")

    _lock: Lock
    _retry_at: float
    _schemas: Sequence[SignalSchema]
    _settings: Settings
    _target: tuple[Telemetry, IdentifierHash] | None
    _telemetry: Telemetry | None

    def __new__(cls, settings: Settings, schemas: Sequence[SignalSchema]) -> Self:
        """Keep the facade dormant until the daemon starts serving."""
        self = super().__new__(cls)
        self._settings = settings
        self._schemas = schemas
        self._lock = Lock()
        self._retry_at = 0.0
        self._telemetry = self._target = None
        return self

    @contextmanager
    def running(self) -> Generator[None]:
        """Start capture and give queued observations a bounded flush."""
        self._retry_at = monotonic() + 30
        self._open()
        try:
            yield
        finally:
            with self._lock:
                telemetry = self._telemetry
                self._target = self._telemetry = None
            if telemetry is not None:
                telemetry.shutdown()

    def _open(self) -> None:
        config = TelemetryConfig(enabled=self._settings.telemetry_enabled)
        telemetry = open_telemetry("quarry", self._schemas, config)
        if isinstance(telemetry, NoopTelemetry):
            self._telemetry = None
            return
        self._telemetry = telemetry
        path = self._settings.lancedb_path.absolute()
        with suppress(OSError):
            path = path.resolve()
        scope = telemetry.hash_id(f"database:{path}")
        if scope.value:
            self._target = telemetry, scope

    @property
    def enabled(self) -> bool:
        """Report configured capture independently of store availability."""
        return self._telemetry is not None

    def database_hash(self) -> str:
        """Return the hashed scope without opening or retrying capture."""
        target = self._target
        return target[1].value if target is not None else ""

    def target(self) -> tuple[Telemetry, IdentifierHash] | None:
        """Return active capture or retry initialization after the cooldown."""
        if self._telemetry is not None and self._target is None:
            self._retry()
        return self._target

    @staticmethod
    @contextmanager
    def isolate_failure() -> Generator[None]:
        """Keep telemetry failures out of search and redact exception text."""
        try:
            yield
        except Exception as exc:
            logger.warning(
                "Recall telemetry capture failed", exc_info=RedactedFailure.of(exc)
            )

    def _retry(self) -> None:
        with self._lock:
            telemetry = self._telemetry
            if telemetry is None or self._target or monotonic() < self._retry_at:
                return
            self._retry_at = monotonic() + 30
            # Don't rebuild providers for a salt error that reopening can't fix.
            try:
                load_salt()
            except (OSError, RuntimeError, ValueError):
                return
            telemetry.shutdown()
            self._open()
