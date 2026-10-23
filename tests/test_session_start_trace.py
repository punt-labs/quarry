"""The SessionStart trace names what happened: a refusal is not traced as a capture."""

from __future__ import annotations

import logging
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from quarry.enabled_marker import EnabledMarker
from quarry.hooks import handle_session_start
from quarry.refusal import RegistrationRefusedError
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path


def _trace_line(caplog: pytest.LogCaptureFixture) -> str:
    [line] = [
        r.getMessage()
        for r in caplog.records
        if r.name == "quarry.hooks" and "session-start: entered" in r.getMessage()
    ]
    return line


def _project(tmp_path: Path) -> tuple[Path, MagicMock]:
    project = tmp_path / "proj"
    project.mkdir()
    EnabledMarker(project).write()
    settings = MagicMock()
    settings.registry_path = tmp_path / "registry.db"
    settings.lancedb_path = tmp_path / "lancedb"
    return project, settings


def _run(
    project: Path, settings: MagicMock, *extra: AbstractContextManager[object]
) -> None:
    with ExitStack() as stack:
        for patched in (
            patch(
                "quarry.hooks._SessionStartContext._resolve_settings",
                return_value=settings,
            ),
            patch(
                "quarry.active_session_context.SyncLock.launch_background_sync",
                return_value="launched",
            ),
            patch(
                "quarry.active_session_context.ActiveSessionContext._session_coverage",
                return_value=None,
            ),
            *extra,
        ):
            stack.enter_context(patched)
        handle_session_start({"cwd": str(project)})


@pytest.fixture(autouse=True)
def _info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="quarry.hooks")


def test_a_registered_session_is_traced_as_a_capture(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project, settings = _project(tmp_path)
    _run(
        project,
        settings,
        patch(
            "quarry.hooks._SessionStartContext._daemon_chunk_collections",
            return_value=frozenset(),
        ),
    )
    assert _trace_line(caplog).endswith("-> capture")


def test_a_refused_registration_is_traced_as_a_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project, settings = _project(tmp_path)
    refusal = RegistrationRefusedError("collection name already in use").about(
        directory=str(project), collection="proj", detail="held"
    )
    _run(
        project,
        settings,
        patch(
            "quarry.hooks._SessionStartContext._daemon_chunk_collections",
            return_value=frozenset(),
        ),
        patch.object(SyncRegistry, "register_directory", side_effect=refusal),
    )
    assert _trace_line(caplog).endswith("-> skip:registration-refused")


def test_an_unreachable_daemon_is_traced_as_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project, settings = _project(tmp_path)
    _run(
        project,
        settings,
        patch(
            "quarry.hooks._SessionStartContext._daemon_chunk_collections",
            side_effect=ConnectionError,
        ),
    )
    assert _trace_line(caplog).endswith("-> error:daemon-unreachable")


def test_a_subsumption_is_traced_as_a_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project, settings = _project(tmp_path)
    child = project / "child"
    child.mkdir()
    registry = SyncRegistry(settings.registry_path)
    registry.register_directory(child, "child")
    registry.close()
    _run(project, settings)
    assert _trace_line(caplog).endswith("-> skip:subsumption")
