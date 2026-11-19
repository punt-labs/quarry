"""Run detached sync dispatch with durable startup and outcome diagnostics."""

from __future__ import annotations

import importlib
import logging
import os
from typing import final

from quarry._cli_entry import CliEntry
from quarry.cli_diagnostics import CliDiagnostics
from quarry.redacted_failure import RedactedFailure

logger = logging.getLogger(__name__)


@final
class SyncWorker:
    """Run the detached sync command and report its exit status."""

    @staticmethod
    def main() -> None:
        """Dispatch the sync request and log its actual exit status."""
        CliDiagnostics.worker()
        pid = os.getpid()
        logger.info("Background sync dispatcher started (pid=%d)", pid)
        try:
            app = importlib.import_module("quarry.__main__").app
            CliEntry.run(lambda: app(["sync"]))
        except SystemExit as exc:
            if exc.code not in (0, None):
                logger.error(
                    "Background sync dispatcher failed (pid=%d, exit=%d)",
                    pid,
                    exc.code if isinstance(exc.code, int) else 1,
                )
                raise
        except Exception as exc:
            logger.error(
                "Background sync dispatcher failed (pid=%d)",
                pid,
                exc_info=RedactedFailure.of(exc),
            )
            raise
        logger.info("Background sync dispatcher finished (pid=%d)", pid)


if __name__ == "__main__":
    SyncWorker.main()
