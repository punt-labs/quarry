"""Facade tests for CaptureSync: the commit-time gate, abort-before-commit, and
the never-push-on-abort invariants (bug class 2), plus visibility enforcement
(bug class 4) and offline resumability (bug class 5)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quarry.shadow.config import ShadowConfig
from quarry.shadow.sync import CaptureSync, ShadowSyncResult
from quarry.shadow.visibility import Visibility


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

    def test_restage_failure_reports_rescrubbed_count(self) -> None:
        # The re-stage-failure abort must carry the real re-scrub count (the
        # re-scrub already ran), not a placeholder zero.
        sync, repo, rescrubber = _sync()
        rescrubber.rescrub_all.return_value = 2
        repo.stage.side_effect = [True, False]  # initial stage ok, re-stage fails
        result = sync.run(fail_open=True)
        assert result.aborted_reason == "stage-failed"
        assert result.rescrubbed == 2

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

    def test_unknown_visibility_warning_names_real_override(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The refusal must point users at the real override — the config key
        # shadow.acknowledge_unverified — not a nonexistent --force flag.
        sync, repo, _ = _sync(config=_config(ack=False))
        repo.remote_visibility.return_value = Visibility.UNKNOWN
        with caplog.at_level("WARNING"):
            sync.run(fail_open=True)
        assert "shadow.acknowledge_unverified" in caplog.text
        assert "--force" not in caplog.text

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

    def test_expected_io_error_aborts_fail_open(self) -> None:
        # An expected filesystem failure (OSError from the working-tree stage)
        # must abort fail-open, not propagate: a shadow problem never blocks a
        # session when fail_open=True.
        sync, repo, _ = _sync()
        repo.bootstrap.side_effect = OSError("mkdir: read-only filesystem")
        result = sync.run(fail_open=True)
        assert result.aborted_reason == "exception"
        assert result.pushed is False
        assert result.committed is False

    def test_corrupt_capture_decode_aborts_fail_open(self) -> None:
        # A capture file with invalid UTF-8 bytes (a crash-truncated multibyte
        # write or external tampering) makes rescrub's read_text raise
        # UnicodeDecodeError. That untrusted-input fault must abort fail-open,
        # not propagate and block the session/`quarry sync`.
        sync, _, rescrubber = _sync()
        rescrubber.rescrub_all.side_effect = UnicodeDecodeError(
            "utf-8", b"\xff", 0, 1, "invalid start byte"
        )
        result = sync.run(fail_open=True)
        assert result.aborted_reason == "exception"
        assert result.pushed is False
        assert result.committed is False

    def test_unexpected_exception_propagates_fail_open(self) -> None:
        # A truly unexpected exception (a programming error) is NOT an expected
        # shadow failure: it surfaces even under fail_open rather than being
        # swallowed as an abort, so bugs fail fast instead of hiding.
        sync, repo, _ = _sync()
        repo.bootstrap.side_effect = ValueError("programming error")
        with pytest.raises(ValueError):
            sync.run(fail_open=True)


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


class TestUnpushedDetail:
    """unpushed_detail keys off committed first; aborted_reason labels the
    not-committed case only."""

    def test_aborted_reason_when_not_committed(self) -> None:
        # No commit + a non-empty aborted_reason: the gate refused, the shadow
        # was disabled, or an exception aborted the run before committing.
        result = ShadowSyncResult.aborted("bootstrap-refused")
        assert result.unpushed_detail() == "aborted before commit (bootstrap-refused)"

    def test_committed_wins_even_with_aborted_reason(self) -> None:
        # The visibility gate can abort AFTER a local commit; that run DID
        # commit, so it must read "committed but not pushed", carrying the
        # reason as the cause — never "aborted before commit".
        result = ShadowSyncResult.aborted("public-remote", committed=True)
        detail = result.unpushed_detail()
        assert detail == "committed but not pushed (public-remote)"

    def test_committed_but_push_failed(self) -> None:
        result = ShadowSyncResult(
            pushed=False,
            committed=True,
            rescrubbed=0,
            aborted_reason="",
            race_failures=(),
        )
        assert result.unpushed_detail() == "committed but not pushed (push failed)"

    def test_not_committed_not_aborted_is_not_before_commit(self) -> None:
        # Nothing to commit / deferred offline push: NOT an abort.
        result = ShadowSyncResult(
            pushed=False,
            committed=False,
            rescrubbed=0,
            aborted_reason="",
            race_failures=(),
        )
        detail = result.unpushed_detail()
        assert "aborted before commit" not in detail
        assert detail == "captures not pushed; run `quarry doctor` for shadow state"


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
