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
    pre-compact      — PreCompact: ingest the pre-compaction conversation transcript.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from quarry._stdlib import load_hook_config
from quarry.web_capture import WebFetchPayload

if TYPE_CHECKING:
    from quarry.api import CaptureIngestRequest
    from quarry.artifacts import SessionArtifacts
    from quarry.config import Settings
    from quarry.db.facade import Database
    from quarry.sync_registry import DirectoryRegistration, SyncRegistry

logger = logging.getLogger(__name__)


def _find_registration(  # pyright: ignore[reportUnusedFunction]
    registrations: list[DirectoryRegistration],
    directory: str,
) -> DirectoryRegistration | None:
    """Find an existing registration matching *directory*."""
    for reg in registrations:
        if reg.directory == directory:
            return reg
    return None


def _unique_collection_name(
    conn: SyncRegistry,
    directory: Path,
) -> str:
    """Derive a collection name that doesn't collide with existing ones.

    Prefers ``directory.name``.  If that's taken (another directory with the
    same leaf name), appends the parent directory name to disambiguate:
    ``leaf-parent``.
    """
    candidate = directory.name
    if conn.get_registration(candidate) is None:
        return candidate
    # Disambiguate with parent directory name.
    parent = directory.parent.name or "root"
    candidate = f"{directory.name}-{parent}"
    if conn.get_registration(candidate) is None:
        return candidate
    # Last resort: use the full resolved path hash suffix.
    import hashlib  # noqa: PLC0415

    suffix = hashlib.sha256(str(directory).encode()).hexdigest()[:8]
    return f"{directory.name}-{suffix}"


def _resolve_settings() -> Settings:
    """Load settings resolved for the default database."""
    from quarry.config import Settings  # noqa: PLC0415

    return Settings.load().resolve_db_paths(None)


def _sync_lockfile() -> Path:
    """Return the path to the sync lock file in a user-owned directory."""
    return Path.home() / ".punt-labs" / "quarry" / "sync.pid"


def _is_sync_running() -> bool:
    """Check if a quarry sync process is already running via PID file.

    Returns True if a live sync process exists, False otherwise.
    Stale PID files (process no longer running) are cleaned up.

    Handles signal-0 results correctly:
    - ProcessLookupError → process is gone (stale)
    - PermissionError (EPERM) → process exists, another user (running)
    - ValueError → corrupt PID file (stale)
    """
    import os  # noqa: PLC0415

    pidfile = _sync_lockfile()
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text().strip())
        if pid <= 0:
            raise ValueError("non-positive PID")
        os.kill(pid, 0)
        return True
    except PermissionError:
        # EPERM: process exists but we can't signal it — treat as running.
        return True
    except (ValueError, ProcessLookupError):
        # Stale PID file — process is gone or PID is garbage.
        with contextlib.suppress(OSError):
            pidfile.unlink()
        return False


