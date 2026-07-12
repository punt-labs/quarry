"""Contract test for POST /captures/push: it runs the same CaptureSync the CLI
uses and returns the identical result shape (bug class 3 — remote/local parity)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from quarry.http_server import _QuarryContext, build_app
from quarry.shadow.sync import ShadowSyncResult


def _mock_settings(tmp_path: Path) -> MagicMock:
    settings = MagicMock()
    settings.lancedb_path = tmp_path / "lancedb"
    settings.lancedb_path.mkdir(parents=True)
    settings.registry_path = tmp_path / "registry.db"
    return settings


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    ctx = _QuarryContext(_mock_settings(tmp_path))
    return TestClient(build_app(ctx), raise_server_exceptions=False)


class TestCapturesPushRoute:
    def test_runs_capture_sync_and_returns_results(self, client: TestClient) -> None:
        results = {
            "proj": ShadowSyncResult(
                pushed=True,
                committed=True,
                rescrubbed=2,
                aborted_reason="",
                race_failures=(),
            )
        }
        with patch(
            "quarry.shadow.CaptureSync.push_registered", return_value=results
        ) as push:
            resp = client.post(
                "/captures/push", headers={"content-length": "2"}, json={}
            )

        assert resp.status_code == 200
        push.assert_called_once()
        # fail_open=True on the automatic/daemon path.
        assert push.call_args.kwargs["fail_open"] is True
        body = resp.json()
        assert set(body["results"]["proj"]) == {
            "pushed",
            "committed",
            "rescrubbed",
            "aborted_reason",
            "race_failures",
        }
        assert body["results"]["proj"]["pushed"] is True

    def test_empty_registrations_returns_empty(self, client: TestClient) -> None:
        with patch("quarry.shadow.CaptureSync.push_registered", return_value={}):
            resp = client.post(
                "/captures/push", headers={"content-length": "2"}, json={}
            )
        assert resp.status_code == 200
        assert resp.json() == {"results": {}}

    def test_body_size_limit_enforced(self, client: TestClient) -> None:
        resp = client.post(
            "/captures/push",
            headers={"content-length": str(64 * 1024)},
            content=b"x" * (64 * 1024),
        )
        assert resp.status_code == 413
