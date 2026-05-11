"""Enable and disable quarry knowledge capture for project directories."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnableResult:
    """Result of enabling quarry for a project directory."""

    directory: str
    collection: str
    captures_collection: str
    memory_collections: list[str] = field(default_factory=list)
    config_path: str = ""
    created_registration: bool = False
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


_GLOBAL_IDENTITIES = Path.home() / ".punt-labs" / "ethos" / "identities"

_CONFIG_TEMPLATE = """\
---
auto_capture:
  session_sync: true
  web_fetch: true
  compaction: true
---

# Quarry Project Configuration

This file controls quarry's passive knowledge capture for this project.
Set any field to `false` to disable that capture type.

- `session_sync`: auto-index project files on session start
- `web_fetch`: auto-ingest URLs fetched during research
- `compaction`: capture session transcripts before context compaction
"""


def enable_project(
    directory: Path,
    collection_override: str = "",
) -> EnableResult:
    """Enable quarry knowledge capture for a project directory."""
    if not directory.is_dir():
        msg = f"directory not found: {directory}"
        raise ValueError(msg)

    from quarry.config import load_settings, resolve_db_paths  # noqa: PLC0415
    from quarry.sync_registry import open_registry  # noqa: PLC0415

    settings = resolve_db_paths(load_settings(), None)
    conn = open_registry(settings.registry_path)
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

    return EnableResult(
        directory=str(directory),
        collection=collection,
        captures_collection=captures_collection,
        memory_collections=memory_collections,
        config_path=config_path,
        created_registration=created,
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
    from quarry.config import load_settings, resolve_db_paths  # noqa: PLC0415
    from quarry.database import (  # noqa: PLC0415
        delete_collection as db_delete_collection,
        get_db,
    )
    from quarry.hooks import (  # noqa: PLC0415
        _collection_for_cwd_conn,  # pyright: ignore[reportPrivateUsage]
    )
    from quarry.sync_registry import (  # noqa: PLC0415
        deregister_directory,
        open_registry,
    )

    settings = resolve_db_paths(load_settings(), None)
    conn = open_registry(settings.registry_path)
    try:
        collection = _collection_for_cwd_conn(conn, str(directory))  # pyright: ignore[reportPrivateUsage]
        if collection is None:
            msg = f"no registration covers {directory}"
            raise ValueError(msg)

        captures_collection = f"{collection}-captures"
        deregister_directory(conn, collection)

        deleted_chunks = 0
        if not keep_data:
            db = get_db(settings.lancedb_path)
            deleted_chunks += db_delete_collection(db, collection)
            deleted_chunks += db_delete_collection(db, captures_collection)

        config_path = directory / ".punt-labs" / "quarry" / "config.md"
        config_removed = False
        if config_path.exists():
            config_path.unlink()
            config_removed = True

        quarry_dir = directory / ".punt-labs" / "quarry"
        if quarry_dir.is_dir() and not any(quarry_dir.iterdir()):
            quarry_dir.rmdir()

        return DisableResult(
            directory=str(directory),
            collection=collection,
            captures_collection=captures_collection,
            deleted_chunks=deleted_chunks,
            config_removed=config_removed,
        )
    finally:
        conn.close()


def _resolve_or_register(
    conn: sqlite3.Connection,
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
    from quarry.sync_registry import (  # noqa: PLC0415
        list_registrations,
        register_directory,
    )

    collection = _collection_for_cwd_conn(conn, str(directory))  # pyright: ignore[reportPrivateUsage]

    if collection is not None:
        # Determine whether this is an exact match or a parent match.
        registrations = list_registrations(conn)
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
    register_directory(conn, directory, name)
    return name, True


def _bootstrap_ethos_memory() -> tuple[list[str], list[str], list[str], bool]:
    """Create quarry.yaml ext files and write session_context.

    Unconditionally reads the global identities directory. Repo-level
    identities are read-only and must not be modified.

    Returns (created, updated, already_set, skipped) where skipped is
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

        result = _write_ethos_ext_session_context(quarry_yaml, handle)
        if result == "updated":
            updated.append(handle)
        elif result == "already_set":
            already_set.append(handle)

    return created, updated, already_set, False


def _write_project_config(directory: Path) -> str:
    """Write .punt-labs/quarry/config.md. Idempotent: no overwrite.

    Returns the config file path as a string.
    """
    config_dir = directory / ".punt-labs" / "quarry"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.md"
    if not config_path.exists():
        config_path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    return str(config_path)