def _acquire_sync_lock() -> int | None:
    """Atomically create the sync lock file and return the fd.

    Uses O_CREAT|O_EXCL to prevent TOCTOU races: if the file already
    exists, os.open raises FileExistsError and no lock is acquired.

    Returns the file descriptor on success, None if the lock is held
    or on any OS error.
    """
    import os  # noqa: PLC0415

    pidfile = _sync_lockfile()
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(
            str(pidfile),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return None
    except OSError as exc:
        logger.error("session-start: failed to create lock file: %s", exc)
        return None


def _sync_in_background() -> str:
    """Fire-and-forget sync via detached subprocess.

    Uses ``sys.executable -m quarry`` to avoid PATH trust issues (the
    hook runs automatically on SessionStart with no user confirmation).
    Redirects all stdio to DEVNULL — especially stdin, to prevent the
    child from holding Claude Code's stdin pipe open after the parent
    exits.  The subprocess gets its own process group so it survives
    the hook process.

    Guards against concurrent syncs via an atomic lock file in
    ``~/.punt-labs/quarry/sync.pid``.  Uses O_CREAT|O_EXCL to prevent TOCTOU
    races between concurrent SessionStart hooks.

    Returns ``"launched"`` if the subprocess was started, ``"running"``
    if a sync is already in progress (or the lock is held), or
    ``"failed"`` if the launch itself errored.
    """
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    # Fast path: if a sync is already running, skip without trying the lock.
    if _is_sync_running():
        logger.debug("session-start: sync already running, skipping")
        return "running"

    # Atomic lock acquisition — prevents TOCTOU races.
    fd = _acquire_sync_lock()
    if fd is None:
        logger.debug("session-start: could not acquire sync lock, skipping")
        return "running"

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "quarry", "sync"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        logger.error("session-start: failed to launch background sync: %s", exc)
        # Clean up the lock file since no sync is running.
        os.close(fd)
        with contextlib.suppress(OSError):
            _sync_lockfile().unlink()
        return "failed"

    # Write the PID to the lock file (fd is already open).
    try:
        os.write(fd, str(proc.pid).encode())
    except OSError as exc:
        logger.warning("session-start: sync launched but pidfile write failed: %s", exc)
    finally:
        os.close(fd)

    logger.info("session-start: background sync launched (pid=%d)", proc.pid)
    return "launched"


def handle_session_start(payload: dict[str, object]) -> dict[str, object]:
    """Handle SessionStart hook.

    Auto-registers the current working directory (from the payload ``cwd``
    field) with quarry and kicks off a background sync.  Returns
    ``additionalContext`` immediately so Claude knows quarry is available
    without waiting for the sync to complete.

    Walk-up matching: if cwd is a child of an existing registration, the
    parent's collection is reused (no new registration).  Auto-register
    only fires when no coverage exists.  If cwd is a parent of existing
    child registrations, auto-register is skipped to prevent subsumption.
    """
    from quarry.sync_registry import (  # noqa: PLC0415
        SyncRegistry,
        _is_ancestor_of,  # pyright: ignore[reportPrivateUsage]
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
    conn = SyncRegistry(settings.registry_path)
    try:
        # Step 1: Walk up from cwd to find covering registration.
        collection = _collection_for_cwd_conn(conn, str(directory))

        if collection is None:
            # Step 2: No coverage -- check for descendant registrations
            # before auto-registering.  A parent registration would
            # subsume existing child registrations, causing data loss.
            registrations = conn.list_registrations()
            has_children = any(
                _is_ancestor_of(directory, Path(r.directory))  # pyright: ignore[reportPrivateUsage]
                for r in registrations
            )
            if has_children:
                logger.warning(
                    "session-start: existing child registrations found "
                    "under %s; skipping auto-register to prevent "
                    "subsumption. Run 'quarry enable %s' to explicitly "
                    "register the parent.",
                    directory,
                    directory,
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": (
                            f"Quarry: child registrations exist under {directory}. "
                            "Auto-register skipped to prevent subsumption. "
                            f"Run 'quarry enable {directory}' to register the parent."
                        ),
                    },
                }

            collection = _unique_collection_name(conn, directory)
            conn.register_directory(directory, collection)
            logger.info(
                "session-start: auto-registered %s as '%s'",
                directory,
                collection,
            )

        captures_collection = f"{collection}-captures"

        # Return context immediately; sync runs in background.
        sync_status = _sync_in_background()

        if sync_status == "launched":
            sync_line = "Background sync in progress."
        elif sync_status == "running":
            sync_line = "Background sync already running."
        else:
            sync_line = "Background sync failed to launch."
        context = (
            "Quarry semantic search is active for this project.\n"
            f'Collection: "{collection}" ({directory})\n'
            f'Captures: "{captures_collection}"\n'
            f"{sync_line}\n"
            "Use the quarry MCP tools (find, show, ingest, remember) "
            "to search this codebase semantically.\n"
            "Slash commands: /find, /ingest, /remember, /explain, "
            "/source, /quarry.\n"
            "For deep research across local docs and the web, use the "
            "researcher agent."
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        }
    finally:
        conn.close()


