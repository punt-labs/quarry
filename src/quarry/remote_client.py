"""HTTP client the CLI uses to talk to a remote quarry server."""

from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from typing import final

import typer
from rich.console import Console

from quarry.remote import ws_to_http

_err_console = Console(stderr=True)

_DEFAULT_REMOTE_TIMEOUT = 15.0
_POLL_INTERVAL_S = 0.5
_POLL_TIMEOUT_S = 120.0


@final
@dataclass(eq=False)
class RemoteError(RuntimeError):
    """Error from the remote quarry server carrying an HTTP status code."""

    _status: int
    _message: str

    def __post_init__(self) -> None:
        super().__init__(self._message)

    @property
    def status(self) -> int:
        return self._status


@final
@dataclass(frozen=True, slots=True)
class RemoteClient:
    """Authenticated HTTP client bound to one remote quarry config.

    The ``config`` mapping mirrors the ``[quarry]`` table from
    ``read_proxy_config()`` — keys ``url`` (a ``wss://`` or ``ws://`` URL),
    optional ``ca_cert``, and optional ``headers``.
    """

    _config: dict[str, object]

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        timeout: float = _DEFAULT_REMOTE_TIMEOUT,
    ) -> dict[str, object]:
        """Make an authenticated request and return the parsed JSON body.

        Args:
            method: HTTP method (GET, POST, DELETE).
            path: Request path including query string, e.g.
                ``/search?q=foo&limit=10``.
            body: Optional JSON-serialisable dict sent as the request body.
            timeout: Socket timeout in seconds.  Defaults to 15; long-running
                operations like ``/sync`` should pass a larger value.

        Returns:
            Parsed JSON response as a dict (``{}`` for an empty body).

        Raises:
            RemoteError: On a non-2xx status, a non-JSON / non-dict response
                body, or a connection failure (status 0).
            SystemExit: If the remote URL uses HTTPS but no CA cert is pinned,
                or the pinned CA cert cannot be loaded.
        """
        raw_url = str(self._config["url"])
        parsed = urllib.parse.urlparse(ws_to_http(raw_url))
        host = parsed.hostname or "localhost"
        port = parsed.port or 8420
        conn = self._open_connection(raw_url, host, port, timeout)
        encoded_body, headers = self._prepare_body(body)
        try:
            conn.request(method, path, body=encoded_body, headers=headers)
            return self._read_response(conn.getresponse())
        except OSError as exc:
            raise RemoteError(
                0,
                f"Cannot connect to remote quarry server at {host}:{port}: {exc}",
            ) from exc
        finally:
            conn.close()

    def get(self, path: str) -> dict[str, object]:
        """Make an authenticated GET request to the remote quarry server."""
        return self.request("GET", path)

    def find(
        self,
        query: str,
        limit: int,
        collection: str,
        document: str,
        page_type: str,
        source_format: str,
        agent_handle: str,
        memory_type: str,
    ) -> tuple[list[dict[str, object]], str]:
        """Execute a remote find and return ``(json_results, text)``.

        Exits 1 on a remote request failure — the caller emits the returned
        payload only on success.
        """
        params = self._search_params(
            query,
            limit,
            collection,
            document,
            page_type,
            source_format,
            agent_handle,
            memory_type,
        )
        qs = urllib.parse.urlencode(params)
        try:
            remote_resp = self.get(f"/search?{qs}")
        except RemoteError as exc:
            _err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        raw_results = remote_resp.get("results", [])
        remote_results: list[dict[str, object]] = (
            list(raw_results) if isinstance(raw_results, list) else []
        )
        json_results: list[dict[str, object]] = []
        lines: list[str] = []
        for r in remote_results:
            similarity = round(float(str(r.get("similarity", 0))), 4)
            meta = f"{r.get('page_type', '')}/{r.get('source_format', '')}"
            doc = r.get("document_name", "")
            pg = r.get("page_number", "")
            lines.append(f"\n[{doc} p.{pg} | {meta}] (similarity: {similarity})")
            text = str(r.get("text", ""))
            lines.append(text[:300])
            json_results.append(
                {
                    "document_name": r.get("document_name", ""),
                    "collection": r.get("collection", ""),
                    "page_number": r.get("page_number", 0),
                    "chunk_index": r.get("chunk_index", 0),
                    "page_type": r.get("page_type", ""),
                    "source_format": r.get("source_format", ""),
                    "agent_handle": r.get("agent_handle", ""),
                    "memory_type": r.get("memory_type", ""),
                    "summary": r.get("summary", ""),
                    "similarity": similarity,
                    "text": text,
                }
            )
        return json_results, "\n".join(lines)

    def await_task(self, task_id: str) -> dict[str, object]:
        """Poll a 202 task to a terminal result; exit 1 on failure or timeout."""
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            resp = self._poll_once(task_id)
            if resp.get("status") == "completed":
                results = resp.get("results", {})
                return results if isinstance(results, dict) else {}
            if resp.get("status") == "failed":
                _err_console.print(
                    f"Deregister failed: {resp.get('error', 'unknown error')}",
                    style="red",
                )
                raise typer.Exit(code=1)
            time.sleep(_POLL_INTERVAL_S)
        _err_console.print(
            f"Deregister did not complete within {int(_POLL_TIMEOUT_S)}s "
            f"(task_id={task_id}); the registration was removed but chunk "
            "cleanup may still be running — run 'quarry status' to verify.",
            style="yellow",
        )
        raise typer.Exit(code=1)

    @staticmethod
    def _search_params(
        query: str,
        limit: int,
        collection: str,
        document: str,
        page_type: str,
        source_format: str,
        agent_handle: str,
        memory_type: str,
    ) -> dict[str, str | int]:
        """Return the query-string params for ``/search``, dropping empties."""
        params: dict[str, str | int] = {"q": query, "limit": limit}
        optional = {
            "collection": collection,
            "document": document,
            "page_type": page_type,
            "source_format": source_format,
            "agent_handle": agent_handle,
            "memory_type": memory_type,
        }
        params.update({k: v for k, v in optional.items() if v})
        return params

    def _open_connection(
        self,
        raw_url: str,
        host: str,
        port: int,
        timeout: float,
    ) -> http.client.HTTPConnection:
        """Return an HTTP(S) connection, pinning the CA cert for ``wss://``."""
        scheme = "https" if raw_url.startswith("wss://") else "http"
        if scheme != "https":
            return http.client.HTTPConnection(host, port, timeout=timeout)
        ca_cert = self._config.get("ca_cert")
        if not ca_cert:
            raise SystemExit(
                "Remote server uses HTTPS but no CA cert is pinned. "
                "Run 'quarry login' to trust the server's certificate."
            )
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            ssl_ctx.load_verify_locations(str(ca_cert))
        except (OSError, ssl.SSLError) as exc:
            raise SystemExit(
                f"Cannot load CA certificate {ca_cert!r}. "
                f"Run 'quarry login' to configure. ({exc})"
            ) from exc
        return http.client.HTTPSConnection(host, port, context=ssl_ctx, timeout=timeout)

    def _prepare_body(
        self, body: dict[str, object] | None
    ) -> tuple[bytes | None, dict[str, str]]:
        """Return the encoded body and request headers for ``body``."""
        headers_raw = self._config.get("headers", {})
        headers: dict[str, str] = (
            {k: str(v) for k, v in headers_raw.items()}
            if isinstance(headers_raw, dict)
            else {}
        )
        if body is None:
            return None, headers
        headers["Content-Type"] = "application/json"
        return json.dumps(body).encode("utf-8"), headers

    @staticmethod
    def _read_response(resp: http.client.HTTPResponse) -> dict[str, object]:
        """Parse a response into a dict, raising RemoteError on any anomaly."""
        resp_body = resp.read()
        if resp.status >= 300:
            body_text = resp_body.decode("utf-8", errors="replace")
            raise RemoteError(
                resp.status,
                f"Remote quarry server returned HTTP {resp.status}: {body_text}",
            )
        if not resp_body:
            return {}
        try:
            parsed_body = json.loads(resp_body)
        except json.JSONDecodeError as exc:
            preview = resp_body[:200].decode("utf-8", errors="replace")
            raise RemoteError(
                resp.status,
                f"Remote quarry server returned non-JSON response: {preview!r}",
            ) from exc
        if not isinstance(parsed_body, dict):
            raise RemoteError(
                resp.status,
                f"Malformed response from remote server: "
                f"expected JSON object, got {type(parsed_body).__name__}",
            )
        response_data: dict[str, object] = parsed_body
        return response_data

    def _poll_once(self, task_id: str) -> dict[str, object]:
        """Poll once; a transient connection blip reads as still-running."""
        try:
            return self.request("GET", f"/tasks/{task_id}")
        except RemoteError as exc:
            if exc.status == 0:
                return {"status": "running"}
            _err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
