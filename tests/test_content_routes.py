"""Binary content is refused with one reason on every content surface (bug class 3)."""

from __future__ import annotations

import logging
import time
from http.client import HTTPMessage, HTTPResponse
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.client import QuarryClient, TargetResolver
from quarry.content_guard import BinaryContent
from quarry.daemon.content_jobs import CaptureIngestJob, ScrubbedIngestJob
from quarry.daemon.tasks import TaskState
from quarry.mcp_server import McpTools
from tests.inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_PDF = "%PDF-1.5\n%" + "\ufffd" * 4 + "\n" + "x" * 400
_PDF_REASON = "binary content rejected: pdf header"
_DENSE = "a" * 100 + "�" * 300
_DENSE_REASON = "binary content rejected: non-text ratio 0.75 exceeds 0.25"
_CLEAN = "A perfectly ordinary note about quarry."

runner = CliRunner()


@pytest.fixture
def daemon(tmp_path: Path) -> InProcessDaemon:
    return InProcessDaemon(tmp_path)


@pytest.fixture
def http(daemon: InProcessDaemon) -> Iterator[TestClient]:
    with TestClient(daemon.app, raise_server_exceptions=False) as tc:
        yield tc


class TestRememberRoute:
    def test_a_pdf_body_is_a_400_with_the_exact_reason(self, http: TestClient) -> None:
        resp = http.post("/v1/remember", json={"name": "paper.md", "content": _PDF})
        assert resp.status_code == 400
        assert resp.json() == {"error": _PDF_REASON}

    def test_a_pdf_behind_a_media_type_marker_is_refused(
        self, http: TestClient
    ) -> None:
        content = "<!-- media_type: application/pdf -->\n" + _PDF
        resp = http.post("/v1/remember", json={"name": "paper.md", "content": content})
        assert resp.json() == {"error": _PDF_REASON}

    def test_a_dense_body_is_refused_with_its_ratio(self, http: TestClient) -> None:
        resp = http.post("/v1/remember", json={"name": "n.md", "content": _DENSE})
        assert resp.status_code == 400
        assert resp.json() == {"error": _DENSE_REASON}

    def test_a_binary_tail_of_a_large_body_is_sampled(self, http: TestClient) -> None:
        window = 32 * 1024
        content = "a" * (window * 4) + "�" * (window * 2)
        resp = http.post("/v1/remember", json={"name": "n.md", "content": content})
        assert resp.status_code == 400

    def test_nothing_is_queued_for_a_refused_body(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        http.post("/v1/remember", json={"name": "paper.md", "content": _PDF})
        assert len(daemon.ctx.tasks) == 0

    def test_clean_text_is_still_accepted(self, http: TestClient) -> None:
        with patch(
            "quarry.ingestion.web_ingest.ingest_content", return_value={"chunks": 1}
        ):
            resp = http.post("/v1/remember", json={"name": "n.md", "content": _CLEAN})
        assert resp.status_code == 202

    def test_a_missing_content_field_keeps_its_own_400(self, http: TestClient) -> None:
        resp = http.post("/v1/remember", json={"name": "n.md"})
        assert resp.status_code == 400
        assert "content" in resp.json()["error"]


class TestCaptureRoute:
    def test_a_pdf_capture_is_a_400_with_the_exact_reason(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        resp = http.post(
            "/v1/capture",
            json={"content": _PDF, "session_id": "abcd1234ef", "cwd": str(tmp_path)},
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": _PDF_REASON}

    def test_nothing_is_queued_for_a_refused_capture(
        self, http: TestClient, daemon: InProcessDaemon, tmp_path: Path
    ) -> None:
        http.post(
            "/v1/capture",
            json={"content": _PDF, "session_id": "abcd1234ef", "cwd": str(tmp_path)},
        )
        assert len(daemon.ctx.tasks) == 0

    def test_clean_html_is_still_accepted(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        with patch(
            "quarry.ingestion.web_ingest.ingest_content", return_value={"chunks": 1}
        ):
            resp = http.post(
                "/v1/capture",
                json={
                    "content": "<html><body><p>hello</p></body></html>",
                    "session_id": "abcd1234ef",
                    "cwd": str(tmp_path),
                },
            )
        assert resp.status_code == 202


class TestLearnRoute:
    def test_a_binary_lesson_is_a_400_with_the_exact_reason(
        self, http: TestClient
    ) -> None:
        resp = http.post("/v1/learn", json={"lesson": _PDF})
        assert resp.status_code == 400
        assert resp.json() == {"error": _PDF_REASON}


class TestMemoryTypeStillRejected:
    """The shared validators keep the rules the three routes already had."""

    @pytest.mark.parametrize("path", ["/v1/remember", "/v1/capture"])
    def test_an_unknown_memory_type_is_a_400(
        self, http: TestClient, tmp_path: Path, path: str
    ) -> None:
        body = {
            "name": "n.md",
            "content": _CLEAN,
            "session_id": "abcd1234ef",
            "cwd": str(tmp_path),
            "memory_type": "facts",
        }
        resp = http.post(path, json=body)
        assert resp.status_code == 400
        assert "unknown memory_type 'facts'" in resp.json()["error"]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [(_PDF, _PDF_REASON), (_DENSE, _DENSE_REASON)],
    ids=["pdf-header", "dense"],
)
class TestSurfacesAgree:
    """HTTP, CLI and MCP return the identical reason for the same payload."""

    @pytest.fixture
    def client(self, daemon: InProcessDaemon) -> Iterator[QuarryClient]:
        with daemon.client() as quarry_client:
            yield quarry_client

    def test_remember(
        self,
        http: TestClient,
        client: QuarryClient,
        payload: str,
        reason: str,
    ) -> None:
        wire = http.post("/v1/remember", json={"name": "n.md", "content": payload})
        mcp = McpTools(connect=lambda: client).remember(payload, "n.md")
        with patch.object(TargetResolver, "connect", return_value=client):
            cli = runner.invoke(app, ["remember", "--name", "n.md"], input=payload)

        assert wire.json()["error"] == reason
        assert mcp.startswith("Error:")
        assert reason in mcp
        assert cli.exit_code == 1
        assert f"Error: {reason}" in cli.output

    def test_learn(
        self,
        http: TestClient,
        client: QuarryClient,
        payload: str,
        reason: str,
    ) -> None:
        wire = http.post("/v1/learn", json={"lesson": payload})
        mcp = McpTools(connect=lambda: client).learn(payload)
        with patch.object(TargetResolver, "connect", return_value=client):
            cli = runner.invoke(app, ["learn", payload])

        assert wire.json()["error"] == reason
        assert mcp.startswith("Error:")
        assert reason in mcp
        assert cli.exit_code == 1
        assert f"Error: {reason}" in cli.output


class TestIngestRoute:
    """A URL is fetched after the 202, so a refusal arrives on the task."""

    @staticmethod
    def _pdf_response() -> MagicMock:
        headers = HTTPMessage()
        headers["Content-Type"] = "application/pdf"
        resp = MagicMock(spec=HTTPResponse)
        resp.headers = headers
        resp.url = "https://example.com/paper.pdf"
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_a_pdf_url_is_accepted_then_fails_with_the_reason(
        self, http: TestClient
    ) -> None:
        with (
            patch(
                "quarry.daemon.routes.ingestion.UrlSafetyCheck.reject_reason",
                return_value=None,
            ),
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
            ),
            patch(
                "quarry.ingestion.web_fetch.GUARDED_OPENER.open",
                return_value=self._pdf_response(),
            ),
        ):
            accepted = http.post(
                "/v1/ingest",
                json={"source": "https://example.com/paper.pdf", "scrub": True},
            )
            assert accepted.status_code == 202
            task = self._await_terminal(http, accepted.json()["task_id"])

        assert task["status"] == "failed"
        assert task["error"] == (
            "content rejected: unsupported media type application/pdf"
        )

    @staticmethod
    def _await_terminal(http: TestClient, task_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task: dict[str, object] = http.get(f"/v1/tasks/{task_id}").json()
            if task["status"] in {"completed", "failed"}:
                return task
            time.sleep(0.05)
        msg = f"task {task_id} did not finish"
        raise AssertionError(msg)


def _gap_binary() -> str:
    """Return text whose binary stretches sit between the sniff's sample windows.

    200,000 characters; the head, middle and tail 32 KiB windows are clean, so
    the sniff passes, but 40 percent of the whole document is non-text.
    """
    text = ["a"] * 200_000
    for start in (40_000, 120_000):
        text[start : start + 40_000] = ["�"] * 40_000
    return "".join(text)


def _await_terminal(http: TestClient, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        task: dict[str, object] = http.get(f"/v1/tasks/{task_id}").json()
        if task["status"] in {"completed", "failed"}:
            return task
        time.sleep(0.05)
    msg = f"task {task_id} did not finish"
    raise AssertionError(msg)


class TestRefusedAfterTheSniff:
    """Binary the sniff cannot see is refused by the chunker, quietly."""

    def test_the_payload_really_passes_the_sniff(self) -> None:
        BinaryContent().sniff(_gap_binary())

    @pytest.mark.parametrize("path", ["/v1/remember", "/v1/capture"])
    def test_the_task_fails_with_the_reason_and_one_quiet_warning(
        self,
        http: TestClient,
        daemon: InProcessDaemon,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        path: str,
    ) -> None:
        body = {
            "name": "n.md",
            "content": _gap_binary(),
            "session_id": "abcd1234ef",
            "cwd": str(tmp_path),
        }
        with caplog.at_level(logging.WARNING):
            accepted = http.post(path, json=body)
            assert accepted.status_code == 202
            task = _await_terminal(http, accepted.json()["task_id"])

        assert task["status"] == "failed"
        assert str(task["error"]).startswith("binary content rejected: ")
        [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert daemon.ctx.database.store.count() == 0


async def test_a_capture_job_fails_quietly_on_gap_binary_inline_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    inline = ScrubbedIngestJob(
        name="session-1",
        content=_gap_binary(),
        collection="repo-captures",
        format_hint="auto",
        overwrite=True,
        scrub_label="capture",
        agent_handle="",
        memory_type="",
        summary="",
    )
    state = TaskState(task_id="t-cap", kind="capture")

    with caplog.at_level(logging.WARNING):
        await CaptureIngestJob(inline=inline, source_url="").run(daemon.ctx, state)

    assert state.status == "failed"
    assert state.error.startswith("binary content rejected: ")
    [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.exc_info is None
    assert daemon.ctx.database.store.count() == 0
