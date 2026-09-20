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


class TestRenderInsights:
    def test_empty_store_renders_without_breakdown_sections(self) -> None:
        text = SyncCli._render_insights(_EMPTY_RESPONSE.model_dump())
        assert "Total queries:    0" in text
        assert "Top empty queries:" not in text
        assert "Per-collection hits:" not in text

    def test_telemetry_disabled_renders_as_disabled(self) -> None:
        disabled = _EMPTY_RESPONSE.model_copy(update={"telemetry_enabled": False})
        text = SyncCli._render_insights(disabled.model_dump())
        assert "disabled" in text

    def test_populated_store_renders_every_breakdown(self) -> None:
        text = SyncCli._render_insights(_POPULATED_RESPONSE.model_dump())
        assert "Top empty queries:" in text
        assert "nada: 2" in text
        assert "Per-collection hits:" in text
        assert "default: 5" in text
        assert "Per-agent recall:" in text
        assert "rmh: 3" in text
        assert "Hit decay bands:" in text
        assert "0-7d: 4" in text

    def test_latency_and_rate_are_formatted(self) -> None:
        text = SyncCli._render_insights(_POPULATED_RESPONSE.model_dump())
        assert "20.0%" in text
        assert "12.3ms" in text
        assert "45.6ms" in text
