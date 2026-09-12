"""Tests for the repo CLAUDE.md enable/disable orchestrator."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Self, final

import pytest

from quarry.api import (
    DeleteCollectionRequest,
    DeregisterAccepted,
    DeregisterRequest,
    RegistrationList,
    TaskAccepted,
)
from quarry.claude_import import ClaudeMdImport
from quarry.enable import disable_project
from quarry.enabled_marker import EnabledMarker
from quarry.enablement import Enablement
from quarry.enablement_result import DisablementResult
from quarry.file_lock import FileLock
from quarry.gitignore import CAPTURES_GITIGNORE_ENTRY, QuarryGitignore
from quarry.guidance import REPO_IMPORT_LINE
from tests.conftest import FakeRegistryClient


def test_enable_writes_guide_marker_and_import(tmp_path: Path) -> None:
    result = Enablement(tmp_path).enable()
    assert result.guide_deposited is True
    assert result.enabled_marker_written is True
    assert result.import_registered is True
    assert EnabledMarker(tmp_path).is_present()
    assert REPO_IMPORT_LINE in (tmp_path / "CLAUDE.md").read_text()
    assert (tmp_path / ".punt-labs" / "quarry" / "CLAUDE.md").exists()


def test_enable_ensures_captures_gitignore_entry(tmp_path: Path) -> None:
    result = Enablement(tmp_path).enable()
    assert result.gitignore_ensured is True
    assert CAPTURES_GITIGNORE_ENTRY in (tmp_path / ".gitignore").read_text()


def test_enable_gitignore_is_idempotent(tmp_path: Path) -> None:
    first = Enablement(tmp_path).enable()
    second = Enablement(tmp_path).enable()
    assert first.gitignore_ensured is True
    assert second.gitignore_ensured is False
    content = (tmp_path / ".gitignore").read_text()
    assert content.count(CAPTURES_GITIGNORE_ENTRY) == 1


def test_enable_backfills_gitignore_on_already_enabled_repo(tmp_path: Path) -> None:
    """A repo enabled before this step existed gets the line backfilled, not skipped."""
    Enablement(tmp_path).enable()
    (tmp_path / ".gitignore").unlink()  # simulate: enabled, but no gitignore entry yet

    result = Enablement(tmp_path).enable()

    assert result.gitignore_ensured is True
    assert CAPTURES_GITIGNORE_ENTRY in (tmp_path / ".gitignore").read_text()


def test_enable_gitignore_survives_disable(tmp_path: Path) -> None:
    """disable() is additive-only for .gitignore — it never prunes the entry."""
    Enablement(tmp_path).enable()
    Enablement(tmp_path).disable()
    assert CAPTURES_GITIGNORE_ENTRY in (tmp_path / ".gitignore").read_text()
    assert QuarryGitignore(tmp_path).ensure() is False


def test_enable_excludes_the_claude_md_lock_file(tmp_path: Path) -> None:
    """enable() must not leave .CLAUDE.md.lock unignored (Bugbot MEDIUM finding).

    FileLock creates .CLAUDE.md.lock and never removes it (see FileLock's
    docstring); without a matching .gitignore entry, every enable() leaves a
    machine-local artifact a bare ``git add -A`` could commit.
    """
    from quarry.file_lock import FILE_LOCK_GITIGNORE_GLOB

    Enablement(tmp_path).enable()

    assert (tmp_path / ".CLAUDE.md.lock").exists()
    ignore_lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert FILE_LOCK_GITIGNORE_GLOB in ignore_lines


def test_enable_ensures_gitignore_before_guide_deposit_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .gitignore exclusion must land even when a later enable step fails.

    A repo that never gets past guide deposit is still protected: the
    vulnerability window (unprotected capture writing) never opens, even
    though enable() itself did not complete.
    """
    from quarry.guidance import Guidance

    def boom(self: Guidance) -> None:
        raise OSError("deposit failed")

    monkeypatch.setattr(Guidance, "deposit", boom)

    with pytest.raises(OSError, match="deposit failed"):
        Enablement(tmp_path).enable()

    assert CAPTURES_GITIGNORE_ENTRY in (tmp_path / ".gitignore").read_text()
    assert not EnabledMarker(tmp_path).is_present()


