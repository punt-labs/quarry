"""The ``quarry captures`` command group: push/init the private capture shadow.

The shared top-level CLI plumbing (JSON/text emit, the error-boundary decorator,
proxy config, settings, console) is injected as a :class:`CliPlumbing` bundle so
this module never imports back into ``__main__`` — the two stay free of an import
cycle, and the command group is testable in isolation with a stub plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.cli_formatters import ResultFormatter
from quarry.remote_client import RemoteClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.console import Console

    from quarry.config import Settings


@dataclass(frozen=True, slots=True)
class CliPlumbing:
    """The top-level CLI helpers the captures commands borrow."""

    emit: Callable[[object, str], None]
    cli_errors: Callable[[Callable[..., None]], Callable[..., None]]
    safe_proxy_config: Callable[[], dict[str, object]]
    resolved_settings: Callable[[], Settings]
    err_console: Console


@final
class CapturesCli:
    """Build the ``captures`` Typer sub-app around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def build(self) -> typer.Typer:
        """Return the ``captures`` sub-app with its push and init commands."""
        app = typer.Typer(
            help="Manage the private capture shadow repo (<repo> -> <repo>-quarry).",
            invoke_without_command=True,
            rich_markup_mode=None,
        )
        app.callback(invoke_without_command=True)(self._callback)
        app.command(name="push")(self._p.cli_errors(self._push))
        app.command(name="init")(self._p.cli_errors(self._init))
        return app

    def _callback(self, ctx: typer.Context) -> None:
        """Manage the private capture shadow repo."""
        if ctx.invoked_subcommand is None:
            self._p.err_console.print(
                "Error: specify a subcommand — push, init.", style="red"
            )
            raise typer.Exit(code=1)

    def _push(self) -> None:
        """Re-scrub and push redacted captures to each project's private shadow."""
        from quarry.shadow import CaptureSync  # noqa: PLC0415

        proxy_config = self._p.safe_proxy_config().get("quarry", {})
        if isinstance(proxy_config, dict) and "url" in proxy_config:
            resp = RemoteClient(proxy_config).request("POST", "/captures/push", body={})
            rendered = ResultFormatter.coerce_results(resp.get("results", resp))
            self._p.emit(resp, ResultFormatter.captures_push(rendered))
        else:
            results = CaptureSync.push_registered(
                self._p.resolved_settings(), fail_open=True
            )
            rendered = {col: res.to_dict() for col, res in results.items()}
            self._p.emit({"results": rendered}, ResultFormatter.captures_push(rendered))
        # Both surfaces exit non-zero when any project failed to push (bug class 3
        # parity — the remote path previously returned 0 even on refused pushes).
        if ResultFormatter.has_failures(rendered):
            raise typer.Exit(code=1)

    def _init(
        self,
        create: Annotated[
            bool,
            typer.Option("--create", help="Create the private remote via gh first"),
        ] = False,
    ) -> None:
        """Bootstrap the current project's private capture shadow (no push)."""
        from quarry.shadow import CaptureSync  # noqa: PLC0415

        shadow = CaptureSync.from_directory(Path.cwd())
        if shadow is None:
            self._p.emit(
                {"error": "no shadow config"},
                "no shadow: block in .punt-labs/quarry/config.md (run 'quarry enable')",
            )
            raise typer.Exit(code=1)
        ok = shadow.bootstrap(create=create)
        self._p.emit(
            {"bootstrapped": ok},
            "shadow ready" if ok else "bootstrap refused — see the logged reason",
        )
        if not ok:
            raise typer.Exit(code=1)
