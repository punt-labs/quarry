"""Nested-git working-tree operations for a project's private capture shadow.

``ShadowRepo`` owns the captures directory as a standalone git repository whose
``origin`` is the per-project private shadow ``<repo>-quarry``.  Every method is
fail-open at its boundary: a ``git``/``gh`` failure returns ``False``/``UNKNOWN``/
an empty list rather than raising, so a push problem never blocks a session.  The
security-load-bearing choices — the allowlist ``.gitignore``, the refusal of a
parent-tracked or non-ignored captures dir, and visibility enforcement — live
here; ``CaptureSync`` sequences them around the commit-time re-scrub gate.
"""

from __future__ import annotations

import json
import logging
import subprocess
from enum import Enum
from pathlib import Path
from typing import Self, final

from quarry.shadow.config import ShadowConfig

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30
_COMMIT_MESSAGE = "quarry: sync redacted captures"

# Fail-closed allowlist: only quarry's own capture files (and this .gitignore)
# can ever be staged, so a stray notes.txt/debug.log with PII can never be
# git-add-ed past the .md-scoped re-scrubber and I/O-race guard.
_ALLOWLIST_GITIGNORE = "*\n!.gitignore\n!session-*.md\n"

# A `.gitignore` rule does not untrack already-committed files.  `git rm --cached`
# stops *future* tracking but does NOT purge history: a capture already committed
# (and especially already pushed) stays exposed via `git log`/`git show`.
PARENT_TRACKED_REMEDIATION = (
    "run `git rm --cached -r .punt-labs/quarry/captures && git commit` to stop "
    "tracking (the working files stay on disk for the shadow to adopt). Note: "
    "this does NOT purge history — a capture already committed/pushed needs a "
    "history purge (git filter-repo or BFG) plus a force-push, coordinated with "
    "the repo owner (force-push rewrites shared history)."
)


class Visibility(Enum):
    """The verified visibility of the shadow remote."""

    PRIVATE = "private"
    PUBLIC = "public"
    UNKNOWN = "unknown"

    @classmethod
    def from_gh(cls, value: str) -> Visibility:
        """Map a ``gh repo view`` visibility string to an enum member."""
        normalized = value.strip().lower()
        if normalized == "public":
            return cls.PUBLIC
        if normalized == "private":
            return cls.PRIVATE
        return cls.UNKNOWN


@final
class _Git:
    """Run a ``git``/``gh`` subprocess in one working directory, never raising."""

    __slots__ = ("_cwd",)

    _cwd: Path

    def __new__(cls, cwd: Path) -> Self:
        self = super().__new__(cls)
        self._cwd = cwd
        return self

    def run(self, argv: list[str]) -> tuple[int, str]:
        """Return ``(returncode, stripped_stdout)``; ``(1, "")`` on any failure."""
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                cwd=self._cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return proc.returncode, proc.stdout.strip()

    def ok(self, argv: list[str]) -> bool:
        """Return whether *argv* exited zero."""
        return self.run(argv)[0] == 0


