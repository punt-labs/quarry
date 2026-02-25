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
from dataclasses import dataclass
from pathlib import Path

from quarry.config import Settings, load_settings, resolve_db_paths
from quarry.database import get_db, list_documents
from quarry.pipeline import ingest_content, ingest_url
from quarry.sync import SyncResult, sync_collection
from quarry.sync_registry import (
    DirectoryRegistration,
    get_registration,
    list_registrations,
    open_registry,
    register_directory,
)
from quarry.types import LanceDB

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = ".claude/quarry.local.md"


@dataclass(frozen=True)
class HookConfig:
    """Per-project hook configuration from ``.claude/quarry.local.md``."""

    session_sync: bool = True
    web_fetch: bool = True
    compaction: bool = True


def load_hook_config(cwd: str) -> HookConfig:
    """Load hook config from YAML frontmatter in the project's config file.

    Returns defaults (all enabled) if the file is missing or unparseable.
    """
    path = Path(cwd) / _CONFIG_FILENAME
    if not path.is_file():
        return HookConfig()

    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return HookConfig()

    # Parse YAML frontmatter between --- delimiter lines.
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return HookConfig()

    end_index = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = i
            break

    if end_index is None:
        return HookConfig()
    frontmatter = "\n".join(lines[1:end_index]).strip()

    import yaml  # noqa: PLC0415

    try:
        data = yaml.safe_load(frontmatter)
    except Exception:  # noqa: BLE001
        logger.warning("hook-config: invalid YAML in %s", path)
        return HookConfig()

    if not isinstance(data, dict):
        return HookConfig()

    auto = data.get("auto_capture")
    if not isinstance(auto, dict):
        return HookConfig()

    session_sync_val = auto.get("session_sync", True)
    web_fetch_val = auto.get("web_fetch", True)
    compaction_val = auto.get("compaction", True)

    return HookConfig(
        session_sync=session_sync_val if isinstance(session_sync_val, bool) else True,
        web_fetch=web_fetch_val if isinstance(web_fetch_val, bool) else True,
        compaction=compaction_val if isinstance(compaction_val, bool) else True,
    )


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
    cwd_obj = payload.get("cwd")
    cwd = cwd_obj if isinstance(cwd_obj, str) else ""
    if not cwd:
        logger.debug("session-start: no cwd in payload, skipping")
        return {}

    config = load_hook_config(cwd)
    if not config.session_sync:
        logger.debug("session-start: disabled by config")
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


_WEB_CAPTURES_COLLECTION = "web-captures"


def _extract_url(payload: dict[str, object]) -> str | None:
    """Extract the fetched URL from a PostToolUse WebFetch payload."""
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        url = tool_input.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def _is_already_ingested(url: str, db: LanceDB) -> bool:
    """Check if *url* is already in the web-captures collection."""
    docs = list_documents(db, collection_filter=_WEB_CAPTURES_COLLECTION)
    return any(d["document_name"] == url for d in docs)


def handle_post_web_fetch(payload: dict[str, object]) -> dict[str, object]:
    """Handle PostToolUse on WebFetch.

    Extracts the fetched URL and ingests it into the ``web-captures``
    collection.  Skips URLs that are already ingested (dedup by
    document_name).
    """
    cwd_obj = payload.get("cwd")
    cwd = cwd_obj if isinstance(cwd_obj, str) else ""
    if cwd:
        config = load_hook_config(cwd)
        if not config.web_fetch:
            logger.debug("post-web-fetch: disabled by config")
            return {}

    url = _extract_url(payload)
    if not url:
        logger.debug("post-web-fetch: no valid URL in payload, skipping")
        return {}

    settings = _resolve_settings()
    db = get_db(settings.lancedb_path)

    if _is_already_ingested(url, db):
        logger.debug("post-web-fetch: already ingested %s, skipping", url)
        return {}

    result = ingest_url(
        url,
        db,
        settings,
        collection=_WEB_CAPTURES_COLLECTION,
    )
    logger.info(
        "post-web-fetch: ingested %s (%d chunks)",
        url,
        result["chunks"],
    )
    return {}


_SESSION_NOTES_COLLECTION = "session-notes"

_MAX_TRANSCRIPT_CHARS = 500_000


def _extract_message_text(record: dict[str, object]) -> str | None:
    """Extract text from a single transcript record, or None if not a message."""
    record_type = record.get("type", "")
    if record_type not in ("user", "assistant"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role", record_type)
    content = message.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        block["text"].strip()
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and str(block["text"]).strip()
    ]
    if not texts:
        return None
    return f"[{role}] {' '.join(texts)}"


def _extract_transcript_text(transcript_path: str) -> str:
    """Read a Claude Code transcript JSONL and extract conversation text.

    Extracts user and assistant messages, prefixing each with the role.
    Skips tool-use content blocks, file snapshots, and system messages.
    """
    import json as _json  # noqa: PLC0415

    path = Path(transcript_path)
    if not path.is_file():
        return ""

    try:
        raw = path.read_text()
    except (OSError, UnicodeDecodeError):
        logger.warning("pre-compact: could not read transcript %s", path)
        return ""

    parts: list[str] = []
    total_chars = 0
    for line in raw.splitlines():
        try:
            obj = _json.loads(line)
        except (ValueError, TypeError):
            continue
        entry = _extract_message_text(obj)
        if entry:
            entry_len = len(entry)
            if total_chars + entry_len >= _MAX_TRANSCRIPT_CHARS:
                break
            parts.append(entry)
            total_chars += entry_len
    return "\n\n".join(parts)


def handle_pre_compact(payload: dict[str, object]) -> dict[str, object]:
    """Handle PreCompact hook.

    Reads the conversation transcript before compaction and ingests it
    as a searchable document in the ``session-notes`` collection.
    Each compaction creates a new document keyed by session ID and
    timestamp.
    """
    cwd_obj = payload.get("cwd")
    cwd = cwd_obj if isinstance(cwd_obj, str) else ""
    if cwd:
        config = load_hook_config(cwd)
        if not config.compaction:
            logger.debug("pre-compact: disabled by config")
            return {}

    transcript_path = str(payload.get("transcript_path", ""))
    session_id = str(payload.get("session_id", ""))
    if not transcript_path or not session_id:
        logger.debug("pre-compact: missing transcript_path or session_id")
        return {}

    text = _extract_transcript_text(transcript_path)
    if not text:
        logger.debug("pre-compact: no conversation text found")
        return {}

    from datetime import UTC, datetime  # noqa: PLC0415

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    document_name = f"session-{session_id[:8]}-{timestamp}"

    settings = _resolve_settings()
    db = get_db(settings.lancedb_path)

    result = ingest_content(
        text,
        document_name,
        db,
        settings,
        collection=_SESSION_NOTES_COLLECTION,
        format_hint="markdown",
    )
    logger.info(
        "pre-compact: captured %s (%d chunks, %d chars)",
        document_name,
        result["chunks"],
        len(text),
    )
    return {}
