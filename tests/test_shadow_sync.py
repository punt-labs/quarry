"""Facade tests for CaptureSync: the commit-time gate, abort-before-commit, and
the never-push-on-abort invariants (bug class 2), plus visibility enforcement
(bug class 4) and offline resumability (bug class 5)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quarry.shadow.config import ShadowConfig
from quarry.shadow.repo import Visibility
from quarry.shadow.sync import CaptureSync, ShadowSyncResult


def _config(*, enabled: bool = True, ack: bool = False) -> ShadowConfig:
    return ShadowConfig(
        enabled=enabled, remote="git@h:o/r.git", acknowledge_unverified=ack
    )


def _sync(
    *,
    config: ShadowConfig | None = None,
    repo: MagicMock | None = None,
    rescrubber: MagicMock | None = None,
) -> tuple[CaptureSync, MagicMock, MagicMock]:
    """Build a CaptureSync over mock collaborators. Return (sync, repo, rescrubber)."""
    repo = repo or MagicMock(name="repo")
    rescrubber = rescrubber or MagicMock(name="rescrubber")
    repo.bootstrap.return_value = True
    repo.stage.return_value = True
    repo.staged_captures.return_value = {}
    repo.commit.return_value = True
    repo.push.return_value = True
    repo.remote_visibility.return_value = Visibility.PRIVATE
    rescrubber.rescrub_all.return_value = 0
    rescrubber.verify_staged_clean.return_value = []
    sync = CaptureSync(Path("/proj"), config or _config(), repo, rescrubber)
    return sync, repo, rescrubber


class TestCommitTimeGateOrder:
    def test_rescrub_runs_before_commit(self) -> None:
        parent = MagicMock()
        sync, _, _ = _sync(repo=parent.repo, rescrubber=parent.rescrubber)
        sync.run(fail_open=True)
        order = [c[0] for c in parent.mock_calls if "." in c[0]]
        # Strip the "repo." / "rescrubber." prefix for a flat op sequence.
        ops = [name.split(".", 1)[1] for name in order]
        assert ops.index("stage") < ops.index("rescrub_all")
        assert ops.index("rescrub_all") < ops.index("verify_staged_clean")
        assert ops.index("verify_staged_clean") < ops.index("commit")
        # stage is called twice (before and after re-scrub).
        assert ops.count("stage") == 2

    def test_no_commit_before_rescrub(self) -> None:
        parent = MagicMock()
        sync, _, _ = _sync(repo=parent.repo, rescrubber=parent.rescrubber)
        sync.run(fail_open=True)
        calls = [c[0] for c in parent.mock_calls]
        first_commit = calls.index("repo.commit")
        first_rescrub = calls.index("rescrubber.rescrub_all")
        assert first_rescrub < first_commit


class TestAbortBeforeCommit:
    def test_race_guard_aborts_before_commit(self) -> None:
        sync, repo, rescrubber = _sync()
        rescrubber.verify_staged_clean.return_value = [Path("session-abc.md")]
        result = sync.run(fail_open=True)
        assert result.committed is False
        assert result.pushed is False
        assert result.aborted_reason == "race-guard"
        assert result.race_failures == (Path("session-abc.md"),)
        repo.commit.assert_not_called()
        repo.push.assert_not_called()

    def test_restage_failure_aborts_before_commit(self) -> None:
        # A silent re-stage failure (index.lock race) must abort: the index may
        # still hold pre-rescrub blobs, so committing would ship unscrubbed data.
        sync, repo, rescrubber = _sync()
        repo.stage.side_effect = [True, False]  # initial stage ok, re-stage fails
        result = sync.run(fail_open=True)
        assert result.aborted_reason == "stage-failed"
        assert result.committed is False
        assert result.pushed is False
        repo.commit.assert_not_called()
        repo.push.assert_not_called()
        # rescrub ran (between the two stages) but the staged verify never did.
        rescrubber.verify_staged_clean.assert_not_called()

    def test_rescrub_raises_no_commit_no_push(self) -> None:
        sync, repo, rescrubber = _sync()
        rescrubber.rescrub_all.side_effect = OSError("disk full")
        result = sync.run(fail_open=True)
        assert result.pushed is False
        repo.commit.assert_not_called()
        repo.push.assert_not_called()

    def test_verify_guard_raises_no_commit_no_push(self) -> None:
        sync, repo, rescrubber = _sync()
        rescrubber.verify_staged_clean.side_effect = OSError("stat failed")
        result = sync.run(fail_open=True)
        assert result.pushed is False
        repo.commit.assert_not_called()
        repo.push.assert_not_called()

    def test_push_never_called_when_rescrub_raises(self) -> None:
        sync, repo, rescrubber = _sync()
        rescrubber.rescrub_all.side_effect = RuntimeError("boom")
        sync.run(fail_open=True)
        repo.push.assert_not_called()

    def test_bootstrap_refusal_aborts(self) -> None:
        sync, repo, rescrubber = _sync()
        repo.bootstrap.return_value = False
        result = sync.run(fail_open=True)
        assert result.aborted_reason == "bootstrap-refused"
        assert result.pushed is False
        rescrubber.rescrub_all.assert_not_called()
        repo.commit.assert_not_called()


class TestVisibilityGate:
    def test_public_remote_refused(self) -> None:
        sync, repo, _ = _sync()
        repo.remote_visibility.return_value = Visibility.PUBLIC
        result = sync.run(fail_open=True)
        assert result.aborted_reason == "public-remote"
        assert result.pushed is False
        repo.push.assert_not_called()

    def test_unknown_visibility_requires_ack(self) -> None:
        sync, repo, _ = _sync(config=_config(ack=False))
        repo.remote_visibility.return_value = Visibility.UNKNOWN
        result = sync.run(fail_open=True)
        assert result.aborted_reason == "unverified-visibility"
        repo.push.assert_not_called()

    def test_unknown_visibility_with_ack_pushes(self) -> None:
        sync, repo, _ = _sync(config=_config(ack=True))
        repo.remote_visibility.return_value = Visibility.UNKNOWN
        result = sync.run(fail_open=True)
        assert result.pushed is True
        assert result.aborted_reason == ""
        repo.push.assert_called_once()

    def test_visibility_gate_runs_after_commit(self) -> None:
        # A commit is local-only and safe; the gate must still block the push.
        sync, repo, _ = _sync()
        repo.remote_visibility.return_value = Visibility.PUBLIC
        result = sync.run(fail_open=True)
        assert result.committed is True
        assert result.pushed is False


class TestFailOpen:
    def test_git_error_returns_result(self) -> None:
        sync, repo, _ = _sync()
        repo.commit.side_effect = RuntimeError("git segfault")
        result = sync.run(fail_open=True)
        assert isinstance(result, ShadowSyncResult)
        assert result.pushed is False

    def test_disabled_config_aborts(self) -> None:
        sync, repo, _ = _sync(config=_config(enabled=False))
        result = sync.run(fail_open=True)
        assert result.aborted_reason == "disabled"
        repo.bootstrap.assert_not_called()

    def test_fail_closed_reraises(self) -> None:
        sync, _, rescrubber = _sync()
        rescrubber.rescrub_all.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            sync.run(fail_open=False)


class TestOfflineRetry:
    def test_push_failure_then_success(self) -> None:
        sync, repo, _ = _sync()
        # First run: commit succeeds locally but push fails (offline).
        repo.push.return_value = False
        first = sync.run(fail_open=True)
        assert first.committed is True
        assert first.pushed is False
        # Second run: push now succeeds, sending the accumulated commit.
        repo.push.return_value = True
        second = sync.run(fail_open=True)
        assert second.pushed is True


class TestResultSerialization:
    def test_to_dict_field_names_stable(self) -> None:
        result = ShadowSyncResult.aborted("race-guard", races=(Path("session-x.md"),))
        data = result.to_dict()
        assert set(data) == {
            "pushed",
            "committed",
            "rescrubbed",
            "aborted_reason",
            "race_failures",
        }
        assert data["race_failures"] == ["session-x.md"]
