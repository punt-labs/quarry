"""Behaviour of the Loop 2 composer and sync, plus the f.3 equivalence guard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Self, final
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.api import RememberRequest
from quarry.client import (
    QuarryClient,
    QuarryConnectionError,
    QuarryError,
    TargetResolver,
)
from quarry.client.transport import Response
from quarry.mcp_missions import MissionTools
from quarry.mission_memory import MissionMemoryComposer, MissionMemorySync
from quarry.mission_records import MissionRound
from quarry.mission_store import MissionRecord, MissionScan, MissionStore
from quarry.mission_sync_types import SyncOptions
from tests.mission_fixtures import CLOSED_MISSION, Rounds, repo_with_missions

if TYPE_CHECKING:
    from collections.abc import Mapping

runner = CliRunner()

_EXPECTED_HEADER = (
    "# Mission m-2026-09-02-003 — round 1 (repo quarry, worker rmh, "
    "evaluator djb, created 2026-09-02T12:42:20Z)"
)


@final
class FakeDaemon:
    """A transport that answers ``/v1/show`` from a page table and records writes.

    ``pages`` maps a document name to its stored page-1 text; an unknown name
    is a 404 — the daemon's "not filed yet" answer. ``show_status`` overrides
    every show response (for the non-404 failure path). ``remember_errors``
    maps a document name to the error its remember raises (a 503, a dropped
    connection). Non-2xx statuses raise the same typed :class:`HttpError` the
    real transport classifies, so the client sees exactly what a live daemon
    would send it.
    """

    __slots__ = ("pages", "remember_errors", "remembered", "show_status")

    pages: dict[str, str]
    remembered: list[dict[str, object]]
    show_status: int
    remember_errors: dict[str, QuarryError]

    def __new__(
        cls,
        pages: dict[str, str] | None = None,
        *,
        show_status: int = 0,
        remember_errors: dict[str, QuarryError] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self.pages = dict(pages or {})
        self.remembered = []
        self.show_status = show_status
        self.remember_errors = dict(remember_errors or {})
        return self

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> Response:
        base = path.split("?")[0]
        if base == "/v1/show":
            if self.show_status:
                raise QuarryError.from_response(self.show_status, {"error": "boom"})
            name = (params or {}).get("document", "")
            if name not in self.pages:
                raise QuarryError.from_response(404, {"error": "not found"})
            return Response(
                200, {"document_name": name, "page_number": 1, "text": self.pages[name]}
            )
        if base == "/v1/remember":
            body = dict(json_body or {})
            if (error := self.remember_errors.get(str(body.get("name")))) is not None:
                raise error
            self.remembered.append(body)
            return Response(202, {"task_id": "t", "status": "accepted"})
        raise AssertionError(f"unexpected {method} {base}")

    def client(self) -> QuarryClient:
        return QuarryClient(self)


@final
class DownDaemon:
    """A transport whose every request is a refused connection."""

    __slots__ = ()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> Response:
        raise QuarryConnectionError(
            "Cannot connect to remote quarry server at http://127.0.0.1:8420",
            "http://127.0.0.1:8420",
        )


class TestComposer:
    def test_document_name_carries_repo_id_and_round(self) -> None:
        name = MissionMemoryComposer.document_name(Rounds.contract(), Rounds.full())
        assert name == "mission-quarry-m-2026-09-02-003-r1"

    def test_header_is_the_identity_key(self) -> None:
        assert MissionMemoryComposer.header(Rounds.contract(), Rounds.full()) == (
            _EXPECTED_HEADER
        )

    def test_compose_golden(self) -> None:
        req = MissionMemoryComposer.compose(Rounds.contract(), Rounds.full())
        assert req.name == "mission-quarry-m-2026-09-02-003-r1"
        assert req.agent_handle == "rmh"
        assert req.collection == ""  # the daemon routes handle -> memory-rmh
        assert req.memory_type == "observation"
        assert req.overwrite is True
        assert req.format_hint == "markdown"
        first_signal = Rounds.reflection().signals[0]
        assert len(first_signal) > 100  # the fixture exercises the cut
        assert req.summary == (
            f"m-2026-09-02-003 r1 (quarry): worker pass/continue — {first_signal[:100]}"
        )

    def test_document_sections_in_order(self) -> None:
        doc = MissionMemoryComposer.document(Rounds.contract(), Rounds.full())
        lines = doc.splitlines()
        assert lines[0] == _EXPECTED_HEADER
        assert lines[2] == "Worker verdict (self-assessed): pass, confidence 0.90"
        assert lines[3] == (
            "Evaluator reflection: continue (converging: true), authored by claude"
        )
        order = [
            "## Reflection signals",
            "## Recommendation",
            "## Open questions (worker)",
            "## Worker report",
        ]
        positions = [doc.index(section) for section in order]
        assert positions == sorted(positions)
        assert "- evaluator djb: major — schedule_tree aborts the whole tree" in doc
        assert "continue — Round 1's DRY consolidation stands." in doc
        assert doc.rstrip().endswith("Round 1: IgnoreRules extracted.")

    def test_reflectionless_final_round_of_a_closed_mission(self) -> None:
        contract = Rounds.contract(status="closed", current_round=2)
        round_ = MissionRound(2, Rounds.result(2), None)
        doc = MissionMemoryComposer.document(contract, round_)
        assert "Evaluator reflection: none — closed closed at round 2" in doc
        assert "## Reflection signals" not in doc
        assert "## Recommendation" not in doc
        assert MissionMemoryComposer.summary(contract, round_) == (
            "m-2026-09-02-003 r2 (quarry): worker pass/closed"
        )

    def test_resultless_round(self) -> None:
        round_ = MissionRound(1, None, Rounds.reflection(1))
        doc = MissionMemoryComposer.document(Rounds.contract(), round_)
        assert "Worker verdict: none — no result submitted" in doc
        assert "## Worker report" not in doc
        assert MissionMemoryComposer.summary(Rounds.contract(), round_).startswith(
            "m-2026-09-02-003 r1 (quarry): worker none/continue — "
        )

    def test_no_open_questions_omits_the_section(self) -> None:
        round_ = MissionRound(
            1, Rounds.result(1, open_questions=[]), Rounds.reflection(1)
        )
        assert "## Open questions" not in MissionMemoryComposer.document(
            Rounds.contract(), round_
        )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repo_with_missions(tmp_path / "quarry")


def _scan(repo: Path, mission_id: str = "") -> MissionScan:
    store = MissionStore.for_repo(repo)
    assert store is not None
    return store.scan(mission_id)


def _closed_round_names(repo: Path) -> tuple[str, str]:
    """Return the document names of the closed fixture mission's two rounds."""
    first, second = (
        req.name for _h, req in MissionMemorySync.requests(_scan(repo, CLOSED_MISSION))
    )
    return first, second


