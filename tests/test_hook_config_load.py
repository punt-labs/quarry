"""``load_hook_config`` returns defaults for anything but a readable regular file."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import pytest

from quarry._stdlib import HookConfig, load_hook_config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_CONFIG = ".punt-labs/quarry/config.md"
_OPT_OUT = "---\nauto_capture:\n  web_fetch: false\n---\n"
_WAIT_SECONDS = 3.0


def _config(root: Path) -> Path:
    path = root / _CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_within_deadline(root: Path) -> HookConfig:
    """Run the load in a thread; fail instead of hanging the suite."""
    box: list[HookConfig] = []
    worker = threading.Thread(
        target=lambda: box.append(load_hook_config(str(root))), daemon=True
    )
    worker.start()
    worker.join(_WAIT_SECONDS)
    if worker.is_alive():
        release = os.open(root / _CONFIG, os.O_WRONLY | os.O_NONBLOCK)
        os.close(release)
        worker.join(_WAIT_SECONDS)
        pytest.fail(f"load_hook_config blocked for {_WAIT_SECONDS}s on {_CONFIG}")
    return box[0]


@pytest.fixture
def restore_modes() -> Iterator[Callable[[Path], None]]:
    """Return a registrar for directories to chmod back so tmp cleanup works."""
    touched: list[Path] = []
    yield touched.append
    for directory in touched:
        directory.chmod(0o755)


def test_fifo_config_yields_defaults(tmp_path: Path) -> None:
    os.mkfifo(_config(tmp_path))
    assert _load_within_deadline(tmp_path) == HookConfig()


def test_symlink_to_fifo_yields_defaults(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    _config(tmp_path).symlink_to(fifo)
    assert _load_within_deadline(tmp_path) == HookConfig()


def test_directory_config_yields_defaults(tmp_path: Path) -> None:
    _config(tmp_path).mkdir()
    assert load_hook_config(str(tmp_path)) == HookConfig()


def test_missing_config_yields_defaults(tmp_path: Path) -> None:
    assert load_hook_config(str(tmp_path)) == HookConfig()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unsearchable_directory_yields_defaults(
    tmp_path: Path, restore_modes: Callable[[Path], None]
) -> None:
    _config(tmp_path).write_text(_OPT_OUT)
    locked = tmp_path / ".punt-labs" / "quarry"
    restore_modes(locked)
    locked.chmod(0o000)
    assert load_hook_config(str(tmp_path)) == HookConfig()


def test_regular_config_is_parsed(tmp_path: Path) -> None:
    _config(tmp_path).write_text(_OPT_OUT)
    assert load_hook_config(str(tmp_path)) == HookConfig(web_fetch=False)


def test_symlink_to_regular_config_is_followed(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text(_OPT_OUT)
    _config(tmp_path).symlink_to(real)
    assert load_hook_config(str(tmp_path)) == HookConfig(web_fetch=False)


def test_undecodable_config_yields_defaults(tmp_path: Path) -> None:
    _config(tmp_path).write_bytes(b"\xff\xfe\x00")
    assert load_hook_config(str(tmp_path)) == HookConfig()
