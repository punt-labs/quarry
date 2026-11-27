"""The fail-open capture-shadow push that ends every finalize."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.shadow import ShadowSyncResult

logger = logging.getLogger(__name__)


@final
class ShadowPushStep:
    """Push every enabled project's captures to its private shadow."""

    __slots__ = ("_settings",)

    _settings: Settings

    def __new__(cls, settings: Settings) -> Self:
        """Retain settings for discovering and pushing enabled capture shadows."""
        self = super().__new__(cls)
        self._settings = settings
        return self

    def run(self) -> None:
        """Push each enabled project's redacted captures to its private shadow.

        Fail-open, and the WHOLE step is wrapped: a registry-open or import
        failure must not raise past here and block the sync tail (index rebuild
        already ran; GC must still run).  A committed-but-not-pushed result is
        logged rather than swallowed silently (fail-open but LOGGED).
        """
        self._warn_unpushed(self._push(self._settings))

    @staticmethod
    def _push(settings: Settings) -> dict[str, ShadowSyncResult]:
        """Push every registered shadow; a step-level failure yields no results."""
        try:
            from quarry.shadow import CaptureSync  # noqa: PLC0415

            return CaptureSync.push_registered(settings, fail_open=True)
        except (ImportError, OSError, sqlite3.Error) as exc:
            logger.warning("sync: capture-shadow push skipped (fail-open): %s", exc)
            return {}

    @staticmethod
    def _warn_unpushed(results: dict[str, ShadowSyncResult]) -> None:
        """Log each enabled shadow whose captures did not reach the remote.

        The message reflects the real state: a run that aborted, a run that
        committed but could not push, or a run that neither committed nor
        aborted (nothing to commit or a deferred offline push).
        """
        for collection, result in results.items():
            if not result.pushed:
                logger.warning(
                    "sync: %s captures %s", collection, result.unpushed_detail()
                )
