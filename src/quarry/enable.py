"""Enable and disable quarry knowledge capture for project directories."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quarry.sync_registry import SyncRegistry

logger = logging.getLogger(__name__)

_CLAUDEMD_BEGIN = "<!-- quarry:begin -->"
_CLAUDEMD_END = "<!-- quarry:end -->"

_CLAUDEMD_BLOCK = """\
<!-- quarry:begin -->
## Quarry

Local semantic search is available via quarry. Use it to search indexed
documents by meaning, ingest new content, and recall knowledge across sessions.

- Before using WebSearch or WebFetch for research, run `/find` with the query
  first. Quarry indexes this codebase, design docs, prior session transcripts,
  and web pages from previous research. If quarry returns relevant results,
  use them — do not re-research what has already been found.
- Use grep for symbol lookups and value lookups; use quarry for "why", "how",
  and "what did we decide about X" questions.
- **Slash commands**: `/find`, `/ingest`, `/remember`, `/explain`, `/source`,
  `/quarry`
- **Research agent**: `researcher` — combines quarry local search with web
  research. Use for deep investigation across local docs and the web.
- **Auto-behaviors**: working directory is auto-indexed at session start;
  URLs fetched via WebFetch are auto-ingested; transcripts are captured before
  context compaction.
- **Search tip**: natural language queries work best ("What were Q3 margins?"
  outperforms "Q3 margins").
<!-- quarry:end -->
"""


@dataclass(frozen=True)
class EnableResult:
    """Result of enabling quarry for a project directory."""

    directory: str
    collection: str
    captures_collection: str
    memory_collections: list[str] = field(default_factory=list)
    config_path: str = ""
    created_registration: bool = False
    claudemd_appended: bool = False
    ethos_skipped: bool = False
    ethos_updated: list[str] = field(default_factory=list)
    ethos_already_set: list[str] = field(default_factory=list)
    ethos_created: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DisableResult:
    """Result of disabling quarry for a project directory."""

    directory: str
    collection: str
    captures_collection: str
    deleted_chunks: int = 0
    config_removed: bool = False
    claudemd_removed: bool = False


_GLOBAL_IDENTITIES = Path.home() / ".punt-labs" / "ethos" / "identities"

_CONFIG_TEMPLATE = """\
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
# shadow:                          # push redacted captures to a PRIVATE shadow repo
#   enabled: false                 # opt-in network+security action, off by default
#   remote: ""                     # empty -> derive <origin>-quarry from origin
#   acknowledge_unverified: false  # push even when gh cannot confirm private
---

# Quarry Project Configuration

