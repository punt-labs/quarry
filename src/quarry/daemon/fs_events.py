"""The filesystem-event seam: the value type and the source Protocol.

Splitting the seam from its watchdog implementation (``fs_watchdog.py``) and its
debounce consumer (``debounce.py``) is what keeps the watch loop hermetically
testable: a test injects a synthetic :class:`FsEventSource` that emits
:class:`FsEvent`s on demand — no watchdog import, no real filesystem latency —
so the debounce/coalesce/submit logic is exercised deterministically.  Only
``fs_watchdog.py`` imports the ``watchdog`` library.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, final

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@final
@dataclass(frozen=True, slots=True)
class FsEvent:
    """One filesystem change: a path that was written, or removed.

    ``deleted`` distinguishes a removal (routes to a ``DocumentDeleteJob``) from
    a create/modify (routes to a ``FileIndexJob``).  A rename is delivered as a
    delete of the source path and a modify of the destination path.
    """

    path: Path
    deleted: bool


class FsEventSource(Protocol):
    """A source of filesystem events for one watched tree.

    ``schedule`` begins watching *root* and invokes *on_event* — from whatever
    thread the source runs on — for each change; the returned handle is opaque
    and only meaningful to :meth:`unschedule`.  ``stop`` tears the source down.
    The watch loop marshals every ``on_event`` onto its event loop via
    ``call_soon_threadsafe``, so an implementation may call it from a background
    thread (the watchdog observer thread does exactly that).
    """

    def schedule(self, root: Path, on_event: Callable[[FsEvent], None]) -> object:
        """Begin watching *root*; return an opaque handle for :meth:`unschedule`."""
        ...

    def unschedule(self, handle: object) -> None:
        """Stop watching the tree associated with *handle*."""
        ...

    def stop(self) -> None:
        """Tear the source down, joining any background observer thread."""
        ...
