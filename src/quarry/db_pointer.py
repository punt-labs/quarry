"""The on-disk pointer to the database in use, and this process's override of it."""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, Final, Self, final

from quarry.config import Settings

if TYPE_CHECKING:
    from pathlib import Path

_POINTER_FILE: Final[str] = "config.toml"

# The name that means "no persisted choice". Writing it is refused (clear() says
# the same thing without ambiguity); reading it still means absent, because that
# is what a file naming the default database conveys.
_UNSET: Final[str] = "default"

# A database name becomes both a path segment and a bare TOML string. This admits
# only what survives round-tripping through the file unescaped and unambiguously:
# no quotes or backslashes to break the syntax, no separators or dot segments to
# escape the root. Anything else is refused at the write boundary rather than
# written into a file that would read back as "nothing selected".
_PERSISTABLE_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@final
class DatabaseSelection:
    """A per-process override layered over a default persisted on disk.

    Two things travel together and so belong in one class: the ``--db`` flag the
    current process was started with, and the default recorded in
    ``config.toml``. The override wins when set; otherwise the persisted default
    answers. Callers ask for the effective choice and do not resolve the
    precedence themselves.

    Recording the override matters beyond convenience: the client tier resolves
    the daemon's startup-db run directory from it, and ``serve.token`` lives in
    that directory. Client and daemon therefore agree on which database is in
    play only because both derive it from a matching ``--db``.

    Keep mutable process-scoped selection and its TOML file format separate
    from the ``Settings`` configuration container.
    """

    __slots__ = ("_override",)

    _override: str

    def __new__(cls) -> Self:
        """Start with no process override so the persisted default governs selection."""
        self = super().__new__(cls)
        self._override = ""
        return self

    @property
    def path(self) -> Path:
        """Return the pointer file's location, resolved per access.

        A sibling of the data root, and derived from the same
        ``Settings.data_root()`` the data itself uses, so *however* the root is
        configured — process environment or ``.env`` — the pointer moves with
        it. Deriving this from a separate environment read instead would agree
        only for the process-environment case, and under a ``.env``-configured
        root would leave the pointer beside the operator's real config while the
        data went elsewhere.

        Resolved on every access rather than bound once: a path fixed at import
        time cannot follow an environment the caller sets afterwards.
        """
        return Settings.data_root().parent / _POINTER_FILE

    def persisted(self) -> str | None:
        """Return the database recorded on disk, or None when none is.

        ``None`` is the documented contract for absence, not a swallowed
        failure: no file, a malformed file, and a file naming the default
        database all mean "nothing has been chosen", and every caller treats
        them alike.

        Only a *missing* file reads as absence. An unreadable one — a permission
        or I/O failure — propagates, because "the operator's choice cannot be
        read" is not the same fact as "the operator chose nothing", and silently
        conflating them would send the caller to the wrong database.
        """
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        name = str(data.get("default", {}).get("database", ""))
        return name if name and name != _UNSET else None

    def persist(self, name: str) -> None:
        """Record *name* as the default database for future processes.

        Refuses a name that would not survive the round trip. Written
        unescaped, a name carrying a quote or backslash yields a file that
        ``persisted`` cannot parse and therefore reads back as "nothing
        selected" — the operator's choice lost with no error at either end.

        The write is atomic: a full temporary file replaced into position, so a
        crash mid-write leaves the previous pointer intact rather than a
        truncated one. The temporary is removed if anything fails, so a failed
        write leaves nothing behind.
        """
        self._reject_unpersistable(name)
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        try:
            tmp.write_text(f'[default]\ndatabase = "{name}"\n')
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        """Forget the persisted default, leaving the process override alone."""
        self.path.unlink(missing_ok=True)

    @staticmethod
    def _reject_unpersistable(name: str) -> None:
        """Raise unless *name* can be written and read back as itself."""
        if name == _UNSET:
            msg = (
                f"{_UNSET!r} names the absence of a selection, not a selection; "
                f"call clear() to forget the persisted default"
            )
            raise ValueError(msg)
        if not _PERSISTABLE_NAME.match(name):
            msg = f"Database name cannot be persisted: {name!r}"
            raise ValueError(msg)

    def override(self, name: str) -> None:
        """Record this process's ``--db`` choice; an empty name clears it."""
        self._override = name

    def active(self) -> str | None:
        """Return the effective database: the override, else the persisted default."""
        return self._override or self.persisted()


# The override is a property of the running process, so one instance stands for
# it. Tests build their own to keep an override out of the shared one.
SELECTION: Final[DatabaseSelection] = DatabaseSelection()