def test_enable_biconditional_marker_iff_import(tmp_path: Path) -> None:
    """§2.11: after enable, marker present AND import present, together."""
    Enablement(tmp_path).enable()
    marker_present = EnabledMarker(tmp_path).is_present()
    import_present = REPO_IMPORT_LINE in (tmp_path / "CLAUDE.md").read_text()
    assert marker_present and import_present


def test_enable_is_idempotent(tmp_path: Path) -> None:
    first = Enablement(tmp_path).enable()
    second = Enablement(tmp_path).enable()
    assert first.import_registered is True
    assert first.enabled_marker_written is True
    assert second.import_registered is False
    assert second.enabled_marker_written is False
    assert (tmp_path / "CLAUDE.md").read_text().count(REPO_IMPORT_LINE) == 1


def test_enable_leaves_no_marker_when_register_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.11: a register() failure leaves neither marker nor import behind."""

    def boom(self: ClaudeMdImport, import_line: str) -> bool:
        raise OSError("register failed")

    monkeypatch.setattr(ClaudeMdImport, "register", boom)

    with pytest.raises(OSError, match="register failed"):
        Enablement(tmp_path).enable()

    assert not EnabledMarker(tmp_path).is_present()


def test_enable_leaves_no_marker_when_host_ends_in_open_fence(tmp_path: Path) -> None:
    """§2.11: enabling a CLAUDE.md that ends in an unterminated fence fails closed.

    The import would land inside the open fence — inert — so register raises and
    the marker is never written, leaving neither an inert import nor a marker
    that would falsely advertise enablement.
    """
    (tmp_path / "CLAUDE.md").write_text("# rules\n\n```\nnever closed\n")

    with pytest.raises(ValueError, match="unterminated code fence"):
        Enablement(tmp_path).enable()

    assert not EnabledMarker(tmp_path).is_present()
    assert REPO_IMPORT_LINE not in (tmp_path / "CLAUDE.md").read_text()


def test_disable_prunes_import_and_marker_leaves_guide(tmp_path: Path) -> None:
    Enablement(tmp_path).enable()
    result = Enablement(tmp_path).disable()
    assert result.import_pruned is True
    assert result.enabled_marker_removed is True
    assert not EnabledMarker(tmp_path).is_present()
    assert REPO_IMPORT_LINE not in (tmp_path / "CLAUDE.md").read_text()
    # §2.9: vendored guide stays dormant.
    assert (tmp_path / ".punt-labs" / "quarry" / "CLAUDE.md").exists()


def test_disable_biconditional_marker_iff_import(tmp_path: Path) -> None:
    Enablement(tmp_path).enable()
    Enablement(tmp_path).disable()
    marker_present = EnabledMarker(tmp_path).is_present()
    import_present = REPO_IMPORT_LINE in (tmp_path / "CLAUDE.md").read_text()
    assert not marker_present and not import_present


def test_disable_removes_marker_before_prune_can_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.11: a prune() failure during disable leaves marker-absent, never present."""
    Enablement(tmp_path).enable()

    def boom(self: ClaudeMdImport, import_line: str) -> bool:
        raise OSError("prune failed")

    monkeypatch.setattr(ClaudeMdImport, "prune", boom)

    with pytest.raises(OSError, match="prune failed"):
        Enablement(tmp_path).disable()

    assert not EnabledMarker(tmp_path).is_present()


