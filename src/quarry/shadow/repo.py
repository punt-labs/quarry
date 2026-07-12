"""Nested-git working-tree operations for a project's private capture shadow.

``ShadowRepo`` owns the captures directory as a standalone git repository whose
``origin`` is the per-project private shadow ``<repo>-quarry``.  Operation is
fail-open at its boundary: a ``git``/``gh`` failure returns ``False``/``UNKNOWN``/
an empty list rather than raising, so a push problem never blocks a session.  The
fail-CLOSED exceptions are the two leak-gate enumerations.  ``staged_captures``:
a failed ``git ls-files``, or a path it reports that ``git show`` cannot read,
raises so the re-scrub gate aborts rather than committing a blob it never
verified.  ``parent_tracked_captures``: a failed ``git ls-files`` raises so
bootstrap refuses and doctor flags an unverifiable state rather than reading a
blind enumeration as "the parent tracks nothing".  The
security-load-bearing choices — the
allowlist ``.gitignore``, the refusal of a parent-tracked or non-ignored captures
dir, and visibility enforcement — live here; ``CaptureSync`` sequences them
around the commit-time re-scrub gate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Self, final

from quarry.shadow._git import GitRunner
from quarry.shadow.config import ShadowConfig
from quarry.shadow.visibility import Visibility

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

    def parent_in_work_tree(self) -> bool:
        """Return whether the parent path sits inside a git work tree.

        Distinguishes a benign no-work-tree case (``quarry doctor`` run outside
        any repo: no parent checkout can track a capture) from a git error
        INSIDE a real work tree (fail-CLOSED: tracked captures could exist).
        ``git rev-parse --is-inside-work-tree`` prints ``true`` only within a
        work tree; a non-zero exit (no repo) or ``false`` (a bare repo or a
        ``.git`` dir) both mean no parent checkout exists to leak into.
        """
        code, out = self._parent_git.run(["git", "rev-parse", "--is-inside-work-tree"])
        return code == 0 and out == "true"

    @staticmethod
    def _ls_files(runner: GitRunner, argv: list[str], label: str) -> str:
        """Return ``git ls-files`` stdout, raising (fail-CLOSED) on non-zero exit.

        A non-zero exit is an enumeration FAILURE, not "nothing to report":
        paths git could not enumerate may exist, so raising lets the leak gate
        refuse rather than reading a blind empty result as verified-clean.
        """
        code, out = runner.run(argv)
        if code != 0:
            msg = f"{label} enumeration failed: git ls-files exited {code}"
            raise RuntimeError(msg)
        return out

    def parent_tracked_captures(self) -> list[Path]:
        """Return capture paths the parent public repo already TRACKS (B3).

        A non-empty result means already-committed captures live in the public
        repo's index/history; bootstrap refuses and doctor flags this.

        Outside a git work tree there is no parent checkout that could track a
        capture, so enumeration is benign and returns ``[]``.  INSIDE a work
        tree a failed ``git ls-files`` fails closed via ``_ls_files`` — a blind
        enumeration is never read as "the parent tracks nothing".  A zero exit
        with empty output genuinely means the parent tracks none.
        """
        if not self.parent_in_work_tree():
            return []
        out = self._ls_files(
            self._parent_git,
            ["git", "ls-files", "--", self._captures_rel()],
            "parent capture",
        )
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
        gitignored by the parent and the parent tracks no captures.  A failed
        parent enumeration is unverifiable, not safe: catch its fail-CLOSED
        ``RuntimeError`` and turn it into a refusal so bootstrap returns False
        (no init) rather than crashing the caller — the shadow is never
        initialized while a blind enumeration might hide already-tracked leaks.
        """
        if not self.is_ignored_by_parent():
            return f"{self._captures_dir} is not gitignored by the parent; refusing"
        try:
            tracked = self.parent_tracked_captures()
        except RuntimeError as exc:
            return f"cannot verify parent-tracked captures ({exc}); refusing"
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

        A failed ``git ls-files`` fails closed via ``_ls_files`` so poisoned
        blobs are never read as an empty index.  A zero exit with empty output
        genuinely means an empty index -> ``{}``; a path ``ls-files`` reports is
        in the index, and ``_staged_blob`` likewise raises rather than dropping
        one ``git show`` cannot read.
        """
        out = self._ls_files(
            self._repo_git,
            ["git", "ls-files", "-z", "--", "session-*.md"],
            "staged capture",
        )
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
        target = self._gh_target(remote)
        code, out = self._parent_git.run(
            ["gh", "repo", "view", "--json", "visibility", "--", target]
        )
        if code != 0 or not out:
            return Visibility.UNKNOWN
        return Visibility.from_json(out)

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

    def create_remote(self) -> bool:
        """Create the shadow as a PRIVATE repo via ``gh`` and verify visibility."""
        # ``_gh_target`` yields ``host/owner/repo``; drop the host for the
        # ``gh repo create OWNER/REPO`` form (create targets the authed host).
        _, _, owner_repo = self._gh_target(self.resolved_remote()).partition("/")
        if owner_repo.count("/") != 1:
            logger.warning("shadow: cannot derive owner/repo; create it manually")
            return False
        argv = ["gh", "repo", "create", "--private", "--", owner_repo]
        if not self._parent_git.ok(argv):
            logger.warning(
                "shadow: 'gh repo create --private -- %s' failed; install/auth gh "
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
