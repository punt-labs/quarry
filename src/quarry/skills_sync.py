"""Best-effort cross-harness skill deposit, run by ``Enablement.enable``.

:class:`SkillDeposit` deposits quarry's OWN packaged skill tree
(:meth:`~quarry.skills_source.SkillSource.packaged`) into every detected
non-Claude harness — ungated by the enabled repo, since the source is the
installed binary itself, not anything the repo supplies. The deposit
is machine-global and binary-owned, so ``disable`` does NOT retract it: one
repo's disable must not delete skills another still-enabled repo depends on
(``quarry skills install`` manages them explicitly instead). A deposit never
overwrites a skill quarry did not write (the manifest-ownership check in
:meth:`quarry.skills_install.SkillsInstaller.install_detected`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, final

from quarry.home_paths import HomeUnresolvedError, QuarryHome
from quarry.skills_install import SkillsInstaller
from quarry.skills_source import SkillSource

if TYPE_CHECKING:
    from quarry.skills_install import SkillOutcome

__all__ = ["SkillDeposit"]

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
