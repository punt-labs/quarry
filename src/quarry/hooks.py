"""Hook handlers for Claude Code integration.

Each handler receives the parsed stdin JSON from Claude Code and returns
a dict that will be serialized to stdout.  All handlers are fail-open:
exceptions are logged but never propagated to the caller.

Heavy imports (lancedb, pydantic, onnxruntime) are deferred to the
handler functions that actually need them.  The module-level imports
are stdlib-only so that ``quarry-hook`` can load this module without
paying the full dependency tax.

Hook events:
    session-start    — SessionStart: auto-register and sync the current repo.
    post-web-fetch   — PostToolUse on WebFetch: auto-ingest fetched URLs.
    pre-compact      — PreCompact: capture compaction summaries.
    pre-tool-hint    — PreToolUse on Bash: emit convention hints.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from quarry._stdlib import load_hook_config

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.sync_registry import DirectoryRegistration
    from quarry.types import LanceDB

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


def _unique_collection_name(
    conn: sqlite3.Connection,
    directory: Path,
) -> str:
    """Derive a collection name that doesn't collide with existing ones.

    Prefers ``directory.name``.  If that's taken (another directory with the
    same leaf name), appends the parent directory name to disambiguate:
    ``leaf-parent``.
    """
    from quarry.sync_registry import get_registration  # noqa: PLC0415

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
    from quarry.config import load_settings, resolve_db_paths  # noqa: PLC0415

    return resolve_db_paths(load_settings(), None)


def _sync_in_background() -> bool:
    """Fire-and-forget sync via detached subprocess.

    Uses ``sys.executable -m quarry`` to avoid PATH trust issues (the
    hook runs automatically on SessionStart with no user confirmation).
    Redirects all stdio to DEVNULL — especially stdin, to prevent the
    child from holding Claude Code's stdin pipe open after the parent
    exits.  The subprocess gets its own process group so it survives
    the hook process.

    Returns True if the subprocess was launched, False on failure.
    """
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    try:
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "quarry", "sync"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        logger.error("session-start: failed to launch background sync: %s", exc)
        return False
    logger.info("session-start: background sync launched")
    return True


def handle_session_start(payload: dict[str, object]) -> dict[str, object]:
    """Handle SessionStart hook.

    Auto-registers the current working directory (from the payload ``cwd``
    field) with quarry and kicks off a background sync.  Returns
    ``additionalContext`` immediately so Claude knows quarry is available
    without waiting for the sync to complete.
    """
    from quarry.sync_registry import (  # noqa: PLC0415
        list_registrations,
        open_registry,
        register_directory,
    )

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

        # Return context immediately; sync runs in background.
        launched = _sync_in_background()

        sync_line = (
            "Background sync in progress."
            if launched
            else "Background sync skipped (could not launch)."
        )
        context = (
            "Quarry semantic search is active for this project.\n"
            f'Collection: "{collection}" ({directory})\n'
            f"{sync_line}\n"
            "Use the quarry MCP tools (search_documents, get_page) to search "
            "this codebase semantically."
        )
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


def _extract_web_fetch_content(payload: dict[str, object]) -> str | None:
    """Extract already-fetched content from a PostToolUse WebFetch payload.

    The ``tool_response`` field is a JSON-encoded string containing the
    fetched HTML/text.  When present and valid, using this avoids a second
    network fetch and reduces SSRF exposure by not issuing an additional
    request from quarry itself on that code path.
    """
    import json as _json  # noqa: PLC0415

    tool_response = payload.get("tool_response")
    if not isinstance(tool_response, str):
        return None
    try:
        parsed = _json.loads(tool_response)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        result = parsed.get("result")
        if isinstance(result, str) and result.strip():
            return result
    if isinstance(parsed, str) and parsed.strip():
        return parsed
    return None


def _is_already_ingested(url: str, db: LanceDB) -> bool:
    """Check if *url* is already in the web-captures collection."""
    from quarry.database import list_documents  # noqa: PLC0415

    docs = list_documents(db, collection_filter=_WEB_CAPTURES_COLLECTION)
    return any(d["document_name"] == url for d in docs)


def handle_post_web_fetch(payload: dict[str, object]) -> dict[str, object]:
    """Handle PostToolUse on WebFetch.

    Ingests the already-fetched content from the hook payload into the
    ``web-captures`` collection.  Uses ``tool_response`` directly — no
    second network request.  Falls back to ``ingest_url`` only if the
    payload lacks content.  Skips URLs already ingested (dedup by
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

    # Heavy imports deferred past early-return guards.
    from quarry.database import get_db  # noqa: PLC0415
    from quarry.pipeline import ingest_content, ingest_url  # noqa: PLC0415

    settings = _resolve_settings()
    db = get_db(settings.lancedb_path)

    if _is_already_ingested(url, db):
        logger.debug("post-web-fetch: already ingested %s, skipping", url)
        return {}

    # Prefer already-fetched content from tool_response (avoids extra fetch).
    # Trade-off: chunks are tagged source_format="inline" instead of ".html"
    # since we strip HTML before ingestion. The URL is preserved as
    # document_name, which is the primary identifier in search results.
    content = _extract_web_fetch_content(payload)
    result = None
    if content:
        from quarry.html_processor import process_html_text  # noqa: PLC0415

        pages = process_html_text(content, url, url)
        if pages:
            clean_text = "\n\n".join(p.text for p in pages)
            result = ingest_content(
                clean_text,
                url,
                db,
                settings,
                collection=_WEB_CAPTURES_COLLECTION,
                format_hint="markdown",
            )
        else:
            logger.debug("post-web-fetch: no text in tool_response, falling back")

    if result is None:
        # Fallback: re-fetch if tool_response is missing/empty/boilerplate.
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

    # Defense-in-depth: reject non-JSONL paths (suffix check only).
    tp = Path(transcript_path).resolve()
    if tp.suffix != ".jsonl":
        logger.warning("pre-compact: unexpected suffix %s", tp.suffix)
        return {}

    text = _extract_transcript_text(transcript_path)
    if not text:
        logger.debug("pre-compact: no conversation text found")
        return {}

    # Heavy imports deferred past early-return guards.
    from datetime import UTC, datetime  # noqa: PLC0415

    from quarry.database import get_db  # noqa: PLC0415
    from quarry.pipeline import ingest_content  # noqa: PLC0415

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


def _sanitize_session_id(raw: str) -> str:
    """Sanitize *raw* to a safe filename component.

    Strips path separators and non-alphanumeric characters to prevent
    path traversal.  Returns only ``[a-zA-Z0-9_-]`` characters.
    """
    import re  # noqa: PLC0415

    return re.sub(r"[^a-zA-Z0-9_-]", "", raw)[:64]


def _hint_state_path(session_id: str) -> Path:
    """Return the path to the hint state file for *session_id*."""
    import tempfile  # noqa: PLC0415

    safe_id = _sanitize_session_id(session_id)
    if not safe_id:
        import hashlib  # noqa: PLC0415

        safe_id = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    tmpdir = Path(tempfile.gettempdir())
    return tmpdir / f"hint-state-{safe_id}.json"


def _write_hint_state(state_path: Path, acc_json: str) -> None:
    """Atomically write hint state with restricted permissions.

    Uses a temp file + replace to avoid partial writes and symlink attacks.
    """
    import os  # noqa: PLC0415
    import tempfile as _tf  # noqa: PLC0415

    parent = str(state_path.parent)
    with _tf.NamedTemporaryFile(
        mode="w",
        dir=parent,
        prefix=".hint-",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(acc_json)
        tmp.flush()
        os.fsync(tmp.fileno())
    tmp_path = Path(tmp.name)
    try:
        tmp_path.chmod(0o600)
        tmp_path.replace(state_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def handle_pre_tool_hint(
    payload: dict[str, object],
) -> dict[str, object]:
    """Handle PreToolUse on Bash: emit convention hints.

    Accumulates recent tool calls in a session-scoped state file and
    checks both instant and sequence rules.  Returns an allow decision
    with ``additionalContext`` when a hint fires, otherwise ``{}``.
    """
    import time  # noqa: PLC0415

    from quarry.hint_accumulator import HintAccumulator, ToolEvent  # noqa: PLC0415
    from quarry.hint_rules import (  # noqa: PLC0415
        check_instant_rules,
        check_sequence_rules,
    )

    # Extract command from payload.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return {}

    # Check config toggle.
    cwd_obj = payload.get("cwd")
    cwd = cwd_obj if isinstance(cwd_obj, str) else ""
    if cwd:
        config = load_hook_config(cwd)
        if not config.convention_hints:
            logger.debug("pre-tool-hint: disabled by config")
            return {}

    # Check instant rules first (no accumulator needed).
    hint = check_instant_rules(command)

    # Sequence rules require a valid session_id for state persistence.
    session_id_raw = payload.get("session_id")
    has_session = isinstance(session_id_raw, str) and bool(session_id_raw)

    if has_session:
        state_path = _hint_state_path(str(session_id_raw))
        try:
            state_data = state_path.read_text()
        except OSError:
            state_data = "[]"

        acc = HintAccumulator.from_json(state_data)

        # Check sequence rules if no instant hint fired.
        if hint is None:
            hint = check_sequence_rules(acc.recent(), command)

        # Add the current event to the accumulator.
        acc.add(ToolEvent(ts=time.time(), tool="Bash", command=command))

        # Persist state atomically (fail-open).
        try:
            _write_hint_state(state_path, acc.to_json())
        except OSError:
            logger.debug(
                "pre-tool-hint: could not write state to %s",
                state_path,
            )

    if hint:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": hint,
            },
        }
    return {}
