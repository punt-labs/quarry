"""End-to-end post-web-search capture regression (quarry-871u / G5).

The daemon's post-release smoke test showed WebSearch captures never
landing in ``<repo>-captures`` on the live v3.2.0 daemon, even though the
unit tests for :class:`quarry.web_search_capture.WebSearchPayload` all
pass.  The unit suite stubs the payload class directly and never runs the
handler through the ``quarry-hook`` binary against a real daemon, so a
regression at the entry point (missing logging config → skip-with-no-
breadcrumb, or a shape drift the fallback branch does not cover) is
invisible.

These tests spawn a real ``quarryd`` and drive ``quarry-hook
post-web-search`` with each of the three canonical payload shapes.  After
the hook exits, the test polls the daemon's HTTP API for a captured
document named ``search: <query>``.  On v3.2.0 the poll times out with
zero matches for every parametrized shape — every case FAILS.  The
``unknown_shape`` case additionally asserts that the WARN-with-shape-
metadata line fires (blocked today by quarry-ridg's logging gap; that
case double-fails until BOTH beads land).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest

from tests.hook_fixtures import (
    HookInvoker,
    ephemeral_daemon,
    hook_subprocess,
    load_payload,
)

if TYPE_CHECKING:
    from tests.hook_fixtures import EphemeralDaemon


__all__ = ["ephemeral_daemon", "hook_subprocess"]


pytestmark = [pytest.mark.hook_integration, pytest.mark.network]
# ``network`` opts out of the autouse ``_fake_dns`` in ``tests/conftest.py`` —
# that fixture patches ``socket.getaddrinfo`` globally via a ``socket_module``
# alias, and its 5-tuple with ``None`` family/proto crashes ``httpx``'s socket
# connect (TypeError, not HTTPError, so the except in ``_search_for_capture``
# never catches it).  The daemon poll here uses the real resolver against
# ``127.0.0.1``, so no DNS happens; the marker is only there to leave the
# stdlib socket seam untouched.


_QUARRY_871U_SUMMARY = (
    "quarry-871u (G5): the live daemon receives no capture from "
    "quarry-hook post-web-search — the digest never lands in "
    "<repo>-captures on any of the three known payload shapes."
)
_QUARRY_RIDG_SUMMARY = (
    "quarry-ridg (G6): quarry-hook never configures logging, so the "
    "post-web-search WARN with query_present/query_len/tool_response "
    "metadata is discarded and the operator cannot diagnose the skip."
)


_KNOWN_SHAPE_CASES: tuple[tuple[str, str, str], ...] = (
    # Each row: (payload_fixture, expected_query_substr, expected_doc_name_substr).
    (
        "websearch/pre_2026_05.json",
        "reciprocal rank fusion tuning",
        "search: reciprocal rank fusion tuning",
    ),
    (
        "websearch/2026_05_markdown.json",
        "onnxruntime coreml provider fallback",
        "search: onnxruntime coreml provider fallback",
    ),
)


def _hook_env(daemon: EphemeralDaemon) -> dict[str, str]:
    """Return env for a hook subprocess that talks to *daemon*."""
    return {
        "QUARRY_URL": f"http://{daemon.host}:{daemon.port}",
        "QUARRY_TOKEN": daemon.api_token,
        "QUARRY_ROOT": str(daemon.data_dir),
        "HOME": str(daemon.data_dir.parent),
    }


def _search_for_capture(
    daemon: EphemeralDaemon, name_substr: str, timeout_s: float
) -> dict[str, object] | None:
    """Poll the daemon HTTP API for a captured document whose name matches.

    Returns the first matching hit dict on success, or ``None`` when the
    timeout expires with no match.  Uses the ``/v1/search`` endpoint against
    the ``default-captures`` collection (the hook lands unregistered cwds
    there).
    """
    headers = {"Authorization": f"Bearer {daemon.api_token}"}
    deadline = time.monotonic() + timeout_s
    # Engine routes live under ``/v1`` (see ``quarry.daemon.route_table`` and
    # ``_API_PREFIX`` in ``quarry.client.client``); hitting ``/search`` without
    # the prefix returns 404 and the capture appears missing.
    endpoint = f"{daemon.base_url}/v1/search"
    while time.monotonic() < deadline:
        with httpx.Client(headers=headers, timeout=5.0) as client:
            try:
                resp = client.get(
                    endpoint,
                    params={
                        "q": name_substr,
                        "collection": "default-captures",
                        "limit": 20,
                    },
                )
            except httpx.HTTPError:
                time.sleep(0.5)
                continue
        if resp.status_code == 200:
            for hit in _hits(resp.json()):
                # ``SearchResult.to_dict`` emits ``document_name`` — no legacy
                # ``document`` key on the wire (see :mod:`quarry.results`).
                if name_substr in str(hit.get("document_name", "")):
                    return hit
        time.sleep(0.5)
    return None


def _hits(payload: object) -> list[dict[str, object]]:
    """Return the ``results`` list from a search response, if present."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results") or payload.get("hits") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


