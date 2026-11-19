"""The canonical ``plugin/skills/`` tree quarry's deposits are copied from.

Owns every concern that touches only the source tree and a single deposit
target: locating the tree, listing its skills, hashing a skill's content,
reading a deposited manifest, and populating the atomic swap
(:mod:`quarry.atomic_dir_swap`) that writes one skill into place.
:class:`~quarry.skills_install.SkillsInstaller` composes this and adds
harness-specific orchestration (which harness, which root) on top — this
module knows nothing about harnesses.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import shutil
from typing import TYPE_CHECKING, Final, Self, cast, final

from quarry.atomic_dir_swap import AtomicDirectorySwap, AtomicSwapError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SkillDepositError", "SkillSource"]

# Re-exported under its historical name: callers outside this module (and the
# re-raise inside :meth:`SkillSource.deposit`) still speak in terms of a
# SKILL deposit failing, not the generic swap primitive underneath it.
SkillDepositError = AtomicSwapError

# Bumped only if the manifest's OWN shape changes (e.g. a new field); a skill
# content edit is tracked by its content hash, not this constant.
_MANIFEST_FORMAT_VERSION: Final = 1
_MANIFEST_NAME: Final = ".quarry-skill.json"


@final
class SkillSource:
    """The ``plugin/skills/`` tree at *root*, and the deposits copied from it."""

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        """Select the skill-source tree without reading or depositing its contents."""
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def root(self) -> Path:
        """Expose the tree from which packaged skills are read."""
        return self._root

    @staticmethod
    def packaged() -> Path:
        """Return the skill tree shipped inside the INSTALLED ``quarry`` package.

        ``src/quarry/_skills/`` is a committed, sync-checked mirror of
        ``plugin/skills/`` (``make sync-skills`` / ``check-skills-sync``), so
        it resolves identically whether ``quarry`` is an editable checkout or
        an installed wheel -- data under ``src/quarry/`` ships automatically
        with the ``module-name = "quarry"`` build backend, the same way
        ``py.typed`` does. Unlike :meth:`locate`, there is no tree to walk up
        for and no identity to prove: the path is fixed by the package's own
        layout, not supplied by whatever repo happens to enclose the caller,
        so it carries nothing for a hostile repo to forge.
        ``importlib.resources.files`` returns a ``Traversable``, typed as
        such because a package CAN be zipped; ``quarry._skills`` never is
        (``py.typed`` and ``data/repo-guide.md`` already ship as real files
        under the installed module, not zip members), so the runtime value
        here is always a concrete ``pathlib.Path`` -- the cast documents
        that contract rather than suppressing a genuine type error.
        """
        return cast("Path", importlib.resources.files("quarry") / "_skills")

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
    def _hash_tree(skill_dir: Path, *, exclude: str | None = None) -> str:
        """Return a short, order-independent content hash of *skill_dir*.

        A symlink is hashed by its link-target string, never dereferenced.
        Reading through a symlink to hash whatever bytes it points at would
        follow it anywhere on the filesystem the same way an unguarded
        ``copytree`` would (see :meth:`deposit`'s ``symlinks=True``) — the
        hash must reflect what actually gets copied, a link, not a
        substitute for its target's content.

        *exclude*, when given, is a path relative to *skill_dir* (as a
        POSIX string) skipped entirely -- :meth:`is_owned` recomputes this
        same hash over a DEPOSITED directory, which carries the manifest
        file the SOURCE tree never had; excluding it is what makes the two
        hashes comparable at all.
        """
        digest = hashlib.sha256()
        for path in sorted(skill_dir.rglob("*")):
            relative = path.relative_to(skill_dir).as_posix()
            if relative == exclude:
                continue
            if path.is_symlink():
                digest.update(relative.encode())
                digest.update(path.readlink().as_posix().encode())
            elif path.is_file():
                digest.update(relative.encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    @staticmethod
    def _load_manifest(target: Path) -> dict[str, object] | None:
        """Return *target*'s parsed manifest, or ``None`` if absent/unreadable.

        The one JSON-parsing routine shared by :meth:`read_manifest` (which
        trusts the declared hash) and :meth:`is_owned` (which does not) --
        a missing file, unreadable file, invalid JSON, or a JSON value that
        is not an object all collapse to the same "nothing to read here"
        outcome (PY-EH-8's ``None``-for-absence contract applies; a
        manifest's shape is a wire boundary, so ``dict[str, object]`` is
        the honest type until each field is validated by its caller).
        """
        manifest = target / _MANIFEST_NAME
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def read_manifest(target: Path) -> str | None:
        """Return the deposited content hash, or ``None`` if absent/unreadable.

        ``None`` is the documented "no valid manifest here" contract (a fresh
        target, a hand-edited one, or a corrupt file all look the same to an
        idempotent re-install: deposit fresh, or to a removal: refuse to
        touch it), not an error the caller handles. This is the manifest's
        DECLARED hash, unverified -- see :meth:`is_owned` for the check that
        the declaration is actually true of the directory's current contents.
        """
        data = SkillSource._load_manifest(target)
        if data is None:
            return None
        content_hash = data.get("content_hash")
        return content_hash if isinstance(content_hash, str) else None

    @staticmethod
    def is_owned(target: Path) -> bool:
        """Whether *target* is a directory this installer can PROVE it deposited.

        Presence of a parseable manifest is forgeable: any directory can
        carry a hand-crafted ``.quarry-skill.json`` naming an arbitrary
        string as its ``content_hash``. Ownership instead requires the
        declared hash to MATCH one recomputed from the directory's CURRENT
        contents (:meth:`_hash_tree`, excluding the manifest file itself --
        the same exclusion the SOURCE tree gets for free by never
        containing one). A forged or minimal manifest fails this
        comparison and is reported "foreign" by every caller (see
        :meth:`quarry.skills_install.SkillsInstaller._is_foreign`) rather
        than trusted; so does a manifest an operator hand-edited alongside
        the skill's content, which is the intended, conservative failure
        mode -- refuse rather than silently overwrite or delete.
        """
        if not target.is_dir():
            return False
        data = SkillSource._load_manifest(target)
        if data is None:
            return False
        if data.get("format_version") != _MANIFEST_FORMAT_VERSION:
            return False
        content_hash = data.get("content_hash")
        if not isinstance(content_hash, str):
            return False
        return content_hash == SkillSource._hash_tree(target, exclude=_MANIFEST_NAME)

    def deposit(self, name: str, target: Path) -> str:
        """Copy skill *name* into *target*, atomically; return its content hash.

        Populates a temp sibling via
        :class:`~quarry.atomic_dir_swap.AtomicDirectorySwap`, then hands it
        off to commit into place — an interrupted copy never reaches
        *target*, and a failed rename restores the prior occupant rather
        than leaving *target* missing (bug class 1).

        Raises :class:`SkillDepositError` — chaining the original failure —
        only in the double-failure case: the swap itself failed AND restoring
        the prior occupant also failed. Every other failure (the initial
        copy, or a swap failure with no prior deposit to restore) re-raises
        the original ``OSError`` unchanged.
        """
        source_dir = self._root / name
        content_hash = self._hash_tree(source_dir)
        swap = AtomicDirectorySwap(name, target)
        tmp = swap.prepare()
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

        swap.commit(tmp)
        return content_hash

    @staticmethod
    def remove_tree(target: Path) -> None:
        """Delete a deposited skill directory outright."""
        shutil.rmtree(target)