WEB_CAPTURES_FALLBACK = "web-captures"
_SESSION_NOTES_FALLBACK = "session-notes"


def _collection_for_cwd_conn(
    conn: SyncRegistry,
    cwd: str,
) -> str | None:
    """Resolve the registered collection for cwd using an open connection.

    Walk up from cwd to find a registered parent or exact match.
    """
    registrations = conn.list_registrations()
    if not registrations:
        return None

    reg_map = {r.directory: r.collection for r in registrations}
    current = Path(cwd).resolve()
    while True:
        key = str(current)
        if key in reg_map:
            return reg_map[key]
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _collection_for_cwd(cwd: str) -> str | None:
    """Resolve the registered collection for a working directory.

    Walks up from *cwd* to find a registered parent directory (or exact
    match).  Returns the collection name, or ``None`` if no registration
    covers *cwd*.
    """
    if not cwd:
        return None

    from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

    settings = _resolve_settings()
    conn = SyncRegistry(settings.registry_path)
    try:
        return _collection_for_cwd_conn(conn, cwd)
    finally:
        conn.close()


def _is_already_ingested(url: str, database: Database, collection: str) -> bool:
    """Check if *url* is already in the given collection."""
    docs = database.catalog.list_documents(collection_filter=collection)
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

    parsed = WebFetchPayload(payload)
    url = parsed.url
    if not url:
        logger.debug("post-web-fetch: no valid URL in payload, skipping")
        return {}

    # Heavy imports deferred past early-return guards.
    from quarry.db.facade import Database  # noqa: PLC0415
    from quarry.ingestion.pipeline import ingest_content, ingest_url  # noqa: PLC0415

    base_collection = _collection_for_cwd(cwd)
    collection = (
        f"{base_collection}-captures" if base_collection else WEB_CAPTURES_FALLBACK
    )

    settings = _resolve_settings()
    database = Database.connect(settings.lancedb_path)

    from quarry.capture_url import CaptureUrl  # noqa: PLC0415
    from quarry.scrub import scrub_and_log  # noqa: PLC0415

    # Both ingest paths scrub before content reaches the pushable web-captures
    # collection, so DB ingress is PII-clean on primary AND fallback.
    def web_fetch_scrub(raw: str) -> str:
        return scrub_and_log(raw, "web-fetch")

    # A capture must not persist the URL's userinfo/query/fragment as document
    # metadata. Redact once and reuse for dedup and the primary branch; the
    # fallback ingest_url redacts identically, so the stored name/path matches.
    meta_url = CaptureUrl(url).redacted(web_fetch_scrub)

    if _is_already_ingested(meta_url, database, collection):
        logger.debug("post-web-fetch: already ingested %s, skipping", meta_url)
        return {}

    # Prefer already-fetched tool_response content (avoids a second fetch).
    content = parsed.content
    result = None
    if content:
        from quarry.extractors.html_extractor import HtmlExtractor  # noqa: PLC0415

        pages = HtmlExtractor().extract_from_html(content, meta_url, meta_url)
        if pages:
            clean_text = web_fetch_scrub("\n\n".join(p.text for p in pages))
            result = ingest_content(
                clean_text,
                meta_url,
                database,
                settings,
                collection=collection,
                format_hint="markdown",
            )
        else:
            logger.debug("post-web-fetch: no text in tool_response, falling back")

    if result is None:
        # Fallback re-fetch; ingest_url redacts the URL and scrubs each page, so
        # this capture is stored identically to the primary branch.
        result = ingest_url(
            url,
            database,
            settings,
            collection=collection,
            content_scrubber=web_fetch_scrub,
        )
    logger.info(
        "post-web-fetch: ingested %s (%d chunks)",
        meta_url,
        result["chunks"],
    )
    return {}


