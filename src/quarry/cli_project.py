"""The ``quarry enable`` / ``quarry disable`` project-capture commands.

The registry is the daemon's (DES-031 I2): ``enable``/``disable`` read coverage
over the wire and register/deregister via the injected client, never a local
``SyncRegistry``.  The project files (config.md, CLAUDE.md, ethos ext) are the
client's and are written/removed locally.  The chunk purge on disable is a
daemon call dispatched fire-and-forget (DES-001).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

if TYPE_CHECKING:
    from collections.abc import Iterable

    from quarry.cli_captures import CliPlumbing


@final
class ProjectCli:
    """Serve ``enable``/``disable`` around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        """Bind project enablement commands to shared CLI plumbing."""
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach the ``enable`` and ``disable`` commands to *app*."""
        app.command(name="enable")(self._p.cli_errors(self._enable))
        app.command(name="disable")(self._p.cli_errors(self._disable))

    @staticmethod
    def _json_safe_dict(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
        """``dataclasses.asdict``'s ``dict_factory``: stringify every ``Path``.

        ``EnableResult.skills_deposited`` nests
        :class:`~quarry.skills_install.SkillOutcome`, whose ``path`` field is
        a ``Path`` -- JSON has no native path type, so the bare ``dict``
        default factory leaves a ``PosixPath`` in the payload and
        ``json.dump`` raises at the ``--json`` emit boundary. Applied
        uniformly (not just on the one nested field) so any future
        ``Path``-carrying field gets the same treatment for free.
        """
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in pairs}

    def _enable(
        self,
        directory: Annotated[
            Path, typer.Argument(help="Project directory to enable (default: cwd)")
        ] = Path(),
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Override collection name")
        ] = "",
    ) -> None:
        """Enable quarry knowledge capture for a project directory."""
        from quarry.enable import enable_project  # noqa: PLC0415

        # A ValueError (e.g. a --collection that contradicts the covering
        # registration) propagates to the shared _cli_errors boundary: stdout
        # stays empty (no spurious JSON error object under --json), the
        # diagnostic goes to stderr, exit 1.
        # Pass the raw path: enable_project owns normalization
        # (expanduser().resolve()). Resolving here would turn "~/proj" into
        # "./~/proj" before the tilde is ever expanded.
        from quarry.enable_report import EnableReport  # noqa: PLC0415

        result = enable_project(
            directory, self._p.client(), collection_override=collection
        )
        lines = EnableReport(result).lines()
        self._p.emit(
            dataclasses.asdict(result, dict_factory=self._json_safe_dict),
            "\n".join(lines),
        )

    def _disable(
        self,
        directory: Annotated[
            Path, typer.Argument(help="Project directory to disable (default: cwd)")
        ] = Path(),
        keep_data: Annotated[
            bool, typer.Option("--keep-data", help="Keep indexed data in LanceDB")
        ] = False,
    ) -> None:
        """Disable quarry knowledge capture for a project directory."""
        from quarry.enable import disable_project  # noqa: PLC0415

        # disable_project is idempotent: a directory with no covering registration
        # is a no-op success (result.collection == ""), not an error. Only the
        # child-of-registered-parent guard raises ValueError, which propagates to
        # the shared _cli_errors boundary (empty stdout under --json, exit 1).
        # Pass the raw path: disable_project owns normalization
        # (expanduser().resolve()). Resolving here would mangle "~/proj".
        from quarry.enable_report import DisableReport  # noqa: PLC0415

        result = disable_project(directory, self._p.client(), keep_data=keep_data)
        lines = DisableReport(result, keep_data=keep_data).lines()
        self._p.emit(
            dataclasses.asdict(result, dict_factory=self._json_safe_dict),
            "\n".join(lines),
        )
