"""Install the hermetic test environment before anything imports quarry.

pytest imports the conftest of each directory from the rootdir downwards, so
this file's body runs before ``tests/conftest.py`` -- which imports quarry at
module scope, binding ``Settings.quarry_root`` and the log destination from
whatever ``Path.home()`` said at that moment.  Importing
:mod:`tests.hermetic_env` here performs the ``HOME`` redirect while that is
still ahead of us.  There is no earlier hook: ``pytest_configure`` runs after
every conftest has been imported, and a ``-p`` plugin is loaded before the
rootdir reaches ``sys.path``.
"""

from __future__ import annotations

from tests.hermetic_env import ENV, pytest_unconfigure
from tools.test_quarantine import pytest_collection_modifyitems

__all__ = ["ENV", "pytest_collection_modifyitems", "pytest_unconfigure"]
