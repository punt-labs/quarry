"""Expose daemon-backed directory sync, status, and telemetry commands.

All pure client calls against the daemon that owns the registry (DES-031 I2): the
CLI never touches a local ``SyncRegistry``. The directory commands delegate to
:class:`~quarry.cli_sync_dispatch.DirectorySyncCli`, while this group renders
database status and recall insights.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.cli_sync_dispatch import DirectorySyncCli
from quarry.reports import InsightsReport, StatusReport

if TYPE_CHECKING:
    import typer

    from quarry.cli_captures import CliPlumbing

_logger = logging.getLogger(__name__)


@final
class SyncCli:
    """Serve ``sync``/``status``/``insights``; attach ``register``/``deregister``."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        """Bind synchronization and telemetry commands to shared CLI plumbing."""
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``sync``/``register``/``deregister``/``status``/``insights``."""
        DirectorySyncCli(self._p).register(app)
        app.command(name="status")(self._p.cli_errors(self._status))
        app.command(name="insights")(self._p.cli_errors(self._insights))

    def _status(self) -> None:
        """Show database status: documents, chunks, storage, model info."""
        data = self._p.client().status().model_dump()
        self._p.emit(data, StatusReport(data).render())
        _logger.info("CLI operation status succeeded")

    def _insights(self) -> None:
        """Show recall telemetry: query volume, latency, and recall breakdowns."""
        resp = self._p.client().insights()
        self._p.emit(resp.model_dump(), InsightsReport(resp).render())
