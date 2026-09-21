"""The ``quarry skills`` command group: cross-harness skill deposit + status.

The shared top-level CLI plumbing (JSON/text emit, the error-boundary
decorator, the console) is injected as a :class:`CliPlumbing` bundle so this
module never imports back into ``__main__`` (matches ``cli_captures.py``,
``cli_missions.py``). No daemon client is needed here — deposit is a pure
filesystem operation against the local machine's harness config directories.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn, Self, final

import typer

from quarry.skills_install import Harness, SkillsInstaller

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing
    from quarry.skills_install import SkillOutcome

_USAGE_ERROR = 2


@final
class SkillsCli:
    """Build the ``skills`` Typer sub-app around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def build(self) -> typer.Typer:
        """Return the ``skills`` sub-app with ``install``/``status`` attached."""
        app = typer.Typer(
            help="Deposit quarry's skills into detected coding-agent harnesses."
        )
        app.command(name="install")(self._p.cli_errors(self._install))
        app.command(name="status")(self._p.cli_errors(self._status))
        return app

    def _install(
        self,
        agent: Annotated[
            str,
            typer.Option("--agent", help="One harness: claude, pi, opencode, codex"),
        ] = "",
        *,
        all_: Annotated[
            bool,
            typer.Option("--all", help="Every detected harness (the default)"),
        ] = False,
    ) -> None:
        """Deposit the quarry-recall and quarry-capture skills into a harness.

        With neither flag, deposits into every harness detected on this
        machine (the same set ``--all`` names explicitly).
        """
        if agent and all_:
            self._usage_error("--agent and --all are mutually exclusive")
        installer = self._installer()
        no_harnesses = False
        if agent:
            outcomes = installer.install(self._parse_harness(agent))
        else:
            no_harnesses = not installer.detected_harnesses()
            outcomes = installer.install_detected()
        self._p.emit(
            [self._row(o) for o in outcomes],
            self._render(outcomes, no_harnesses=no_harnesses),
        )

    def _status(self) -> None:
        """Report which harnesses have the skills deposited, and whether current."""
        outcomes = self._installer().status()
        self._p.emit([self._row(o) for o in outcomes], self._render(outcomes))

    def _installer(self) -> SkillsInstaller:
        source_dir = SkillsInstaller.locate_source(Path.cwd())
        return SkillsInstaller(source_dir, Path.home())

    def _parse_harness(self, agent: str) -> Harness:
        try:
            return Harness(agent)
        except ValueError:
            choices = ", ".join(h.value for h in Harness)
            self._usage_error(f"unknown harness {agent!r}; choose one of {choices}")

    def _usage_error(self, message: str) -> NoReturn:
        self._p.err_console.print(f"Error: {message}", style="red")
        raise typer.Exit(code=_USAGE_ERROR)

    @staticmethod
    def _render(
        outcomes: tuple[SkillOutcome, ...], *, no_harnesses: bool = False
    ) -> str:
        if outcomes:
            return "\n".join(
                f"{o.harness.value:<8} {o.skill:<15} {o.action}" for o in outcomes
            )
        if no_harnesses:
            return "No harnesses detected."
        return "No skills found in plugin/skills/ to deposit."

    @staticmethod
    def _row(outcome: SkillOutcome) -> dict[str, object]:
        return {
            "harness": outcome.harness.value,
            "skill": outcome.skill,
            "action": outcome.action,
            "path": str(outcome.path) if outcome.path is not None else None,
        }
