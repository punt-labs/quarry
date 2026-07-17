from __future__ import annotations

import functools
import importlib.metadata
import json
import logging
import sys
from typing import TYPE_CHECKING, Annotated

import typer
import typer.core
from rich.console import Console

from quarry.cli_captures import CapturesCli, CliPlumbing
from quarry.cli_documents import DocumentsCli
from quarry.cli_ingest import IngestCli
from quarry.cli_maintenance import MaintenanceCli
from quarry.cli_project import ProjectCli
from quarry.cli_remote import RemoteCli
from quarry.cli_search import SearchCli
from quarry.cli_sync import SyncCli
from quarry.client import (
    BadRequestError,
    QuarryConnectionError,
    QuarryError,
    TargetResolver,
)
from quarry.client.errors import CONFLICT_STATUS
from quarry.config import Settings
from quarry.logging_config import LoggingConfig

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Appended after any QuarryConnectionError so a down daemon points at the fix.
_AUTOSTART_HINT = (
    "If quarryd is not running, start it with 'quarry install' (managed) or "
    "'quarryd' (foreground)."
)

_COMMAND_ORDER: list[str] = [
    # Product commands
    "find",
    "ingest",
    "show",
    "remember",
    "status",
    "use",
    "delete",
    "register",
    "deregister",
    "sync",
    "enable",
    "disable",
    "optimize",
    "captures",
    "backfill-sessions",
    "login",
    "logout",
    "remote",
    "list",
    # Admin commands
    "install",
    "doctor",
    "mcp",
    "version",
    "uninstall",
]


class _OrderedGroup(typer.core.TyperGroup):
    """Typer group that enforces a fixed command order in --help."""

    def list_commands(self, ctx: typer.Context) -> list[str]:  # type: ignore[override]
        commands = super().list_commands(ctx)
        order = {name: i for i, name in enumerate(_COMMAND_ORDER)}
        return sorted(commands, key=lambda c: order.get(c, 999))


app = typer.Typer(
    help="quarry: extract searchable knowledge from any document",
    rich_markup_mode=None,
    cls=_OrderedGroup,
)
hooks_app = typer.Typer(
    help="Claude Code hook handlers (called by hook scripts)",
    rich_markup_mode=None,
)
app.add_typer(hooks_app, name="hooks", hidden=True)
err_console = Console(stderr=True)

# Global state set by @app.callback.
_json_output: bool = False
_verbose: bool = False
_quiet: bool = False
_global_db: str = ""


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version as get_version  # noqa: PLC0415

        print(f"quarry {get_version('punt-quarry')}")
        raise typer.Exit


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Annotated[  # noqa: ARG001
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show INFO-level diagnostic logs on stderr (timing, plans, counts).",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress non-essential output."),
    ] = False,
    database: Annotated[
        str,
        typer.Option(
            "--db",
            help="Named database (default: 'default'). "
            "Resolves to ~/.punt-labs/quarry/data/<name>/lancedb.",
        ),
    ] = "",
) -> None:
    """quarry: extract searchable knowledge from any document."""
    global _json_output, _verbose, _quiet, _global_db
    if verbose and quiet:
        err_console.print("Error: --verbose and --quiet are mutually exclusive.")
        raise typer.Exit(code=1)
    _json_output = output_json
    _verbose = verbose
    _quiet = quiet
    _global_db = database
    # Record --db process-wide so the client tier resolves the daemon's
    # startup-db run dir (serve.token/serve.port) the same way — client and
    # daemon agree on the database by a matching --db.
    Settings.set_active_db(database)
    if _verbose:
        stderr_level = "INFO"
    elif _quiet:
        stderr_level = "CRITICAL"
    else:
        stderr_level = "WARNING"
    LoggingConfig.configure(stderr_level=stderr_level)
    if ctx.invoked_subcommand is None:
        print(ctx.get_help())
        raise typer.Exit(code=0)


def _emit(data: object, text: str = "") -> None:
    """Output helper: JSON when --json is active, otherwise text.

    Commands always pass both a structured payload and a human-readable string;
    ``--json`` serialises *data* as a single line, otherwise *text* prints.
    """
    if _json_output:
        json.dump(data, sys.stdout)
        sys.stdout.write("\n")
    elif text:
        print(text)