def _read_ethos_agent_handle(cwd: str) -> str:
    """Read the agent handle from the ethos sidecar config.

    Looks for ``.punt-labs/ethos/config.yaml`` relative to *cwd* and
    walks up to the filesystem root.  Returns the ``agent`` field value
    (which is the agent handle), or empty string if not found.
    """
    import yaml as _yaml  # noqa: PLC0415

    current = Path(cwd).resolve()
    while True:
        config_path = current / ".punt-labs" / "ethos" / "config.yaml"
        if config_path.is_file():
            try:
                data = _yaml.safe_load(config_path.read_text())
            except (OSError, _yaml.YAMLError):
                logger.warning(
                    "pre-compact: could not parse ethos config %s",
                    config_path,
                    exc_info=True,
                )
                return ""
            if isinstance(data, dict):
                agent = data.get("agent", "")
                if isinstance(agent, str) and agent:
                    return agent
            return ""
        parent = current.parent
        if parent == current:
            break
        current = parent
    return ""


_MAX_TRANSCRIPT_CHARS = 500_000


_MAX_TOOL_RESULT_CHARS = 500


def _extract_tool_result_text(block: dict[str, object]) -> str:
    """Extract text from a tool_result content block.

    Returns the concatenated text if under _MAX_TOOL_RESULT_CHARS, else empty string.
    """
    tool_content = block.get("content")
    tool_text = ""
    if isinstance(tool_content, str):
        tool_text = tool_content.strip()
    elif isinstance(tool_content, list):
        parts = [
            str(b["text"]).strip()
            for b in tool_content
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        ]
        tool_text = " ".join(parts)
    if tool_text and len(tool_text) <= _MAX_TOOL_RESULT_CHARS:
        return tool_text
    return ""


def _extract_content_texts(content: list[object]) -> list[str]:
    """Extract text fragments from a list of content blocks."""
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            stripped = str(block["text"]).strip()
            if stripped:
                texts.append(stripped)
        elif block_type == "tool_result":
            tool_text = _extract_tool_result_text(block)
            if tool_text:
                texts.append(f"[tool_result] {tool_text}")
    return texts


