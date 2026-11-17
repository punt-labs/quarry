"""Register every command group on the top-level Typer app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.cli_admin import AdminCli
from quarry.cli_captures import CapturesCli
from quarry.cli_documents import DocumentsCli
from quarry.cli_ingest import IngestCli
from quarry.cli_maintenance import MaintenanceCli
from quarry.cli_missions import MissionsCli
from quarry.cli_project import ProjectCli
from quarry.cli_remote import RemoteCli
from quarry.cli_repair import RepairCli
from quarry.cli_search import SearchCli
from quarry.cli_skills import SkillsCli
from quarry.cli_sync import SyncCli

if TYPE_CHECKING:
    import typer

    from quarry.cli_captures import CliPlumbing


@final
class CliRegistry:
    """Own which command groups exist, so ``__main__`` wires none of them itself."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach every data and admin command group to *app*."""
        for group in (
            SearchCli,
            IngestCli,
            DocumentsCli,
            SyncCli,
            ProjectCli,
            MaintenanceCli,
            RepairCli,
            RemoteCli,
            AdminCli,
        ):
            group(self._p).register(app)
        app.add_typer(CapturesCli(self._p).build(), name="captures")
        app.add_typer(MissionsCli(self._p).build(), name="missions")
        app.add_typer(SkillsCli(self._p).build(), name="skills")
