"""Lightweight hook entry point — bypasses full CLI import chain.

The ``quarry`` CLI (``__main__.py``) imports typer, pydantic, lancedb,
onnxruntime, and the full pipeline stack — seconds of module load
before a single line of handler code runs.

This module is the entry point for ``quarry-hook``, which dispatches
directly to handler functions via ``sys.argv``.  Each handler lazily
imports only what it needs, avoiding the full dependency tree.

Import cost: ~0.1s (stdlib only) vs ~1.5s+ (full CLI).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry._stdlib import run_hook
from quarry.background_ingest import BackgroundIngestJob

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.db import Database

_MIN_INGEST_ARGS = 5
_MAX_INGEST_ARGS = 8


def main() -> None:
    """Dispatch hook commands via sys.argv — no typer overhead."""
    args = sys.argv[1:]
    if not args:
        sys.exit("Usage: quarry-hook <event>")

    event = args[0]
    handler = _HANDLERS.get(event)
    if handler is None:
        sys.exit(f"Unknown hook event: {event}")
    handler()


# ── Handler dispatch ─────────────────────────────────────────────────


def _session_setup() -> None:
    from quarry._stdlib import handle_session_setup  # noqa: PLC0415

    run_hook(handle_session_setup)


def _session_start() -> None:
    from quarry.hooks import handle_session_start  # noqa: PLC0415

    run_hook(handle_session_start)


def _post_web_fetch() -> None:
    from quarry.hooks import handle_post_web_fetch  # noqa: PLC0415

    run_hook(handle_post_web_fetch)


def _pre_compact() -> None:
    from quarry.hooks import handle_pre_compact  # noqa: PLC0415

    run_hook(handle_pre_compact)


def _ingest_background() -> None:
    """Run dedup + ingestion in a detached background process."""
    BackgroundIngest().run()


@final
class BackgroundIngest:
    """Dedup + ingest one captured transcript in a detached subprocess.

    Owns the parsed job, the temp text file, and the resources acquired during
    a run (settings, database, transcript text), so the read/dedup/ingest/cleanup
    steps share state rather than threading eight values through free functions.
    Heavy imports (config, db, pipeline) stay inside the methods to keep this
    module's stdlib-only import budget for the fast-path hook events.

    Construction parses ``sys.argv`` — the parent process has already exited by
    the time this runs, so arguments arrive via argv (slots ``sys.argv[2:]``)
    rather than stdin. A bad argument count exits with a usage message.
    """

    __slots__ = ("_database", "_job", "_logger", "_settings", "_text", "_text_file")

    _text_file: Path
    _job: BackgroundIngestJob
    _logger: logging.Logger
    _settings: Settings
    _database: Database
    _text: str

    def __new__(cls) -> Self:
        args = sys.argv[2:]
        if not _MIN_INGEST_ARGS <= len(args) <= _MAX_INGEST_ARGS:
            sys.exit(
                "Usage: quarry-hook ingest-background"
                " <text_file> <doc_name> <collection>"
                " <lancedb_path> <session_prefix>"
                " [agent_handle] [memory_type] [summary]"
            )
        self = super().__new__(cls)
        self._logger = logging.getLogger(__name__)
        self._text_file = Path(args[0])
        self._job = BackgroundIngestJob(
            document_name=args[1],
            collection=args[2],
            lancedb_path=Path(args[3]),
            session_prefix=args[4],
            agent_handle=args[5] if len(args) > 5 else "",
            memory_type=args[6] if len(args) > 6 else "",
            summary=args[7] if len(args) > 7 else "",
        )
        return self

    def run(self) -> None:
        """Read the transcript, dedup prior captures, ingest, then clean up."""
        # Detached subprocess: configure its own logging per the standard.
        from quarry.logging_config import LoggingConfig  # noqa: PLC0415

        LoggingConfig.configure(stderr_level="WARNING")

        try:
            self._text = self._text_file.read_text()
        except OSError:
            self._logger.exception(
                "ingest-background: could not read text file %s", self._text_file
            )
            self._text_file.unlink(missing_ok=True)
            return

        try:
            from quarry.config import Settings  # noqa: PLC0415
            from quarry.db import Database  # noqa: PLC0415

            # Re-resolve settings for embedding model config.  The db path is
            # taken from the job (parent already resolved it) for consistency.
            self._settings = Settings.load().resolve_db_paths(None)
            self._database = Database.connect(self._job.lancedb_path)
            self._dedup()
            self._ingest()
        finally:
            # Clean up temp file regardless of import or ingestion failures.
            self._text_file.unlink(missing_ok=True)

    def _dedup(self) -> None:
        """Remove this session's prior captures before re-ingesting."""
        job = self._job
        prefix = f"session-{job.session_prefix}-"
        try:
            existing = self._database.catalog.list_documents(
                collection_filter=job.collection
            )
            prior = [d for d in existing if d["document_name"].startswith(prefix)]
            for doc in prior:
                self._database.store.delete_document(
                    doc["document_name"], collection=job.collection, count=False
                )
            if prior:
                self._logger.info(
                    "ingest-background: deleted %d prior capture(s) for session %s",
                    len(prior),
                    job.session_prefix,
                )
        except Exception:
            self._logger.exception(
                "ingest-background: dedup failed, proceeding with ingest"
            )

    def _ingest(self) -> None:
        """Ingest the transcript text as a markdown capture."""
        from quarry.ingestion.pipeline import ingest_content  # noqa: PLC0415

        job = self._job
        try:
            result = ingest_content(
                self._text,
                job.document_name,
                self._database,
                self._settings,
                collection=job.collection,
                format_hint="markdown",
                agent_handle=job.agent_handle,
                memory_type=job.memory_type,
                summary=job.summary,
            )
            self._logger.info(
                "ingest-background: captured %s (%d chunks, %d chars)",
                job.document_name,
                result["chunks"],
                len(self._text),
            )
        except Exception:
            self._logger.exception(
                "ingest-background: ingestion failed for %s", job.document_name
            )


_HANDLERS: dict[str, Callable[[], None]] = {
    "session-setup": _session_setup,
    "session-start": _session_start,
    "post-web-fetch": _post_web_fetch,
    "pre-compact": _pre_compact,
    "ingest-background": _ingest_background,
}


if __name__ == "__main__":
    main()
