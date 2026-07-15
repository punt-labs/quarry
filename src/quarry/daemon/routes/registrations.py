"""The ``/registrations`` routes: list, register, and deregister sync directories.

Registration resolves a client-supplied directory against the daemon's own home
(from the passwd database, never ``$HOME``) so a remote caller cannot register
``/etc`` and siphon it out through a later sync.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pwd
from pathlib import Path
from typing import TYPE_CHECKING, final

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.tasks import TaskState, task_terminal
from quarry.http_guards import RequestGuards
from quarry.sync_registry import DirectoryRegistration, SyncRegistry

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

# The /registrations body carries only a small option dict.
MAX_REGISTRATIONS_BODY_BYTES = 16 * 1024


@final
class RegistrationRoutes(RouteGroup):
    """Serve ``GET/POST/DELETE /registrations`` over the sync registry."""

    async def registrations(self, request: Request) -> JSONResponse:
        """Dispatch GET/POST/DELETE on /registrations to the right handler."""
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        if request.method == "GET":
            return await self._list(request)
        if request.method == "DELETE":
            return await self._delete(request)
        return await self._add(request)

    async def _list(self, request: Request) -> JSONResponse:  # noqa: ARG002
        settings = self.ctx.settings
        if not settings.registry_path.exists():
            return JSONResponse({"total_registrations": 0, "registrations": []})

        regs = await run_in_threadpool(self._list_sync, settings.registry_path)
        payload = [
            {
                "collection": reg.collection,
                "directory": reg.directory,
                "registered_at": reg.registered_at,
            }
            for reg in regs
        ]
        return JSONResponse(
            {"total_registrations": len(payload), "registrations": payload}
        )

    async def _add(self, request: Request) -> JSONResponse:
        """Register a directory as an async 202 background task."""
        size_err = RequestGuards.check_body_size(request, MAX_REGISTRATIONS_BODY_BYTES)
        if size_err is not None:
            return size_err

        body = await self.json_object(request)
        if isinstance(body, JSONResponse):
            return body

        directory = body.get("directory")
        if not isinstance(directory, str) or not directory.strip():
            return JSONResponse(
                {"error": "Missing required field: directory"}, status_code=400
            )
        collection = body.get("collection")
        if not isinstance(collection, str) or not collection.strip():
            return JSONResponse(
                {"error": "Missing required field: collection"}, status_code=400
            )

        resolved, reason = self._resolve_path(directory)
        if resolved is None:
            return JSONResponse({"error": reason}, status_code=400)
        if not resolved.is_dir():
            return JSONResponse(
                {"error": f"directory not found: {resolved}"}, status_code=400
            )

        state = self.ctx.tasks.begin("register")
        return self.accept(state, self._run_register(state, resolved, collection))

    async def _delete(self, request: Request) -> JSONResponse:
        """Deregister synchronously (existence + registry row); purge chunks async."""
        collection = request.query_params.get("collection", "")
        if not collection:
            return JSONResponse(
                {"error": "Missing required parameter: collection"}, status_code=400
            )
        keep_data_raw = request.query_params.get("keep_data", "false").lower()
        if keep_data_raw not in {"true", "false"}:
            return JSONResponse(
                {"error": "keep_data must be 'true' or 'false'"}, status_code=400
            )
        keep_data = keep_data_raw == "true"

        not_found = JSONResponse(
            {"error": f"No registration found for {collection!r}"}, status_code=404
        )
        if not self.ctx.settings.registry_path.exists():
            return not_found

        # Registry mutation off-thread; an unknown collection is a 404 below, and any
        # unexpected error propagates to the global 500 handler (no raw text echoed).
        found, removed_docs = await run_in_threadpool(
            self._deregister_sync, self.ctx.settings.registry_path, collection
        )
        if not found:
            return not_found

        state = self.ctx.tasks.begin("deregister")
        state.results = {
            "collection": collection,
            "removed": len(removed_docs),
            "deleted_chunks": 0,
            "type": "registration",
        }
        purge_docs = [] if keep_data else removed_docs
        if purge_docs:
            self.ctx.tasks.track(
                state,
                asyncio.create_task(self._run_purge(state, collection, purge_docs)),
            )
        else:
            state.status = "completed"  # nothing to purge; complete immediately

        return JSONResponse(
            {
                "task_id": state.task_id,
                "status": "accepted",
                "removed": len(removed_docs),
            },
            status_code=202,
        )

    async def _run_register(
        self, state: TaskState, resolved: Path, collection: str
    ) -> None:
        """Execute register_directory in background and update task state."""
        try:
            reg = await run_in_threadpool(
                self._register_sync,
                self.ctx.settings.registry_path,
                resolved,
                collection,
            )
            state.status = "completed"
            state.results = {
                "directory": reg.directory,
                "collection": reg.collection,
                "registered_at": reg.registered_at,
            }
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

    async def _run_purge(
        self, state: TaskState, collection: str, removed_docs: list[str]
    ) -> None:
        """Purge chunks for an already-deregistered collection; update state."""

        def _purge() -> int:
            store = self.ctx.database.store
            return sum(
                store.delete_document(d, collection=collection) for d in removed_docs
            )

        with task_terminal(state):
            state.results["deleted_chunks"] = await run_in_threadpool(_purge)
            state.status = "completed"

    @staticmethod
    def _list_sync(registry_path: Path) -> list[DirectoryRegistration]:
        """Open registry, list, close — all in one thread."""
        conn = SyncRegistry(registry_path)
        try:
            return conn.list_registrations()
        finally:
            conn.close()

    @staticmethod
    def _register_sync(
        registry_path: Path, resolved: Path, collection: str
    ) -> DirectoryRegistration:
        """Open registry, register, close — all in the caller's thread.

        SQLite connections are bound to the thread that created them, so the
        open/use/close lifecycle must stay inside the worker thread.
        """
        conn = SyncRegistry(registry_path)
        try:
            return conn.register_directory(resolved, collection)
        finally:
            conn.close()

    @staticmethod
    def _deregister_sync(
        registry_path: Path, collection: str
    ) -> tuple[bool, list[str]]:
        """Open registry, deregister, close — all in one thread."""
        conn = SyncRegistry(registry_path)
        try:
            existing = conn.get_registration(collection)
            if existing is None:
                return False, []
            removed_docs = conn.deregister_directory(collection)
            return True, removed_docs
        finally:
            conn.close()

    @staticmethod
    def _server_home() -> tuple[Path | None, str | None]:
        """Return the daemon's home dir from passwd, or ``(None, reason)``.

        Uses ``pwd.getpwuid(os.getuid())`` rather than ``$HOME`` so a remote
        client cannot widen the allowlist by influencing the server's
        environment.  The ``None`` branch carries the reason, so this is a
        result pair, not a giving-up ``Optional``.
        """
        try:
            entry = pwd.getpwuid(os.getuid())
        except KeyError as exc:
            return None, f"cannot determine server home directory: {exc}"
        try:
            return Path(entry.pw_dir).resolve(), None
        except (OSError, RuntimeError) as exc:
            return None, f"cannot resolve server home directory: {exc}"

    @classmethod
    def _resolve_path(cls, directory: str) -> tuple[Path | None, str | None]:
        """Return the resolved absolute path, or ``(None, reason)``.

        Rejects anything resolving outside the daemon's home directory (from the
        passwd database, not ``$HOME``): a remote client must not register
        ``/etc`` or ``/root/.ssh`` and siphon it out via a later sync.  The
        ``None`` branch carries the reason — a result pair, not a bare Optional.
        """
        if ".." in Path(directory).parts:
            return None, "directory must not contain '..'"
        try:
            resolved = Path(directory).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            return None, f"cannot resolve directory: {exc}"

        home, reason = cls._server_home()
        if home is None:
            return None, reason
        try:
            resolved.relative_to(home)
        except ValueError:
            return None, f"directory {str(resolved)!r} is outside {str(home)!r}"
        return resolved, None
