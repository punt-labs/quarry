"""No URL secret reaches a log, a task, an HTTP body or a stored name.

Each scenario drives the real in-process daemon against a local origin, with URLs
that carry marker secrets in every position a URL can hold one (userinfo, query,
fragment, redirect target, path parameter), then searches every sink for the
markers.  The sinks are the log file as written by the production handlers, the
first-party log records, the task's error and results, and the HTTP body.
"""

from __future__ import annotations

import _socket
import json
import logging
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from starlette.testclient import TestClient

from quarry.logging_config import LoggingConfig
from quarry.url_safety import UrlSafetyCheck
from tests.inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

real_getaddrinfo = _socket.getaddrinfo
_MARKERS = ("USEC", "QSEC", "FSEC", "RSEC", "PSEC")
_HTML = b"<html><title>t</title><body><h1>Hi</h1><p>hello world text</p></body></html>"


class _Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    port: ClassVar[int] = 0

    def log_message(self, *args: object, **kwargs: object) -> None:
        pass

    def _send(
        self,
        code: int,
        body: bytes = b"",
        ctype: str = "text/html",
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        base = f"http://127.0.0.1:{self.port}"
        path = self.path
        if path.startswith("/sitemap.xml"):
            locs = [
                f"{base}/page1?token=QSEC_entry",
                f"http://u:USEC_entry@127.0.0.1:{self.port}/page2",
                f"{base}/page3#FSEC_frag",
                "http://127.0.0.1:99999/p?token=QSEC_badport",
                f"{base}/page4?token=QSEC_space x",
            ]
            entries = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
            xml = (
                "<?xml version='1.0'?><urlset xmlns="
                f"'http://www.sitemaps.org/schemas/sitemap/0.9'>{entries}</urlset>"
            )
            self._send(200, xml.encode(), "application/xml")
        elif path.startswith("/index.xml"):
            xml = (
                "<?xml version='1.0'?><sitemapindex xmlns="
                "'http://www.sitemaps.org/schemas/sitemap/0.9'>"
                f"<sitemap><loc>{base}/sub1.xml?token=QSEC_child</loc></sitemap>"
                f"<sitemap><loc>http://u:USEC_child@127.0.0.1:{self.port}/sub2.xml"
                "</loc></sitemap></sitemapindex>"
            )
            self._send(200, xml.encode(), "application/xml")
        elif path.startswith("/redir"):
            target = f"{base}/page?token=RSEC_target"
            self._send(302, headers=(("Location", target),))
        elif path.startswith("/err500"):
            self._send(500, b"boom", "text/plain")
        elif path.startswith("/pdf"):
            self._send(200, b"%PDF-1.4 xx", "application/pdf")
        elif path.startswith("/page"):
            self._send(200, _HTML)
        else:
            self._send(404, b"not found", "text/plain")


@pytest.fixture
def origin(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A loopback origin, admitted by the SSRF policy for the test's duration."""
    monkeypatch.setattr(UrlSafetyCheck, "_is_blocked", staticmethod(lambda _a: False))

    def resolve(host: str, *args: Any, **kwargs: Any) -> Any:
        """Every host is the loopback origin, except the unresolvable ones."""
        if host.endswith(".invalid"):
            raise socket.gaierror(socket.EAI_NONAME, "unresolvable")
        return real_getaddrinfo("127.0.0.1", *args, **kwargs)

    monkeypatch.setattr("quarry.url_safety.socket_module.getaddrinfo", resolve)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    _Origin.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{_Origin.port}"
    server.shutdown()


@pytest.fixture
def logfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Configure production logging for real and yield the file it writes."""
    monkeypatch.setenv("QUARRY_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
    root = logging.getLogger()
    prior = (root.handlers[:], root.level)
    LoggingConfig.configure(log_file=LoggingConfig.DAEMON_LOG)
    yield tmp_path / "logs" / LoggingConfig.DAEMON_LOG
    for handler in root.handlers:
        handler.close()
    root.handlers[:], root.level = prior


class _Run:
    """One scenario's observations, searchable for markers."""

    def __init__(self, status: int, body: Any, task: dict[str, Any] | None) -> None:
        self.status = status
        self.body = body
        self.task = task

    def surfaces(self) -> str:
        return json.dumps({"body": self.body, "task": self.task})


def _post(
    http: TestClient, path: str, body: dict[str, object], *, wait: bool = True
) -> _Run:
    resp = http.post(path, json=body)
    payload: Any = resp.json()
    task = None
    if resp.status_code == 202 and wait:
        for _ in range(300):
            polled = http.get(f"/v1/tasks/{payload['task_id']}").json()
            if polled.get("status") in {"completed", "failed"}:
                task = polled
                break
            time.sleep(0.1)
    return _Run(resp.status_code, payload, task)


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


@pytest.fixture
def daemon(tmp_path: Path) -> InProcessDaemon:
    return InProcessDaemon(tmp_path / "data")


@pytest.fixture
def http(daemon: InProcessDaemon) -> Iterator[TestClient]:
    with TestClient(daemon.app, raise_server_exceptions=False) as client:
        yield client


_REFUSED = (400, "")
_EXPECTED_OUTCOMES = {
    "plain page": (202, "completed"),
    "sitemap": (202, "completed"),
    "sitemap index": (202, "failed"),
    "userinfo": _REFUSED,
    "space": _REFUSED,
    "bad port": _REFUSED,
    "mixed scheme": (202, "completed"),
    "ftp": _REFUSED,
    "nfkc": _REFUSED,
    "unresolvable": _REFUSED,
    "redirect": (202, "completed"),
    "server error": (202, "failed"),
    "pdf": (202, "failed"),
    "scheme token": _REFUSED,
    "path param": (202, "completed"),
}


def _ingest_sources(base: str) -> dict[str, str]:
    return {
        "plain page": f"{base}/page?token=QSEC_a",
        "sitemap": f"{base}/sitemap.xml?token=QSEC_b",
        "sitemap index": f"{base}/index.xml?token=QSEC_c",
        "userinfo": f"http://u:USEC_d@127.0.0.1:{_Origin.port}/page?token=QSEC_d",
        "space": f"{base}/page?token=QSEC_e x",
        "bad port": "http://u:USEC_f@127.0.0.1:99999/p?token=QSEC_f",
        "mixed scheme": f"HTTP://127.0.0.1:{_Origin.port}/page?token=QSEC_g",
        "ftp": "ftp://u:USEC_h@h.test/x?token=QSEC_h",
        "nfkc": "http://u:USEC_i@\uff03127.0.0.1/p?token=QSEC_i",
        "unresolvable": "http://nonexistent-host.invalid/p?token=QSEC_j",
        "redirect": f"{base}/redir?token=QSEC_k",
        "server error": f"{base}/err500?token=QSEC_l",
        "pdf": f"{base}/pdf?token=QSEC_m",
        "scheme token": "QSEC_scheme-token:foo",
        "path param": f"{base}/page;sess=PSEC_n?token=QSEC_n",
    }


def test_ingest_never_leaks_a_url_secret(
    origin: str, logfile: Path, http: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    surfaces: list[str] = []
    outcomes: dict[str, tuple[int, str]] = {}
    with caplog.at_level(logging.DEBUG):
        for label, source in _ingest_sources(origin).items():
            run = _post(http, "/v1/ingest", {"source": source, "overwrite": True})
            surfaces.append(f"{label}: {run.surfaces()}")
            task_status = run.task["status"] if run.task else ""
            outcomes[label] = (run.status, task_status)
        # a second pass over the same stored documents exercises the overwrite path
        _post(http, "/v1/ingest", {"source": f"{origin}/page?token=QSEC_a"})
    _flush()

    assert outcomes == _EXPECTED_OUTCOMES

    first_party = [
        r.getMessage() for r in caplog.records if r.name.startswith("quarry")
    ]
    written = logfile.read_text()
    # The sinks must be able to show a leak, so each must hold something real.
    assert first_party
    assert "127.0.0.1" in written
    sinks = {
        "surfaces": "\n".join(surfaces),
        "first-party records": "\n".join(first_party),
        "written log": written,
    }
    for sink, text in sinks.items():
        for marker in _MARKERS:
            assert marker not in text, f"{marker} reached {sink}"


def test_the_written_log_shows_a_bare_marker_when_one_is_logged(
    logfile: Path,
) -> None:
    """Self-check of the file sink: only a URL-shaped marker is redacted."""
    logging.getLogger("quarry.selfcheck").warning("bare token USEC here")
    _flush()
    assert "USEC" in logfile.read_text()


def test_capture_and_remember_and_lookup_never_leak_a_url_secret(
    origin: str,
    logfile: Path,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    empty = "<html><body></body></html>"
    runs = [
        _post(
            http,
            "/v1/capture",
            {
                "content": empty,
                "cwd": str(tmp_path),
                "document_name": f"doc-{n}",
                "format_hint": "html",
                "source_url": source,
            },
        )
        for n, source in enumerate(
            [
                f"{origin}/redir?token=QSEC_c1",
                f"{origin}/err500?token=QSEC_c2",
                "http://127.0.0.1:99999/p?token=QSEC_c3&u=USEC_c3",
                "http://u:USEC_c4@127.0.0.1/p",
            ]
        )
    ]
    runs.append(
        _post(
            http,
            "/v1/remember",
            {
                "name": "https://u:USEC_r@example.com/p?token=QSEC_r#FSEC_r",
                "content": "hello remember body text here",
                "overwrite": True,
            },
        )
    )
    lookups = (
        "http://u:USEC_l1@\uff03host.example/p?token=QSEC_l1",
        "http://h:99999/p?token=QSEC_l2",
        "http://h/p?token=QSEC_l3 x",
    )
    runs.extend(
        _post(http, "/v1/captures/lookup", {"url": url, "cwd": ""}) for url in lookups
    )
    outcomes = [(r.status, r.task["status"] if r.task else "") for r in runs[:5]]
    assert outcomes == [
        (202, "completed"),  # c1: redirect, re-fetched and stored
        (202, "failed"),  # c2: server error
        (400, ""),  # c3: bad port refused at the route
        (400, ""),  # c4: userinfo refused at the route
        (202, "completed"),  # remember
    ]
    for lookup in runs[5:]:
        assert lookup.status == 200
        assert lookup.body == {"matched": False, "document_name": None}
    _flush()

    sinks = {
        "surfaces": "\n".join(run.surfaces() for run in runs),
        "written log": logfile.read_text(),
    }
    for sink, text in sinks.items():
        for marker in _MARKERS:
            assert marker not in text, f"{marker} reached {sink}"


def test_the_harness_sees_a_leak_when_there_is_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first-party sink is raw: a careless log line would be caught here."""
    with caplog.at_level(logging.INFO):
        logging.getLogger("quarry.careless").info("%s", "https://u:USEC@h/p?t=QSEC")
    raw = "\n".join(r.getMessage() for r in caplog.records)
    assert any(marker in raw for marker in _MARKERS)


def _stored_names(daemon: InProcessDaemon) -> set[str]:
    return {
        doc["document_name"] for doc in daemon.ctx.database.catalog.list_documents()
    }


def test_every_stored_url_name_is_scheme_host_port_and_path(
    origin: str, http: TestClient, daemon: InProcessDaemon, tmp_path: Path
) -> None:
    """Plain ingest, sitemap pages, a URL-named remember and a capture alike."""
    for source in (f"{origin}/page?token=QSEC_a", f"{origin}/page;sess=PSEC_b?x=1"):
        _post(http, "/v1/ingest", {"source": source, "overwrite": True})
    _post(http, "/v1/ingest", {"source": f"{origin}/sitemap.xml", "overwrite": True})
    _post(
        http,
        "/v1/remember",
        {
            "name": "https://u:USEC_r@example.com/notes?token=QSEC_r#FSEC_r",
            "content": "hello remember body text here",
        },
    )
    _post(
        http,
        "/v1/capture",
        {
            "content": "<html><body><p>captured text here</p></body></html>",
            "cwd": str(tmp_path),
            "document_name": f"{origin}/captured?token=QSEC_c",
            "format_hint": "html",
        },
    )

    names = _stored_names(daemon)
    # A query is kept as a digest only: ``?q=`` and 16 hex digits.
    digest = re.compile(r"\?q=[0-9a-f]{16}$")
    bare = {digest.sub("", name) for name in names}
    assert f"{origin}/page" in bare
    assert f"{origin}/page1" in bare
    assert f"{origin}/page3" in bare
    assert "https://example.com/notes" in bare
    assert f"{origin}/captured" in bare
    for name in names:
        for marker in _MARKERS:
            assert marker not in name
        assert "?" not in digest.sub("", name)
        assert "#" not in name
        assert ";" not in name


def test_a_real_sitemap_parser_failure_is_the_exact_refusal_and_leaves_no_secret(
    monkeypatch: pytest.MonkeyPatch, logfile: Path
) -> None:
    """USP logs the failing URL and its reason; neither reaches the written log."""
    import quarry.sitemap_web_client as swc
    from quarry.http_url import HttpUrl
    from quarry.ingestion.fetch_refused import FetchRefusedError
    from quarry.sitemap import SitemapDiscovery

    url = "https://safe.example/sitemap.xml?token=QSEC_usp"

    class _Opener:
        def open(self, request: object, timeout: float | None = None) -> Any:
            from urllib.error import URLError

            raise URLError(f"refused {url}")

    monkeypatch.setattr(swc, "GUARDED_OPENER", _Opener())
    monkeypatch.setattr("time.sleep", lambda _seconds: None)  # USP's retry back-off
    with pytest.raises(FetchRefusedError) as info:
        SitemapDiscovery.discover_urls(HttpUrl(url))
    _flush()

    assert str(info.value) == "fetch failed: sitemap could not be fetched or parsed"
    written = logfile.read_text()
    assert "safe.example" in written
    assert "QSEC" not in written
