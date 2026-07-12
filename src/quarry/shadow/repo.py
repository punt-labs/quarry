"""Nested-git working-tree operations for a project's private capture shadow.

``ShadowRepo`` owns the captures directory as a standalone git repository whose
``origin`` is the per-project private shadow ``<repo>-quarry``.  Operation is
fail-open at its boundary: a ``git``/``gh`` failure returns ``False``/``UNKNOWN``/
an empty list rather than raising, so a push problem never blocks a session.  The
one fail-CLOSED exception is ``staged_captures``: a failed ``git ls-files``
enumeration, or a path it reports that ``git show`` cannot read, raises so the
re-scrub gate aborts rather than committing a blob it never verified.  The
security-load-bearing choices — the
allowlist ``.gitignore``, the refusal of a parent-tracked or non-ignored captures
dir, and visibility enforcement — live here; ``CaptureSync`` sequences them
around the commit-time re-scrub gate.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Self, final

from quarry.shadow._git import GitRunner
from quarry.shadow.config import ShadowConfig

logger = logging.getLogger(__name__)

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
    _parent_git: GitRunner
    _repo_git: GitRunner

    def __new__(cls, captures_dir: Path, parent: Path, configured_remote: str) -> Self:
        self = super().__new__(cls)
        self._captures_dir = captures_dir
        self._parent = parent
        self._configured_remote = configured_remote
        self._repo_git = GitRunner(captures_dir)
        self._parent_git = GitRunner(parent)
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
        """Return whether the parent repo gitignores the captures dir.

        Probes a repo-relative capture path *under* the dir rather than the dir
        itself: ``git check-ignore`` resolves arguments against its working
        directory, and a directory rule like ``captures/`` only matches a bare
        directory argument when that directory exists on disk — probing a file
        path under it matches the rule whether or not the dir yet exists.
        """
        probe = str(Path(self._captures_rel()) / "session-probe.md")
        return self._parent_git.ok(["git", "check-ignore", "--", probe])

    def parent_tracked_captures(self) -> list[Path]:
        """Return capture paths the parent public repo already TRACKS (B3).

        A non-empty result means already-committed captures live in the public
        repo's index/history; bootstrap refuses and doctor flags this.
        """
        code, out = self._parent_git.run(
            ["git", "ls-files", "--", self._captures_rel()]
        )
        if code != 0:  # empty stdout already yields [] via the comprehension
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
        refusal = self._refusal_reason()
        if refusal is not None:
            logger.warning("shadow: %s", refusal)
            return False
        remote = self.resolved_remote()
        if not remote:
            logger.warning("shadow: no shadow remote configured or derivable")
            return False
        if not self._init_repo():
            return False
        self._write_allowlist_gitignore()
        self._ensure_origin(remote)
        self._fetch_and_reconcile()
        return True

    def _refusal_reason(self) -> str | None:
        """Return why bootstrap must refuse (a leak risk), or None when safe.

        None is the "safe to proceed" state, not an error: the captures dir is
        gitignored by the parent and the parent tracks no captures.
        """
        if not self.is_ignored_by_parent():
            return f"{self._captures_dir} is not gitignored by the parent; refusing"
        tracked = self.parent_tracked_captures()
        if tracked:
            names = ", ".join(str(p) for p in tracked)
            return (
                f"parent repo already tracks captures ({names}); refusing. "
                f"{PARENT_TRACKED_REMEDIATION}"
            )
        return None

    def _init_repo(self) -> bool:
        """Create the captures dir and nested git repo; False on init failure."""
        self._captures_dir.mkdir(parents=True, exist_ok=True)
        if not self.is_initialized and not self._repo_git.ok(
            ["git", "init", "-b", "main", "."]
        ):
            logger.warning("shadow: git init failed in %s", self._captures_dir)
            return False
        return True

    def stage(self) -> bool:
        """Stage the working tree; the allowlist bounds it to session-*.md.

        Return whether ``git add`` succeeded.  A silent failure here (e.g. an
        ``index.lock`` race) would leave the index holding pre-rescrub blobs
        while the working tree reads clean, so the caller MUST abort — never
        commit — when this returns ``False``.
        """
        return self._repo_git.ok(["git", "add", "-A"])

    def staged_captures(self) -> dict[str, str]:
        """Return ``{relpath: blob text}`` for every staged ``session-*.md``.

        Reads the git INDEX — exactly the bytes a commit will ship — not the
        working tree, so the commit-time fixed-point guard covers what actually
        leaves the machine.  ``GitRunner.run`` strips surrounding whitespace; the
        scrubber never alters leading/trailing whitespace, so a fixed point
        stays a fixed point and PII (never whitespace) stays detectable.

        A non-zero ``git ls-files`` exit is an enumeration FAILURE, not "no
        captures": staged blobs may exist that git could not report, so raising
        (fail-CLOSED) aborts the gate before commit rather than letting an empty
        result pass verification vacuously while poisoned blobs sit staged.  A
        zero exit with empty output genuinely means an empty index -> ``{}``.  A
        path ``ls-files`` DOES report is in the index; ``_staged_blob`` likewise
        raises rather than dropping one ``git show`` cannot read.
        """
        code, out = self._repo_git.run(["git", "ls-files", "-z", "--", "session-*.md"])
        if code != 0:
            msg = f"staged capture enumeration failed: git ls-files exited {code}"
            raise RuntimeError(msg)
        return {rel: self._staged_blob(rel) for rel in filter(None, out.split("\0"))}

    def _staged_blob(self, rel: str) -> str:
        """Return the index blob text for *rel*, raising when git cannot read it.

        Fail-CLOSED: ``git ls-files`` only reports paths that are in the index,
        so a ``git show`` failure here is a git-level inconsistency, never a "not
        staged" outcome.  Raising — rather than dropping the path — surfaces the
        unverifiable blob so the commit-time re-scrub gate aborts before the
        commit, never shipping bytes the fixed-point guard never checked.
        """
        code, content = self._repo_git.run(["git", "show", f":{rel}"])
        if code != 0:
            msg = f"staged blob unreadable for {rel!r}: git show exited {code}"
            raise RuntimeError(msg)
        return content

    def commit(self) -> bool:
        """Commit the staged index; False when there is nothing to commit."""
        return self._repo_git.ok(["git", "commit", "-m", _COMMIT_MESSAGE])

    def push(self) -> bool:
        """Push ``main`` to origin, sending any accumulated offline commits."""
        return self._repo_git.ok(["git", "push", "-u", "origin", "main"])

    def has_unpushed_commits(self) -> bool:
        """Return whether local ``main`` holds commits not on ``origin/main``.

        When ``origin/main`` is unresolvable — the remote was never created, or
        a visibility-refused push left it absent — ``rev-list`` exits non-zero.
        A non-zero exit must NOT read as "in sync": any local commit is by
        definition unpushed, so fail toward "unpushed" (the safe direction for
        a leak indicator) whenever the repo holds a commit.
        """
        code, out = self._repo_git.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"]
        )
        if code == 0:
            return out.isdigit() and int(out) > 0
        return self._repo_git.ok(["git", "rev-parse", "--verify", "HEAD"])

    def remote_visibility(self) -> Visibility:
        """Return the shadow remote's visibility via ``gh`` (UNKNOWN if absent)."""
        remote = self.resolved_remote()
        if not remote:
            return Visibility.UNKNOWN
        code, out = self._parent_git.run(
            ["gh", "repo", "view", self._gh_target(remote), "--json", "visibility"]
        )
        if code != 0 or not out:
            return Visibility.UNKNOWN
        return self._parse_visibility(out)

    @staticmethod
    def _gh_target(remote: str) -> str:
        """Normalize a git remote URL to ``host/owner/repo`` for ``gh repo view``.

        ``gh repo view`` accepts ``HOST/OWNER/REPO`` or an https URL, but not an
        scp-style SSH URL (``git@github.com:org/repo.git`` — the common punt-labs
        form), which it reports as UNKNOWN.  That spuriously forces
        ``acknowledge_unverified`` and guts the visibility gate for private SSH
        remotes.  Handles scp-style SSH, ``ssh://`` URLs, and https, stripping a
        trailing ``.git``.  A URL that does not parse to host/owner/repo passes
        through unchanged (fail-safe -> UNKNOWN, which the gate refuses/acks).
        """
        _, _, body = remote.removesuffix(".git").rpartition("://")  # drop scheme
        _, _, body = body.rpartition("@")  # drop any user@ prefix
        # scp-style joins host to path with ``:``; normalizing it to ``/`` makes
        # every form a plain ``host/owner/repo`` split.
        parts = [seg for seg in body.replace(":", "/", 1).split("/") if seg]
        if len(parts) < 3:
            return remote
        return "/".join(parts[-3:])

    @staticmethod
    def _parse_visibility(gh_json: str) -> Visibility:
        """Map a ``gh repo view --json visibility`` payload to a Visibility."""
        try:
            data = json.loads(gh_json)
        except json.JSONDecodeError:
            return Visibility.UNKNOWN
        return Visibility.from_gh(str(data.get("visibility", "")))

    def create_remote(self) -> bool:
        """Create the shadow as a PRIVATE repo via ``gh`` and verify visibility."""
        # ``_gh_target`` yields ``host/owner/repo``; drop the host for the
        # ``gh repo create OWNER/REPO`` form (create targets the authed host).
        _, _, owner_repo = self._gh_target(self.resolved_remote()).partition("/")
        if owner_repo.count("/") != 1:
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
            self._repo_git.run(["git", "remote", "add", "--", "origin", remote])
        elif current != remote:
            self._repo_git.run(["git", "remote", "set-url", "--", "origin", remote])

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
