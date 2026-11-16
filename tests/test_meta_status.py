"""``/status`` reports table size and free disk, identically on every path."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from quarry.api.meta import StatusResponse
from quarry.reports import StatusReport

from .inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_NEW_FIELDS = {"disk_bytes", "live_bytes", "free_disk_bytes"}


@pytest.fixture
def daemon(tmp_path: Path) -> InProcessDaemon:
    return InProcessDaemon(tmp_path)


@pytest.fixture
def body(daemon: InProcessDaemon) -> Iterator[dict[str, object]]:
    with TestClient(daemon.app) as http:
        yield http.get("/v1/status").json()


def test_status_response_declares_the_size_and_disk_fields() -> None:
    assert set(StatusResponse.model_fields) >= _NEW_FIELDS


def test_daemon_status_body_carries_exactly_the_declared_fields(
    body: dict[str, object],
) -> None:
    assert set(body) == set(StatusResponse.model_fields)


def test_daemon_status_body_populates_the_new_fields_as_non_negative_ints(
    body: dict[str, object],
) -> None:
    parsed = StatusResponse.model_validate(body)

    assert min(parsed.disk_bytes, parsed.live_bytes, parsed.free_disk_bytes) >= 0


def test_cli_client_path_matches_the_daemon_body(
    daemon: InProcessDaemon, body: dict[str, object]
) -> None:
    with daemon.client() as client:
        parsed = client.status()

    assert set(parsed.model_dump()) == set(body)


def test_cli_client_path_populates_free_disk(daemon: InProcessDaemon) -> None:
    with daemon.client() as client:
        parsed = client.status()

    assert parsed.free_disk_bytes > 0


def test_format_status_shows_size_and_free_disk() -> None:
    info = StatusResponse(
        document_count=1,
        collection_count=1,
        chunk_count=2,
        registered_directories=0,
        database_path="/db",
        embedding_model="m",
        provider="cpu",
        embedding_dimension=768,
        disk_bytes=3 * 1024**3,
        live_bytes=1024**3,
        free_disk_bytes=50 * 1024**3,
    ).model_dump()

    text = StatusReport(info).render()

    assert "3.00 GiB on disk" in text
    assert "1.00 GiB live" in text
    assert "50.0 GiB free" in text
