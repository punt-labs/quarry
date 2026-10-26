"""Binary content is refused with one reason on every content surface."""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from http.client import HTTPMessage, HTTPResponse
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.client import QuarryClient, TargetResolver
from quarry.content_guard import BinaryContent
from quarry.daemon.content_jobs import CaptureIngestJob, ScrubbedIngestJob
from quarry.daemon.ingest_labels import IngestLabels
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
        assert resp.json() == {
            "error": "binary content rejected: non-text ratio 0.33 exceeds 0.25"
        }

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
    def test_nothing_is_queued_for_a_refused_lesson(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        http.post("/v1/learn", json={"lesson": _PDF})
        assert len(daemon.ctx.tasks) == 0

    def test_a_lesson_both_binary_and_overlong_reports_the_binary_refusal(
        self, http: TestClient
    ) -> None:
        resp = http.post("/v1/learn", json={"lesson": _PDF + "x" * 1000})
        assert resp.status_code == 400
        assert resp.json() == {"error": _PDF_REASON}

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
        assert resp.json() == {
            "error": (
                "unknown memory_type 'facts'; expected one of "
                "fact, observation, opinion, procedure"
            )
        }


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
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        refused = [f"remember refused: {reason}"]
        with caplog.at_level(logging.WARNING):
            wire = http.post("/v1/remember", json={"name": "n.md", "content": payload})
            assert _messages(caplog, _ROUTE_LOGGER) == refused
            caplog.clear()
            mcp = McpTools(connect=lambda: client).remember(payload, "n.md")
            assert _messages(caplog, _ROUTE_LOGGER) == refused
            assert _messages(caplog, _GUARD_LOGGER) == [f"Error in remember: {reason}"]
            caplog.clear()
            with patch.object(TargetResolver, "connect", return_value=client):
                cli = runner.invoke(app, ["remember", "--name", "n.md"], input=payload)

        assert wire.json()["error"] == reason
        assert mcp == f"Error: {reason}"
        assert cli.exit_code == 1
        assert re.fullmatch(
            rf"[\d-]+ [\d:]+ \[WARNING\] {re.escape(_ROUTE_LOGGER)}: "
            rf"{re.escape(refused[0])}\nError: {re.escape(reason)}",
            cli.output.strip(),
        )
        assert not [r for r in caplog.records if r.exc_info]

    def test_learn(
        self,
        http: TestClient,
        client: QuarryClient,
        payload: str,
        reason: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        refused = [f"learn refused: {reason}"]
        with caplog.at_level(logging.WARNING):
            wire = http.post("/v1/learn", json={"lesson": payload})
            assert _messages(caplog, _ROUTE_LOGGER) == refused
            caplog.clear()
            mcp = McpTools(connect=lambda: client).learn(payload)
            assert _messages(caplog, _ROUTE_LOGGER) == refused
            assert _messages(caplog, _GUARD_LOGGER) == [f"Error in learn: {reason}"]
            caplog.clear()
            with patch.object(TargetResolver, "connect", return_value=client):
                cli = runner.invoke(app, ["learn", payload])

        assert wire.json()["error"] == reason
        assert mcp == f"Error: {reason}"
        assert cli.exit_code == 1
        assert re.fullmatch(
            rf"[\d-]+ [\d:]+ \[WARNING\] {re.escape(_ROUTE_LOGGER)}: "
            rf"{re.escape(refused[0])}\nError: {re.escape(reason)}",
            cli.output.strip(),
        )
        assert not [r for r in caplog.records if r.exc_info]


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
                "quarry.daemon.routes.content_group.UrlAdmission.reject_reason",
                return_value=None,
            ),
            patch(
                "quarry.url_safety.socket_module.getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
            ),
            patch(
                "quarry.ingestion.opened_response.GUARDED_OPENER.open",
                return_value=self._pdf_response(),
            ),
        ):
            accepted = http.post(
                "/v1/ingest",
                json={"source": "https://example.com/paper.pdf", "scrub": True},
            )
            assert accepted.status_code == 202
            task = _await_terminal(http, accepted.json()["task_id"])

        assert task["status"] == "failed"
        assert task["error"] == (
            "content rejected: unsupported media type application/pdf"
        )


