"""Shared cwd-to-collection ancestor walk for suffixed per-project collections.

:class:`~quarry.captures_collection.CapturesCollection` and
:class:`~quarry.lesson.LessonsCollection` both derive a suffixed collection
name (``<repo>-captures``, ``<repo>-lessons``) from a client-sent ``cwd``
resolved against the sync registry. The walk itself -- find the nearest
registered ancestor of *cwd* -- does not depend on the suffix, so it lives
here once rather than as a private method either class would have to expose
to the other.
"""

from __future__ import annotations

from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from collections.abc import Mapping


@final
class CollectionRouting:
    """Namespace for the shared cwd-to-collection ancestor walk."""

    __slots__ = ()

    @staticmethod
    def covering_collection(cwd: str, registrations: Mapping[str, str]) -> str | None:
        """Return the base collection of the registered ancestor of *cwd*."""
        # A blank or RELATIVE cwd is "unregistered", not the daemon's own dir:
        # both resolve against the daemon PROCESS's cwd, which -- if quarryd
        # was started inside a repo checkout -- would misfile the request
        # into that project. cwd is untrusted client input; only an absolute
        # path names a real client directory.
        if not registrations or not Path(cwd).is_absolute():
            return None
        if (current := CollectionRouting._resolved(cwd)) is None:
            return None
        # Iterate ancestors lazily; never materialize the full parent list --
        # an untrusted deep cwd (``/a/a/.../a``) would retain O(depth^2) prefixes.
        for path in chain((current,), current.parents):
            if (collection := registrations.get(str(path))) is not None:
                return collection
        return None

    @staticmethod
    def _resolved(cwd: str) -> Path | None:
        try:
            return Path(cwd).resolve()
        except (OSError, ValueError):
            # An embedded NUL or OS-invalid path falls back to the caller's default.
            return None
