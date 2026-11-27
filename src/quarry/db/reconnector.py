"""Reopen a database connection once its recycle gate is armed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.db.recycle_gate import RecycleGate
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@final
class Reconnector:
    """Replace a connection with a fresh one when its gate says a recycle is due."""

    __slots__ = ("_connect", "_gate")

    _connect: Callable[[], LanceDB]
    _gate: RecycleGate

    def __new__(cls, connect: Callable[[], LanceDB], gate: RecycleGate) -> Self:
        """Bind the connection factory to the gate that decides when to use it."""
        self = super().__new__(cls)
        self._connect = connect
        self._gate = gate
        return self

    def reopen(self, install: Callable[[LanceDB], None]) -> None:
        """Reopen the connection and *install* it when a recycle is armed.

        The reopen is *retry-safe*. A failing ``connect()`` — most likely
        exactly when descriptors are exhausted and a fresh open hits ``EMFILE``
        — must not lose the pending recycle. So the armed flag and the rebuild
        counter are cleared only *after* a successful reopen: if ``connect()``
        raises, the gate stays armed, the old connection stays intact and
        usable, and the exception propagates. The next connection boundary
        (``open_table``, ``list_tables``, or ``create_table``) then retries the
        reopen. The gate's claim serialises the reopen so a concurrent boundary
        serves the still-valid old connection rather than double-opening a
        second one.

        Installing the fresh connection drops the *wrapper's* reference to the
        old one; once any still-live table handles referencing it are also
        dropped, it is unreferenced except for the reference cycle the lancedb
        binding holds it in. Plain refcounting does not free that cycle — only
        cyclic GC collects it and releases its cached index-file descriptors
        (proven: the fd ceiling over 200 optimize cycles is ~53 with an explicit
        ``gc.collect()`` here and ~107 without, so refcounting alone is not doing
        the freeing).

        No explicit ``gc.collect()`` is forced here: on the daemon, the recycle
        fires during ``sync``'s optimize, and ``TableFinalizer`` already runs
        ``gc.collect(2)`` at the end of every rewriting finalize
        (table_finalizer.py), so the superseded connection is reclaimed on that
        pass. Elsewhere Python's automatic gen-2 collection bounds it. A
        per-recycle full-heap collection would be redundant work on the hot sync
        path.
        """
        if not self._gate.begin():
            return
        try:
            fresh = self._connect()
        except BaseException:
            # The reopen raised: release only the claim, leaving the gate armed
            # so the next boundary retries onto the still-live old connection.
            self._gate.fail()
            raise
        # Install and disarm atomically under one lock: a concurrent boundary
        # must never observe the gate still armed with the claim already
        # dropped, or it would open a second connection on top of this one.
        with self._gate.lock:
            install(fresh)
            self._gate.succeed()
        logger.debug("Recycled LanceDB connection to release cached fds")
