"""Best-effort cross-harness skill sync, shared by ``Enablement.enable``/``disable``.

:class:`SkillDeposit` and :class:`SkillRetraction` are the two collaborators
``Enablement`` composes for the deposit and retraction sides of the
cross-harness skill lifecycle. Deposit (PL-1049) reads quarry's OWN packaged
skill tree (:meth:`~quarry.skills_source.SkillSource.packaged`) — ungated by
the enabled repo, since the source is the installed binary itself, not
anything the repo supplies. Retraction keeps its pre-existing behavior:
it only acts when the DISABLED repo is provably quarry's own checkout
(:class:`quarry.own_checkout.OwnCheckout`), the dev-time symmetry that lets
disabling quarry's own checkout clean up after it; that gate, plus the
manifest-ownership check every removal already enforces
(:meth:`quarry.skills_install.SkillsInstaller.remove_all`), is unrelated to
deposit's source trust and is not part of this change.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.home_paths import HomeUnresolvedError, QuarryHome
from quarry.own_checkout import OwnCheckout
from quarry.skills_install import SkillsInstaller
from quarry.skills_source import SkillSource

if TYPE_CHECKING:
    from quarry.skills_install import SkillOutcome

__all__ = ["SkillDeposit", "SkillRetraction"]

logger = logging.getLogger(__name__)


@final
class SkillDeposit:
    """Best-effort deposit of quarry's OWN cross-harness skills, on ``enable``.

    Deposits into every DETECTED harness on this machine
    (``install_detected``) — matching what ``quarry skills install`` (no
    flag) does — from the INSTALLED binary's own packaged skill tree
    (:meth:`~quarry.skills_source.SkillSource.packaged`), never from the
    enabled repo's tree. This is what makes the deposit fire in ANY repo (a
    normal user's project has no ``plugin/skills/`` of its own) and leaves
    nothing for a hostile repo to forge: the source is fixed by the
    package's own layout, not supplied by whatever repo happens to be
    enabled. Claude Code is served by the plugin directly, so a deposit
    attempt there reports ``"unsupported"`` with no deposit target; that
    outcome is returned like any other, never special-cased here.

    Genuinely best-effort: :meth:`run` never raises, so an unresolvable home
    directory or a filesystem failure (e.g. a permission error under an
    unusual home layout) can never abort
    :meth:`~quarry.enablement.Enablement.enable` or discard the
    gitignore/guide/import/marker work that already committed.
    """

    __slots__ = ()

    @staticmethod
    def run() -> tuple[SkillOutcome, ...]:
        """Deposit quarry's packaged skills into every detected harness."""
        try:
            installer = SkillsInstaller(SkillSource.packaged(), QuarryHome.home())
            return installer.install_detected()
        except (OSError, HomeUnresolvedError):
            logger.warning(
                "quarry enable: failed to deposit cross-harness skills",
                exc_info=True,
            )
            return ()


@final
class SkillRetraction:
    """Best-effort retraction of quarry's cross-harness skill deposits, on ``disable``.

    Acts only when *root* is provably quarry's OWN checkout, with a
    ``plugin/skills/`` tree of its own — the pre-existing, narrower symmetry
    that lets disabling quarry's OWN dev checkout retract what its OWN
    ``enable`` deposited; disabling any other repo (the other 99% of
    ``disable`` calls) is a no-op here, which is the expected case
    (PY-EH-8), not a failure. Identity alone is not the whole story: every
    marketplace-layout Claude Code plugin (lux, prfaq, dungeon, punt-kit)
    ships an identically-shaped ``plugin/skills/`` tree, so shape never
    proves provenance on its own — only a checkout whose ``pyproject.toml``
    names the ``punt-quarry`` package passes
    (:class:`quarry.own_checkout.OwnCheckout`). Manifest ownership
    (:meth:`quarry.skills_install.SkillsInstaller.remove_all`) is the further
    guard that protects user files regardless: even given identity, a
    removal never deletes a same-named entry lacking quarry's own
    ``.quarry-skill.json`` manifest.

    Genuinely best-effort: :meth:`run` never raises, so a retraction failure
    (e.g. a permission error walking an unusual home layout) can never abort
    :meth:`~quarry.enablement.Enablement.disable` or block the marker/import
    teardown that already committed.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def _source_dir(self) -> Path:
        """Return the enabled repo's own ``plugin/skills/`` tree, if any."""
        return self._root / "plugin" / "skills"

    def _should_retract(self) -> bool:
        """Whether *root* is quarry's OWN checkout, with a skill source to retract."""
        return self._source_dir.is_dir() and OwnCheckout(self._root).confirmed()

    def run(self) -> None:
        """Retract every deposit if, and only if, *root* is quarry's own checkout."""
        if not self._should_retract():
            return
        removed = self._remove_all_deposits()
        if removed is None:
            return
        logger.info(
            "quarry disable: retracted %d cross-harness skill deposit(s)",
            sum(1 for outcome in removed if outcome.action == "removed"),
        )

    def _remove_all_deposits(self) -> tuple[SkillOutcome, ...] | None:
        """Best-effort ``SkillsInstaller.remove_all()``.

        ``None`` means the attempt raised (an unresolvable home directory or
        a filesystem failure, already logged here) — the documented "could
        not retract, but that must never abort the caller" contract, not a
        value :meth:`run` inspects further.
        """
        try:
            installer = SkillsInstaller(self._source_dir, QuarryHome.home())
            return installer.remove_all()
        except (OSError, HomeUnresolvedError):
            logger.warning(
                "quarry disable: failed to retract cross-harness skill deposits",
                exc_info=True,
            )
            return None
