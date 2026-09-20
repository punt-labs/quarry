"""Tests for the `quarry insights` command (SyncCli).

Exercised in isolation with a stub CliPlumbing whose ``client`` factory returns
a fake QuarryClient, the same pattern as ``test_cli_captures.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import typer
from typer.testing import CliRunner

from quarry.api import InsightsResponse
from quarry.cli_captures import CliPlumbing
from quarry.cli_sync import SyncCli

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.client import QuarryClient

runner = CliRunner()


def _plumbing(*, client: object, recorder: list[object]) -> CliPlumbing:
    def emit(data: object, _text: str = "") -> None:
        recorder.append(data)

    def cli_errors(fn: Callable[..., None]) -> Callable[..., None]:
        return fn

    return CliPlumbing(
        emit=emit,
        cli_errors=cli_errors,
        client=lambda: cast("QuarryClient", client),
        err_console=MagicMock(),
        is_quiet=lambda: False,
    )


def _app(*, client: object, recorder: list[object]) -> typer.Typer:
    app = typer.Typer()
    SyncCli(_plumbing(client=client, recorder=recorder)).register(app)
    return app


def _client_with_insights(resp: InsightsResponse) -> MagicMock:
    client = MagicMock()
    client.insights.return_value = resp
    return client


_EMPTY_RESPONSE = InsightsResponse(
    telemetry_enabled=True,
    total_queries=0,
    empty_result_rate=0.0,
    p50_latency_ms=0.0,
    p95_latency_ms=0.0,
    top_empty_queries=[],
    per_collection_hits=[],
    per_agent_recall=[],
    memory_queries=0,
    knowledge_queries=0,
    hit_decay_bands=[],
)

_POPULATED_RESPONSE = InsightsResponse(
    telemetry_enabled=True,
    total_queries=10,
    empty_result_rate=0.2,
    p50_latency_ms=12.3,
    p95_latency_ms=45.6,
    top_empty_queries=[{"query_scrubbed": "nada", "count": 2}],
    per_collection_hits=[{"collection": "default", "hit_count": 5}],
    per_agent_recall=[{"agent_handle": "rmh", "query_count": 3}],
    memory_queries=3,
    knowledge_queries=7,
    hit_decay_bands=[{"band": "0-7d", "hit_count": 4}],
)


class TestInsightsCommand:
    def test_exits_zero(self) -> None:
        client = _client_with_insights(_EMPTY_RESPONSE)
        result = runner.invoke(_app(client=client, recorder=[]), ["insights"])
        assert result.exit_code == 0

    def test_calls_client_insights(self) -> None:
        client = _client_with_insights(_EMPTY_RESPONSE)
        runner.invoke(_app(client=client, recorder=[]), ["insights"])
        client.insights.assert_called_once_with()

    def test_emits_the_full_response_envelope(self) -> None:
        recorder: list[object] = []
        client = _client_with_insights(_POPULATED_RESPONSE)
        runner.invoke(_app(client=client, recorder=recorder), ["insights"])
        data = recorder[0]
        assert isinstance(data, dict)
        assert data == _POPULATED_RESPONSE.model_dump()