_GAP_REASON = "binary content rejected: page 1 non-text ratio 0.40 exceeds 0.25"
_ROUTE_LOGGER = "quarry.daemon.routes.content_group"
_GUARD_LOGGER = "quarry.mcp_guard"


def _messages(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    """Return the WARNING-or-worse messages one logger emitted."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == name and r.levelno >= logging.WARNING
    ]


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

    @pytest.mark.parametrize(
        ("path", "kind", "subject"),
        [
            ("/v1/remember", "remember", "n.md"),
            ("/v1/capture", "capture", "session-abcd1234"),
        ],
    )
    def test_the_task_fails_with_the_reason_and_one_quiet_warning(
        self,
        http: TestClient,
        daemon: InProcessDaemon,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        path: str,
        kind: str,
        subject: str,
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
        assert task["error"] == _GAP_REASON
        [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert record.getMessage() == (
            f"Background {kind} {subject} (task {accepted.json()['task_id']}) "
            f"rejected: {_GAP_REASON}"
        )
        assert daemon.ctx.database.store.count() == 0


async def test_a_capture_job_fails_quietly_on_gap_binary_inline_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    inline = ScrubbedIngestJob(
        name="session-1",
        content=_gap_binary(),
        format_hint="auto",
        scrub_label="capture",
        labels=IngestLabels(
            overwrite=True,
            collection="repo-captures",
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )
    state = TaskState(task_id="t-cap", kind="capture")

    with caplog.at_level(logging.WARNING):
        await CaptureIngestJob(inline=inline, source_url=None).run(daemon.ctx, state)

    assert state.status == "failed"
    assert state.error == _GAP_REASON
    [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.exc_info is None
    assert daemon.ctx.database.store.count() == 0


class TestOversizedLabels:
    """summary and names are copied onto every chunk row, so the routes cap them."""

    _BIG = "x" * 8_193
    _REASON = "row budget exceeded: {column} 8,193 bytes > 8,192"

    def _assert_refused(
        self, status: int, body: object, column: str, daemon: InProcessDaemon
    ) -> None:
        assert status == 400
        assert body == {"error": self._REASON.format(column=column)}
        assert len(daemon.ctx.tasks) == 0

    def test_remember_refuses_an_oversized_summary(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        resp = http.post(
            "/v1/remember",
            json={"name": "n.md", "content": _CLEAN, "summary": self._BIG},
        )
        self._assert_refused(resp.status_code, resp.json(), "summary", daemon)

    def test_remember_refuses_an_oversized_non_string_label(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        resp = http.post(
            "/v1/remember",
            json={"name": "n.md", "content": _CLEAN, "summary": [self._BIG]},
        )
        assert resp.status_code == 400
        assert "row budget exceeded: summary" in resp.json()["error"]
        assert len(daemon.ctx.tasks) == 0

    def test_remember_refuses_an_oversized_name(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        resp = http.post("/v1/remember", json={"name": self._BIG, "content": _CLEAN})
        self._assert_refused(resp.status_code, resp.json(), "name", daemon)

    def test_capture_refuses_an_oversized_summary(
        self, http: TestClient, daemon: InProcessDaemon, tmp_path: Path
    ) -> None:
        resp = http.post(
            "/v1/capture",
            json={
                "content": _CLEAN,
                "session_id": "abcd1234ef",
                "cwd": str(tmp_path),
                "summary": self._BIG,
            },
        )
        self._assert_refused(resp.status_code, resp.json(), "summary", daemon)

    def test_capture_refuses_an_oversized_document_name(
        self, http: TestClient, daemon: InProcessDaemon, tmp_path: Path
    ) -> None:
        resp = http.post(
            "/v1/capture",
            json={"content": _CLEAN, "document_name": self._BIG, "cwd": str(tmp_path)},
        )
        self._assert_refused(resp.status_code, resp.json(), "document_name", daemon)

    def test_learn_refuses_an_oversized_topic(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        resp = http.post("/v1/learn", json={"lesson": "short", "topic": self._BIG})
        self._assert_refused(resp.status_code, resp.json(), "topic", daemon)

    def test_ingest_refuses_an_oversized_summary(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        resp = http.post(
            "/v1/ingest",
            json={"source": "https://example.com/p", "summary": self._BIG},
        )
        self._assert_refused(resp.status_code, resp.json(), "summary", daemon)

    @pytest.mark.parametrize("field", ["agent_handle", "collection"])
    def test_remember_refuses_an_oversized_routing_label(
        self, http: TestClient, daemon: InProcessDaemon, field: str
    ) -> None:
        resp = http.post(
            "/v1/remember", json={"name": "n.md", "content": _CLEAN, field: self._BIG}
        )
        self._assert_refused(resp.status_code, resp.json(), field, daemon)

    def test_remember_refuses_a_derived_collection_over_the_cap(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        """``memory-<handle>`` is 7 bytes longer than the handle that passed."""
        resp = http.post(
            "/v1/remember",
            json={"name": "n.md", "content": _CLEAN, "agent_handle": "h" * 8_190},
        )
        assert resp.status_code == 400
        assert resp.json() == {
            "error": "row budget exceeded: collection 8,197 bytes > 8,192"
        }
        assert len(daemon.ctx.tasks) == 0

    @pytest.mark.parametrize("field", ["agent_handle", "collection"])
    def test_ingest_refuses_an_oversized_routing_label(
        self, http: TestClient, daemon: InProcessDaemon, field: str
    ) -> None:
        resp = http.post(
            "/v1/ingest", json={"source": "https://example.com/p", field: self._BIG}
        )
        self._assert_refused(resp.status_code, resp.json(), field, daemon)

    @pytest.mark.parametrize("field", ["agent_handle", "collection"])
    def test_capture_refuses_an_oversized_routing_label(
        self, http: TestClient, daemon: InProcessDaemon, tmp_path: Path, field: str
    ) -> None:
        resp = http.post(
            "/v1/capture",
            json={
                "content": _CLEAN,
                "session_id": "abcd1234ef",
                "cwd": str(tmp_path),
                field: self._BIG,
            },
        )
        self._assert_refused(resp.status_code, resp.json(), field, daemon)

    def test_ingest_refuses_an_over_long_source(
        self, http: TestClient, daemon: InProcessDaemon
    ) -> None:
        resp = http.post("/v1/ingest", json={"source": "https://e.test/" + self._BIG})
        assert resp.status_code == 400
        assert resp.json() == {"error": "URL rejected: malformed URL"}
        assert len(daemon.ctx.tasks) == 0

    def test_an_oversized_label_is_logged_once_at_warning(
        self, http: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            http.post(
                "/v1/remember",
                json={"name": "n.md", "content": _CLEAN, "summary": self._BIG},
            )
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            (
                "WARNING",
                "remember refused: row budget exceeded: summary 8,193 bytes > 8,192",
            )
        ]

    def test_labels_at_the_cap_are_accepted(self, http: TestClient) -> None:
        with patch(
            "quarry.ingestion.web_ingest.ingest_content", return_value={"chunks": 1}
        ):
            resp = http.post(
                "/v1/remember",
                json={"name": "n.md", "content": _CLEAN, "summary": "x" * 8_192},
            )
        assert resp.status_code == 202


class TestFormatHint:
    _REASON = (
        "unknown format_hint 'docx'; expected one of auto, plain, markdown, latex, html"
    )

    def test_remember_refuses_an_unknown_format_hint(self, http: TestClient) -> None:
        resp = http.post(
            "/v1/remember",
            json={"name": "n.md", "content": _CLEAN, "format_hint": "docx"},
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": self._REASON}

    def test_capture_refuses_an_unknown_format_hint(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        resp = http.post(
            "/v1/capture",
            json={
                "content": _CLEAN,
                "session_id": "abcd1234ef",
                "cwd": str(tmp_path),
                "format_hint": "docx",
            },
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": self._REASON}

    @pytest.mark.parametrize("hint", ["auto", "plain", "markdown", "latex", "html"])
    def test_every_documented_hint_is_accepted(
        self, http: TestClient, hint: str
    ) -> None:
        with patch(
            "quarry.ingestion.web_ingest.ingest_content", return_value={"chunks": 1}
        ):
            resp = http.post(
                "/v1/remember",
                json={"name": "n.md", "content": _CLEAN, "format_hint": hint},
            )
        assert resp.status_code == 202


class TestUrlAdmission:
    """A URL with userinfo, a bad port or whitespace is a fixed-text 400."""

    @pytest.mark.parametrize(
        "source",
        [
            "http://u:SECRET@example.com/p?token=SECRET",
            "http://example.com:99999/p",
            "http://example.com/p?token=SECRET x",
        ],
        ids=["userinfo", "port", "space"],
    )
    def test_ingest_refuses_it_without_echoing_it(
        self, http: TestClient, daemon: InProcessDaemon, source: str
    ) -> None:
        resp = http.post("/v1/ingest", json={"source": source})
        assert resp.status_code == 400
        assert resp.json() == {"error": "URL rejected: malformed URL"}
        assert len(daemon.ctx.tasks) == 0

    def test_capture_refuses_a_source_url_with_userinfo(
        self, http: TestClient, daemon: InProcessDaemon, tmp_path: Path
    ) -> None:
        resp = http.post(
            "/v1/capture",
            json={
                "content": _CLEAN,
                "session_id": "abcd1234ef",
                "cwd": str(tmp_path),
                "source_url": "http://u:SECRET@example.com/p",
            },
        )
        assert resp.status_code == 400
        assert "SECRET" not in resp.text
        assert len(daemon.ctx.tasks) == 0


class TestACaptureIsUrlSourcedOnlyWhenItIsAPageCapture:
    """The route decides; the name alone never does."""

    _URL = "https://example.com/page"

    @staticmethod
    def _filed(http: TestClient, tmp_path: Path, **fields: str) -> CaptureIngestJob:
        body = {"content": "x", "cwd": str(tmp_path), **fields}
        with (
            patch("quarry.url_admission.UrlAdmission.reject_reason", return_value=None),
            patch(
                "quarry.daemon.routes.captures.CaptureRoutes.submit",
                autospec=True,
                return_value=JSONResponse({}),
            ) as submit,
        ):
            http.post("/v1/capture", json=body)
        job = submit.call_args.args[1]
        assert isinstance(job, CaptureIngestJob)
        return job

    def test_a_page_capture_named_for_its_source_is(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        job = self._filed(http, tmp_path, document_name=self._URL, source_url=self._URL)
        assert job.inline.url_sourced
        assert job.source_url is not None

    def test_a_transcript_with_a_url_shaped_name_is_not(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        job = self._filed(http, tmp_path, document_name=self._URL)
        assert not job.inline.url_sourced

    def test_a_source_url_with_a_different_url_shaped_name_is_not(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        job = self._filed(
            http,
            tmp_path,
            document_name="https://example.com/other",
            source_url=self._URL,
        )
        assert not job.inline.url_sourced

    def test_a_capture_that_is_not_a_page_never_refetches(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        job = self._filed(
            http,
            tmp_path,
            document_name="https://example.com/other",
            source_url=self._URL,
        )
        assert job.source_url is None

    def test_a_non_page_capture_with_empty_content_stores_a_label_without_a_fetch(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        job = self._filed(
            http,
            tmp_path,
            document_name="https://example.com/other",
            source_url=self._URL,
            content="<html></html>",
            format_hint="html",
        )
        with (
            patch(
                "quarry.ingestion.web_ingest.ingest_content", return_value={"chunks": 0}
            ),
            patch.object(CaptureIngestJob, "_refetch") as refetch,
        ):
            job._capture(MagicMock())  # pyright: ignore[reportPrivateUsage]
        refetch.assert_not_called()

    def test_the_mismatch_is_logged_once_without_the_name_or_the_url(
        self, http: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="quarry.capture_naming"):
            self._filed(
                http,
                tmp_path,
                document_name="https://example.com/other",
                source_url=self._URL,
            )
        messages = [
            r.getMessage() for r in caplog.records if r.name == "quarry.capture_naming"
        ]
        assert messages == [
            (
                "capture: source_url given but document_name is not its page name; "
                "stored as a label"
            )
        ]

    def test_a_page_capture_logs_no_mismatch(
        self, http: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="quarry.capture_naming"):
            self._filed(http, tmp_path, document_name=self._URL, source_url=self._URL)
        assert [r for r in caplog.records if r.name == "quarry.capture_naming"] == []


class TestTheRoutesCheckTheNamesThatAreStored:
    @staticmethod
    def _submitted(http: TestClient, path: str, body: dict[str, object]) -> int:
        with (
            patch("quarry.url_admission.UrlAdmission.reject_reason", return_value=None),
            patch(
                "quarry.daemon.routes.base.RouteGroup.submit",
                autospec=True,
                return_value=JSONResponse({}),
            ),
        ):
            return http.post(path, json=body).status_code

    def test_a_long_query_with_a_short_stored_name_is_accepted(
        self, http: TestClient
    ) -> None:
        source = "https://example.com/p?token=" + "x" * 7_000
        assert self._submitted(http, "/v1/ingest", {"source": source}) == 200

    def test_a_short_url_whose_stored_name_is_over_the_cap_is_refused(
        self, http: TestClient
    ) -> None:
        # Percent-encoding makes the stored name longer than the URL as sent.
        source = "https://example.com/" + "\u00e9" * 2_000
        assert self._submitted(http, "/v1/ingest", {"source": source}) == 400

    def test_a_captures_name_over_the_cap_only_after_its_suffix_is_refused(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        name = SimpleNamespace(name="r" * 8_190 + "-captures")
        with patch(
            "quarry.daemon.routes.captures.CapturesCollection.for_registry_path",
            return_value=name,
        ):
            status = self._submitted(
                http,
                "/v1/capture",
                {"content": _CLEAN, "session_id": "abcd1234ef", "cwd": str(tmp_path)},
            )
        assert status == 400

    def test_a_lessons_name_over_the_cap_only_after_its_suffix_is_refused(
        self, http: TestClient
    ) -> None:
        name = SimpleNamespace(name="r" * 8_190 + "-lessons")
        with patch(
            "quarry.daemon.routes.ingestion.LessonsCollection.for_registry_path",
            return_value=name,
        ):
            status = self._submitted(http, "/v1/learn", {"lesson": "short"})
        assert status == 400


class TestACaptureSnapshotTime:
    @staticmethod
    def _filed(
        http: TestClient, tmp_path: Path, **fields: object
    ) -> tuple[int, object]:
        body = {"content": "x", "session_id": "abcd1234ef", "cwd": str(tmp_path)}
        with patch(
            "quarry.daemon.routes.captures.CaptureRoutes.submit",
            autospec=True,
            return_value=JSONResponse({}),
        ) as submit:
            resp = http.post("/v1/capture", json={**body, **fields})
        job = submit.call_args.args[1] if submit.call_args else None
        return resp.status_code, job

    def test_a_valid_instant_reaches_the_job(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        status, job = self._filed(http, tmp_path, captured_at="2026-01-02T03:04:05Z")
        assert status == 200
        assert isinstance(job, CaptureIngestJob)
        assert job.inline.captured_at is not None
        assert job.inline.captured_at.isoformat() == "2026-01-02T03:04:05+00:00"

    def test_an_absent_instant_is_queue_time(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        _, job = self._filed(http, tmp_path)
        assert isinstance(job, CaptureIngestJob)
        assert job.inline.captured_at is None

    def test_a_future_instant_is_clamped_to_now_not_refused(
        self, http: TestClient, tmp_path: Path
    ) -> None:
        status, job = self._filed(http, tmp_path, captured_at="2999-01-01T00:00:00Z")
        assert status == 200
        assert isinstance(job, CaptureIngestJob)
        assert job.inline.captured_at is not None
        assert abs((datetime.now(UTC) - job.inline.captured_at).total_seconds()) < 60

    @pytest.mark.parametrize(
        "body",
        [
            {"captured_at": 123},
            {"captured_at": ["2026-01-01T00:00:00Z"]},
            {"session_id": 5},
        ],
    )
    def test_a_non_string_captured_at_or_session_id_is_a_400(
        self, body: dict[str, object], http: TestClient, tmp_path: Path
    ) -> None:
        status, job = self._filed(http, tmp_path, **body)
        assert status == 400
        assert job is None

    @pytest.mark.parametrize(
        "raw",
        [
            "yesterday",
            "1999-01-01T00:00:00Z",
            "0001-01-01T00:00:00+05:00",
            "9999-12-31T23:59:59-05:00",
        ],
    )
    def test_a_bad_or_out_of_bounds_instant_is_a_400(
        self, raw: str, http: TestClient, tmp_path: Path
    ) -> None:
        status, job = self._filed(http, tmp_path, captured_at=raw)
        assert status == 400
        assert job is None
