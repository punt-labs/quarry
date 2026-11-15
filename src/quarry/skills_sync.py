"""Best-effort cross-harness skill sync, shared by ``Enablement.enable``/``disable``.

:class:`SkillDeposit` and :class:`SkillRetraction` are the two collaborators
``Enablement`` composes for the PL-1049 (deposit) and pre-existing (retract)
sides of the cross-harness skill lifecycle. Both share one guard — "is *root*
provably quarry's OWN checkout, with a skill source tree to act on" — so it
lives once here (:class:`_SkillSyncGuard`) rather than being copy-pasted
across the two halves of the symmetry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.home_paths import QuarryHome
from quarry.own_checkout import OwnCheckout
from quarry.skills_install import SkillsInstaller

if TYPE_CHECKING:
    from quarry.skills_install import SkillOutcome

__all__ = ["SkillDeposit", "SkillRetraction"]

logger = logging.getLogger(__name__)


@final
class _SkillSyncGuard:
    """Whether *root* is quarry's OWN checkout, with a skill source to sync.

    Two conditions, both required (defense-in-depth for a deposit/``rmtree``
    under the operator's real ``$HOME``):

    1. **A real source tree exists** at ``<root>/plugin/skills`` — nothing to
       sync for the other 99% of repos ``enable``/``disable`` run against,
       and that absence is the expected case (PY-EH-8), not a failure.
    2. **Identity** (:class:`quarry.own_checkout.OwnCheckout`): every
       marketplace-layout Claude Code plugin — lux, prfaq, dungeon,
       punt-kit, and quarry itself — ships an identically-shaped
       ``plugin/skills/`` tree, so shape alone proves nothing. Only a
       checkout whose ``pyproject.toml`` names the ``punt-quarry`` package
       passes. Checked on *root* itself, never an ancestor walk (unlike
       :meth:`SkillsInstaller.locate_source`) — an ancestor walk would
       true-positive on any directory nested inside the quarry checkout
       (every test's ``tmp_path`` included) and reach the operator's real
       cross-harness deposits as a side effect of enabling/disabling an
       unrelated nested path.

    Manifest ownership (:meth:`SkillsInstaller.install`/``install_detected``/
    ``remove_all``) is the THIRD guard, enforced inside those calls
    themselves: even given identity, a deposit never overwrites, and a
    removal never deletes, a same-named entry lacking quarry's own
    ``.quarry-skill.json`` manifest.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._root = root
        return self

    @property
    def source_dir(self) -> Path:
        """Return the skill source tree this guard checks for."""
        return self._root / "plugin" / "skills"

    def should_sync(self) -> bool:
        """Whether both conditions above hold for *root*."""
        return self.source_dir.is_dir() and OwnCheckout(self._root).confirmed()


@final
class SkillDeposit:
    """Best-effort deposit of quarry's cross-harness skills, on ``enable``.

    Deposits into every DETECTED harness on this machine
    (``install_detected``) — matching what ``quarry skills install`` (no
    flag) does — not every harness unconditionally. Claude Code is served by
    the plugin directly, so a deposit attempt there reports ``"unsupported"``
    with no deposit target; that outcome is returned like any other, never
    special-cased here.

    Genuinely best-effort: :meth:`run` never raises, so a deposit failure
    (e.g. a permission error under an unusual home layout) can never abort
    :meth:`~quarry.enablement.Enablement.enable` or discard the
    gitignore/guide/import/marker work that already committed.
    """

    __slots__ = ("_guard",)

    _guard: _SkillSyncGuard

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._guard = _SkillSyncGuard(root)
        return self

    def run(self) -> tuple[SkillOutcome, ...]:
        """Deposit into every detected harness if, and only if, both guards clear."""
        if not self._guard.should_sync():
            return ()
        installer = SkillsInstaller(self._guard.source_dir, QuarryHome.home())
        try:
            return installer.install_detected()
        except OSError:
            logger.warning(
                "quarry enable: failed to deposit cross-harness skills",
                exc_info=True,
            )
            return ()


@final
class SkillRetraction:
    """Best-effort retraction of quarry's cross-harness skill deposits, on ``disable``.

    Genuinely best-effort: :meth:`run` never raises, so a retraction failure
    (e.g. a permission error walking an unusual home layout) can never abort
    :meth:`~quarry.enablement.Enablement.disable` or block the marker/import
    teardown that already committed.
    """

    __slots__ = ("_guard",)

    _guard: _SkillSyncGuard

    def __new__(cls, root: Path) -> Self:
        self = super().__new__(cls)
        self._guard = _SkillSyncGuard(root)
        return self

    def run(self) -> None:
        """Retract every deposit if, and only if, both guards clear."""
        if not self._guard.should_sync():
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

        ``None`` means the attempt raised (a filesystem failure, already
        logged here) — the documented "could not retract, but that must
        never abort the caller" contract, not a value :meth:`run` inspects
        further.
        """
        installer = SkillsInstaller(self._guard.source_dir, QuarryHome.home())
        try:
            return installer.remove_all()
        except OSError:
            logger.warning(
                "quarry disable: failed to retract cross-harness skill deposits",
                exc_info=True,
            )
            return None
