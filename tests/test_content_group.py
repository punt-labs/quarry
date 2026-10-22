"""Tests for quarry.daemon.routes.content_group: the shared route validators."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from quarry.content_guard import BinaryContent
from quarry.daemon.context import DaemonContext
from quarry.daemon.routes.content_group import ContentRouteGroup

_PDF = "%PDF-1.5\n%" + "�" * 4 + "\n" + "x" * 100


def _group(ctx: object | None = None) -> ContentRouteGroup:
    return ContentRouteGroup(cast("DaemonContext", ctx or SimpleNamespace()))


def _error(response: JSONResponse | str) -> str:
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    return bytes(response.body).decode()


class TestRequireContent:
    def test_clean_text_is_returned(self) -> None:
        assert _group()._require_content({"c": "hello"}, "c", "remember") == "hello"

    def test_missing_text_keeps_the_field_error(self) -> None:
        response = _group()._require_content({}, "c", "remember")
        assert json.loads(_error(response)) == {"error": "Missing required field: c"}

    def test_binary_text_is_refused_with_the_reason(self) -> None:
        response = _group()._require_content({"c": _PDF}, "c", "remember")
        assert json.loads(_error(response)) == {
            "error": "binary content rejected: pdf header"
        }

    def test_a_refusal_is_logged_once_with_the_kind_and_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            _group()._require_content({"c": _PDF}, "c", "capture")
        [record] = caplog.records
        assert (
            record.getMessage()
            == "capture refused: binary content rejected: pdf header"
        )
        assert record.exc_info is None

    def test_the_guard_is_built_once_not_per_request(self) -> None:
        with patch(
            "quarry.daemon.routes.content_group.BinaryContent",
            wraps=BinaryContent,
        ) as constructor:
            for _ in range(3):
                _group()._require_content({"c": "text"}, "c", "remember")
        constructor.assert_not_called()


class TestRequireMemoryType:
    @pytest.mark.parametrize("memory_type", ["", "fact", "procedure"])
    def test_an_agent_writable_type_is_returned(self, memory_type: str) -> None:
        body: dict[str, object] = {"memory_type": memory_type}
        assert _group()._require_memory_type(body) == memory_type

    def test_an_absent_type_is_empty(self) -> None:
        assert _group()._require_memory_type({}) == ""

    def test_an_unknown_type_is_a_400(self) -> None:
        response = _group()._require_memory_type({"memory_type": "facts"})
        assert json.loads(_error(response)) == {
            "error": (
                "unknown memory_type 'facts'; expected one of "
                "fact, observation, opinion, procedure"
            )
        }

    def test_the_reserved_lesson_type_is_a_400(self) -> None:
        response = _group()._require_memory_type({"memory_type": "lesson"})
        assert json.loads(_error(response)) == {
            "error": "memory_type 'lesson' is reserved for quarry learn"
        }


class TestAccept:
    def test_a_400_passes_through_without_a_task(self) -> None:
        tasks = MagicMock()
        refusal = JSONResponse({"error": "x"}, status_code=400)
        assert (
            _group(SimpleNamespace(tasks=tasks))._accept("remember", refusal) is refusal
        )
        tasks.begin.assert_not_called()

    def test_a_job_is_queued_as_a_task_of_its_kind(self) -> None:
        queue = MagicMock()
        queue.try_submit.return_value = True
        ctx = SimpleNamespace(tasks=MagicMock(), database_name="db", ingest_queue=queue)
        ctx.tasks.begin.return_value = SimpleNamespace(task_id="remember-1")
        job = SimpleNamespace(collection="c")

        response = _group(ctx)._accept("remember", cast("MagicMock", job))

        assert response.status_code == 202
        ctx.tasks.begin.assert_called_once_with("remember")
        queue.try_submit.assert_called_once()
