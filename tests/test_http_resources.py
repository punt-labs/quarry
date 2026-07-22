"""Tests for QuarryResources — the daemon connection/session lifecycle.

These verify the DES-032 isolation contract: the write connection, the query
read connection, and the query ONNX session are three distinct, separately
cached instances built from one Settings object.  Construction is mocked so
no real LanceDB connection or ONNX model is needed.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from quarry.http_resources import QuarryResources

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _mock_settings(tmp_path: Path) -> MagicMock:
    settings = MagicMock()
    settings.lancedb_path = tmp_path / "lancedb"
    return settings


def _patch_connect() -> AbstractContextManager[MagicMock]:
    # Each call returns a fresh sentinel so identity comparisons are meaningful.
    return patch(
        "quarry.http_resources.Database.connect",
        side_effect=lambda _path: MagicMock(name="Database"),
    )


def _patch_new_embedding_backend() -> AbstractContextManager[MagicMock]:
    return patch(
        "quarry.http_resources.new_embedding_backend",
        side_effect=lambda: MagicMock(name="EmbeddingBackend"),
    )


class TestQuarryResources:
    def test_builds_from_settings(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        resources = QuarryResources(settings)
        assert resources.settings is settings

    def test_database_connects_to_configured_path(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        with _patch_connect() as connect:
            _ = QuarryResources(settings).database
        connect.assert_called_once_with(settings.lancedb_path)

    def test_database_is_cached(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        with _patch_connect():
            resources = QuarryResources(settings)
            first = resources.database
            second = resources.database
        assert first is second

    def test_query_database_is_cached(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        with _patch_connect():
            resources = QuarryResources(settings)
            first = resources.query_database
            second = resources.query_database
        assert first is second

    def test_database_and_query_database_are_distinct(self, tmp_path: Path) -> None:
        # DES-032: the query read connection must not be the write connection,
        # so sync write locks cannot block search readers.
        settings = _mock_settings(tmp_path)
        with _patch_connect():
            resources = QuarryResources(settings)
            assert resources.database is not resources.query_database

    def test_both_connections_use_the_same_path(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        with _patch_connect() as connect:
            resources = QuarryResources(settings)
            _ = resources.database
            _ = resources.query_database
        paths = {call.args[0] for call in connect.call_args_list}
        assert paths == {settings.lancedb_path}

    def test_embedder_is_cached(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        with _patch_new_embedding_backend():
            resources = QuarryResources(settings)
            first = resources.embedder
            second = resources.embedder
        assert first is second

    def test_embedder_is_fresh_session_not_shared_singleton(
        self, tmp_path: Path
    ) -> None:
        # DES-032: queries get their own ONNX session via new_embedding_backend,
        # never the cached get_embedding_backend singleton the sync worker uses.
        settings = _mock_settings(tmp_path)
        with _patch_new_embedding_backend() as factory:
            _ = QuarryResources(settings).embedder
        factory.assert_called_once_with()

    def test_resources_are_independent_across_instances(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        with _patch_connect(), _patch_new_embedding_backend():
            one = QuarryResources(settings)
            two = QuarryResources(settings)
            assert one.database is not two.database
            assert one.embedder is not two.embedder

    def test_warm_resolves_every_resource(self, tmp_path: Path) -> None:
        # warm() must build all three resources so the serving threads never
        # race to construct a cached_property on first use (DES-032).
        settings = _mock_settings(tmp_path)
        with _patch_connect() as connect, _patch_new_embedding_backend() as factory:
            resources = QuarryResources(settings)
            resources.warm()
            # Both connections built (2 connect calls), one embedder built.
            assert connect.call_count == 2
            factory.assert_called_once_with()
            # Cached: a post-warm access does not rebuild anything.
            _ = resources.database
            _ = resources.query_database
            _ = resources.embedder
            assert connect.call_count == 2
            assert factory.call_count == 1

    def test_warm_honors_embedder_override_and_skips_onnx(self, tmp_path: Path) -> None:
        # An injected embedder is THE embedder on every path: warm() must NOT
        # build the real ONNX backend when an override is set, and every read of
        # .embedder returns the override. This is what makes the in-process
        # daemon fixture hermetic even for a code path that triggers warm().
        settings = _mock_settings(tmp_path)
        override = MagicMock(name="OverrideEmbedder")
        with _patch_connect() as connect, _patch_new_embedding_backend() as factory:
            resources = QuarryResources(settings, embedder=override)
            resources.warm()
            factory.assert_not_called()
            assert resources.embedder is override
            # The DB connections are still warmed — only the ONNX build is skipped.
            assert connect.call_count == 2

    def test_warm_logs_each_phase_distinctly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A query_database failure must not be mis-attributed to the model:
        # warm() logs the write db, query db, and ONNX session as separate phases.
        settings = _mock_settings(tmp_path)
        with (
            _patch_connect(),
            _patch_new_embedding_backend(),
            caplog.at_level(logging.INFO, logger="quarry.http_resources"),
        ):
            QuarryResources(settings).warm()
        messages = [r.getMessage() for r in caplog.records]
        assert any("write database" in m for m in messages)
        assert any("query database" in m for m in messages)
        assert any("ONNX embedding session" in m for m in messages)
        assert any("ready" in m for m in messages)
