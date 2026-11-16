"""``/status`` reports table size and free disk, identically on every path."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.api.meta import StatusResponse
from quarry.formatting import format_status

from .test_http_server import client  # noqa: F401

if TYPE_CHECKING:
    from starlette.testclient import TestClient

_NEW_FIELDS = {"disk_bytes", "live_bytes", "free_disk_bytes"}


def test_status_response_declares_the_size_and_disk_fields() -> None:
    assert set(StatusResponse.model_fields) >= _NEW_FIELDS


def test_daemon_status_body_carries_exactly_the_declared_fields(
    client: TestClient,  # noqa: F811
) -> None:
    body = client.get("/v1/status").json()

    assert set(body) == set(StatusResponse.model_fields)


def test_daemon_status_body_populates_the_new_fields_as_non_negative_ints(
    client: TestClient,  # noqa: F811
) -> None:
    body = client.get("/v1/status").json()

    assert all(isinstance(body[f], int) and body[f] >= 0 for f in _NEW_FIELDS)


def test_client_parse_round_trips_the_daemon_body(
    client: TestClient,  # noqa: F811
) -> None:
    body = client.get("/v1/status").json()

    parsed = StatusResponse.model_validate(body)

    assert parsed.model_dump() == body


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

    text = format_status(info)

    assert "3.00 GiB on disk" in text
    assert "1.00 GiB live" in text
    assert "50.0 GiB free" in text