@pytest.mark.parametrize(
    ("payload_fixture", "query_substr", "doc_name_substr"),
    _KNOWN_SHAPE_CASES,
    ids=["pre_2026_05", "2026_05_markdown"],
)
def test_post_web_search_lands_in_captures(
    ephemeral_daemon: EphemeralDaemon,
    hook_subprocess: HookInvoker,
    payload_fixture: str,
    query_substr: str,
    doc_name_substr: str,
) -> None:
    """A known payload shape must produce a ``search: <query>`` capture end-to-end.

    Runs the real ``quarry-hook post-web-search`` binary against the real
    ephemeral quarryd, waits up to 15s for the capture to appear in the
    daemon's HTTP search, and asserts on the returned document name.  Fails
    today (v3.2.0) — no capture ever appears.
    """
    payload = load_payload(payload_fixture)
    run = hook_subprocess.run(
        "post-web-search",
        payload,
        env_overrides=_hook_env(ephemeral_daemon),
        timeout_s=20.0,
    )
    assert run.exit_code == 0, (
        f"quarry-hook exited {run.exit_code}; stderr[:400]={run.stderr[:400]!r}"
    )

    hit = _search_for_capture(ephemeral_daemon, doc_name_substr, timeout_s=15.0)
    assert hit is not None, (
        f"{_QUARRY_871U_SUMMARY}\n"
        f"  payload={payload_fixture!r} (query~{query_substr!r})\n"
        f"  expected document name to contain: {doc_name_substr!r}\n"
        f"  hook exit_code={run.exit_code}\n"
        f"  hook stderr[:200]={run.stderr[:200]!r}\n"
        f"  log lines written: {len(run.log_lines)}"
    )


def test_unknown_shape_logs_warn_with_metadata(
    ephemeral_daemon: EphemeralDaemon,
    hook_subprocess: HookInvoker,
) -> None:
    """An unknown ``tool_response`` shape must WARN with query_present/query_len.

    The shape-metadata WARN in ``HookAgent._warn_no_search_digest`` is the
    only signal an operator has that a WebSearch payload arrived but was
    rejected as unparseable.  Its persistence to quarry.log requires the
    hook entry point to have configured logging (quarry-ridg), so this
    case DOUBLE-FAILS today: the capture doesn't land (871u), and the
    WARN is silently dropped (ridg).
    """
    payload = load_payload("websearch/unknown_shape.json")
    run = hook_subprocess.run(
        "post-web-search",
        payload,
        env_overrides=_hook_env(ephemeral_daemon),
        timeout_s=20.0,
    )
    assert run.exit_code == 0, (
        f"quarry-hook exited {run.exit_code}; stderr[:400]={run.stderr[:400]!r}"
    )

    warn_lines = run.grep("post-web-search: no result digest in payload")
    assert warn_lines, (
        f"{_QUARRY_871U_SUMMARY}\n"
        f"{_QUARRY_RIDG_SUMMARY}\n"
        "  expected a WARN line containing 'query_present=', 'query_len=', and "
        "'tool_response type=' — the operator's only diagnostic that an "
        "unknown-shape payload arrived and was skipped.\n"
        f"  log_path={run.log_path}\n"
        f"  log_line_count={len(run.log_lines)}"
    )
    joined = "\n".join(warn_lines)
    assert "query_present=" in joined and "query_len=" in joined, (
        f"{_QUARRY_871U_SUMMARY}\n"
        "  WARN line missing the shape-metadata fields "
        "(query_present / query_len).\n"
        f"  lines={warn_lines}"
    )
