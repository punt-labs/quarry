"""Tests for quarry.active_session_context: counts, or the admission there are none."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from quarry.active_session_context import ActiveSessionContext
from quarry.client import ClientConfigError

if TYPE_CHECKING:
    import pytest

_SYNC = "quarry.active_session_context.SyncLock.launch_background_sync"
_COVERAGE = "quarry.active_session_context.ActiveSessionContext._session_coverage"


class TestText:
    def test_it_launches_the_sync_and_shows_the_counts(self) -> None:
        counts = {
            "documents_indexed": 3,
            "transcripts_captured": 5,
            "memories_saved": 7,
        }
        with (
            patch(_SYNC, return_value="launched") as sync,
            patch(_COVERAGE, return_value=counts),
        ):
            text = ActiveSessionContext(Path("/repo"), "repo").text()
        sync.assert_called_once()
        assert "repo-captures" in text
        assert "5 transcripts" in text

    def test_it_says_so_when_the_daemon_cannot(self) -> None:
        with patch(_SYNC, return_value="launched"), patch(_COVERAGE, return_value=None):
            text = ActiveSessionContext(Path("/repo"), "repo").text()
        assert "repo" in text
        assert "transcripts" not in text


class TestCoverage:
    def test_the_daemons_answer_is_the_counts(self) -> None:
        client = MagicMock()
        client.coverage.return_value = MagicMock(
            documents_indexed=1, transcripts_captured=2, memories_saved=3
        )
        with patch("quarry.client.TargetResolver.connect", return_value=client):
            assert ActiveSessionContext._session_coverage("repo") == {
                "documents_indexed": 1,
                "transcripts_captured": 2,
                "memories_saved": 3,
            }

    def test_a_client_failure_is_none_and_logs_the_class(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = MagicMock()
        client.coverage.side_effect = ClientConfigError("bad QUARRY_URL")
        with (
            patch("quarry.client.TargetResolver.connect", return_value=client),
            caplog.at_level(logging.WARNING),
        ):
            assert ActiveSessionContext._session_coverage("repo") is None
        assert "ClientConfigError" in caplog.text
