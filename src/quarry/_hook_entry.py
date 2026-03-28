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

import sys
from collections.abc import Callable

from quarry._stdlib import run_hook


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
    """Run dedup + ingestion in a detached background process.

    Called by ``handle_pre_compact`` via ``subprocess.Popen``.  Accepts
    arguments via ``sys.argv`` (not stdin) because the parent process
    has already exited by the time this runs.

    Args (via sys.argv[2:]):
        text_file: Path to the temp file containing extracted text.
        document_name: The document name for the ingested content.
        collection: The target collection name.
        lancedb_path: Path to the LanceDB database.
        session_prefix: First 8 chars of session ID (for dedup).
        agent_handle: Agent handle from ethos sidecar (optional, may be empty).
        memory_type: Memory classification (optional, may be empty).
        summary: One-line summary of the content (optional, may be empty).
    """
    import logging  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    logger = logging.getLogger(__name__)

    min_arg_count = 5
    max_arg_count = 8
    args = sys.argv[2:]
    if not (min_arg_count <= len(args) <= max_arg_count):
        sys.exit(
            "Usage: quarry-hook ingest-background"
            " <text_file> <doc_name> <collection>"
            " <lancedb_path> <session_prefix>"
            " [agent_handle] [memory_type] [summary]"
        )

    text_file_path = args[0]
    document_name = args[1]
    collection = args[2]
    lancedb_path = args[3]
    session_prefix = args[4]
    agent_handle = args[5] if len(args) > 5 else ""
    memory_type = args[6] if len(args) > 6 else ""
    summary = args[7] if len(args) > 7 else ""
    text_file = Path(text_file_path)

    # Detached subprocess: configure its own logging per the standard.
    from quarry.logging_config import configure_logging as _configure  # noqa: PLC0415

    _configure(stderr_level="WARNING")

    try:
        text = text_file.read_text()
    except OSError:
        logger.exception("ingest-background: could not read text file %s", text_file)
        text_file.unlink(missing_ok=True)
        return

    try:
        from quarry.config import (  # noqa: PLC0415
            load_settings,
            resolve_db_paths,
        )
        from quarry.database import (  # noqa: PLC0415
            delete_document,
            get_db,
            list_documents,
        )
        from quarry.pipeline import ingest_content  # noqa: PLC0415

        # Re-resolve settings for embedding model config.  The db path is
        # taken from argv (parent already resolved it) to ensure consistency.
        settings = resolve_db_paths(load_settings(), None)
        db = get_db(Path(lancedb_path))

        # Deduplicate: remove prior captures for this session.
        try:
            prefix = f"session-{session_prefix}-"
            existing = list_documents(db, collection_filter=collection)
            prior = [doc for doc in existing if doc["document_name"].startswith(prefix)]
            for doc in prior:
                delete_document(db, doc["document_name"], collection=collection)
            if prior:
                logger.info(
                    "ingest-background: deleted %d prior capture(s) for session %s",
                    len(prior),
                    session_prefix,
                )
        except Exception:
            logger.exception("ingest-background: dedup failed, proceeding with ingest")

        try:
            result = ingest_content(
                text,
                document_name,
                db,
                settings,
                collection=collection,
                format_hint="markdown",
                agent_handle=agent_handle,
                memory_type=memory_type,
                summary=summary,
            )
            logger.info(
                "ingest-background: captured %s (%d chunks, %d chars)",
                document_name,
                result["chunks"],
                len(text),
            )
        except Exception:
            logger.exception(
                "ingest-background: ingestion failed for %s",
                document_name,
            )
    finally:
        # Clean up temp file regardless of import or ingestion failures.
        text_file.unlink(missing_ok=True)


_HANDLERS: dict[str, Callable[[], None]] = {
    "session-setup": _session_setup,
    "session-start": _session_start,
    "post-web-fetch": _post_web_fetch,
    "pre-compact": _pre_compact,
    "ingest-background": _ingest_background,
}


if __name__ == "__main__":
    main()