def test_disable_symlinked_ancestor_does_not_strand_import(tmp_path: Path) -> None:
    """A SafeRepoPath marker refusal during disable must still prune the import.

    A hostile symlinked .punt-labs ancestor makes marker.remove() refuse. The
    prune must still run so a prior deregister does not leave the @-import
    lingering; the refused marker is not a real in-repo marker, and the external
    symlink target is untouched.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text(f"# rules\n{REPO_IMPORT_LINE}\n")
    external = tmp_path / "external"
    external.mkdir()
    (repo / ".punt-labs").symlink_to(external)

    result = Enablement(repo).disable()

    assert result.import_pruned is True
    assert result.enabled_marker_removed is False
    assert REPO_IMPORT_LINE not in (repo / "CLAUDE.md").read_text()
    assert list(external.iterdir()) == []  # no external effect


def test_disable_is_idempotent(tmp_path: Path) -> None:
    Enablement(tmp_path).enable()
    Enablement(tmp_path).disable()
    second = Enablement(tmp_path).disable()
    assert second.import_pruned is False
    assert second.enabled_marker_removed is False


# ── concurrency: enable/disable are atomic, never stranding the marker ─


def _churn_enable_disable(dir_str: str, iterations: int) -> None:
    enablement = Enablement(Path(dir_str))
    for _ in range(iterations):
        enablement.enable()
        enablement.disable()


def _sample_invariant(
    dir_str: str, samples: int, out: multiprocessing.Queue[int]
) -> None:
    root = Path(dir_str)
    claude = root / "CLAUDE.md"
    marker = EnabledMarker(root)
    violations = 0
    for _ in range(samples):
        # Sample marker and import together UNDER the lock, so no enable/disable
        # is mid-flight: the snapshot is a committed state, never a torn one.
        with FileLock(claude):
            marker_present = marker.is_present()
            text = claude.read_text() if claude.exists() else ""
        if marker_present and REPO_IMPORT_LINE not in text:
            violations += 1
    out.put(violations)


def test_concurrent_enable_disable_never_strands_marker(tmp_path: Path) -> None:
    """§2.11 under concurrency: no observer ever sees marker-present + import-absent.

    Churners hammer enable/disable while a sampler reads both signals under the
    shared FileLock. Because enable (register+marker) and disable (marker+prune)
    each commit atomically under that lock, a locked observer only ever sees a
    consistent state. Without the marker inside the lock, a churner's marker
    write could land between another's prune and this read — the forbidden state.
    """
    ctx = multiprocessing.get_context("spawn")
    out: multiprocessing.Queue[int] = ctx.Queue()
    churners = [
        ctx.Process(target=_churn_enable_disable, args=(str(tmp_path), 50))
        for _ in range(3)
    ]
    sampler = ctx.Process(target=_sample_invariant, args=(str(tmp_path), 500, out))
    for p in churners:
        p.start()
    sampler.start()
    for p in churners:
        p.join(timeout=60)
    sampler.join(timeout=60)
    for p in (*churners, sampler):
        if p.is_alive():
            p.terminate()
        assert p.exitcode == 0, "a child did not finish cleanly"
    violations = out.get(timeout=5)
    assert violations == 0, f"marker-present + import-absent observed {violations}x"


# ── disable_project step ordering: § 2.11 commit point before deregister ────


@final
class _OrderingRecorder:
    """Record the ordinal at which each disable step fires.

    The regression the design pins is that ``Enablement.disable`` MUST run
    before ``client.deregister``. Instrumenting both call sites through one
    monotonically increasing counter is the smallest instrument that will
    fail if either half of the ordering regresses.
    """

    __slots__ = ("_calls", "_next")

    _calls: list[str]
    _next: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._calls = []
        self._next = 0
        return self

    @property
    def calls(self) -> list[str]:
        """Return the step names recorded so far, in call order."""
        return self._calls

    def record(self, name: str) -> int:
        self._next += 1
        self._calls.append(name)
        return self._next


class TestDisableProjectOrdering:
    def _prepare_repo(self, tmp_path: Path) -> Path:
        project = tmp_path / "proj"
        project.mkdir()
        (project / "CLAUDE.md").write_text(f"# rules\n{REPO_IMPORT_LINE}\n")
        EnabledMarker(project).write()
        return project

    def _instrument(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorder: _OrderingRecorder,
        client: FakeRegistryClient,
        *,
        disable_raises: Exception | None = None,
    ) -> None:
        real_disable = Enablement.disable

        def spy_disable(self: Enablement) -> DisablementResult:
            recorder.record("Enablement.disable")
            if disable_raises is not None:
                raise disable_raises
            return real_disable(self)

        real_list = FakeRegistryClient.list_registrations
        real_deregister = FakeRegistryClient.deregister
        real_delete = FakeRegistryClient.delete_collection

        def spy_list(self: FakeRegistryClient) -> RegistrationList:
            recorder.record("list_registrations")
            return real_list(self)

        def spy_deregister(
            self: FakeRegistryClient, req: DeregisterRequest
        ) -> DeregisterAccepted:
            recorder.record("deregister")
            return real_deregister(self, req)

        def spy_delete(
            self: FakeRegistryClient, req: DeleteCollectionRequest
        ) -> TaskAccepted:
            recorder.record("delete_collection")
            return real_delete(self, req)

        monkeypatch.setattr(Enablement, "disable", spy_disable)
        monkeypatch.setattr(FakeRegistryClient, "list_registrations", spy_list)
        monkeypatch.setattr(FakeRegistryClient, "deregister", spy_deregister)
        monkeypatch.setattr(FakeRegistryClient, "delete_collection", spy_delete)
        _ = client  # instrumentation lives on the class, not the instance

    def test_deregister_runs_after_marker_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§ 4: list → Enablement.disable → deregister → delete_collection."""
        project = self._prepare_repo(tmp_path)
        client = FakeRegistryClient([("proj", project)])
        recorder = _OrderingRecorder()
        self._instrument(monkeypatch, recorder, client)

        result = disable_project(project, client)

        assert recorder.calls == [
            "list_registrations",
            "Enablement.disable",
            "deregister",
            "delete_collection",
        ]
        assert result.import_pruned is True
        assert result.enabled_marker_removed is True
        assert result.removed == 1
        assert [r.collection for r in client.deregistered] == ["proj"]
        assert client.deregistered[0].keep_data is False
        assert not EnabledMarker(project).is_present()

    def test_list_registrations_precedes_any_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The covering lookup MUST resolve before any state changes."""
        project = self._prepare_repo(tmp_path)
        client = FakeRegistryClient([("proj", project)])
        recorder = _OrderingRecorder()
        self._instrument(monkeypatch, recorder, client)

        disable_project(project, client)

        assert recorder.calls.index("list_registrations") == 0
        for mutation in ("Enablement.disable", "deregister", "delete_collection"):
            assert recorder.calls.index("list_registrations") < recorder.calls.index(
                mutation
            )

    def test_idempotent_second_call_does_not_redegister(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retry sees covering=None and never dispatches a second deregister."""
        project = self._prepare_repo(tmp_path)
        client = FakeRegistryClient([("proj", project)])
        disable_project(project, client)
        assert len(client.deregistered) == 1

        recorder = _OrderingRecorder()
        self._instrument(monkeypatch, recorder, client)

        disable_project(project, client)

        assert "deregister" not in recorder.calls
        assert "delete_collection" not in recorder.calls
        assert len(client.deregistered) == 1  # unchanged

    def test_deregister_never_called_when_enablement_disable_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed: an Enablement.disable failure MUST NOT deregister.

        The old order deregistered first — a mid-disable failure would leave
        the marker advertising a repo whose collection was already gone.
        The reordered flow leaves the collection intact on failure so a retry
        converges cleanly.
        """
        project = self._prepare_repo(tmp_path)
        client = FakeRegistryClient([("proj", project)])
        recorder = _OrderingRecorder()
        self._instrument(monkeypatch, recorder, client, disable_raises=OSError("boom"))

        with pytest.raises(OSError, match="boom"):
            disable_project(project, client)

        assert "deregister" not in recorder.calls
        assert "delete_collection" not in recorder.calls
        assert client.deregistered == []
        assert client.deleted == []
