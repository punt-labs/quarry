"""The watchdog construction chain that wires :class:`PrunedInotify` in.

Split out of :mod:`~quarry.daemon.inotify_prune` (which owns the pruning
ALGORITHM) because watchdog's ``InotifyBuffer`` → ``InotifyEmitter`` →
``BaseObserver`` chain each hardcode the next class down inline, with no
factory hook to override — reaching :class:`~quarry.daemon.inotify_prune.
PrunedInotify` from :class:`PrunedInotifyObserver` requires a change at each
of the three levels, but NOT the same kind of change at each level:

* :class:`PrunedInotifyBuffer` and :class:`PrunedInotifyObserver` each
  override a CONSTRUCTOR that would otherwise do something harmful if left
  unmodified (build a vanilla ``Inotify``; omit the injected
  ``emitter_class`` ``BaseObserver.__init__`` requires with no default).
  Neither defines ``__init__`` (PY-CC-1): each uses a ``create`` classmethod
  factory (PY-CC-5) that constructs directly via ``object.__new__`` rather
  than relying on ``type.__call__``'s automatic post-``__new__`` invocation
  of the inherited ``__init__``. Only :class:`PrunedInotifyBuffer` also
  overrides ``__new__`` to refuse direct construction (PY-CC-3): a bare
  ``PrunedInotifyBuffer(path, ...)`` call would otherwise SILENTLY build a
  vanilla, unpruned ``Inotify`` (its inherited ``__init__``'s signature is
  perfectly satisfiable). A bare ``PrunedInotifyObserver(timeout=...)`` call
  already fails loudly on its own — ``BaseObserver.__init__`` requires
  ``emitter_class`` positionally with no default — so no equivalent guard is
  needed there; ``create()`` is the only path that can supply it.
* :class:`PrunedInotifyEmitter` overrides ``on_thread_start`` — a METHOD, not
  a constructor. ``EventEmitter.__init__`` (its inherited constructor) does
  nothing that needs replacing, so it is built the ordinary way by
  ``BaseObserver.schedule()``; the pruned construction happens later, when
  its own thread starts.

Every override here is a verbatim, low-drift copy of its vanilla
counterpart's 3-4 line body with one class name substituted;
``tests/test_inotify_prune_chain.py``'s
``test_pins_the_watchdog_internals_this_module_subclasses`` fails loudly if a
future watchdog release renames or removes any of the methods this module
depends on.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Self, final

from watchdog.observers.api import DEFAULT_OBSERVER_TIMEOUT, BaseObserver
from watchdog.observers.inotify import InotifyEmitter
from watchdog.observers.inotify_buffer import InotifyBuffer
from watchdog.utils import BaseThread
from watchdog.utils.delayed_queue import DelayedQueue

from quarry.daemon.inotify_prune import PrunedInotify

if TYPE_CHECKING:
    from watchdog.observers.inotify_c import InotifyEvent


@final
class PrunedInotifyBuffer(InotifyBuffer):
    """An ``InotifyBuffer`` that builds a :class:`PrunedInotify`."""

    _queue: DelayedQueue[InotifyEvent | tuple[InotifyEvent, InotifyEvent]]

    def __new__(
        cls, path: bytes, *, recursive: bool = False, event_mask: int | None = None
    ) -> Self:
        """Refuse direct construction (PY-CC-3): use :meth:`create` instead.

        ``PrunedInotifyBuffer`` defines no ``__init__`` override, so a bare
        ``PrunedInotifyBuffer(path, ...)`` call would run the INHERITED
        ``InotifyBuffer.__init__`` unmodified -- silently building a vanilla,
        UNPRUNED ``Inotify`` instead of a :class:`PrunedInotify`, with no
        error at all. This guard only affects that path: :meth:`create`
        constructs via ``object.__new__(cls)`` directly, never through
        ``cls(...)``, so it never reaches this override. The signature
        mirrors ``InotifyBuffer.__init__`` (rather than a generic
        ``*args, **kwargs``) so pyright's constructor-consistency check
        (``reportInconsistentConstructor``) sees the two agree.
        """
        del path, recursive, event_mask
        msg = f"{cls.__name__} must be built via {cls.__name__}.create()"
        raise TypeError(msg)

    @classmethod
    def create(
        cls, path: bytes, *, recursive: bool = False, event_mask: int | None = None
    ) -> Self:
        """Build one, running ``InotifyBuffer``'s own thread-start side effect.

        ``InotifyBuffer.__init__`` constructs the vanilla ``Inotify`` inline
        and this needs :class:`PrunedInotify` there instead, so this
        replicates its four-line body directly rather than calling
        ``super().__init__()`` (which would reintroduce the vanilla
        ``Inotify``) or defining an ``__init__`` override at all -- the
        class-level ``__init__`` this factory bypasses is never invoked
        because nothing ever calls ``PrunedInotifyBuffer(...)`` directly.
        """
        self = object.__new__(cls)
        BaseThread.__init__(self)
        self._queue = DelayedQueue(self.delay)
        self._inotify = PrunedInotify(path, recursive=recursive, event_mask=event_mask)
        self.start()
        return self


@final
class PrunedInotifyEmitter(InotifyEmitter):
    """An ``InotifyEmitter`` whose thread builds a :class:`PrunedInotifyBuffer`."""

    def on_thread_start(self) -> None:
        path = os.fsencode(self.watch.path)
        event_mask = self.get_event_mask_from_filter()
        self._inotify = PrunedInotifyBuffer.create(
            path, recursive=self.watch.is_recursive, event_mask=event_mask
        )


@final
class PrunedInotifyObserver(BaseObserver):
    """A Linux inotify observer whose recursive watches are pruned per DES-045e.

    Constructs :class:`PrunedInotifyEmitter` directly rather than subclassing
    ``InotifyObserver`` (whose entire body is picking between ``InotifyEmitter``
    and ``InotifyFullEmitter`` -- full move events are never used here, so
    there is nothing else in it worth inheriting).
    """

    @classmethod
    def create(cls, *, timeout: float = DEFAULT_OBSERVER_TIMEOUT) -> Self:
        """Build one, injecting :class:`PrunedInotifyEmitter` as the emitter class.

        ``BaseObserver.__init__`` requires ``emitter_class`` positionally
        with no default, so a plain ``PrunedInotifyObserver(timeout=...)``
        call cannot supply it -- this factory calls ``BaseObserver.__init__``
        directly with the class injected, and (like the sibling factories in
        this module) never lets ``type.__call__`` invoke any inherited
        ``__init__`` automatically.
        """
        self = object.__new__(cls)
        BaseObserver.__init__(self, PrunedInotifyEmitter, timeout=timeout)
        return self
