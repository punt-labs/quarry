"""The watch loop's roster: per-database connections and per-collection watches.

DES-045 §14.1 has the daemon watch every database in the operator's roster, not
only its startup-bound active database.  ``WatchRoster`` owns that state: one
persistent LanceDB connection per database (the active database reuses the
daemon's own connection, so the fd-plateau invariant holds across all of them —
quarry-0dss) and, per ``(database, collection)`` route key, the resolved root and
the observer handle for its tree.

Trust invariant (DES-045 §14.1): the only databases ever opened are those in the
operator's own on-disk roster — the directories under ``quarry_root`` that hold a
registry, the same set ``quarry databases`` lists.  No network or registry
request reaches this class with a database *root*; a request names a collection
within an already-open database, never a path to open.  Keep it that way.
"""

from __future__ import annotations

import gc
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.route_key import RouteKey
from quarry.db import Database
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.config import Settings
    from quarry.daemon.fs_events import FsEvent, FsEventSource

logger = logging.getLogger(__name__)


@final
@dataclass(slots=True)
class _CollectionWatch:
    """One watched tree: its resolved root and the observer handle to unschedule.

    ``handle`` is ``None`` when the tree could not be observed (inotify
    ``ENOSPC``, or sync-only mode) — the reconcile disk-scans it regardless.
    """

    resolved_root: Path
    handle: object | None


@final
class WatchRoster:
    """Own per-database connections and the per-``(database, collection)`` watch set."""

    __slots__ = (
        "_active_database",
        "_active_db",
        "_base_settings",
        "_conns",
        "_db_settings",
        "_source",
        "_watches",
    )

    _source: FsEventSource
    _active_db: str
    _active_database: Database
    _base_settings: Settings
    _conns: dict[str, Database]
    _db_settings: dict[str, Settings]
    _watches: dict[RouteKey, _CollectionWatch]

    def __new__(
        cls,
        source: FsEventSource,
        *,
        active_db: str,
        active_database: Database,
        base_settings: Settings,
    ) -> Self:
        self = super().__new__(cls)
        self._source = source
        self._active_db = active_db
        self._active_database = active_database
        self._base_settings = base_settings
        self._conns = {active_db: active_database}
        self._db_settings = {active_db: base_settings}
        self._watches = {}
        return self

    def roster_names(self) -> list[str]:
        """Return the active database plus every registered sibling under the root.

        A sibling counts as a roster member only if it holds a ``registry.db`` —
        an operator-created database, never an arbitrary path.  The active
        database is always included even before its registry exists.
        """
        names = {self._active_db}
        root = self._base_settings.quarry_root
        if root.exists():
            names.update(
                entry.name
                for entry in root.iterdir()
                if entry.is_dir() and (entry / "registry.db").exists()
            )
        return sorted(names)

    def ensure_database(self, name: str) -> None:
        """Open a persistent connection for *name* if the roster has none yet."""
        if name in self._conns:
            return
        settings = self._base_settings.model_copy(
            update={
                "lancedb_path": self._base_settings.quarry_root / name / "lancedb",
                "registry_path": self._base_settings.quarry_root / name / "registry.db",
            }
        )
        self._db_settings[name] = settings
        self._conns[name] = Database.connect(settings.lancedb_path)

    def database_of(self, name: str) -> Database:
        """Return *name*'s open connection (call :meth:`ensure_database` first)."""
        return self._conns[name]

    def settings_of(self, name: str) -> Settings:
        """Return *name*'s resolved settings (call :meth:`ensure_database` first)."""
        return self._db_settings[name]

    def registrations(self, name: str) -> list[tuple[str, Path]]:
        """Return *name*'s ``(collection, resolved_root)`` registrations from disk."""
        registry_path = self._db_settings[name].registry_path
        if not registry_path.exists():
            return []
        conn = SyncRegistry(registry_path)
        try:
            return [
                (reg.collection, Path(reg.directory))
                for reg in conn.list_registrations()
            ]
        finally:
            conn.close()

    def watch(
        self, key: RouteKey, resolved_root: Path, on_event: Callable[[FsEvent], None]
    ) -> None:
        """Schedule *key*'s tree on the source, replacing any prior watch."""
        self.unwatch(key)
        handle = self._source.schedule(resolved_root, on_event)
        self._watches[key] = _CollectionWatch(resolved_root, handle)

    def unwatch(self, key: RouteKey) -> None:
        """Stop watching *key*'s tree, if it is watched."""
        watch = self._watches.pop(key, None)
        if watch is not None:
            self._source.unschedule(watch.handle)

    def resolved_root(self, key: RouteKey) -> Path | None:
        """Return *key*'s watched root, or ``None`` if it is not (still) watched."""
        watch = self._watches.get(key)
        return None if watch is None else watch.resolved_root

    def keys(self) -> list[RouteKey]:
        """Return every currently-watched route key."""
        return list(self._watches)

    def unwatch_all(self) -> None:
        """Unschedule every watched tree (the source itself is stopped elsewhere)."""
        for key in list(self._watches):
            self.unwatch(key)

    def close(self) -> None:
        """Drop sibling database connections (the active DB's is owned elsewhere).

        A fresh ``start()`` after ``stop()`` would otherwise stack a second set
        of sibling connections; dropping the references and forcing a cyclic
        collect releases the LanceDB descriptors the binding holds in a reference
        cycle (plain refcounting does not free them — see ``LanceConnection``).
        """
        for name in list(self._conns):
            if name != self._active_db:
                self._conns.pop(name, None)
                self._db_settings.pop(name, None)
        gc.collect()
