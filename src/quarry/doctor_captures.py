"""Capture-related doctor checks: unlinked captures and the private shadow repo."""

from __future__ import annotations

import contextlib
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, final

from quarry.captures_collection import CapturesCollection
from quarry.doctor_failure import CheckFailure
from quarry.results import CheckResult
from quarry.shadow.repo import PARENT_TRACKED_REMEDIATION

if TYPE_CHECKING:
    from quarry.shadow.config import ShadowConfig
    from quarry.shadow.repo import ShadowRepo

# Where a project keeps its private capture shadow, relative to the project.
_SHADOW_REPO = ".punt-labs/quarry/captures"


@final
class CaptureDiagnostics:
    """Health checks for capture collections and the per-project capture shadow."""

    __slots__ = ()

    @staticmethod
    def unlinked(registry_path: Path, db_path: Path) -> CheckResult:
        """Report ``*-captures`` collections whose base has no registration."""
        result = partial(CheckResult, name="Unlinked captures", required=False)
        if not db_path.exists() or not registry_path.exists():
            return result(passed=True, message="no data yet")
        try:
            unlinked = CaptureDiagnostics._unlinked_names(registry_path, db_path)
        except Exception as exc:  # noqa: BLE001
            return result(
                passed=False,
                message=(
                    f"check failed ({registry_path.name}, {db_path.name}): "
                    f"{CheckFailure.detail(exc)}"
                ),
            )
        if unlinked:
            return result(passed=False, message=f"unlinked: {', '.join(unlinked)}")
        return result(passed=True, message="all captures map to a registered base")

    @staticmethod
    def _unlinked_names(registry_path: Path, db_path: Path) -> list[str]:
        """Return sorted ``*-captures`` collections whose base is unregistered."""
        from quarry.db.facade import Database  # noqa: PLC0415
        from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

        database = Database.connect(db_path)
        collections = {c["collection"] for c in database.catalog.list_collections()}
        # Virtual capture collections are unregistered by design — the
        # unregistered-dir fallback and the WebFetch bucket — so spare them; the
        # check flags only a *-captures collection whose base has no registration.
        col_names = collections - CapturesCollection.virtual_names()
        with contextlib.closing(SyncRegistry(registry_path)) as conn:
            registered = {r.collection for r in conn.list_registrations()}
        return sorted(
            name
            for name in col_names
            if name.endswith("-captures")
            and name.removesuffix("-captures") not in registered
        )

    @staticmethod
    def shadow_repo(cwd: str) -> CheckResult:
        """Report the state of the current project's private capture shadow.

        States: not-in-a-git-repo (informational — no parent repo to leak into)
        / parent-tracked-captures (an active leak — flagged even when the shadow
        is not enabled, since a committed capture leaks regardless) /
        not-configured / not-bootstrapped / public-remote-refusal / dirty
        (unpushed) / in-sync.
        """
        from quarry.shadow.config import ShadowConfig  # noqa: PLC0415
        from quarry.shadow.repo import ShadowRepo  # noqa: PLC0415

        directory = Path(cwd)
        captures_dir = directory / _SHADOW_REPO
        config = ShadowConfig.from_project(directory)
        repo = ShadowRepo(captures_dir, directory, config.remote if config else "")
        if not repo.parent_in_work_tree():
            return CaptureDiagnostics._not_in_repo()
        leak = CaptureDiagnostics._parent_tracked_leak(repo)
        if leak is not None:
            return leak
        return CaptureDiagnostics._configured_state(repo, config)

    @staticmethod
    def _not_in_repo() -> CheckResult:
        """Report the informational no-parent-repo state (never a leak).

        Outside a git work tree there is no public repo a capture could leak
        into, so the leak gate is vacuous: a failed ``git ls-files`` here means
        "there is no repo", not an unverifiable tracked-capture state.  This is
        informational and passes — never the required failure that an in-repo
        enumeration error (where a leak COULD exist) must raise.
        """
        return CheckResult(
            name="Shadow repo",
            passed=True,
            message="not in a git repo",
            required=False,
        )

    @staticmethod
    def _configured_state(repo: ShadowRepo, config: ShadowConfig | None) -> CheckResult:
        """Map the enable gate to a result: not-configured, else the git/gh state."""
        if config is None or not config.enabled:
            return CheckResult(
                name="Shadow repo",
                passed=True,
                message="not configured",
                required=False,
            )
        return CaptureDiagnostics._repo_state(repo)

    @staticmethod
    def _parent_tracked_leak(repo: ShadowRepo) -> CheckResult | None:
        """Return a required-failure result if the PUBLIC repo tracks captures.

        None means no leak.  This runs before the enable gate because an
        already-committed capture in the public repo is a leak whether or not
        the shadow is configured or enabled.

        A failed ``git ls-files`` enumeration raises ``RuntimeError``: an
        unverifiable state is not "no leak".  Catch it and report a required
        failure (fail-CLOSED) rather than crashing doctor or, worse, letting a
        blind enumeration surface as a green check that masks a tracked-capture
        leak.
        """
        try:
            tracked = repo.parent_tracked_captures()
        except RuntimeError as exc:
            return CheckResult(
                name="Shadow repo",
                passed=False,
                required=True,
                message=(
                    "cannot verify parent-tracked captures for the shadow repo "
                    f"{_SHADOW_REPO} (git error: {CheckFailure.detail(exc)})"
                ),
            )
        if not tracked:
            return None
        paths = ", ".join(str(p) for p in tracked)
        message = f"public repo tracks captures ({paths}). {PARENT_TRACKED_REMEDIATION}"
        return CheckResult(
            name="Shadow repo", passed=False, required=True, message=message
        )

    @staticmethod
    def _repo_state(repo: ShadowRepo) -> CheckResult:
        """Map a configured shadow repo's git/gh state to a doctor result."""
        from quarry.shadow.visibility import Visibility  # noqa: PLC0415

        result = partial(CheckResult, name="Shadow repo", required=False)
        if repo.remote_visibility() is Visibility.PUBLIC:
            return result(passed=False, message="remote is PUBLIC — pushes refused")
        if not repo.is_initialized:
            return result(passed=False, message="not bootstrapped — captures init")
        if repo.has_unpushed_commits():
            return result(passed=False, message="unpushed — captures push")
        return result(passed=True, message="in sync")
