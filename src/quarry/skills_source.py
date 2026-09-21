"""The canonical ``plugin/skills/`` tree quarry's deposits are copied from.

Owns every concern that touches only the source tree and a single deposit
target: locating the tree, listing its skills, hashing a skill's content,
reading a deposited manifest, and the atomic copy-and-swap that writes one
skill into place. :class:`~quarry.skills_install.SkillsInstaller` composes
this and adds harness-specific orchestration (which harness, which root) on
top — this module knows nothing about harnesses.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, final

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SkillDepositError", "SkillSource"]

# Bumped only if the manifest's OWN shape changes (e.g. a new field); a skill
# content edit is tracked by its content hash, not this constant.
_MANIFEST_FORMAT_VERSION: Final = 1
_MANIFEST_NAME: Final = ".quarry-skill.json"


@final
@dataclass(eq=False)
class SkillDepositError(OSError):
    """A deposit's swap failed and the previous copy could not be restored.

    Carries *backup* — the sibling directory still holding the pre-deposit
    content — so an operator can recover by hand. Losing this path in a bare
    re-raise would strand a skill in a state nothing can name: the original
    exception says the swap failed, but not where the last-known-good copy
    ended up. ``@dataclass(eq=False)`` rather than ``frozen`` for the same
    reason as ``quarry.client.errors``: an exception must let the interpreter
    set ``__traceback__``/``__cause__`` as it propagates, which a frozen
    ``__setattr__`` would block.
    """

    _message: str
    _backup: Path

    @property
    def backup(self) -> Path:
        return self._backup

    def __str__(self) -> str:
        return self._message


@final
class SkillSource:
    """The ``plugin/skills/`` tree at *root*, and the deposits copied from it."""

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def locate(start: Path) -> Path:
        """Walk upward from *start* to find the repo's ``plugin/skills`` tree.

        Raises ``FileNotFoundError`` (PY-EH-8) rather than returning ``None``:
        a caller asking to install skills has nothing to copy without a
        source, and silently no-op-ing would hide a usage mistake (running
        outside a quarry checkout) as an empty success.
        """
        for candidate in (start, *start.parents):
            skills_dir = candidate / "plugin" / "skills"
            if skills_dir.is_dir():
                return skills_dir
        msg = (
            f"no plugin/skills/ tree found above {start} -- run "
            "'quarry skills install' from inside a quarry checkout."
        )
        raise FileNotFoundError(msg)

    def skill_names(self) -> tuple[str, ...]:
        """Return every canonical skill's name, sorted, that carries a SKILL.md."""
        return tuple(
            sorted(
                path.name
                for path in self._root.iterdir()
                if path.is_dir() and (path / "SKILL.md").is_file()
            )
        )

    def content_hash(self, name: str) -> str:
        """Return skill *name*'s current content hash, computed from source."""
        return self._hash_tree(self._root / name)

    @staticmethod
    def _hash_tree(skill_dir: Path) -> str:
        """Return a short, order-independent content hash of *skill_dir*.

        A symlink is hashed by its link-target string, never dereferenced.
        Reading through a symlink to hash whatever bytes it points at would
        follow it anywhere on the filesystem the same way an unguarded
        ``copytree`` would (see :meth:`deposit`'s ``symlinks=True``) — the
        hash must reflect what actually gets copied, a link, not a
        substitute for its target's content.
        """
        digest = hashlib.sha256()
        for path in sorted(skill_dir.rglob("*")):
            relative = path.relative_to(skill_dir).as_posix()
            if path.is_symlink():
                digest.update(relative.encode())
                digest.update(path.readlink().as_posix().encode())
            elif path.is_file():
                digest.update(relative.encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    @staticmethod
    def read_manifest(target: Path) -> str | None:
        """Return the deposited content hash, or ``None`` if absent/unreadable.

        ``None`` is the documented "no valid manifest here" contract (a fresh
        target, a hand-edited one, or a corrupt file all look the same to an
        idempotent re-install: deposit fresh, or to a removal: refuse to
        touch it), not an error the caller handles.
        """
        manifest = target / _MANIFEST_NAME
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        content_hash = data.get("content_hash") if isinstance(data, dict) else None
        return content_hash if isinstance(content_hash, str) else None

    def deposit(self, name: str, target: Path) -> str:
        """Copy skill *name* into *target*, atomically; return its content hash.

        Writes into a temp sibling first, then swaps the old target (if any)
        to a backup sibling before renaming the temp into place — an
        interrupted copy never reaches *target*, and a failed rename restores
        the backup rather than leaving *target* missing (bug class 1). A
        stray temp/backup sibling from a prior run that crashed mid-deposit
        (a different pid than this one, so the pid-scoped names below never
        collide with it) is swept first — otherwise it lingers forever.

        Raises :class:`SkillDepositError` — chaining the original failure —
        only in the double-failure case: the swap itself failed AND restoring
        the backup also failed. Every other failure (the initial copy, or a
        swap failure with no prior deposit to restore) re-raises the
        original ``OSError`` unchanged.
        """
        source_dir = self._root / name
        content_hash = self._hash_tree(source_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._sweep_orphans(target.parent, target.name)

        tmp = target.parent / f".{target.name}.tmp-{os.getpid()}"
        try:
            # symlinks=True: preserve a symlink as a symlink rather than
            # dereferencing it -- the default would silently copy whatever
            # bytes the link happens to point at (anywhere on the
            # filesystem the process can read), materializing them as a
            # regular file in the deposited skill.
            shutil.copytree(source_dir, tmp, symlinks=True)
            manifest = {
                "content_hash": content_hash,
                "format_version": _MANIFEST_FORMAT_VERSION,
            }
            (tmp / _MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        self._swap_in(name, tmp, target)
        return content_hash

    @classmethod
    def _swap_in(cls, name: str, tmp: Path, target: Path) -> None:
        """Rename *tmp* into *target*, backing up and restoring on failure.

        The backup-rename and the tmp-rename share one ``try`` so a failure
        at EITHER step cleans up the freshly-copied *tmp* — a failed
        backup-rename used to happen before any ``try``, leaking *tmp*
        until the next deposit's orphan sweep happened to find it.
        """
        backup = target.parent / f".{target.name}.bak-{os.getpid()}"
        had_target = target.exists()
        try:
            if had_target:
                target.rename(backup)
            tmp.rename(target)
        except OSError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            # ``backup.exists()`` distinguishes the two failure sites: if the
            # backup rename itself failed, *target* was never touched (POSIX
            # rename is all-or-nothing) and there is nothing to restore.
            if not had_target or not backup.exists():
                raise
            cls._restore_backup(name, backup, target, exc)
        else:
            if had_target:
                shutil.rmtree(backup, ignore_errors=True)

    @staticmethod
    def _restore_backup(name: str, backup: Path, target: Path, cause: OSError) -> None:
        """Restore *backup* to *target* after a failed swap, then re-raise.

        Raises the chained :class:`SkillDepositError` only if the restore
        itself fails — the double-failure case where *backup* is the only
        remaining copy of the pre-deposit content.
        """
        try:
            backup.rename(target)
        except OSError as restore_exc:
            msg = (
                f"deposit of {name!r} failed ({cause}) and restoring the "
                f"previous copy from {backup} also failed -- the previous "
                "copy is preserved there for manual recovery"
            )
            raise SkillDepositError(msg, backup) from restore_exc
        raise cause

    @staticmethod
    def _sweep_orphans(parent: Path, name: str) -> None:
        """Delete stale ``.<name>.tmp-*``/``.<name>.bak-*`` siblings of *name*.

        Pid-scoped naming (see :meth:`deposit`) means a run's own temp and
        backup directories never collide with a concurrent run's — but it
        also means a run that crashed mid-deposit leaves its pid-named
        orphan behind forever, since nothing with that pid will ever clean
        it up. Swept unconditionally at the start of every real deposit.

        ``name`` is escaped with :func:`glob.escape` before it goes into the
        pattern: a skill directory literally named e.g. ``*`` would
        otherwise turn ``.{name}.tmp-*`` into the wildcard ``.*.tmp-*``,
        matching -- and deleting -- unrelated siblings' orphaned temp/backup
        directories.
        """
        escaped = glob.escape(name)
        for pattern in (f".{escaped}.tmp-*", f".{escaped}.bak-*"):
            for orphan in parent.glob(pattern):
                if orphan.is_dir():
                    shutil.rmtree(orphan, ignore_errors=True)
                else:
                    orphan.unlink(missing_ok=True)

    @staticmethod
    def remove_tree(target: Path) -> None:
        """Delete a deposited skill directory outright."""
        shutil.rmtree(target)