def _cli_errors(fn: Callable[..., None]) -> Callable[..., None]:
    """Map typed client errors (and any escape) to a ``typer.Exit`` at the CLI edge.

    This is the one place the client tier's :class:`QuarryError` hierarchy becomes
    an exit code and a message; command bodies never catch it.  A 409 conflict is
    "already in progress" (exit 0); a connection failure carries the autostart
    nudge; every other typed error and any unexpected escape exits 1.
    """

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> None:
        try:
            fn(*args, **kwargs)
        except (SystemExit, KeyboardInterrupt, typer.Exit):
            raise
        except QuarryConnectionError as exc:
            err_console.print(f"Error: {exc.message}", style="red")
            err_console.print(_AUTOSTART_HINT, style="yellow")
            raise typer.Exit(code=1) from exc
        except BadRequestError as exc:
            if exc.status == CONFLICT_STATUS:
                if not _quiet:
                    err_console.print(
                        f"Already in progress: {exc.message}", style="yellow"
                    )
                raise typer.Exit(code=0) from exc
            err_console.print(f"Error: {exc.message}", style="red")
            raise typer.Exit(code=1) from exc
        except QuarryError as exc:
            err_console.print(f"Error: {exc.message}", style="red")
            raise typer.Exit(code=1) from exc
        except Exception as exc:
            logger.exception("Command %s failed", fn.__name__)
            err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc

    return wrapper


# ---------------------------------------------------------------------------
# Data commands — assembled from the per-domain CLI modules, each a client call.
# ---------------------------------------------------------------------------

_plumbing = CliPlumbing(
    emit=_emit,
    cli_errors=_cli_errors,
    # Deferred to call time so resolution reads live --db/env state (and tests can
    # substitute the client) — never resolved at import.
    client=lambda: TargetResolver.connect(),
    err_console=err_console,
    is_quiet=lambda: _quiet,
)

SearchCli(_plumbing).register(app)
IngestCli(_plumbing).register(app)
DocumentsCli(_plumbing).register(app)
SyncCli(_plumbing).register(app)
ProjectCli(_plumbing).register(app)
MaintenanceCli(_plumbing).register(app)
RemoteCli(_plumbing).register(app)
app.add_typer(CapturesCli(_plumbing).build(), name="captures")


@app.command(name="use")
@_cli_errors
def use_cmd(
    name: Annotated[str, typer.Argument(help="Database name (e.g., 'coding', 'work')")],
) -> None:
    """Set the persistent default database for subsequent commands.

    Database selection is a client-side preference: it points the client at that
    database's daemon run dir.  Use 'default' to reset; the ``--db`` flag
    overrides this per call.
    """
    Settings.load().resolve_db_paths(name if name != "default" else None)
    Settings.write_default_db(name)
    _emit({"database": name}, f"Default database set to {name!r}")


# ---------------------------------------------------------------------------
# Admin commands — install, doctor, mcp, version, uninstall.  ``mcp`` is the one
# command that hosts the engine (via a lazy in-body import); all others are
# client-side or pure config.
# ---------------------------------------------------------------------------


@app.command()
def install() -> None:
    """Set up data directory and download embedding model."""
    from quarry.doctor import run_install  # noqa: PLC0415

    raise typer.Exit(code=run_install())


@app.command()
def doctor() -> None:
    """Check environment: Python, data directory, model, imports."""
    from quarry.doctor import check_environment  # noqa: PLC0415

    raise typer.Exit(code=check_environment())


@app.command()
def mcp() -> None:
    """Start the MCP server (stdio transport)."""
    from quarry.mcp_server import main as mcp_main  # noqa: PLC0415

    mcp_main(db_name=_global_db or Settings.read_default_db())


@app.command()
def version() -> None:
    """Print the quarry version."""
    ver = importlib.metadata.version("punt-quarry")
    _emit({"version": ver}, ver)


@app.command()
@_cli_errors
def uninstall() -> None:
    """Remove quarry system daemon and service file.

    Stops the daemon and removes the launchd plist (macOS) or systemd unit
    (Linux).  Clean inverse of the daemon step in ``quarry install``.
    """
    from quarry.service import uninstall as svc_uninstall  # noqa: PLC0415

    msg = svc_uninstall()
    _emit({"message": msg}, msg)


# ---------------------------------------------------------------------------
# Hook subcommands — called by Claude Code hook scripts.  All are fail-open:
# exceptions are caught, logged, and the process exits 0 so Claude is never
# blocked.
# ---------------------------------------------------------------------------


@hooks_app.command(name="session-start")
def hook_session_start() -> None:
    """SessionStart: auto-register and sync the current repo."""
    from quarry._stdlib import run_hook  # noqa: PLC0415
    from quarry.hooks import handle_session_start  # noqa: PLC0415

    run_hook(handle_session_start)


@hooks_app.command(name="post-web-fetch")
def hook_post_web_fetch() -> None:
    """PostToolUse on WebFetch: auto-ingest fetched URLs."""
    from quarry._stdlib import run_hook  # noqa: PLC0415
    from quarry.hooks import handle_post_web_fetch  # noqa: PLC0415

    run_hook(handle_post_web_fetch)


@hooks_app.command(name="pre-compact")
def hook_pre_compact() -> None:
    """PreCompact: capture compaction summaries."""
    from quarry._stdlib import run_hook  # noqa: PLC0415
    from quarry.hooks import handle_pre_compact  # noqa: PLC0415

    run_hook(handle_pre_compact)


if __name__ == "__main__":
    app()
