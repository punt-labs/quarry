"""Hook handlers for Claude Code integration.

Each handler receives the parsed stdin JSON from Claude Code and returns
a dict that will be serialized to stdout.  All handlers are fail-open:
exceptions are logged but never propagated to the caller.

Hook events:
    session-start    — SessionStart: auto-register and sync the current repo.
    post-web-fetch   — PostToolUse on WebFetch: auto-ingest fetched URLs.
    pre-compact      — PreCompact: capture compaction summaries.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from quarry.config import Settings, load_settings, resolve_db_paths
from quarry.database import get_db
from quarry.sync import SyncResult, sync_collection
from quarry.sync_registry import (
    DirectoryRegistration,
    get_registration,
    list_registrations,
    open_registry,
    register_directory,
)

logger = logging.getLogger(__name__)


def _find_registration(
    registrations: list[DirectoryRegistration],
    directory: str,
) -> DirectoryRegistration | None:
    """Find an existing registration matching *directory*."""
    for reg in registrations:
        if reg.directory == directory:
            return reg
    return None


def _format_context(collection: str, directory: str, result: SyncResult) -> str:
    """Build the additionalContext string for SessionStart."""
    parts = [
        "Quarry semantic search is active for this project.",
        f'Collection: "{collection}" ({directory})',
    ]
    total = result.ingested + result.skipped + result.deleted + result.failed
    if total > 0:
        parts.append(
            f"Sync: {result.ingested} ingested, {result.deleted} deleted, "
            f"{result.skipped} unchanged, {result.failed} failed."
        )
    else:
        parts.append("Sync: collection is empty (no supported files found).")
    parts.append(
        "Use the quarry MCP tools (search_documents, get_page) to search "
        "this codebase semantically."
    )
    return "\n".join(parts)


def _unique_collection_name(
    conn: sqlite3.Connection,
    directory: Path,
) -> str:
    """Derive a collection name that doesn't collide with existing ones.

    Prefers ``directory.name``.  If that's taken (another directory with the
    same leaf name), appends the parent directory name to disambiguate:
    ``leaf-parent``.
    """
    candidate = directory.name
    if get_registration(conn, candidate) is None:
        return candidate
    # Disambiguate with parent directory name.
    parent = directory.parent.name or "root"
    candidate = f"{directory.name}-{parent}"
    if get_registration(conn, candidate) is None:
        return candidate
    # Last resort: use the full resolved path hash suffix.
    import hashlib  # noqa: PLC0415

    suffix = hashlib.sha256(str(directory).encode()).hexdigest()[:8]
    return f"{directory.name}-{suffix}"


def _resolve_settings() -> Settings:
    """Load settings resolved for the default database."""
    return resolve_db_paths(load_settings(), None)


def handle_session_start(payload: dict[str, object]) -> dict[str, object]:
    """Handle SessionStart hook.

    Auto-registers the current working directory (from the payload ``cwd``
    field) with quarry and runs an incremental sync.  Returns
    ``additionalContext`` so Claude knows quarry is available.
    """
    cwd = str(payload.get("cwd", ""))
    if not cwd:
        logger.debug("session-start: no cwd in payload, skipping")
        return {}

    directory = Path(cwd).resolve()
    if not directory.is_dir():
        logger.warning("session-start: cwd is not a directory: %s", directory)
        return {}

    settings = _resolve_settings()
    conn = open_registry(settings.registry_path)
    try:
        # Idempotent registration: check first, register if missing.
        existing = _find_registration(list_registrations(conn), str(directory))
        if existing:
            collection = existing.collection
        else:
            collection = _unique_collection_name(conn, directory)
            register_directory(conn, directory, collection)
            logger.info("session-start: registered %s as '%s'", directory, collection)

        db = get_db(settings.lancedb_path)
        result = sync_collection(directory, collection, db, settings, conn)

        context = _format_context(collection, str(directory), result)
        logger.info("session-start: synced '%s' — %s", collection, result)
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        }
    finally:
        conn.close()


def handle_post_web_fetch(payload: dict[str, object]) -> dict[str, object]:
    """Handle PostToolUse on WebFetch.

    Future: extract the fetched URL and queue it for background ingestion
    into the web-captures collection.
    """
    logger.debug("post-web-fetch hook received payload: %s", payload)
    return {}


def handle_pre_compact(payload: dict[str, object]) -> dict[str, object]:
    """Handle PreCompact hook.

    Future: capture the compaction summary as a searchable document
    in the session-notes collection.
    """
    logger.debug("pre-compact hook received payload: %s", payload)
    return {}
