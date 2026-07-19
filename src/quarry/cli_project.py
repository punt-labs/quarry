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
    from quarry.cli_captures import CliPlumbing


@final
class ProjectCli:
    """Serve ``enable``/``disable`` around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach the ``enable`` and ``disable`` commands to *app*."""
        app.command(name="enable")(self._p.cli_errors(self._enable))
        app.command(name="disable")(self._p.cli_errors(self._disable))

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

        # A ValueError (e.g. parent-covered dir) propagates to the shared
        # _cli_errors boundary: stdout stays empty (no spurious JSON error object
        # under --json), the diagnostic goes to stderr, exit 1.
        result = enable_project(
            directory.resolve(), self._p.client(), collection_override=collection
        )

        lines = [
            f"Enabled quarry for {result.directory}",
            f"  Collection: {result.collection}",
            f"  Captures: {result.captures_collection}",
        ]
        if result.config_path:
            lines.append(f"  Config: {result.config_path}")
        if result.claudemd_appended:
            lines.append("  Appended quarry instructions to CLAUDE.md")
        if result.ethos_skipped:
            lines.append("  Ethos: not installed (agent memory skipped)")
        else:
            if result.ethos_created:
                lines.append(f"  Ethos created: {', '.join(result.ethos_created)}")
            if result.ethos_updated:
                lines.append(f"  Ethos updated: {', '.join(result.ethos_updated)}")
            if result.memory_collections:
                joined = ", ".join(result.memory_collections)
                lines.append(f"  Memory collections: {joined}")
        if result.ethos_failed:
            # session_context never landed for these handles — surface the
            # partial failure rather than let "Ethos created" imply success.
            lines.append(f"  Ethos FAILED: {', '.join(result.ethos_failed)}")
        self._p.emit(dataclasses.asdict(result), "\n".join(lines))

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

        # A ValueError (no registration covers the dir) propagates to the shared
        # _cli_errors boundary: stdout stays empty under --json, exit 1.
        result = disable_project(
            directory.resolve(), self._p.client(), keep_data=keep_data
        )

        lines = [f"Disabled quarry for {result.directory}"]
        if not keep_data:
            lines.append(
                f"  Deregistered {result.collection} "
                f"({result.removed} files); chunk purge queued"
            )
        if result.config_removed:
            lines.append("  Config file removed")
        if result.claudemd_removed:
            lines.append("  Removed quarry instructions from CLAUDE.md")
        self._p.emit(dataclasses.asdict(result), "\n".join(lines))
