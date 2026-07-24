"""Background orchestration for a directory registration's lifecycle.

The HTTP route layer (:class:`~quarry.daemon.routes.registrations.RegistrationRoutes`)
parses the request and delegates the multi-step background work here: writing the
registry, tearing down + purging subsumed collections, installing the parent
watch, and purging a deregistered collection's chunks through the queue.  Keeping
this off the route class leaves each with a single responsibility.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.daemon.purge_service import CollectionPurger
from quarry.daemon.tasks import task_terminal
from quarry.sync_registry import DirectoryRegistration, SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class RegistrationLifecycle:
    """Run the register/deregister background tasks: registry, watch, purge."""

    __slots__ = ("_ctx",)

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    async def run_register(
        self, state: TaskState, resolved: Path, collection: str
    ) -> None:
        """Execute register_directory in the background and update *state*."""
        try:
            reg, subsumed = await run_in_threadpool(
                self._register_sync,
                self._ctx.settings.registry_path,
                resolved,
                collection,
            )
            results: dict[str, object] = {
                "directory": reg.directory,
                "collection": reg.collection,
                "registered_at": reg.registered_at,
                "subsumed": subsumed,
            }
            # Tear down + purge each subsumed collection BEFORE installing the
            # parent watch.  directories.collection is UNIQUE, so re-registering a
            # collection under a wider directory puts the parent's OWN name in
            # `subsumed`: its old (narrower-root) rows were deleted in-transaction
            # and the wider row committed.  Purging first clears the stale chunks;
            # only then does start_watching install the watch and submit the fresh
            # full-tree scan, so the scan owns the collection on a clean slate — no
            # orphans, and never a torn-down parent watch.  A purge the saturated
            # queue defeats leaves orphans behind, so surface the failed children.
            failed = [
                child for child in subsumed if not await self._teardown_subsumed(child)
            ]
            if failed:
                results["subsume_purge_failed"] = failed
            self._ctx.watch_loop.start_watching(collection, resolved)
            # Set the terminal status LAST — only now is the register truly done:
            # registered AND watched AND cleanup-attempted.  A client polling
            # mid-flight must never see "completed" while the parent is unwatched,
            # and a raise in teardown/start_watching must land as failed, not a
            # stale completed.
            state.results = results
            state.status = "completed"
        except asyncio.CancelledError:
            state.status = "failed"
            state.error = "task was cancelled"
            raise
        except (FileNotFoundError, ValueError) as exc:
            state.status = "failed"
            state.error = str(exc)
        except Exception as exc:
            logger.exception("Background register failed")
            state.status = "failed"
            state.error = str(exc)
        finally:
            if state.status == "running":
                state.status = "failed"
                state.error = "task exited without setting terminal status"

    async def run_purge(self, state: TaskState, collection: str) -> None:
        """Purge a deregistered collection's chunks THROUGH the queue."""
        with task_terminal(state):
            purge = await CollectionPurger(self._ctx).purge(
                collection, "deregister-purge"
            )
            state.results["deleted_chunks"] = purge.results.get("deleted", 0)
            if purge.status == "failed":
                # Symmetric with _teardown_subsumed: a shed deregister-purge would
                # otherwise orphan the collection's chunks with no backstop (the
                # rows are gone from the registry, so reconcile never revisits it).
                # Defer it so the reconcile drains the orphan when the queue frees.
                self._ctx.watch_loop.defer_purge(collection)
                state.status = "failed"
                state.error = purge.error or "purge failed"
            else:
                state.status = "completed"

    async def _teardown_subsumed(self, collection: str) -> bool:
        """Stop watching a subsumed child and purge its now-orphaned chunks.

        The child's ``directories`` row was deleted by the parent registration,
        so an in-flight ``FileIndexJob`` for it would FK-fail into orphan chunks
        in the dead collection; stopping the watch also prevents a double-index.

        Return whether the purge completed.  A saturated queue can defeat it; the
        failure is logged, reported, AND deferred for a reconcile-driven retry —
        the one backstop, since reconcile-drop tears the watch down but never
        purges — so an unpurged child is never swallowed into a clean success.
        """
        self._ctx.watch_loop.stop_watching(collection)
        purge = await CollectionPurger(self._ctx).purge(collection, "subsume-purge")
        if purge.status == "failed":
            logger.warning(
                "subsume purge failed for collection %s: %s",
                collection,
                purge.error or "unknown",
            )
            self._ctx.watch_loop.defer_purge(collection)  # retry on next reconcile
            return False
        return True

    @staticmethod
    def _register_sync(
        registry_path: Path, resolved: Path, collection: str
    ) -> tuple[DirectoryRegistration, list[str]]:
        """Open registry, register, close — all in the caller's thread.

        SQLite connections are bound to the thread that created them, so the
        open/use/close lifecycle must stay inside the worker thread.  Return the
        registration and the collections it subsumed.
        """
        conn = SyncRegistry(registry_path)
        try:
            return conn.register_directory(resolved, collection)
        finally:
            conn.close()