@final
class ShadowRepo:
    """The captures dir as a nested git repo pointing at the private shadow."""

    __slots__ = (
        "_captures_dir",
        "_configured_remote",
        "_parent",
        "_parent_git",
        "_repo_git",
    )

    _captures_dir: Path
    _configured_remote: str
    _parent: Path
    _parent_git: _Git
    _repo_git: _Git

    def __new__(cls, captures_dir: Path, parent: Path, configured_remote: str) -> Self:
        self = super().__new__(cls)
        self._captures_dir = captures_dir
        self._parent = parent
        self._configured_remote = configured_remote
        self._repo_git = _Git(captures_dir)
        self._parent_git = _Git(parent)
        return self

    @property
    def captures_dir(self) -> Path:
        return self._captures_dir

    @property
    def is_initialized(self) -> bool:
        """Return whether the nested git repo already exists."""
        return (self._captures_dir / ".git").exists()

    def resolved_remote(self) -> str:
        """Return the configured remote, else derive ``<origin>-quarry``."""
        if self._configured_remote:
            return self._configured_remote
        code, origin = self._parent_git.run(["git", "remote", "get-url", "origin"])
        return ShadowConfig.derive_remote(origin) if code == 0 else ""

    def is_ignored_by_parent(self) -> bool:
        """Return whether the parent repo gitignores the captures dir."""
        return self._parent_git.ok(["git", "check-ignore", str(self._captures_dir)])

    def parent_tracked_captures(self) -> list[Path]:
        """Return capture paths the parent public repo already TRACKS (B3).

        A non-empty result means already-committed captures live in the public
        repo's index/history; bootstrap refuses and doctor flags this.
        """
        code, out = self._parent_git.run(
            ["git", "ls-files", "--", self._captures_rel()]
        )
        if code != 0 or not out:
            return []
        return [Path(line) for line in out.splitlines() if line]

    def bootstrap(self) -> bool:
        """Prepare the nested repo, refusing on either fail-closed gate.

        Refuses if the captures dir is not gitignored by the parent, if the
        parent already tracks captures (B3), or if no remote can be resolved.
        Otherwise inits the repo, writes the allowlist ``.gitignore``, sets the
        origin, and adopts any existing remote history without clobbering local
        capture files.
        """
        if not self.is_ignored_by_parent():
            logger.warning(
                "shadow: %s is not gitignored by the parent repo; refusing",
                self._captures_dir,
            )
            return False
        tracked = self.parent_tracked_captures()
        if tracked:
            logger.warning(
                "shadow: parent repo already tracks captures (%s); refusing. %s",
                ", ".join(str(p) for p in tracked),
                PARENT_TRACKED_REMEDIATION,
            )
            return False
        remote = self.resolved_remote()
        if not remote:
            logger.warning("shadow: no shadow remote configured or derivable")
            return False
        self._captures_dir.mkdir(parents=True, exist_ok=True)
        if not self.is_initialized and not self._repo_git.ok(
            ["git", "init", "-b", "main", "."]
        ):
            logger.warning("shadow: git init failed in %s", self._captures_dir)
            return False
        self._write_allowlist_gitignore()
        self._ensure_origin(remote)
        self._fetch_and_reconcile()
        return True

    def stage(self) -> None:
        """Stage the working tree; the allowlist bounds it to session-*.md."""
        self._repo_git.run(["git", "add", "-A"])

    def commit(self) -> bool:
        """Commit the staged index; False when there is nothing to commit."""
        return self._repo_git.ok(["git", "commit", "-m", _COMMIT_MESSAGE])

    def push(self) -> bool:
        """Push ``main`` to origin, sending any accumulated offline commits."""
        return self._repo_git.ok(["git", "push", "-u", "origin", "main"])

    def has_unpushed_commits(self) -> bool:
        """Return whether local ``main`` is ahead of ``origin/main``."""
        code, out = self._repo_git.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"]
        )
        return code == 0 and out.isdigit() and int(out) > 0

    def remote_visibility(self) -> Visibility:
        """Return the shadow remote's visibility via ``gh`` (UNKNOWN if absent)."""
        remote = self.resolved_remote()
        if not remote:
            return Visibility.UNKNOWN
        code, out = self._parent_git.run(
            ["gh", "repo", "view", remote, "--json", "visibility"]
        )
        if code != 0 or not out:
            return Visibility.UNKNOWN
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return Visibility.UNKNOWN
        return Visibility.from_gh(str(data.get("visibility", "")))

    def create_remote(self) -> bool:
        """Create the shadow as a PRIVATE repo via ``gh`` and verify visibility."""
        owner_repo = self._owner_repo(self.resolved_remote())
        if not owner_repo:
            logger.warning("shadow: cannot derive owner/repo; create it manually")
            return False
        if not self._parent_git.ok(["gh", "repo", "create", owner_repo, "--private"]):
            logger.warning(
                "shadow: 'gh repo create %s --private' failed; install/auth gh "
                "or create the repo manually",
                owner_repo,
            )
            return False
        if self.remote_visibility() is not Visibility.PRIVATE:
            logger.warning("shadow: created %s is not verifiably private", owner_repo)
            return False
        return True

    def _write_allowlist_gitignore(self) -> None:
        (self._captures_dir / ".gitignore").write_text(
            _ALLOWLIST_GITIGNORE, encoding="utf-8"
        )

    def _ensure_origin(self, remote: str) -> None:
        code, current = self._repo_git.run(["git", "remote", "get-url", "origin"])
        if code != 0:
            self._repo_git.run(["git", "remote", "add", "origin", remote])
        elif current != remote:
            self._repo_git.run(["git", "remote", "set-url", "origin", remote])

    def _fetch_and_reconcile(self) -> None:
        if not self._repo_git.ok(["git", "fetch", "origin"]):
            return  # fail-open: offline is fine; the first push creates the remote
        remote_has_main = self._repo_git.ok(
            ["git", "rev-parse", "--verify", "origin/main"]
        )
        local_has_commit = self._repo_git.ok(["git", "rev-parse", "--verify", "HEAD"])
        if remote_has_main and not local_has_commit:
            self._repo_git.run(["git", "reset", "--mixed", "origin/main"])

    def _captures_rel(self) -> str:
        try:
            return str(self._captures_dir.relative_to(self._parent))
        except ValueError:
            return str(self._captures_dir)

    @staticmethod
    def _owner_repo(remote: str) -> str:
        """Extract ``owner/repo`` from an SSH or HTTPS git remote URL."""
        core = remote.removesuffix(".git").replace(":", "/")
        parts = [p for p in core.split("/") if p]
        return "/".join(parts[-2:]) if len(parts) >= 2 else ""