class TestRequests:
    def test_frozen_non_empty_rounds_only_in_order(self, repo: Path) -> None:
        store = MissionStore.for_repo(repo)
        assert store is not None
        names = [req.name for _h, req in MissionMemorySync.requests(store.scan())]
        assert names == [
            "mission-quarry-m-2026-09-02-003-r1",
            "mission-quarry-m-2026-09-09-001-r1",
            "mission-quarry-m-2026-09-09-001-r2",
        ]

    def test_empty_round_is_skipped_with_an_info_line(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        record = MissionRecord(
            Rounds.contract(status="closed"), (MissionRound(1, None, None),)
        )
        with caplog.at_level("INFO", logger="quarry.mission_memory"):
            assert MissionMemorySync.requests(MissionScan((record,), ())) == []
        assert any(
            "neither result nor reflection" in r.getMessage() for r in caplog.records
        )


class TestRun:
    @staticmethod
    def _sync(
        daemon: FakeDaemon, *, force: bool = False, dry_run: bool = False
    ) -> MissionMemorySync:
        return MissionMemorySync(
            daemon.client(), SyncOptions(force=force, dry_run=dry_run)
        )

    def test_files_every_unfiled_round(self, repo: Path) -> None:
        daemon = FakeDaemon()
        outcome = self._sync(daemon).run(_scan(repo))
        assert len(outcome.filed) == 3
        assert outcome.skipped == ()
        assert outcome.errors == ()
        assert [r["name"] for r in daemon.remembered] == list(outcome.filed)
        assert {r["agent_handle"] for r in daemon.remembered} == {"rmh", "gvr"}

    def test_already_filed_round_is_skipped(self, repo: Path) -> None:
        store = MissionStore.for_repo(repo)
        assert store is not None
        header, req = MissionMemorySync.requests(store.scan(CLOSED_MISSION))[0]
        daemon = FakeDaemon({req.name: f"{header}\n\nWorker verdict ..."})
        outcome = self._sync(daemon).run(store.scan(CLOSED_MISSION))
        assert outcome.skipped == (req.name,)
        assert req.name not in [r["name"] for r in daemon.remembered]

    def test_force_refiles_a_matching_key(self, repo: Path) -> None:
        store = MissionStore.for_repo(repo)
        assert store is not None
        header, req = MissionMemorySync.requests(store.scan(CLOSED_MISSION))[0]
        daemon = FakeDaemon({req.name: header})
        outcome = self._sync(daemon, force=True).run(store.scan(CLOSED_MISSION))
        assert req.name in outcome.filed
        assert daemon.remembered[0]["overwrite"] is True

    def test_mission_name_collision_is_error_not_overwrite(self, repo: Path) -> None:
        """A name another checkout's round holds is never replaced, even by --force."""
        store = MissionStore.for_repo(repo)
        assert store is not None
        _header, req = MissionMemorySync.requests(store.scan(CLOSED_MISSION))[0]
        other = (
            "# Mission m-2026-09-09-001 — round 1 (repo quarry, worker kpz, created …)"
        )
        daemon = FakeDaemon({req.name: other})
        outcome = self._sync(daemon, force=True).run(store.scan(CLOSED_MISSION))
        assert req.name not in outcome.filed
        assert any(e.startswith(f"name collision: {req.name}") for e in outcome.errors)
        assert req.name not in [r["name"] for r in daemon.remembered]

    def test_dry_run_posts_nothing(self, repo: Path) -> None:
        daemon = FakeDaemon()
        outcome = self._sync(daemon, dry_run=True).run(_scan(repo))
        assert len(outcome.filed) == 3
        assert outcome.dry_run is True
        assert daemon.remembered == []

    def test_non_404_show_failure_is_an_error_not_a_skip(self, repo: Path) -> None:
        daemon = FakeDaemon(show_status=500)
        outcome = self._sync(daemon).run(_scan(repo, CLOSED_MISSION))
        assert outcome.filed == ()
        assert outcome.skipped == ()
        assert all("HTTP 500" in e for e in outcome.errors)
        assert len(outcome.errors) == 2
        assert daemon.remembered == []

    def test_connection_error_on_remember_is_recorded_and_the_run_continues(
        self, repo: Path
    ) -> None:
        """Design f.1: a QuarryConnectionError on remember → recorded, run continues."""
        first, second = _closed_round_names(repo)
        dropped = QuarryConnectionError(
            "Cannot connect to remote quarry server at http://127.0.0.1:8420",
            "http://127.0.0.1:8420",
        )
        daemon = FakeDaemon(remember_errors={first: dropped})
        outcome = self._sync(daemon).run(_scan(repo, CLOSED_MISSION))
        assert outcome.filed == (second,)
        assert outcome.errors == (f"{first}: {dropped.message}",)
        assert [r["name"] for r in daemon.remembered] == [second]

    def test_http_failure_on_a_later_remember_keeps_the_earlier_tally(
        self, repo: Path
    ) -> None:
        """A 503 on the Nth round must not discard the rounds filed before it."""
        first, second = _closed_round_names(repo)
        queue_full = QuarryError.from_response(503, {"error": "queue full"})
        daemon = FakeDaemon(remember_errors={second: queue_full})
        outcome = self._sync(daemon).run(_scan(repo, CLOSED_MISSION))
        assert outcome.filed == (first,)
        assert outcome.errors == (f"{second}: daemon returned HTTP 503: queue full",)
        assert [r["name"] for r in daemon.remembered] == [first]

    def test_daemon_down_on_the_existence_check_is_one_error_per_round(
        self, repo: Path
    ) -> None:
        sync = MissionMemorySync(QuarryClient(DownDaemon()), SyncOptions())
        outcome = sync.run(_scan(repo, CLOSED_MISSION))
        assert outcome.filed == ()
        assert outcome.skipped == ()
        assert len(outcome.errors) == 2
        assert all("Cannot connect" in e for e in outcome.errors)

    def test_scan_errors_are_carried_into_the_outcome(self, repo: Path) -> None:
        bad = repo / ".punt-labs" / "ethos" / "missions" / "m-2026-09-30-001"
        bad.mkdir()
        (bad / "contract.yaml").write_text("mission_id: [oops\n")
        outcome = self._sync(FakeDaemon()).run(_scan(repo))
        assert len(outcome.filed) == 3
        assert any("m-2026-09-30-001" in e for e in outcome.errors)

    def test_for_repo_without_a_tree_is_empty(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        (bare / ".git").mkdir()
        outcome = MissionMemorySync.for_repo(bare, FakeDaemon().client(), SyncOptions())
        assert outcome.filed == ()
        assert outcome.errors == ()


class TestIdentityCheckRoundTrip:
    """The stored first line survives the daemon's chunking and scrubbing."""

    def test_header_read_back_from_a_real_daemon_matches(self, tmp_path: Path) -> None:
        from tests.inproc_daemon import InProcessDaemon

        contract, round_ = Rounds.contract(), Rounds.full()
        request = MissionMemoryComposer.compose(contract, round_)
        daemon = InProcessDaemon(tmp_path / "daemon")
        with daemon.client() as client:
            outcome = client.await_task(client.remember(request).task_id)
            assert outcome.is_completed, outcome
            sync = MissionMemorySync(client, SyncOptions())
            assert sync._existing_header(request.name) == (
                MissionMemoryComposer.header(contract, round_)
            )
            assert sync._existing_header("mission-quarry-m-0-r9") is None


class TestFourSurfaceParity:
    def test_cli_and_mcp_produce_identical_requests(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI verb and the MCP tool post the same ordered RememberRequests."""
        monkeypatch.chdir(repo)
        via_cli = FakeDaemon()
        with patch.object(TargetResolver, "connect", return_value=via_cli.client()):
            result = runner.invoke(app, ["--json", "missions", "sync"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["filed"]

        via_mcp = FakeDaemon()
        text = MissionTools(connect=via_mcp.client).missions_sync()
        assert text.startswith("▶  Mission memories: filed 3")

        cli_requests = [RememberRequest.model_validate(b) for b in via_cli.remembered]
        mcp_requests = [RememberRequest.model_validate(b) for b in via_mcp.remembered]
        assert cli_requests == mcp_requests
        assert len(cli_requests) == 3