def extract_message_text(record: dict[str, object]) -> str | None:
    """Extract text from a single transcript record, or None if not a message."""
    record_type = record.get("type", "")
    if record_type not in ("user", "assistant"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role", record_type)
    content = message.get("content")
    if isinstance(content, str):
        return f"[{role}] {content}" if content.strip() else None
    if not isinstance(content, list):
        return None
    texts = _extract_content_texts(content)
    if not texts:
        return None
    return f"[{role}] {' '.join(texts)}"


def extract_transcript_text(transcript_path: str) -> str:
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
    for line in raw.splitlines():
        try:
            obj = _json.loads(line)
        except (ValueError, TypeError):
            continue
        entry = extract_message_text(obj)
        if entry:
            parts.append(entry)

    # Front-truncation: drop oldest entries until total fits within budget.
    total_chars = sum(len(p) for p in parts)
    start = 0
    while start < len(parts) and total_chars > _MAX_TRANSCRIPT_CHARS:
        total_chars -= len(parts[start])
        start += 1
    if start > 0:
        logger.debug(
            "pre-compact: dropped %d oldest entries from transcript",
            start,
        )
        parts = parts[start:]

    return "\n\n".join(parts)


_ARCHIVE_RETENTION_DAYS = 90


def _archive_transcript(
    transcript_path: Path,
    session_id: str,
    sessions_dir: Path,
) -> None:
    """Copy raw JSONL transcript to the sessions archive directory.

    Creates the directory if needed, deduplicates prior archives for the
    same session, and lazily prunes files older than ``_ARCHIVE_RETENTION_DAYS``.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    sessions_dir.mkdir(parents=True, exist_ok=True)

    prefix = f"session-{session_id[:8]}-"

    # Copy first — prior archives survive if this fails.
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    dest = sessions_dir / f"{prefix}{timestamp}.jsonl"
    shutil.copy(transcript_path, dest)

    # Then dedup: remove prior archives, excluding the one we just wrote.
    for existing in sessions_dir.glob(f"{prefix}*.jsonl"):
        if existing != dest:
            with contextlib.suppress(OSError):
                existing.unlink()

    # Lazy retention cleanup.
    now = datetime.now(UTC).timestamp()
    retention_seconds = _ARCHIVE_RETENTION_DAYS * 86400
    for f in sessions_dir.glob("session-*.jsonl"):
        with contextlib.suppress(OSError):
            if now - f.stat().st_mtime > retention_seconds:
                f.unlink()


def _write_capture_file(
    project_dir: Path,
    session_id: str,
    timestamp: str,
    artifacts: SessionArtifacts,
    text: str,
) -> None:
    """Write the PreCompact session capture via the shared CaptureWriter.

    The writer scrubs secrets, PII, and profanity before any bytes reach the
    git-tracked capture file, and fails silently so capture issues never
    block the main ingest flow.
    """
    from quarry.capture import CaptureRequest, CaptureWriter  # noqa: PLC0415

    CaptureWriter().write(
        CaptureRequest(
            project_dir=project_dir,
            session_id=session_id,
            timestamp=timestamp,
            artifacts=artifacts,
            text=text,
            label="pre-compact",
        )
    )


def _capture_via_daemon(req: CaptureIngestRequest) -> bool:
    """Send a capture to the local daemon; return False if it could not deliver.

    The hook imports only the thin client — no engine.  A down or unreachable
    daemon is not fatal: the durable ``.md`` and JSONL archive are already
    written, and ``backfill-sessions`` re-ingests later, so this returns False
    and the caller shows an "archived for backfill" message instead of blocking.
    """
    from quarry.client import QuarryError, TargetResolver  # noqa: PLC0415

    try:
        TargetResolver.connect().capture(req)
    except QuarryError:
        logger.warning("capture: daemon unreachable; archived for backfill")
        return False
    return True


def handle_pre_compact(payload: dict[str, object]) -> dict[str, object]:
    """Handle PreCompact hook.

    Archives the raw transcript and writes the scrubbed ``.md`` capture locally,
    then sends the conversation text to the daemon to embed in the background.
    Returns the systemMessage immediately so compaction is never blocked, and a
    down daemon still leaves the durable local copies for ``backfill-sessions``.
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

    # Archive raw JSONL before extraction.
    sessions_dir = Path.home() / ".punt-labs" / "quarry" / "sessions"
    try:
        _archive_transcript(tp, session_id, sessions_dir)
    except Exception:
        logger.exception("pre-compact: archival failed, proceeding with ingest")

    text = extract_transcript_text(transcript_path)
    if not text:
        logger.debug("pre-compact: no conversation text found")
        return {}

    from quarry.artifacts import (  # noqa: PLC0415
        extract_artifacts,
        format_artifacts_header,
    )

    artifacts = extract_artifacts(text)
    raw_text = text  # preserve before header prepend for capture file
    header = format_artifacts_header(artifacts)
    if header:
        text = header + "\n\n" + text

    from datetime import UTC, datetime  # noqa: PLC0415

    agent_handle = _read_ethos_agent_handle(cwd) if cwd else ""

    # Write the scrubbed .md capture to the project directory (durable copy).
    if cwd:
        iso_timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_capture_file(
            project_dir=Path(cwd),
            session_id=session_id,
            timestamp=iso_timestamp,
            artifacts=artifacts,
            text=raw_text,
        )

    from quarry.api import CaptureIngestRequest  # noqa: PLC0415

    req = CaptureIngestRequest(
        content=text,
        cwd=cwd,
        session_id=session_id,
        agent_handle=agent_handle,
        format_hint="markdown",
    )
    if not _capture_via_daemon(req):
        return {
            "systemMessage": (
                "Warning: quarryd is not reachable, so this session was not "
                "indexed now. The raw JSONL archive and scrubbed capture are "
                "saved; run 'quarry backfill-sessions' to index them later."
            ),
        }

    return {
        "systemMessage": (
            "Capturing this session's conversation (background). "
            "Search with /find or show to retrieve it."
        ),
    }