Controls quarry's passive knowledge capture. Set any field to `false` to disable
that capture type; uncomment `shadow` to move redacted captures off the public
repo into a per-project private shadow (`<repo>` -> `<repo>-quarry`).

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
- `shadow`: pre-create the private repo, then set `enabled: true`
"""


def enable_project(
    directory: Path,
    collection_override: str = "",
) -> EnableResult:
    """Enable quarry knowledge capture for a project directory."""
    directory = directory.resolve()
    if not directory.is_dir():
        msg = f"directory not found: {directory}"
        raise ValueError(msg)

    from quarry.config import Settings  # noqa: PLC0415
    from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

    settings = Settings.load().resolve_db_paths(None)
    conn = SyncRegistry(settings.registry_path)
    try:
        collection, created = _resolve_or_register(conn, directory, collection_override)
    finally:
        conn.close()

    captures_collection = f"{collection}-captures"

    created_handles, updated_handles, already_set_handles, ethos_skipped = (
        _bootstrap_ethos_memory()
    )

    memory_collections = [f"memory-{h}" for h in created_handles]

    config_path = _write_project_config(directory)
    claudemd_appended = _append_claudemd_block(directory)
    if claudemd_appended:
        logger.info("Appended quarry instructions to CLAUDE.md")

    return EnableResult(
        directory=str(directory),
        collection=collection,
        captures_collection=captures_collection,
        memory_collections=memory_collections,
        config_path=config_path,
        created_registration=created,
        claudemd_appended=claudemd_appended,
        ethos_skipped=ethos_skipped,
        ethos_updated=updated_handles,
        ethos_already_set=already_set_handles,
        ethos_created=created_handles,
    )


def disable_project(
    directory: Path,
    *,
    keep_data: bool = False,
) -> DisableResult:
    """Disable quarry knowledge capture for a project directory."""
    directory = directory.resolve()
    from quarry.config import Settings  # noqa: PLC0415
    from quarry.db.chunk_store import ChunkStore  # noqa: PLC0415
    from quarry.db.storage import get_db  # noqa: PLC0415
    from quarry.hooks import (  # noqa: PLC0415
        _collection_for_cwd_conn,  # pyright: ignore[reportPrivateUsage]
    )
    from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

    settings = Settings.load().resolve_db_paths(None)
    conn = SyncRegistry(settings.registry_path)
    try:
        collection = _collection_for_cwd_conn(conn, str(directory))  # pyright: ignore[reportPrivateUsage]
        if collection is None:
            msg = f"no registration covers {directory}"
            raise ValueError(msg)

        # Guard against walk-up match deleting a parent registration.
        registrations = conn.list_registrations()
        match = next((r for r in registrations if r.collection == collection), None)
        if match is not None and match.directory != str(directory):
            msg = (
                f"no registration for {directory}; "
                f"it is covered by parent registration at {match.directory}"
            )
            raise ValueError(msg)

        captures_collection = f"{collection}-captures"
        conn.deregister_directory(collection)

        deleted_chunks = 0
        if not keep_data:
            db = get_db(settings.lancedb_path)
            store = ChunkStore(db)
            deleted_chunks += store.delete_collection(collection)
            deleted_chunks += store.delete_collection(captures_collection)

        config_path = directory / ".punt-labs" / "quarry" / "config.md"
        config_removed = False
        if config_path.exists():
            config_path.unlink()
            config_removed = True

        quarry_dir = directory / ".punt-labs" / "quarry"
        if quarry_dir.is_dir() and not any(quarry_dir.iterdir()):
            quarry_dir.rmdir()

        claudemd_removed = _remove_claudemd_block(directory)
        if claudemd_removed:
            logger.info("Removed quarry instructions from CLAUDE.md")

        return DisableResult(
            directory=str(directory),
            collection=collection,
            captures_collection=captures_collection,
            deleted_chunks=deleted_chunks,
            config_removed=config_removed,
            claudemd_removed=claudemd_removed,
        )
    finally:
        conn.close()


def _resolve_or_register(
    conn: SyncRegistry,
    directory: Path,
    collection_override: str,
) -> tuple[str, bool]:
    """Find existing registration or create one.

    Returns (collection_name, created_bool).
    Raises ValueError for parent-covered-child case.
    """
    from quarry.hooks import (  # noqa: PLC0415
        _collection_for_cwd_conn,  # pyright: ignore[reportPrivateUsage]
        _unique_collection_name,  # pyright: ignore[reportPrivateUsage]
    )

    collection = _collection_for_cwd_conn(conn, str(directory))  # pyright: ignore[reportPrivateUsage]

    if collection is not None:
        # Determine whether this is an exact match or a parent match.
        registrations = conn.list_registrations()
        for reg in registrations:
            if reg.collection == collection and reg.directory == str(directory):
                # Exact match -- reuse.
                return collection, False
        # Parent match -- the directory is a child of an existing registration.
        parent_reg = next(r for r in registrations if r.collection == collection)
        msg = (
            f"This directory is already covered by the registration at "
            f"{parent_reg.directory} (collection: {parent_reg.collection}). "
            f"Sessions here use that collection automatically. No action needed."
        )
        raise ValueError(msg)

    # No coverage -- create a new registration.
    name = collection_override or _unique_collection_name(conn, directory)  # pyright: ignore[reportPrivateUsage]
    conn.register_directory(directory, name)
    return name, True


def _bootstrap_ethos_memory() -> tuple[list[str], list[str], list[str], bool]:
    """Create quarry.yaml ext files and write session_context.

    Reads only the global identities directory (repo-level identities are
    read-only). Returns (created, updated, already_set, skipped); skipped is
    True when the global identities directory does not exist.
    """
    from quarry.doctor import (  # noqa: PLC0415
        _write_ethos_ext_session_context,  # pyright: ignore[reportPrivateUsage]
    )

    if not _GLOBAL_IDENTITIES.is_dir():
        return [], [], [], True

    created: list[str] = []
    updated: list[str] = []
    already_set: list[str] = []
    for identity_file in sorted(_GLOBAL_IDENTITIES.glob("*.yaml")):
        handle = identity_file.stem
        ext_dir = _GLOBAL_IDENTITIES / f"{handle}.ext"
        ext_dir.mkdir(exist_ok=True)
        quarry_yaml = ext_dir / "quarry.yaml"
        if not quarry_yaml.exists():
            quarry_yaml.write_text(
                f"memory_collection: memory-{handle}\n",
                encoding="utf-8",
            )
            created.append(handle)

        try:
            result = _write_ethos_ext_session_context(quarry_yaml, handle)
        except Exception:  # noqa: BLE001
            logger.warning("failed to write session context for %s", handle)
            continue
        if result == "updated":
            updated.append(handle)
        elif result == "already_set":
            already_set.append(handle)

    return created, updated, already_set, False


def _write_project_config(directory: Path) -> str:
    """Write config.md atomically (O_CREAT|O_EXCL, no overwrite); return its path."""
    config_dir = directory / ".punt-labs" / "quarry"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.md"
    try:
        fd = os.open(str(config_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(fd, "w") as f:
                fd = -1  # fdopen took ownership before write
                f.write(_CONFIG_TEMPLATE)
        finally:
            if fd >= 0:
                os.close(fd)
    except FileExistsError:
        pass
    return str(config_path)


def _append_claudemd_block(directory: Path) -> bool:
    """Append quarry instruction block to CLAUDE.md. Idempotent.

    Creates the file if it does not exist. Returns True if the block
    was appended, False if it was already present.
    """
    claudemd = directory / "CLAUDE.md"
    if claudemd.exists():
        content = claudemd.read_text(encoding="utf-8")
        if _CLAUDEMD_BEGIN in content:
            return False
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + _CLAUDEMD_BLOCK
    else:
        content = _CLAUDEMD_BLOCK
    claudemd.write_text(content, encoding="utf-8")
    return True


def _remove_claudemd_block(directory: Path) -> bool:
    """Remove quarry instruction block from CLAUDE.md.

    Removes everything from ``<!-- quarry:begin -->`` through
    ``<!-- quarry:end -->`` inclusive. Both markers must be present.
    Cleans up extra trailing blank lines left by removal. Returns
    True if a block was removed, False otherwise.
    """
    claudemd = directory / "CLAUDE.md"
    if not claudemd.exists():
        return False
    content = claudemd.read_text(encoding="utf-8")
    if _CLAUDEMD_BEGIN not in content or _CLAUDEMD_END not in content:
        return False
    pattern = (
        r"\n?" + re.escape(_CLAUDEMD_BEGIN) + r".*?" + re.escape(_CLAUDEMD_END) + r"\n?"
    )
    cleaned = re.sub(pattern, "", content, flags=re.DOTALL)
    cleaned = cleaned.rstrip() + "\n"
    claudemd.write_text(cleaned, encoding="utf-8")
    return True
