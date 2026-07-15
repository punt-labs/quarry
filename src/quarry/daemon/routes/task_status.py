"""The task-status route: poll a background task's terminal state."""

from __future__ import annotations

from typing import final

from starlette.requests import Request
from starlette.responses import JSONResponse

from quarry.daemon.routes.base import RouteGroup


@final
class TaskStatusRoutes(RouteGroup):
    """Serve task status — running, completed (+results), or failed."""

    def status(self, request: Request) -> JSONResponse:
        auth_resp = self.reject_unauthorized(request)
        if auth_resp is not None:
            return auth_resp

        task_id = request.path_params.get("task_id", "")
        state = self.ctx.tasks.get(task_id)
        if state is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)

        response: dict[str, object] = {
            "task_id": state.task_id,
            "status": state.status,
        }
        if state.status == "completed":
            response["results"] = state.results
        elif state.status == "failed":
            response["error"] = state.error
        return JSONResponse(response)
